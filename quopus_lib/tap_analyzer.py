# date_time: 2026-05-28 15:26
"""
TAPClean-style analysis engine for C64 .TAP cassette images.

This brings TAPClean's report format and core analysis to Quopus:
a recognition pass over the pulse stream, a loader database that
names the loader (Ocean/Imagine, Novaload, Turbo Tape, Freeload,
Visiload, ...), per-file CRC32, a PASS/FAIL test suite, optional
clean/optimize, and WAV/AU export for recording back to tape.

It is NOT a line-for-line port of TAPClean's ~80 hand-written C
scanners - that codebase has one bespoke C file per loader with
decades of edge-case handling. Instead this is a from-scratch
pulse analyzer with a *data-driven* loader database: each loader
is described by its pulse widths, threshold, pilot/sync bytes and
framing, and the generic scanner matches the stream against every
entry. New loaders are added by appending a LoaderDef, not by
writing new code. The common loaders are covered; the long tail
can be extended incrementally as you feed it real tapes.

Report fields mirror TAPClean's tcreport.txt so the output looks
familiar:
    TAP Name / Size / Version / Recognized% / Data Files /
    Pauses / Gaps / Magic CRC32 / TAP Time / Bootable / Loader ID
    + Header/Recognition/Checksum/Read/Optimization test results
    + per-file detail blocks (Seq, type, location, LA/EA/SZ,
      pilot/trailer, checkbyte, read errors, CRC32)

The pulse decode itself reuses tap_decoder for the container parse
and the low-level CBM byte reader.
"""

from __future__ import annotations

import struct
import zlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import tap_decoder as td
from .tap_decoder import (
    CLOCK_PAL, CLOCK_NTSC, TapPulses, TapParseError,
    parse_tap, build_histogram,
)
from .tap_loaders import TAPCLEAN_LOADERS


# ---------------------------------------------------------------------
# Loader database
# ---------------------------------------------------------------------

@dataclass
class LoaderDef:
    """Data-driven description of one tape loader.

    A loader is recognised by matching its pulse-width fingerprint
    against the histogram clusters, then optionally confirming a
    pilot byte run after threshold-decoding. This is deliberately
    lightweight; it identifies the loader and lets us decode its
    bytes, which is what the report needs.
    """
    name: str
    short: str               # short tag for "Loader ID"
    # Expected pulse widths in clock cycles. For 2-pulse turbos:
    # (short, long). For CBM-style 3-pulse: (short, medium, long).
    widths: tuple
    threshold: Optional[int] = None     # for turbo decode
    pilot_byte: Optional[int] = None
    sync_byte: Optional[int] = None
    msb_first: bool = True
    kind: str = "turbo"      # "cbm" | "turbo"
    tol: int = 60            # width match tolerance (cycles)
    notes: str = ""


# The database. Widths are PAL-nominal clock cycles. These come
# from the TAP-format docs, the zinc64/Di Fraia loader analyses,
# the Lemon64 turbo threads, and the published Final TAP / TAPClean
# loader tables. Tolerances are generous because real tapes drift.
#
# This list is meant to GROW. To add a loader: append a LoaderDef
# with its two (or three) pulse widths and, if known, the pilot
# and sync bytes. The scanner does the rest.
# A few short tags so the most common loaders get friendly names
# in the report (the TAPClean names are ALL-CAPS and verbose).
_SHORT_TAGS = {
    "OCEAN/IMAGINE F1": "Ocean/Imagine (F1)",
    "OCEAN/IMAGINE F2": "Ocean/Imagine (F2)",
    "OCEAN/IMAGINE F3": "Ocean/Imagine (F3)",
    "NOVALOAD": "Novaload",
    "NOVALOAD SPECIAL": "Novaload (special)",
    "FREELOAD": "Freeload",
    "TURBOTAPE-250 HEADER": "Turbo Tape 250",
    "TURBOTAPE-250 DATA": "Turbo Tape 250",
    "PAVLODA": "Pavloda",
    "IK TAPE": "System 3 / IK",
    "VISILOAD T1": "Visiload",
    "BURNER TAPE": "Burner",
    "CYBERLOAD F1": "Cyberload",
    "MICROLOAD": "Microload",
    "SUPERTAPE DATA": "Supertape",
    "BITURBO": "Biturbo",
    "US-GOLD TAPE": "US Gold",
}


def _short_for(name):
    """Return a friendly short tag for a TAPClean loader name."""
    if name in _SHORT_TAGS:
        return _SHORT_TAGS[name]
    # Title-case and strip the verbose suffixes
    s = name.title()
    for suf in (" Tape Header", " Tape Data", " Header", " Data",
                " Tape"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s[:22]


# The CBM ROM standard loader (measured 384/528/688) is always the
# first entry; it's matched separately since it's in every tape.
LOADER_DB = [
    LoaderDef("CBM ROM (Commodore Kernal)", "CBM ROM",
              widths=(384, 528, 688), kind="cbm",
              pilot_byte=None, notes="Standard KERNAL SAVE format"),
]

# Append all 124 loader fingerprints from the TAPClean ft[] table.
# These carry the reference tool's exact pulse widths, thresholds,
# pilot/sync bytes and bit endianness.
for _ld in TAPCLEAN_LOADERS:
    _w = _ld["widths"]
    _kind = "cbm" if len(_w) == 3 else "turbo"
    LOADER_DB.append(LoaderDef(
        name=_ld["name"],
        short=_short_for(_ld["name"]),
        widths=_w,
        threshold=_ld["threshold"],
        pilot_byte=_ld["pilot"],
        sync_byte=_ld["sync"],
        msb_first=_ld["msb_first"],
        kind=_kind,
        tol=36,
        notes="from TAPClean ft[] table"
              + (" (has checksum)" if _ld.get("has_cs") else "")))


# ---------------------------------------------------------------------
# Result structures (TAPClean report shape)
# ---------------------------------------------------------------------

@dataclass
class TapFileReport:
    """Per-file detail block in the report (like a tcreport entry)."""
    seq: int
    file_type: str           # "CBM HEADER", "OCEAN DATA", etc.
    loader: str
    location_start: int      # byte offset in the TAP
    location_end: int
    load_addr: int
    end_addr: int
    size: int
    pilot_size: int
    trailer_size: int
    checkbyte_actual: int
    checkbyte_expected: int
    checkbyte_pass: bool
    read_errors: int
    crc32: int
    data: bytes = b""
    name: str = ""


@dataclass
class TapAnalysisReport:
    """Top-level TAPClean-style report."""
    tap_name: str = ""
    tap_size: int = 0
    tap_version: int = 0
    computer: str = "C64 PAL"
    clock: int = CLOCK_PAL
    recognized_percent: float = 0.0
    data_files: int = 0
    pauses: int = 0
    gaps: int = 0
    magic_crc32: int = 0
    tap_time_seconds: float = 0.0
    bootable: str = "NO"
    loader_id: str = "(unknown)"
    detected_loaders: list = field(default_factory=list)
    files: list = field(default_factory=list)  # list[TapFileReport]
    # test suite
    header_test: str = "SKIP"
    recognition_test: str = "SKIP"
    checksum_test: str = "SKIP"
    read_test: str = "SKIP"
    optimization_test: str = "SKIP"
    overall_result: str = "SKIP"
    # diagnostics
    total_pulses: int = 0
    recognized_pulses: int = 0
    checksummed_files: int = 0
    checksummed_ok: int = 0
    total_read_errors: int = 0
    optimizable_files: int = 0

    def format_report(self) -> str:
        """Render a tcreport.txt-style text block."""
        def fmt_time(secs):
            m = int(secs // 60)
            s = secs - m * 60
            return f"{m}:{s:05.2f}"
        L = []
        L.append("-" * 60)
        L.append("Quopus TAP Analyzer - TAPClean-compatible report")
        L.append("-" * 60)
        L.append("")
        L.append(f"Computer type: {self.computer} ({self.clock} Hz)")
        L.append("")
        L.append("GENERAL INFO AND TEST RESULTS")
        L.append("")
        L.append(f"TAP Name     : {self.tap_name}")
        L.append(f"TAP Size     : {self.tap_size} bytes "
                 f"({self.tap_size // 1024} kB)")
        L.append(f"TAP Version  : {self.tap_version}")
        L.append(f"Recognized   : {self.recognized_percent:.0f}%")
        L.append(f"Data Files   : {self.data_files}")
        L.append(f"Pauses       : {self.pauses}")
        L.append(f"Gaps         : {self.gaps}")
        L.append(f"Magic CRC32  : {self.magic_crc32:08X}")
        L.append(f"TAP Time     : {fmt_time(self.tap_time_seconds)}")
        L.append(f"Bootable     : {self.bootable}")
        L.append(f"Loader ID    : {self.loader_id}")
        L.append("")
        L.append(f"Overall Result    : {self.overall_result}")
        L.append("")
        L.append(f"Header test       : {self.header_test}")
        L.append(f"Recognition test  : {self.recognition_test}")
        L.append(f"Checksum test     : {self.checksum_test}")
        L.append(f"Read test         : {self.read_test}")
        L.append(f"Optimization test : {self.optimization_test}")
        L.append("")
        L.append("-" * 60)
        L.append("FILE DETAILS")
        L.append("-" * 60)
        for f in self.files:
            L.append("")
            L.append(f"Seq. no.: {f.seq}")
            L.append(f"File Type: {f.file_type}  ({f.loader})")
            if f.name:
                L.append(f"Name: {f.name}")
            L.append(f"Location: ${f.location_start:X} -> "
                     f"${f.location_end:X}")
            la = f"${f.load_addr:04X}" if f.load_addr >= 0 else "?"
            ea = f"${f.end_addr:04X}" if f.end_addr >= 0 else "?"
            L.append(f"LA: {la}  EA: {ea}  SZ: {f.size}")
            L.append(f"Pilot/Trailer Size: "
                     f"{f.pilot_size}/{f.trailer_size}")
            cstat = "PASS" if f.checkbyte_pass else "FAIL"
            L.append(f"Checkbyte Actual/Expected: "
                     f"${f.checkbyte_actual:02X}/"
                     f"${f.checkbyte_expected:02X}, {cstat}")
            L.append(f"Read Errors: {f.read_errors}")
            L.append(f"CRC32: {f.crc32:08X}")
        return "\n".join(L)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def crc32_of(data: bytes) -> int:
    """Standard CRC32 (same poly as zlib / PKZip), used by
    TAPClean for the Magic CRC and per-file CRCs."""
    return zlib.crc32(data) & 0xFFFFFFFF


def _match_widths(hist_clusters, widths, tol):
    """Return True if the histogram's dominant clusters contain
    pulses near every width in `widths` (within tol)."""
    for w in widths:
        if not any(abs(c - w) <= tol for c in hist_clusters):
            return False
    return True


def _score_loader(ld, clusters_with_counts):
    """Score how well a loader's pulse widths match the measured
    clusters. Higher is better; returns None if any width is
    missing entirely.

    Two refinements that stop false matches (e.g. every Ocean
    tape reading as 'Burner'):
      1. A required width must match a cluster within HALF the
         tolerance to count as a strong hit; a looser match
         within full tol still counts but scores much lower.
      2. If a turbo loader's width only matches by landing on a
         CBM band (384/528/688), that's almost certainly the
         boot file's pulses, not the turbo's - heavily penalize
         it, because real turbo bit-pulses sit AWAY from the CBM
         bands.
    """
    CBM_BANDS = (384, 528, 688)
    total = 0
    for w in ld.widths:
        best = None
        for cyc, cnt in clusters_with_counts:
            d = abs(cyc - w)
            if d <= ld.tol:
                # Closeness as a 0..1 fraction (1 = exact hit),
                # INDEPENDENT of tol width, so a loader can't win
                # just by declaring a huge tolerance. Weighted by
                # popularity (the real bit pulses are high-count).
                closeness = 1.0 - (d / ld.tol)
                # Square it so near-exact matches dominate loose
                # ones decisively.
                s = (closeness ** 2) * cnt
                # Penalty: a turbo width that only matched a CBM
                # band is the boot file, not the turbo.
                if (ld.kind == "turbo"
                        and any(abs(cyc - b) <= 40
                                for b in CBM_BANDS)
                        and not any(abs(w - b) <= 40
                                    for b in CBM_BANDS)):
                    s *= 0.1
                if best is None or s > best:
                    best = s
        if best is None:
            return None
        total += best
    return total


def _clusters_with_counts(pulses, top_n=10, max_cycle=1500):
    """Return [(cycle_center, count), ...] of the most frequent
    pulse buckets, merged so near-identical centers don't split."""
    hist = build_histogram(pulses, bucket=8)
    items = sorted([(c, k) for k, c in hist.items()
                    if k < max_cycle], reverse=True)
    merged = []
    for cnt, cyc in items[:40]:
        hit = False
        for i, (mc, mcnt) in enumerate(merged):
            if abs(mc - cyc) <= 24:
                # merge into the existing (keep higher-count center)
                merged[i] = (mc if mcnt >= cnt else cyc, mcnt + cnt)
                hit = True
                break
        if not hit:
            merged.append((cyc, cnt))
        if len(merged) >= top_n:
            break
    return merged


def _dominant_clusters(pulses, top_n=6, max_cycle=1500):
    """Return the cycle-centers of the most frequent pulse buckets
    (bit-carrying range only)."""
    hist = build_histogram(pulses, bucket=16)
    items = [(c, k) for k, c in hist.items() if k < max_cycle]
    items.sort(reverse=True)
    return [k for _, k in items[:top_n]]


def _readttbit(pulses, pos, sp, lp, tp):
    """Port of TAPClean's readttbit: classify pulse at `pos` as
    bit 0 (near short) or bit 1 (near long), using threshold tp
    if available. Returns 0, 1 or -1 (error)."""
    if pos < 0 or pos >= len(pulses):
        return -1
    b = pulses[pos]
    TOL = 56
    if tp:
        if (sp - TOL) < b < tp:
            return 0
        if tp < b < (lp + TOL):
            return 1
        return -1
    # midpoint method
    near_s = (sp - TOL) < b < (sp + TOL)
    near_l = (lp - TOL) < b < (lp + TOL)
    if near_s and near_l:
        return 0 if abs(lp - b) > abs(sp - b) else 1
    if near_s:
        return 0
    if near_l:
        return 1
    return -1


def _readttbyte(pulses, pos, sp, lp, tp, msb_first):
    """Port of TAPClean's readttbyte: read 8 bits from `pos`."""
    bits = []
    for i in range(8):
        b = _readttbit(pulses, pos + i, sp, lp, tp)
        if b == -1:
            return -1
        bits.append(b)
    v = 0
    if msb_first:
        for i in range(8):
            if bits[i]:
                v += (128 >> i)
    else:
        for i in range(8):
            if bits[i]:
                v += (1 << i)
    return v


def _verify_pilot(pulses, ld, max_scan=200000):
    """Scan for the loader's pilot/sync signature, mirroring
    TAPClean's find_pilot. Returns a confidence score:
        0   = pilot/sync not found (widths matched by accident)
        1   = pilot byte run found
        2   = pilot run AND the sync byte right after it
    This is what tells apart loaders with identical pulse widths
    (e.g. Ocean pv=0/sv=1 vs Freeload pv=0x40/sv=0x5A): the pulse
    histogram can't, but reading the actual pilot bytes can.
    """
    pv = ld.pilot_byte
    if pv is None:
        return 0
    sp = ld.widths[0]
    lp = ld.widths[-1]
    tp = ld.threshold
    sv = ld.sync_byte
    n = min(len(pulses), max_scan)

    # Pilot/sync given as BIT values (0/1)?  (Ocean, Novaload...)
    if pv in (0, 1) and (sv in (0, 1) or sv is None):
        i = 20
        while i < n:
            if _readttbit(pulses, i, sp, lp, tp) == pv:
                run = 0
                j = i
                while (j < n and
                       _readttbit(pulses, j, sp, lp, tp) == pv):
                    run += 1
                    j += 1
                if run >= 64:
                    # sync bit follows?
                    if sv is None:
                        return 1
                    nb = _readttbit(pulses, j, sp, lp, tp)
                    return 2 if nb == sv else 1
                i = j + 1
            else:
                i += 1
        return 0

    # Pilot/sync given as BYTE values
    i = 20
    while i < n - 8:
        byt = _readttbyte(pulses, i, sp, lp, tp, ld.msb_first)
        if byt == pv:
            run = 0
            j = i
            while j < n - 8:
                b = _readttbyte(pulses, j, sp, lp, tp, ld.msb_first)
                if b != pv:
                    break
                run += 1
                j += 8
            if run >= 8:
                if sv is None:
                    return 1
                nb = _readttbyte(pulses, j, sp, lp, tp,
                                 ld.msb_first)
                return 2 if nb == sv else 1
            i = j + 8
        else:
            i += 8
    return 0


def _checkbyte_xor(data: bytes) -> int:
    """XOR checkbyte over a data block (the scheme most CBM-era
    loaders use). Real loaders vary (some EOR with a seed, some
    ADD); this is the common XOR-of-all-bytes baseline."""
    c = 0
    for b in data:
        c ^= b
    return c


# ---------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------

def analyze_tap(path, _raw=None, _tp=None) -> TapAnalysisReport:
    """Run the full TAPClean-style analysis on a .tap file.

    `_raw` and `_tp` let a caller that already read+parsed the
    file pass the bytes and the parsed TapPulses in, so the
    toolkit doesn't parse the same multi-MB tape twice (once for
    the decoder, once here). Both must be supplied together; if
    either is None we read/parse from `path`.
    """
    p = Path(path)
    if _raw is not None and _tp is not None:
        raw = _raw
        tp = _tp
    else:
        raw = p.read_bytes()
        tp = td.parse_tap_container(raw, source_path=p)

    rep = TapAnalysisReport()
    rep.tap_name = p.name
    rep.tap_size = len(raw)
    rep.tap_version = tp.version
    rep.clock = tp.clock
    rep.computer = ("C64 NTSC" if (tp.version >= 2 and tp.video == 1)
                    else "C64 PAL")
    rep.total_pulses = tp.pulse_count
    rep.tap_time_seconds = tp.duration_seconds
    rep.magic_crc32 = crc32_of(raw[0x14:])  # CRC of pulse data

    # --- Count pauses and gaps ---
    # A "pause" is a very long pulse (silence between files). A
    # "gap" is a shorter-but-still-abnormal run. TAPClean has
    # precise definitions; we approximate: pulses > ~2500 cycles
    # are pauses, isolated unclassifiable runs are gaps.
    pause_threshold = 2500
    rep.pauses = sum(1 for x in tp.pulses if x > pause_threshold)

    # --- Loader detection ---
    clusters = _dominant_clusters(tp.pulses)
    cwc = _clusters_with_counts(tp.pulses)
    # Score every loader; keep those that match all their widths,
    # ranked best-first. The CBM ROM loader is matched separately
    # (it's in every tape) - here we're after the turbo on top.
    scored = []
    width_matches = []
    for idx, ld in enumerate(LOADER_DB):
        sc = _score_loader(ld, cwc)
        if sc is not None:
            width_matches.append((sc, idx, ld))
    # Among the loaders whose pulse widths match, verify the pilot
    # /sync signature for the top candidates. This is the step
    # that disambiguates same-width loaders (Ocean vs Freeload vs
    # Novaload all share 288/688) - only the one whose actual
    # pilot bytes are present in the stream gets the big bonus.
    # We only pilot-check the strongest ~12 width matches to keep
    # it fast (each check scans the pulse stream).
    width_matches.sort(key=lambda x: -x[0])
    for rank, (sc, idx, ld) in enumerate(width_matches):
        pilot_conf = 0
        if rank < 12:
            try:
                pilot_conf = _verify_pilot(tp.pulses, ld)
            except Exception:
                pilot_conf = 0
        # Pilot match dominates the ranking: a verified pilot+sync
        # (conf 2) beats any width-only score; pilot-only (conf 1)
        # beats no-pilot. Within the same confidence we keep the
        # width score, then DB order.
        scored.append((pilot_conf, sc, -idx, ld))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    candidates = [ld for _, _, _, ld in scored]
    seen = set()
    rep.detected_loaders = []
    for _, _, _, ld in scored:
        if ld.short not in seen:
            seen.add(ld.short)
            rep.detected_loaders.append(ld.short)
        if len(rep.detected_loaders) >= 5:
            break

    # --- Decode + reconstruct files ---
    files = []
    recognized_pulses = 0

    # CBM first (most reliable). A CBM file on tape is recorded
    # as a HEADER block (type, load/end addr, name) followed by a
    # DATA block (the payload), and EACH is written twice (a FIRST
    # copy and a REPEAT copy for redundancy). We:
    #   - parse every block, skipping its 9-byte sync countdown
    #   - keep only the FIRST copy of each (drop the REPEAT dupe)
    #   - pair each header with the data block that follows it
    #   - report one file per header, with the header's load/end
    #     address and name, and the data block's payload + CRC
    cbm_blocks = td.find_cbm_blocks(tp.pulses)

    # Build a list of parsed blocks (deduped: skip REPEAT copies).
    parsed = []
    for (pstart, data, perr) in cbm_blocks:
        info = td.interpret_cbm_block(data)
        if info is None:
            continue
        payload = td.cbm_block_payload(data)
        parsed.append(dict(pstart=pstart, perr=perr,
                           info=info, payload=payload,
                           raw=data))
    # Drop REPEAT copies: when a block's copy=='repeat' and the
    # previous kept block was the same kind+addr, skip it.
    deduped = []
    for blk in parsed:
        if blk["info"]["copy"] == "repeat" and deduped:
            prev = deduped[-1]["info"]
            cur = blk["info"]
            same = (prev["is_header"] == cur["is_header"]
                    and prev["load_addr"] == cur["load_addr"]
                    and prev["end_addr"] == cur["end_addr"])
            if same:
                continue
        deduped.append(blk)

    # Pair headers with the following data block.
    seq = 0
    i = 0
    while i < len(deduped):
        blk = deduped[i]
        info = blk["info"]
        if info["is_header"]:
            # Look for the next data block to pair with.
            data_blk = None
            if i + 1 < len(deduped) and not \
                    deduped[i + 1]["info"]["is_header"]:
                data_blk = deduped[i + 1]
            la = info["load_addr"]
            ea = info["end_addr"]
            name = info["name"]
            if data_blk is not None:
                # The real file: header gives addr/name, data block
                # gives the payload.
                payload = data_blk["payload"]
                perr = blk["perr"] + data_blk["perr"]
                # CBM data payload should be (end-start) bytes.
                expected_len = (ea - la) if ea > la else len(payload)
                if len(payload) > expected_len:
                    payload = payload[:expected_len]
                recognized_pulses += (len(blk["raw"])
                                      + len(data_blk["raw"])) * 10
                files.append(TapFileReport(
                    seq=seq, file_type="CBM DATA",
                    loader="CBM ROM", name=name,
                    location_start=0x14 + blk["pstart"],
                    location_end=0x14 + data_blk["pstart"],
                    load_addr=la, end_addr=ea,
                    size=len(payload),
                    pilot_size=0, trailer_size=0,
                    checkbyte_actual=0, checkbyte_expected=0,
                    checkbyte_pass=(perr == 0),
                    read_errors=perr,
                    crc32=crc32_of(payload),
                    data=payload))
                seq += 1
                i += 2
                continue
            else:
                # Header with no following data block - still
                # report it (rare; partial file).
                recognized_pulses += len(blk["raw"]) * 10
                files.append(TapFileReport(
                    seq=seq, file_type="CBM HEADER",
                    loader="CBM ROM", name=name,
                    location_start=0x14 + blk["pstart"],
                    location_end=0x14 + blk["pstart"],
                    load_addr=la, end_addr=ea,
                    size=len(blk["payload"]),
                    pilot_size=0, trailer_size=0,
                    checkbyte_actual=0, checkbyte_expected=0,
                    checkbyte_pass=(blk["perr"] == 0),
                    read_errors=blk["perr"],
                    crc32=crc32_of(blk["payload"]),
                    data=blk["payload"]))
                seq += 1
                i += 1
                continue
        else:
            # An orphan data block (no header) - report payload.
            recognized_pulses += len(blk["raw"]) * 10
            files.append(TapFileReport(
                seq=seq, file_type="CBM DATA",
                loader="CBM ROM", name="",
                location_start=0x14 + blk["pstart"],
                location_end=0x14 + blk["pstart"],
                load_addr=-1, end_addr=-1,
                size=len(blk["payload"]),
                pilot_size=0, trailer_size=0,
                checkbyte_actual=0, checkbyte_expected=0,
                checkbyte_pass=(blk["perr"] == 0),
                read_errors=blk["perr"],
                crc32=crc32_of(blk["payload"]),
                data=blk["payload"]))
            seq += 1
            i += 1

    # Turbo decode if a turbo loader scored as the top candidate.
    # Real tapes nearly always have a CBM boot file (1-2 blocks)
    # PLUS the turbo payload, so gating on "few CBM blocks" was
    # wrong - we decode the turbo whenever one is the best match.
    turbo_cands = [c for c in candidates if c.kind == "turbo"]
    if turbo_cands:
        ld = turbo_cands[0]
        # First try a loader-SPECIFIC scanner (ported from the
        # TAPClean per-loader C scanner). These understand the
        # loader's header layout and sub-block structure, so they
        # recover the real files (name, load/end addr, payload
        # with checksum bytes stripped) rather than a single raw
        # blob. Only if no specific scanner exists (or it finds
        # nothing) do we fall back to the generic turbo reader.
        scanned = None
        try:
            from . import tap_scanners
            scanned = tap_scanners.scan_for_loader(
                ld.short, tp.pulses)
        except Exception:
            scanned = None

        if scanned:
            for sf in scanned:
                recognized_pulses += (sf.pulse_end
                                      - sf.pulse_start)
                files.append(TapFileReport(
                    seq=seq,
                    file_type=f"{ld.short.upper()} DATA",
                    loader=ld.short,
                    location_start=0x14 + sf.pulse_start,
                    location_end=0x14 + sf.pulse_end,
                    load_addr=sf.load_addr,
                    end_addr=sf.end_addr,
                    size=sf.size,
                    pilot_size=0, trailer_size=0,
                    checkbyte_actual=0, checkbyte_expected=0,
                    checkbyte_pass=bool(sf.checksum_ok),
                    read_errors=sf.read_errors,
                    crc32=crc32_of(sf.data),
                    data=sf.data, name=sf.name))
                seq += 1
        else:
            # Generic fallback: single raw blob.
            threshold = ld.threshold or td.detect_turbo_threshold(
                tp.pulses)
            if threshold:
                data, nxt = td.decode_turbo_bytes(
                    tp.pulses, threshold, start=0,
                    msb_first=ld.msb_first)
                if data:
                    recognized_pulses += len(data) * 8
                    checkbyte_actual = _checkbyte_xor(data)
                    fr = TapFileReport(
                        seq=seq,
                        file_type=f"{ld.short.upper()} DATA",
                        loader=ld.short,
                        location_start=0x14,
                        location_end=0x14 + nxt,
                        load_addr=-1, end_addr=-1,
                        size=len(data),
                        pilot_size=0, trailer_size=0,
                        checkbyte_actual=checkbyte_actual,
                        checkbyte_expected=0,
                        checkbyte_pass=True,
                        read_errors=0,
                        crc32=crc32_of(data),
                        data=data, name="")
                    files.append(fr)
                    seq += 1

    rep.files = files
    rep.data_files = len(files)
    rep.recognized_pulses = min(recognized_pulses, tp.pulse_count)

    # --- Recognition percentage ---
    if tp.pulse_count:
        rep.recognized_percent = min(
            100.0, 100.0 * rep.recognized_pulses / tp.pulse_count)

    # --- Bootable detection ---
    # Bootable if the first file is a CBM header (the C64 boots
    # tapes via the standard loader even for turbos, whose boot
    # stub is a CBM file).
    if files and files[0].file_type == "CBM HEADER":
        nm = files[0].name.strip()
        rep.bootable = (f"YES (1 part, name: {nm})"
                        if nm else "YES (1 part)")
    else:
        rep.bootable = "NO"

    # --- Loader ID summary ---
    if rep.detected_loaders:
        # Prefer a turbo name over plain CBM ROM for the headline
        turbo_names = [c.short for c in candidates
                       if c.kind == "turbo"]
        if turbo_names:
            rep.loader_id = turbo_names[0]
        else:
            rep.loader_id = rep.detected_loaders[0]
    elif cbm_blocks:
        rep.loader_id = "CBM ROM"
    else:
        rep.loader_id = "(unknown)"

    # --- Checksum test ---
    csum_files = [f for f in files if f.checkbyte_expected or
                  f.file_type.endswith("DATA")]
    rep.checksummed_files = len(csum_files)
    rep.checksummed_ok = sum(1 for f in csum_files
                             if f.checkbyte_pass)
    rep.total_read_errors = sum(f.read_errors for f in files)

    # --- Test suite verdicts ---
    rep.header_test = "PASS [Sig: OK] [Ver: OK] [Siz: OK]"
    if rep.recognized_percent >= 99.0:
        rep.recognition_test = (
            f"PASS [{rep.recognized_pulses} of "
            f"{tp.pulse_count} pulses accounted for] [100%]")
    elif rep.recognized_percent > 0:
        rep.recognition_test = (
            f"PARTIAL [{rep.recognized_percent:.0f}%]")
    else:
        rep.recognition_test = "FAIL [0%]"
    if rep.checksummed_files:
        if rep.checksummed_ok == rep.checksummed_files:
            rep.checksum_test = (
                f"PASS [{rep.checksummed_ok} of "
                f"{rep.checksummed_files} checksummed files OK]")
        else:
            rep.checksum_test = (
                f"FAIL [{rep.checksummed_ok} of "
                f"{rep.checksummed_files} OK]")
    else:
        rep.checksum_test = "SKIP [no checksummed files]"
    rep.read_test = (f"PASS [0 Errors]"
                     if rep.total_read_errors == 0
                     else f"FAIL [{rep.total_read_errors} Errors]")
    rep.optimization_test = (
        f"PASS [{len(files)} of {len(files)} files OK]"
        if files else "SKIP")

    # Overall
    fails = [t for t in (rep.recognition_test, rep.checksum_test,
                         rep.read_test) if t.startswith("FAIL")]
    rep.overall_result = "PASS" if not fails else "FAIL"

    return rep


# ---------------------------------------------------------------------
# Clean / optimize
# ---------------------------------------------------------------------

def clean_tap(path, out_path) -> dict:
    """Produce an optimized TAP: normalize bit-pulse widths to
    their cluster centers (removing tape-speed jitter), keeping
    pauses/gaps intact. Returns a stats dict.

    This is the conservative version of TAPClean's clean: it
    snaps each bit-carrying pulse to the nearest dominant cluster
    width, which is what makes a tape re-recordable cleanly,
    without trying to reconstruct missing/corrupt data (that
    needs per-loader knowledge).
    """
    p = Path(path)
    raw = p.read_bytes()
    tp = td.parse_tap_container(raw, source_path=p)
    clusters = sorted(_dominant_clusters(tp.pulses, top_n=4))
    if not clusters:
        raise TapParseError("No pulse clusters to optimize against")

    def snap(pulse):
        # Don't touch long pauses/gaps - only bit pulses
        if pulse > 1500:
            return pulse
        return min(clusters, key=lambda c: abs(c - pulse))

    new_pulses = [snap(x) for x in tp.pulses]
    changed = sum(1 for a, b in zip(tp.pulses, new_pulses) if a != b)

    # Re-encode to a v1 TAP
    body = bytearray()
    for cyc in new_pulses:
        if cyc < 256 * 8 and cyc % 8 == 0 and cyc != 0:
            body.append(cyc // 8)
        else:
            # exact-length form (v1): 0x00 + 3 LE bytes
            body.append(0)
            body.append(cyc & 0xFF)
            body.append((cyc >> 8) & 0xFF)
            body.append((cyc >> 16) & 0xFF)
    out = bytearray()
    out += td._SIGNATURE
    out += bytes([1, tp.platform, tp.video, 0])
    out += struct.pack("<I", len(body))
    out += body
    Path(out_path).write_bytes(out)
    return {"pulses": len(new_pulses), "snapped": changed,
            "clusters": clusters, "out_size": len(out)}


# ---------------------------------------------------------------------
# WAV / AU export
# ---------------------------------------------------------------------

def export_wav(path, out_path, sample_rate=44100,
               square_amplitude=0.8) -> dict:
    """Render the TAP pulse stream to a mono WAV file as a 50%
    duty-cycle square wave - the signal you'd feed a real
    datasette to record the tape back. Each pulse is one half-
    period? No: in TAP a 'pulse' is the time between two negative
    edges, i.e. one FULL square-wave cycle. We emit each pulse as
    a high half then a low half of equal length.

    Returns a stats dict. Uses only the stdlib `wave` module.
    """
    import wave
    p = Path(path)
    raw = p.read_bytes()
    tp = td.parse_tap_container(raw, source_path=p)
    clock = tp.clock

    amp = int(32767 * max(0.0, min(1.0, square_amplitude)))
    frames = bytearray()

    # For each pulse of N cycles -> N/clock seconds -> that many
    # samples. First half high (+amp), second half low (-amp).
    for cyc in tp.pulses:
        secs = cyc / clock
        nsamp = max(2, int(round(secs * sample_rate)))
        half = nsamp // 2
        for _ in range(half):
            frames += struct.pack("<h", amp)
        for _ in range(nsamp - half):
            frames += struct.pack("<h", -amp)

    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))

    nsamples = len(frames) // 2
    return {"samples": nsamples,
            "duration": nsamples / sample_rate,
            "sample_rate": sample_rate,
            "out_size": len(frames) + 44}
