# date_time: 2026-05-28 15:26
"""
Commodore 64 .TAP cassette image decoder (pure logic, no Qt).

A .TAP file is a pulse-length recording of a C64 datasette tape.
This module parses the container, decodes the pulse stream into
bytes for the CBM ROM loader and the common turbo loaders, and
reconstructs the embedded files (load address, end address,
name, payload) so they can be extracted as .prg.

TAP container format (Schepers/Sundell):
    $0000-$000B  signature "C64-TAPE-RAW"
    $000C        version: 0, 1 or 2
    $000D        platform (v2): 0=C64, 1=VIC20, 2=C16  (else reserved)
    $000E        video std (v2): 0=PAL, 1=NTSC          (else reserved)
    $000F        reserved
    $0010-$0013  data size (little-endian, excludes the 20-byte header)
    $0014-...    pulse data

Pulse encoding:
    Each non-zero byte B is a pulse of length  B * 8  clock cycles.
    => seconds = B * 8 / clock,  clock = 985248 (PAL) / 1022730 (NTSC)
    In v0: a $00 byte means "overflow" - a pulse longer than
        255*8 cycles, exact length unknown (treated as a long gap).
    In v1/v2: a $00 byte is followed by THREE little-endian bytes
        giving the exact pulse length in clock cycles (not *8).

We represent the decoded stream as a list of pulse lengths in
clock cycles (int), so all downstream timing math is uniform
regardless of v0/v1/v2.

CBM ROM loader encoding (the standard KERNAL SAVE format):
    Three pulse widths, in clock cycles (PAL nominal):
        short  S ~ 360 cycles  (TAP ~$2B)
        medium M ~ 520 cycles  (TAP ~$3F)
        long   L ~ 680 cycles  (TAP ~$53)
    A data bit is a PAIR of pulses:
        bit 0 = S,M
        bit 1 = M,S
    A byte = 8 data bits (LSB first) + 1 parity bit, framed by
        a "new data" marker (L,M) at the start of each byte.
    Each tape "block" is preceded by a long pilot/leader of short
    pulses and a sync sequence ($89..$81 countdown for header,
    $09..$01 for the repeated copy). Files are written twice
    (redundancy). A header block holds: file type, start addr,
    end addr, filename, then the data block holds the payload.

Turbo loaders:
    Most turbos use just TWO pulse widths (bit0 short, bit1 long)
    with a loader-specific threshold. We auto-detect the threshold
    from the pulse histogram (two dominant clusters) and decode
    bytes MSB-first or LSB-first, trying both and picking whichever
    yields a plausible pilot/sync structure. Specific known loaders
    (Turbotape-250, etc.) get named when their threshold + pilot
    byte match.

This is necessarily heuristic - tape decoding is famously fiddly -
so the decoder reports a confidence level and always exposes the
raw pulse stream so the GUI can show what it found even when full
file reconstruction fails.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# PAL / NTSC master clock (Hz). The data-size-vs-clock choice only
# affects the microsecond display, not the pulse-cycle math.
CLOCK_PAL = 985248
CLOCK_NTSC = 1022730

_SIGNATURE = b"C64-TAPE-RAW"

# CBM ROM-loader nominal pulse widths in clock cycles (PAL),
# measured from a corpus of real TAP dumps (Ocean, US Gold,
# System 3, Hewson, etc.). Every C64 tape carries a CBM boot
# file so these clusters appear in every dump and are solid:
# short=384, medium=528, long=688. (Textbook 360/520/680 is
# slightly off from what real DC2N/audiotap captures produce.)
CBM_SHORT = 384
CBM_MEDIUM = 528
CBM_LONG = 688
# Tolerance window (+/- cycles) for classifying a pulse as S/M/L.
# 48 is tight enough to separate the three CBM bands (~140 cyc
# apart) while absorbing tape jitter. The old 80 was so wide
# that turbo bit-pulses (288, 504, ...) leaked into the CBM
# bands and triggered false CBM detection on turbo tapes.
CBM_TOL = 48


class TapParseError(Exception):
    """Raised when the container header is missing or corrupt."""
    pass


@dataclass
class TapPulses:
    """The raw decoded pulse stream plus container metadata."""
    version: int
    platform: int          # 0=C64,1=VIC20,2=C16 (v2 only, else 0)
    video: int             # 0=PAL,1=NTSC (v2 only, else 0)
    clock: int             # resolved clock for timing display
    pulses: list           # list[int] pulse lengths in clock cycles
    data_size: int         # bytes declared in the header
    path: Optional[Path] = None

    @property
    def pulse_count(self) -> int:
        return len(self.pulses)

    @property
    def total_cycles(self) -> int:
        return sum(self.pulses)

    @property
    def duration_seconds(self) -> float:
        return self.total_cycles / self.clock if self.clock else 0.0


@dataclass
class TapFileEntry:
    """One reconstructed file found in the tape stream."""
    index: int
    kind: str              # "cbm-header" | "cbm-data" | "turbo" | "raw"
    loader: str            # human-readable loader name
    name: str              # filename (CBM) or "" 
    file_type: int         # CBM file type byte (1=PRG relocatable,3=PRG abs,...)
    load_addr: int         # start address (or -1 if unknown)
    end_addr: int          # end address (or -1)
    data: bytes            # the payload bytes (without load-addr prefix)
    pulse_start: int       # index into the pulse stream where this starts
    pulse_end: int
    checksum_ok: Optional[bool] = None
    notes: str = ""

    @property
    def size(self) -> int:
        return len(self.data)

    def as_prg(self) -> bytes:
        """Return the file as a .prg: 2-byte little-endian load
        address followed by the data. If load_addr is unknown we
        prefix $0000 so the file is at least well-formed."""
        la = self.load_addr if self.load_addr >= 0 else 0
        return struct.pack("<H", la & 0xFFFF) + self.data


@dataclass
class TapDecodeResult:
    """Everything the decoder produced for one TAP file."""
    pulses: TapPulses
    files: list = field(default_factory=list)      # list[TapFileEntry]
    histogram: dict = field(default_factory=dict)  # pulse-cycle -> count
    detected_loaders: list = field(default_factory=list)  # list[str]
    summary: str = ""


# ---------------------------------------------------------------------
# Container parsing
# ---------------------------------------------------------------------

def parse_tap_container(raw: bytes,
                        source_path=None) -> TapPulses:
    """Parse the 20-byte header and expand the pulse data into a
    list of pulse lengths in clock cycles. Raises TapParseError
    on a bad signature or truncated header."""
    if len(raw) < 20:
        raise TapParseError(
            "File too small to be a TAP (need >= 20 header bytes)")
    if raw[0:12] != _SIGNATURE:
        raise TapParseError(
            f"Bad signature: expected {_SIGNATURE!r}, "
            f"got {raw[0:12]!r}")
    version = raw[0x0C]
    platform = raw[0x0D]
    video = raw[0x0E]
    declared = struct.unpack_from("<I", raw, 0x10)[0]

    clock = CLOCK_NTSC if (version >= 2 and video == 1) else CLOCK_PAL

    data = raw[0x14:]
    if declared and declared <= len(data):
        data = data[:declared]

    pulses = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != 0:
            pulses.append(b * 8)
            i += 1
        else:
            # $00: version-dependent
            if version == 0:
                # overflow - unknown long pulse. Use a sentinel of
                # 256*8 so it's classified as a long gap.
                pulses.append(256 * 8)
                i += 1
            else:
                # v1/v2: next 3 bytes are exact cycle count (LE)
                if i + 3 < n:
                    lo, mid, hi = data[i+1], data[i+2], data[i+3]
                    cycles = lo | (mid << 8) | (hi << 16)
                    pulses.append(cycles)
                    i += 4
                else:
                    # truncated - bail out gracefully
                    break

    return TapPulses(
        version=version, platform=platform, video=video,
        clock=clock, pulses=pulses, data_size=declared,
        path=Path(source_path) if source_path else None)


def parse_tap(path) -> TapPulses:
    """Read a TAP file from disk and parse its container."""
    p = Path(path)
    raw = p.read_bytes()
    return parse_tap_container(raw, source_path=p)


# ---------------------------------------------------------------------
# Pulse histogram + threshold detection
# ---------------------------------------------------------------------

def build_histogram(pulses, bucket=8):
    """Bucket pulse lengths (in cycles) for a histogram. Returns
    an ordered dict-like list of (bucket_center, count) sorted by
    bucket. Bucketing by 8 cycles keeps the original byte
    resolution of v0 files."""
    hist = {}
    for p in pulses:
        key = (p // bucket) * bucket
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items()))


def detect_turbo_threshold(pulses):
    """Find the two dominant pulse clusters in a (presumably
    two-width) turbo stream and return a threshold between them,
    or None if the stream doesn't look like a two-width turbo.

    Approach: histogram, find the two highest-count buckets that
    are sufficiently far apart, threshold = midpoint.
    """
    if len(pulses) < 64:
        return None
    hist = build_histogram(pulses, bucket=16)
    # Ignore very long pulses (gaps/pilots overflow) for the
    # cluster detection - we care about the bit-carrying pulses.
    items = [(c, k) for k, c in hist.items() if k < 2000]
    if len(items) < 2:
        return None
    items.sort(reverse=True)   # by count desc
    # Take the top buckets, then find two that are far enough apart
    top = [k for _, k in items[:6]]
    top.sort()
    best_pair = None
    for a in range(len(top)):
        for b in range(a + 1, len(top)):
            gap = top[b] - top[a]
            if gap >= 100:      # at least 100 cycles apart
                if best_pair is None or gap > best_pair[2]:
                    best_pair = (top[a], top[b], gap)
    if best_pair is None:
        return None
    short_w, long_w, _ = best_pair
    return (short_w + long_w) // 2


# ---------------------------------------------------------------------
# CBM ROM-loader decode
# ---------------------------------------------------------------------

# Precomputed classification bounds (low, high) for S/M/L so the
# hot classify loop does simple comparisons instead of abs() calls.
_CBM_S_LO = CBM_SHORT - CBM_TOL
_CBM_S_HI = CBM_SHORT + CBM_TOL
_CBM_M_LO = CBM_MEDIUM - CBM_TOL
_CBM_M_HI = CBM_MEDIUM + CBM_TOL
_CBM_L_LO = CBM_LONG - CBM_TOL
_CBM_L_HI = CBM_LONG + CBM_TOL


def _classify_cbm(pulse):
    """Classify a single pulse as 'S','M','L' or '?' for the CBM
    loader. Range comparisons (no abs()) keep this fast - it's
    called millions of times on a full-tape scan."""
    if _CBM_S_LO <= pulse <= _CBM_S_HI:
        return 'S'
    if _CBM_M_LO <= pulse <= _CBM_M_HI:
        return 'M'
    if _CBM_L_LO <= pulse <= _CBM_L_HI:
        return 'L'
    return '?'


def decode_cbm_bytes(pulses, start=0):
    """Decode CBM-standard bytes starting at pulse index `start`.

    Returns (bytes_decoded, parity_errors, next_index). Stops when
    it runs into a long gap / unclassifiable run (end of block).

    CBM bit encoding (pulse pairs):
        bit 0 = S,M     bit 1 = M,S
        byte marker / new-data = L,M
        word marker = L,L (we treat L,L as block boundary)
    Byte = marker + 8 data bits (LSB first) + 1 parity bit.
    """
    out = bytearray()
    parity_errors = 0
    i = start
    n = len(pulses)

    def cls(idx):
        return _classify_cbm(pulses[idx]) if idx < n else '?'

    while i + 1 < n:
        a, b = cls(i), cls(i + 1)
        # New-data marker (L,M) precedes each byte. Word marker
        # (L,L) ends the block.
        if a == 'L' and b == 'L':
            i += 2
            break
        if a == 'L' and b == 'M':
            i += 2
            # read 8 data bits + 1 parity
            val = 0
            ok = True
            bits = []
            for bitpos in range(9):
                if i + 1 >= n:
                    ok = False
                    break
                pa, pb = cls(i), cls(i + 1)
                if pa == 'S' and pb == 'M':
                    bits.append(0)
                elif pa == 'M' and pb == 'S':
                    bits.append(1)
                else:
                    ok = False
                    break
                i += 2
            if not ok or len(bits) < 9:
                break
            data_bits = bits[:8]
            parity_bit = bits[8]
            for bitpos, bit in enumerate(data_bits):
                val |= (bit << bitpos)
            # CBM uses odd parity: data bits + parity should be odd
            ones = sum(data_bits) + parity_bit
            if ones % 2 == 0:
                parity_errors += 1
            out.append(val)
        else:
            # Not a byte start. If we haven't decoded anything yet
            # this "pilot" wasn't followed by real CBM data (very
            # common when scanning a turbo tape whose pulses happen
            # to include short runs) - bail immediately instead of
            # crawling pulse-by-pulse through the whole stream,
            # which was the O(n)-per-false-pilot blow-up that made
            # big turbo tapes take ~9s to scan.
            break
    return bytes(out), parity_errors, i


def find_cbm_blocks(pulses):
    """Scan the pulse stream for CBM blocks. A block is preceded by
    a long run of short (S) pulses (the pilot/leader). Returns a
    list of (pulse_index, decoded_bytes, parity_errors).

    Performance: we advance i past every run we examine instead of
    re-scanning from i+1, so the whole stream is walked once
    (O(n)) rather than re-counting overlapping short-runs (which
    was O(n^2) on the long pilots real tapes have - a 700k-pulse
    Ocean tape took ~4s, now well under a second).
    """
    blocks = []
    i = 0
    n = len(pulses)
    while i < n:
        # Count the short-pulse run starting at i.
        j = i
        while j < n and _classify_cbm(pulses[j]) == 'S':
            j += 1
        run = j - i
        if run >= 32:
            # Pilot found - decode bytes from the end of the run.
            data, perr, nxt = decode_cbm_bytes(pulses, j)
            if data:
                blocks.append((i, data, perr))
                # Advance past whatever the decoder consumed (or at
                # least past the pilot) so we never re-scan it.
                i = max(nxt, j)
                continue
            # No data after the pilot - jump past the pilot anyway.
            i = j
            continue
        # Not a pilot: advance past the (short-or-not) run we just
        # examined. If run==0 the pulse at i wasn't short, so step
        # one; otherwise skip the whole sub-threshold short run.
        i = j + 1 if run == 0 else j
    return blocks


def interpret_cbm_block(data):
    """Interpret a decoded CBM block. A real CBM block from the
    tape decoder starts with the 9-byte SYNC COUNTDOWN sequence,
    NOT the payload:

        FIRST copy:  $89 $88 $87 $86 $85 $84 $83 $82 $81
        REPEAT copy: $09 $08 $07 $06 $05 $04 $03 $02 $01

    The actual data follows the countdown. For a HEADER block the
    data is:

        +0       file type (1-6: 1=BASIC,3=PRG,4=SEQ,5=EOT)
        +1..+2   start address (LE)
        +3..+4   end address (LE)
        +5..+20  16-byte filename (PETSCII, space-padded)

    This mirrors TAPClean's c64tape.c: detect the $x9..$x1
    countdown, set the start-of-data past it, then read type +
    addresses and apply the plausibility test (type 1-5 AND
    end>start) to decide header vs data.

    Returns a dict with file_type, load_addr, end_addr, name,
    is_header, copy ('first'/'repeat'/None) and data_offset (the
    index in `data` where the payload begins). load/end are -1
    when this isn't a header.
    """
    if len(data) < 1:
        return None

    info = {"file_type": 0, "load_addr": -1, "end_addr": -1,
            "name": "", "is_header": False, "copy": None,
            "data_offset": 0}

    # Detect the sync countdown at the start of the block.
    FIRST = bytes([0x89, 0x88, 0x87, 0x86, 0x85,
                   0x84, 0x83, 0x82, 0x81])
    REPEAT = bytes([0x09, 0x08, 0x07, 0x06, 0x05,
                    0x04, 0x03, 0x02, 0x01])
    off = 0
    if data[:9] == FIRST:
        info["copy"] = "first"
        off = 9
    elif data[:9] == REPEAT:
        info["copy"] = "repeat"
        off = 9
    else:
        # No countdown found - some captures start the block right
        # at the data. Fall back to offset 0 but still try to read
        # a plausible header.
        off = 0
    info["data_offset"] = off

    if len(data) < off + 5:
        return info

    ftype = data[off]
    s = data[off + 1] | (data[off + 2] << 8)
    e = data[off + 3] | (data[off + 4] << 8)

    # Plausibility test (TAPClean): type 1-5 and end > start.
    if 1 <= ftype <= 5 and e > s:
        info["is_header"] = True
        info["file_type"] = ftype
        info["load_addr"] = s
        info["end_addr"] = e
        if len(data) >= off + 21:
            raw_name = data[off + 5: off + 21]
            # CBM filenames are PETSCII and Ocean-style loaders
            # embed colour/cursor control codes (e.g. $05, $1F,
            # reverse-on $12) for a fancy on-screen LOADING banner.
            # Keep printable ASCII letters/digits/space/punct and
            # drop control bytes so the displayed name is readable
            # (TAPClean does the same kind of cleanup).
            chars = []
            for b in raw_name:
                if 32 <= b < 127:
                    chars.append(chr(b))
                # PETSCII uppercase letters also live at $C1-$DA
                elif 0xC1 <= b <= 0xDA:
                    chars.append(chr(b - 0x80))
                # else: control code, skip
            info["name"] = "".join(chars).rstrip(" \x00").strip()
    else:
        info["is_header"] = False
        info["file_type"] = ftype
    return info


def cbm_block_payload(data):
    """Return the payload bytes of a CBM block (everything after
    the 9-byte sync countdown). For a header that's type+addr+name
    +pad; for a data block that's the actual program bytes."""
    info = interpret_cbm_block(data)
    off = info["data_offset"] if info else 0
    return data[off:]


# ---------------------------------------------------------------------
# Turbo-loader decode
# ---------------------------------------------------------------------

def decode_turbo_bytes(pulses, threshold, start=0,
                       msb_first=True, max_bytes=65536):
    """Decode a two-width turbo stream into bytes. Pulse <
    threshold = bit 0, >= threshold = bit 1 (the common
    convention; some loaders invert, the caller can try both by
    flipping the bit sense). 8 bits per byte, MSB or LSB first.

    Stops at a long gap (pulse > 3*threshold) or after max_bytes.
    Returns (bytes, next_index)."""
    out = bytearray()
    i = start
    n = len(pulses)
    gap_limit = threshold * 3
    while i < n and len(out) < max_bytes:
        # Skip into the byte: a long pulse signals a gap/pilot end
        if pulses[i] > gap_limit:
            if out:
                break
            i += 1
            continue
        if i + 8 > n:
            break
        val = 0
        ok = True
        for bitpos in range(8):
            p = pulses[i + bitpos]
            if p > gap_limit:
                ok = False
                break
            bit = 1 if p >= threshold else 0
            if msb_first:
                val = (val << 1) | bit
            else:
                val |= (bit << bitpos)
        if not ok:
            break
        out.append(val)
        i += 8
    return bytes(out), i


def detect_turbo_pilot(data):
    """Look for a run of identical bytes at the start of a turbo
    byte stream (the pilot). Returns (pilot_byte, run_length) or
    (None, 0)."""
    if not data:
        return None, 0
    first = data[0]
    run = 0
    for b in data:
        if b == first:
            run += 1
        else:
            break
    if run >= 8:
        return first, run
    return None, 0


# Known turbo loaders keyed by (approximate threshold cycles,
# pilot byte). Values are display names. Thresholds are rough -
# we match within a window.
KNOWN_TURBOS = [
    # (short_cycles, long_cycles, pilot_byte, name)
    (210, 320, 0x02, "Turbo Tape 250 / Novaload-style"),
    (210, 320, 0x40, "CHR / Cauldron-style (pilot $40)"),
    (180, 280, 0x40, "Generic IRQ turbo (pilot $40)"),
]


def identify_turbo(short_w, long_w, pilot_byte):
    """Best-effort name for a turbo loader from its pulse widths
    and pilot byte."""
    for s, l, pb, name in KNOWN_TURBOS:
        if (abs(short_w - s) <= 60 and abs(long_w - l) <= 60
                and (pilot_byte == pb or pilot_byte is None)):
            return name
    return "Unknown turbo loader"


# ---------------------------------------------------------------------
# Top-level decode
# ---------------------------------------------------------------------

def decode_tap(path_or_pulses) -> TapDecodeResult:
    """Full decode pipeline. Accepts a path/str or a TapPulses
    object. Returns a TapDecodeResult with reconstructed files,
    histogram, and detected loader names.

    The strategy:
      1. Try CBM ROM-loader decode (pilot=short-run, S/M/L pulses).
         CBM files come in header+data pairs, each written twice.
      2. If little/no CBM structure is found, attempt turbo decode
         using an auto-detected threshold, trying MSB and LSB bit
         order and reporting whichever produces a clean pilot.
      3. Always expose the pulse histogram.
    """
    if isinstance(path_or_pulses, TapPulses):
        tp = path_or_pulses
    else:
        tp = parse_tap(path_or_pulses)

    result = TapDecodeResult(pulses=tp)
    result.histogram = build_histogram(tp.pulses, bucket=8)

    files = []
    detected = []

    # --- CBM decode ---
    cbm_blocks = find_cbm_blocks(tp.pulses)
    if cbm_blocks:
        detected.append("CBM ROM loader")
        idx = 0
        last_header = None
        for (pstart, data, perr) in cbm_blocks:
            info = interpret_cbm_block(data)
            payload = cbm_block_payload(data)
            if info and info["is_header"]:
                # Header block - remember addresses + name. Skip
                # the REPEAT copy so we don't list every file
                # twice.
                if (info["copy"] == "repeat" and last_header
                        and last_header.get("load_addr")
                        == info["load_addr"]):
                    continue
                last_header = info
                files.append(TapFileEntry(
                    index=idx, kind="cbm-header",
                    loader="CBM ROM loader",
                    name=info["name"],
                    file_type=info["file_type"],
                    load_addr=info["load_addr"],
                    end_addr=info["end_addr"],
                    data=payload,
                    pulse_start=pstart, pulse_end=pstart,
                    checksum_ok=(perr == 0),
                    notes=f"{perr} parity error(s)" if perr else ""))
                idx += 1
            else:
                # Data block - pair with the most recent header.
                # Skip a REPEAT data copy.
                if info and info["copy"] == "repeat" and \
                        files and files[-1].kind == "cbm-data":
                    continue
                la = last_header["load_addr"] if last_header else -1
                ea = last_header["end_addr"] if last_header else -1
                nm = last_header["name"] if last_header else ""
                files.append(TapFileEntry(
                    index=idx, kind="cbm-data",
                    loader="CBM ROM loader",
                    name=nm,
                    file_type=last_header["file_type"]
                              if last_header else 0,
                    load_addr=la, end_addr=ea,
                    data=payload,
                    pulse_start=pstart, pulse_end=pstart,
                    checksum_ok=(perr == 0),
                    notes=f"{perr} parity error(s)" if perr else ""))
                idx += 1
                last_header = None

    # --- Turbo decode (only if CBM gave little AND the stream
    # doesn't look predominantly CBM) ---
    # We measure what fraction of pulses classify as CBM S/M/L.
    # A real CBM tape is ~100% classifiable; a turbo tape has its
    # bit pulses fall OUTSIDE the CBM windows, so a low fraction
    # is the signal to try turbo decoding. This stops us from
    # mis-reading a clean CBM stream as turbo garbage.
    cbm_like = 0
    sample = tp.pulses[:4000]
    for p in sample:
        if _classify_cbm(p) != '?':
            cbm_like += 1
    cbm_frac = (cbm_like / len(sample)) if sample else 0.0

    if len(cbm_blocks) < 1 and cbm_frac < 0.6:
        threshold = detect_turbo_threshold(tp.pulses)
        if threshold:
            # Find the two cluster widths around the threshold for
            # naming.
            hist = build_histogram(tp.pulses, bucket=16)
            below = [(c, k) for k, c in hist.items()
                     if k < threshold and k < 2000]
            above = [(c, k) for k, c in hist.items()
                     if k >= threshold and k < 2000]
            short_w = max(below)[1] if below else threshold - 50
            long_w = max(above)[1] if above else threshold + 50
            # Try MSB-first then LSB-first, keep the one with a
            # cleaner pilot.
            best = None
            for msb in (True, False):
                data, nxt = decode_turbo_bytes(
                    tp.pulses, threshold, start=0, msb_first=msb)
                pilot, run = detect_turbo_pilot(data)
                score = run + (len(data) // 100)
                if best is None or score > best[0]:
                    best = (score, data, msb, pilot, run)
            if best:
                _, data, msb, pilot, run = best
                name = identify_turbo(short_w, long_w, pilot)
                detected.append(name)
                files.append(TapFileEntry(
                    index=len(files), kind="turbo",
                    loader=name,
                    name="", file_type=0,
                    load_addr=-1, end_addr=-1,
                    data=data,
                    pulse_start=0, pulse_end=len(tp.pulses),
                    checksum_ok=None,
                    notes=(f"threshold={threshold}cyc, "
                           f"{'MSB' if msb else 'LSB'}-first, "
                           f"pilot=${pilot:02X} x{run}"
                           if pilot is not None
                           else f"threshold={threshold}cyc, "
                                f"{'MSB' if msb else 'LSB'}-first")))

    result.files = files
    result.detected_loaders = detected

    # Summary line
    npulse = tp.pulse_count
    dur = tp.duration_seconds
    parts = [
        f"TAP v{tp.version}",
        f"{npulse} pulses",
        f"{dur:.1f}s",
    ]
    if detected:
        parts.append(", ".join(detected))
    if files:
        parts.append(f"{len(files)} block(s)")
    result.summary = " | ".join(parts)
    return result
