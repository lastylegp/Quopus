# date_time: 2026-05-28 15:33
"""
Loader-specific tape scanners, ported from TAPClean's per-loader
C scanners (src/scanners/*.c, GPL). Unlike the generic two-width
turbo reader in tap_decoder, these understand each loader's actual
header layout and sub-block structure, so they recover the real
embedded files: filename, load/end address, and the de-chunked
payload (with the per-sub-block checksum bytes stripped out).

Currently implemented:
  * Novaload      (nova_f1.c)  - R-Type, Giana Sisters, Danger
                                 Freak, Decathlon turbo parts
  * Ocean/Imagine (ocean.c)    - Cobra, Commando, 1942, Parallax

Each scanner returns a list of ScannedFile. The design mirrors
TAPClean: find_pilot -> sync -> read header -> read data blocks.

These reuse the low-level readttbit/readttbyte ports that live in
tap_analyzer (TAPClean's generic bit/byte readers).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from . import tap_decoder as td


@dataclass
class ScannedFile:
    """A file recovered by a loader-specific scanner."""
    loader: str
    name: str
    load_addr: int
    end_addr: int
    data: bytes
    read_errors: int = 0
    checksum_ok: Optional[bool] = None
    pulse_start: int = 0
    pulse_end: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


# ---------------------------------------------------------------------
# Low-level readers (TAPClean readttbit / readttbyte, in cycles)
# ---------------------------------------------------------------------

def _readbit(pulses, pos, sp, lp, tp, tol=56):
    """Classify pulse at pos as bit 0 (short) or 1 (long)."""
    if pos < 0 or pos >= len(pulses):
        return -1
    b = pulses[pos]
    if tp:
        if (sp - tol) < b < tp:
            return 0
        if tp < b < (lp + tol):
            return 1
        return -1
    near_s = (sp - tol) < b < (sp + tol)
    near_l = (lp - tol) < b < (lp + tol)
    if near_s and near_l:
        return 0 if abs(lp - b) > abs(sp - b) else 1
    if near_s:
        return 0
    if near_l:
        return 1
    return -1


def _readbyte(pulses, pos, sp, lp, tp, msb_first, tol=56):
    """Read 8 bits from pos -> byte, or -1 on error."""
    bits = []
    for i in range(8):
        b = _readbit(pulses, pos + i, sp, lp, tp, tol)
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


def _find_pilot_bit(pulses, start, sp, lp, tp, pilot_bit,
                    pmin, tol=56):
    """find_pilot for loaders whose pilot is a repeated BIT value
    (Novaload, Ocean: pv=0). Returns the index just past the pilot
    run (where the sync bit sits) or -1.

    Scans forward from `start`; the first place we find at least
    `pmin` consecutive pilot bits, we return the end of that run.
    """
    n = len(pulses)
    i = start
    while i < n:
        if _readbit(pulses, i, sp, lp, tp, tol) == pilot_bit:
            run = 0
            j = i
            while (j < n and
                   _readbit(pulses, j, sp, lp, tp, tol)
                   == pilot_bit):
                run += 1
                j += 1
            if run >= pmin:
                return j        # index of first non-pilot (sync)
            i = j + 1
        else:
            i += 1
    return -1


# ---------------------------------------------------------------------
# Turbotape-250 scanner (port of turbotape.c)
# ---------------------------------------------------------------------

# Turbotape-250 fingerprint from TAPClean ft[]:
#   sp=0x1A*8=208  lp=0x28*8=320  tp=0x20*8=256  en=MSbF
#   pilot byte=0x02, sync byte=0x09, then $09..$01 countdown
# This loader (and its many clones: Anirog, American Action,
# Power Load, Tequila, LK Avalon, MMS...) carries the actual game
# on R-Type, Giana Sisters, Danger Freak, etc.
_TT_SP = 208
_TT_LP = 320
_TT_TP = 256
_TT_MSB = True
_TT_LEAD = 0x02
_TT_SYNC = 0x09


def _tt_find_lead(pulses, start, sp, lp, tp, msb, tol):
    """Scan forward for a run of LEAD ($02) bytes ending in the
    SYNC countdown. Returns the byte index just past the $09..$01
    countdown (start of ID byte) or -1.

    Crucially this scans PULSE-by-pulse (not byte-by-byte) when
    looking for the pilot, because the byte grid isn't aligned to
    the stream until we lock onto the pilot. Once we read a $02 at
    some offset we follow the run on the 8-pulse byte grid from
    there. This is what makes the decode land on real byte
    boundaries (the earlier byte-stepped version read garbage like
    a single '$02 x N' blob).
    """
    n = len(pulses)
    i = start
    while i < n - 8 * 12:
        # Try to lock the pilot at THIS pulse offset.
        if _readbyte(pulses, i, sp, lp, tp, msb, tol) == _TT_LEAD:
            # Follow the $02 run on the byte grid from here.
            run = 0
            j = i
            while j < n - 8:
                b = _readbyte(pulses, j, sp, lp, tp, msb, tol)
                if b != _TT_LEAD:
                    break
                run += 1
                j += 8
            if run >= 50:
                # j points at first non-$02 byte; expect SYNC $09
                # then the $09..$01 countdown.
                pat = [_readbyte(pulses, j + k * 8, sp, lp, tp,
                                 msb, tol) for k in range(9)]
                if pat == [9, 8, 7, 6, 5, 4, 3, 2, 1]:
                    return j + 9 * 8        # start of ID byte
                # Pilot locked but countdown didn't match - jump
                # past the run and keep scanning.
                i = j
                continue
        i += 1
    return -1


def scan_turbotape(pulses, tol=56):
    """Recover Turbotape-250 files (and clones). Mirrors
    turbotape.c: lead $02 run -> $09..$01 countdown -> ID byte
    (1/2 = header, 0 = data).

    Header layout (after ID byte):
        hd[1..2] = DATA start addr (LE)
        hd[3..4] = DATA end addr (LE)
        hd[6..21]= 16-byte filename
    Data block: ID byte 0, then payload of (end-start) bytes,
    then an XOR checkbyte.
    """
    sp, lp, tp = _TT_SP, _TT_LP, _TT_TP
    msb = _TT_MSB
    n = len(pulses)
    files = []
    i = 20
    guard = 0
    pending = None      # (start, end, name) from a header
    while i < n - 80 and guard < 200000:
        guard += 1
        sod = _tt_find_lead(pulses, i, sp, lp, tp, msb, tol)
        if sod < 0:
            break
        idb = _readbyte(pulses, sod, sp, lp, tp, msb, tol)
        if idb in (1, 2):
            # header: read 22 bytes
            hd = []
            ok = True
            for k in range(22):
                v = _readbyte(pulses, sod + k * 8, sp, lp, tp,
                              msb, tol)
                if v == -1:
                    ok = False
                    break
                hd.append(v)
            if ok and len(hd) >= 22:
                start = hd[1] | (hd[2] << 8)
                end = hd[3] | (hd[4] << 8)
                name = _clean_petscii(bytes(hd[6:22]))
                if end > start:
                    pending = (start, end, name)
            i = sod + 22 * 8
            continue
        elif idb == 0:
            # data: use pending header's addr if available
            if pending:
                start, end, name = pending
                size = end - start
            else:
                start, end, name = -1, -1, ""
                size = 0
            if size <= 0 or size > 0xFFFF:
                i = sod + 8
                pending = None
                continue
            s = sod + 8          # skip ID byte
            out = bytearray()
            rd_err = 0
            cb = 0
            for k in range(size):
                v = _readbyte(pulses, s + k * 8, sp, lp, tp,
                              msb, tol)
                if v == -1:
                    rd_err += 1
                    v = 0
                cb ^= v
                out.append(v)
            # actual checkbyte
            cb_act = _readbyte(pulses, s + size * 8, sp, lp, tp,
                               msb, tol)
            files.append(ScannedFile(
                loader="Turbo Tape 250",
                name=name or "DATA",
                load_addr=start, end_addr=end,
                data=bytes(out), read_errors=rd_err,
                checksum_ok=(cb_act == (cb & 0xFF)),
                pulse_start=sod, pulse_end=s + size * 8))
            pending = None
            i = s + size * 8
            continue
        else:
            i = sod + 8
    return files


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _clean_petscii(raw: bytes) -> str:
    """Turn a PETSCII filename into readable ASCII, dropping
    control codes (colour/cursor) the way the CBM name cleaner
    does."""
    chars = []
    for b in raw:
        if 32 <= b < 127:
            chars.append(chr(b))
        elif 0xC1 <= b <= 0xDA:
            chars.append(chr(b - 0x80))
    return "".join(chars).rstrip(" \x00").strip()


# ---------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------

# Map a detected loader short-name to its scanner function.
_SCANNERS = {
    "Turbo Tape 250": scan_turbotape,
}


def scan_for_loader(loader_short, pulses, tol=56):
    """Run the loader-specific scanner for `loader_short` if one
    exists, returning a list of ScannedFile (possibly empty). If
    no specific scanner is available, returns None so the caller
    can fall back to the generic turbo reader."""
    fn = _SCANNERS.get(loader_short)
    if fn is None:
        return None
    try:
        return fn(pulses, tol=tol)
    except Exception:
        return None
