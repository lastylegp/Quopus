# date_time: 2026-06-06 11:14
"""
cbmfiles - Commodore 8-bit disk image reader, viewer, and extractor.

Supports the formats Mario uses across his BBS / archive workflow:

    .d64  - 1541 single-sided 35-track image (174,848 bytes).
    .d71  - 1571 double-sided 70-track image (349,696 bytes).
    .d81  - 1581 80-track image (819,200 bytes).
    .d2m  - CMD FD-2000 native partition dump.
    .d4m  - CMD FD-4000 native partition dump.
    .dnp  - CMD HD/RAMLink native partition dump.
    .lnx  - Lynx archive (single file containing multiple PRGs).

The directory-parsing logic is ported from PYCGMS' tools.py so the
on-screen rendering matches what Mario sees in his terminal: PETSCII
header line with disk name + ID, file entries with block count + name
+ type, "BLOCKS FREE" footer, all painted in the C64 blue-on-white
look using the bundled C64 Pro Mono font.

On top of the read-only viewer this module adds extraction: each
directory entry remembers its starting Track/Sector, so we can follow
the on-disk sector chain and write the file content to the host
filesystem - either as a raw PRG/SEQ payload or in a small wrapper
format (PETSCII-named files keep their original case via a sanitised
ASCII fallback).

Quopus integration:
    is_cbm_disk(path)         -> True for any of the extensions above.
    CbmDiskDialog(path, parent) -> modal viewer with extract buttons.
    Lister._auto_open() routes double-clicks here for these files.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import (QPixmap, QImage, QPainter, QColor, QFont,
                          QKeySequence, QShortcut, QIcon)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFileDialog, QMessageBox, QListWidget,
    QListWidgetItem, QAbstractItemView, QApplication, QWidget,
    QInputDialog, QGroupBox,
)


# =====================================================================
# Format detection
# =====================================================================
_CBM_DISK_EXTS = {
    '.d64', '.d71', '.d81',
    '.d2m', '.d4m', '.dnp',
    '.lnx',
}


def is_cbm_disk(path) -> bool:
    """True if `path` has an extension this module can open."""
    try:
        return Path(path).suffix.lower() in _CBM_DISK_EXTS
    except Exception:
        return False


# =====================================================================
# Directory entry data model
# =====================================================================
@dataclass
class CbmDirEntry:
    """One file entry from a CBM disk image directory.

    The fields below are everything we need both for *display* (raw
    PETSCII filename, type label, block count) and for *extraction*
    (start track/sector, file-type code, locked flag).

    The dir_* fields locate the entry's slot inside the image so
    edits (rename, delete, insert separator) can write straight to
    the right 32-byte chunk without re-walking the chain. They get
    filled by _walk_dir_chain.
    """
    name_petscii: bytes        # raw 16-byte filename, PETSCII
    name_ascii: str            # best-effort ASCII for filesystem use
    type_code: int             # low nibble of file-type byte (0..4)
    type_label: str            # 'DEL' / 'SEQ' / 'PRG' / 'USR' / 'REL'
    blocks: int                # block count from directory
    start_track: int           # first data sector track (0 if N/A)
    start_sector: int          # first data sector
    locked: bool               # closed/locked flag (bit 6 of type byte)
    splat: bool = False        # splat (bit 7 cleared = unclosed file '*')
    raw_size_hint: int = 0     # bytes (when known precisely - LNX only)
    dir_track: int = 0         # directory sector track holding this entry
    dir_sector: int = 0        # directory sector number
    dir_slot: int = 0          # 0..7 slot within the dir sector


# =====================================================================
# Filename helpers
# =====================================================================
def _petscii_filename_to_ascii(raw: bytes) -> str:
    """Best-effort conversion of a PETSCII filename to a host-safe
    ASCII string. We:
      - trim trailing $A0 (shifted-space padding) and trailing 0x00
      - lowercase A-Z (PETSCII upper $41-$5A) so the file matches the
        "looks like the C64 directory" shown to the user
      - drop any character that's neither alphanumeric, space, '.',
        '-', or '_' (those would confuse most filesystems anyway)
      - collapse runs of spaces to a single underscore
    The result is never empty - if everything would be filtered out
    we fall back to the hex of the original bytes so the user can at
    least see *something* unique on disk.
    """
    # Trim padding bytes from the right end.
    end = len(raw)
    while end > 0 and raw[end - 1] in (0xA0, 0x00):
        end -= 1
    body = raw[:end]
    out = []
    for b in body:
        if 0x41 <= b <= 0x5A:        # PETSCII upper -> ascii lower
            out.append(chr(b + 0x20))
        elif 0x30 <= b <= 0x39:      # digits
            out.append(chr(b))
        elif b == 0x20:               # space
            out.append('_')
        elif b in (0x2D, 0x2E, 0x5F):  # - . _
            out.append(chr(b))
        elif 0x61 <= b <= 0x7A:      # already lowercase ASCII (rare)
            out.append(chr(b))
    name = ''.join(out).strip('_')
    if not name:
        name = 'file_' + body.hex()[:12]
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    return name


# =====================================================================
# D64 / D71 / D81 sector geometry
# =====================================================================
def _d64_sector_offset(track: int, sector: int) -> int:
    """Byte offset of a track/sector pair inside a 35-track .d64.
    Track numbering is 1-based. Sector counts per zone:
        tracks  1..17 : 21 sectors
        tracks 18..24 : 19 sectors
        tracks 25..30 : 18 sectors
        tracks 31..35 : 17 sectors
    """
    if track < 1 or track > 35:
        raise ValueError(f"D64 track out of range: {track}")
    off = 0
    for t in range(1, track):
        if t <= 17:
            off += 21 * 256
        elif t <= 24:
            off += 19 * 256
        elif t <= 30:
            off += 18 * 256
        else:
            off += 17 * 256
    off += sector * 256
    return off


def _d71_sector_offset(track: int, sector: int) -> int:
    """D71 = two D64 sides back to back. Track 1-35 = side 1, track
    36-70 = side 2, with the same per-zone sector counts as D64."""
    if track <= 35:
        return _d64_sector_offset(track, sector)
    side1_size = 174848
    return side1_size + _d64_sector_offset(track - 35, sector)


def _d81_sector_offset(track: int, sector: int) -> int:
    """D81 has uniform 40 sectors/track for 80 tracks."""
    if track < 1 or track > 80:
        raise ValueError(f"D81 track out of range: {track}")
    return ((track - 1) * 40 + sector) * 256


# Sector counts per track for D64 (used during chain-walking to bound
# valid sector numbers when the on-disk pointer is bogus).
def _d64_sectors_in_track(track: int) -> int:
    if track <= 17:   return 21
    elif track <= 24: return 19
    elif track <= 30: return 18
    else:             return 17


# =====================================================================
# CbmDiskReader: parses directory + extracts files
# =====================================================================
class CbmDiskReader:
    """Open a CBM disk image, parse its directory, and stream out
    individual files on demand.

    Usage:
        r = CbmDiskReader(path)
        r.open()
        for entry in r.entries:
            ...
        data = r.extract(entry)        # bytes of the file contents

    The reader keeps the image file handle open between calls so we
    don't re-read the same blocks repeatedly when the user extracts
    many files in a row. Call close() when done (or use as a context
    manager).
    """

    def __init__(self, path):
        self.path = Path(path)
        self.kind = self.path.suffix.lower().lstrip('.')
        self.disk_name_raw: bytes = b''  # 16 bytes PETSCII
        self.disk_id_raw: bytes = b''    # 5 bytes PETSCII
        self.blocks_free: int = 0
        self.entries: List[CbmDirEntry] = []
        # Pretty-printed PETSCII directory lines (header + entries +
        # "BLOCKS FREE." footer) - matches the look of tools.py'
        # DiskImageViewer output and is what the dialog renders.
        self.dir_lines: List[bytes] = []
        self._fh = None
        # LNX-only: in-memory copy of the entire archive plus parsed
        # entries. The chain-walker doesn't apply to LNX since it is
        # already linearised.
        self._lnx_buf: Optional[bytes] = None
        # When the reader is fed an in-memory byte buffer (from
        # lnx_to_d64_bytes() or similar), we treat it as a virtual
        # file: the disk-image chain walker uses io.BytesIO instead
        # of the real file handle, and the dialog can offer to
        # persist the bytes to disk via "Save D64...". `is_temp`
        # signals that case to the dialog.
        self._temp_bytes: Optional[bytes] = None
        self.is_temp: bool = False
        self.display_name: str = self.path.name
        # Stashed by _walk_dir_chain so mutation methods know which
        # offset_fn to use (D64/D71/D81 each have a different one)
        # and which sectors hold the directory chain (used by
        # validate's reachability check).
        self._offset_fn = None
        self._dir_chain: list = []

    @classmethod
    def from_bytes(cls, data: bytes, kind: str,
                    display_name: str = "(memory)") -> "CbmDiskReader":
        """Build a reader on top of an in-memory image. `kind` is
        one of 'd64'/'d71'/'d81' (no LNX - LNX comes pre-parsed).
        Used by the LNX-PRG -> D64 path: convert in memory, view
        without writing a temp file, optionally save later."""
        # Use a sentinel path so .path.name is meaningful for the
        # window title and display.
        r = cls.__new__(cls)
        r.path = Path(display_name)
        r.kind = kind.lower()
        r.disk_name_raw = b''
        r.disk_id_raw = b''
        r.blocks_free = 0
        r.entries = []
        r.dir_lines = []
        r._fh = None
        r._lnx_buf = None
        r._temp_bytes = data
        r.is_temp = True
        r.display_name = display_name
        r._offset_fn = None
        r._dir_chain = []
        return r

    # ---- lifecycle ----
    def __enter__(self):
        self.open(); return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        """Read the directory once, populate self.entries and
        self.dir_lines. Raises ValueError if the format is unknown
        or the image is too small / corrupt to parse."""
        # In-memory image (from from_bytes) - wrap a BytesIO and
        # delegate to the format-specific reader. We use BytesIO
        # so the existing _read_* methods work unmodified - they
        # only need .seek() and .read().
        if self._temp_bytes is not None:
            import io
            self._fh = io.BytesIO(self._temp_bytes)
            if self.kind == 'd64':         self._read_d64()
            elif self.kind == 'd71':       self._read_d71()
            elif self.kind == 'd81':       self._read_d81()
            else:
                raise ValueError(f"in-memory kind not supported: {self.kind}")
            return
        if self.kind in ('d64', 'd71', 'd81'):
            self._fh = open(self.path, 'rb')
            if self.kind == 'd64':
                self._read_d64()
            elif self.kind == 'd71':
                self._read_d71()
            else:
                self._read_d81()
        elif self.kind in ('d2m', 'd4m', 'dnp'):
            self._fh = open(self.path, 'rb')
            self._read_cmd_native()
        elif self.kind == 'lnx':
            self._lnx_buf = self.path.read_bytes()
            self._read_lnx()
        else:
            raise ValueError(f"unsupported extension: {self.path.suffix}")

    def close(self):
        if self._fh:
            try: self._fh.close()
            except Exception: pass
            self._fh = None

    # ---- D64 ----
    def _read_d64(self):
        """Parse 1541 directory at track 18, sector 0 onwards."""
        self._fh.seek(_d64_sector_offset(18, 0))
        bam = self._fh.read(256)
        self.disk_name_raw = bam[0x90:0x90 + 16]
        self.disk_id_raw = bam[0xA2:0xA7]
        # Free blocks: BAM 4-byte entries, byte 0 of each = free count.
        # Track 18 itself doesn't count (it's the directory track).
        free = 0
        for i in range(35):
            if i == 17: continue
            free += bam[4 + i * 4]
        self.blocks_free = free
        # Walk the directory chain starting at the BAM's link bytes.
        nt, ns = bam[0], bam[1]
        self._walk_dir_chain(nt, ns, _d64_sector_offset)
        self._build_dir_lines()

    # ---- D71 ----
    def _read_d71(self):
        self._fh.seek(_d71_sector_offset(18, 0))
        bam = self._fh.read(256)
        self.disk_name_raw = bam[0x90:0x90 + 16]
        self.disk_id_raw = bam[0xA2:0xA7]
        # Side 1 BAM: same layout as D64 (tracks 1-35 minus track 18).
        # Side 2 BAM: 35 single-byte free counts starting at 0xDD.
        free1 = sum(bam[4 + i * 4] for i in range(35) if i != 17)
        free2 = sum(bam[0xDD + i] for i in range(35) if i != 17)
        self.blocks_free = free1 + free2
        nt, ns = bam[0], bam[1]
        self._walk_dir_chain(nt, ns, _d71_sector_offset)
        self._build_dir_lines()

    # ---- D81 ----
    def _read_d81(self):
        # Header at track 40, sector 0; BAM at sectors 1+2; directory
        # starts at sector 3.
        self._fh.seek(_d81_sector_offset(40, 0))
        hdr = self._fh.read(256)
        self.disk_name_raw = hdr[0x04:0x04 + 16]
        self.disk_id_raw = hdr[0x16:0x1B]
        self._fh.seek(_d81_sector_offset(40, 1))
        bam1 = self._fh.read(256)
        self._fh.seek(_d81_sector_offset(40, 2))
        bam2 = self._fh.read(256)
        # 6 bytes per track entry, byte 0 = free sector count.
        # Tracks 1-40 in BAM1 (skip track 40 itself), 41-80 in BAM2.
        free = 0
        for i in range(40):
            if i == 39: continue
            o = 0x10 + i * 6
            if o < len(bam1): free += bam1[o]
        for i in range(40):
            o = 0x10 + i * 6
            if o < len(bam2): free += bam2[o]
        self.blocks_free = free
        # Directory chain at track 40, sector 3.
        self._walk_dir_chain(40, 3, _d81_sector_offset)
        self._build_dir_lines()

    # ---- D2M / D4M / DNP ----
    def _read_cmd_native(self):
        """CMD partition dumps don't have a fixed directory location,
        so we scan all sectors looking for ones that contain at least
        three plausibly-typed entries with valid PETSCII filenames.
        Same heuristic as tools.py' _read_cmd_native(). Track/sector
        for chain-walking IS preserved per entry so extraction works
        identically to the .d64 case."""
        self._fh.seek(0)
        data = self._fh.read()
        sector_size = 256
        total = len(data) // sector_size

        def get_sector(n):
            o = n * sector_size
            return data[o:o + sector_size] if o + sector_size <= len(data) else None

        def valid_fname(b):
            for x in b:
                if x in (0xA0, 0x00):       continue
                if 0x20 <= x <= 0x5F:        continue
                if 0xA1 <= x <= 0xBF:        continue
                if 0xC0 <= x <= 0xDF:        continue
                return False
            return True

        header = get_sector(1)
        if not header:
            raise ValueError("CMD image too small for a header sector")
        self.disk_name_raw = header[4:20]
        self.disk_id_raw = header[21:26]

        valid_types = {0x80, 0x81, 0x82, 0x83, 0x84,
                        0xC0, 0xC1, 0xC2, 0xC3, 0xC4}
        seen = set()
        for sec_num in range(total):
            sec = get_sector(sec_num)
            if not sec: continue
            candidates = []
            for i in range(8):
                e = sec[i * 32:(i + 1) * 32]
                if e[2] in valid_types and valid_fname(e[5:21]):
                    candidates.append(e)
            if len(candidates) < 3:
                continue
            for e in candidates:
                fn = bytes(e[5:21])
                if fn in seen: continue
                seen.add(fn)
                self.entries.append(self._make_entry(e))

        # Free-block estimate: count fully-zero sectors. Same fudge
        # as tools.py - a precise BAM walk would need a per-format
        # implementation.
        used = sum(1 for i in range(total)
                    if any(b != 0 for b in data[i * sector_size:(i + 1) * sector_size]))
        self.blocks_free = max(0, total - used)
        self._build_dir_lines()

    # ---- LNX ----
    def _read_lnx(self):
        """Lynx archive: a single file holding multiple PRGs back-to-
        back, with an ASCII header listing names/sizes/types. We use
        the same parser as tools.py' _lnx_parse but expose its result
        as CbmDirEntry so the rest of the pipeline (extract, render,
        dialog) doesn't have to special-case LNX."""
        try:
            dir_blocks, num_files, sig, lyx_entries = _lnx_parse(self._lnx_buf)
        except Exception as e:
            raise ValueError(f"LNX parse failed: {e}")
        # Use the volume header that tools.py would render. LNX has
        # no proper disk name, so we use the archive's filename.
        fake_name = self.path.stem.upper().encode('ascii', 'replace')[:16]
        self.disk_name_raw = fake_name + b'\x20' * (16 - len(fake_name))
        # Use the signature first 5 bytes as the "ID" so it shows up
        # in the header line.
        sig_bytes = sig.encode('ascii', 'replace')[:5]
        self.disk_id_raw = sig_bytes + b' ' * (5 - len(sig_bytes))
        # Translate Lynx entries to CbmDirEntry. We stash the data
        # offset + length in start_track/start_sector (overloaded -
        # the extractor checks self.kind to decide how to read).
        type_map = {'P': (2, 'PRG'), 'S': (1, 'SEQ'),
                     'U': (3, 'USR'), 'R': (4, 'REL'), 'D': (0, 'DEL')}
        for le in lyx_entries:
            tcode, tlabel = type_map.get(le.ftype, (0, 'DEL'))
            name_bytes = le.name.encode('ascii', 'replace')[:16]
            name_padded = name_bytes + b'\xa0' * (16 - len(name_bytes))
            self.entries.append(CbmDirEntry(
                name_petscii=name_padded,
                name_ascii=_petscii_filename_to_ascii(name_padded) or le.name,
                type_code=tcode,
                type_label=tlabel,
                blocks=le.blocks,
                # Reuse start_track/sector to carry the byte offset
                # + total length. Extractor branches on self.kind.
                start_track=le.data_offset,
                start_sector=le.total_bytes,
                locked=False,
                raw_size_hint=le.total_bytes,
            ))
        self.blocks_free = 0
        self._build_dir_lines()

    # ---- Common chain walker for D64/D71/D81 ----
    def _walk_dir_chain(self, track, sector, offset_fn):
        """Follow the directory chain starting at (track, sector).
        Each sector holds 8 directory entries of 32 bytes; the first
        2 bytes of the sector point to the next directory sector
        (track=0 -> end of chain).

        We also remember the offset_fn for the format so later
        edit/delete/insert ops can compute the right byte offsets
        without the dialog having to know whether it's d64/d71/d81.
        """
        # Stash for use by rename/delete/insert/validate later.
        self._offset_fn = offset_fn
        seen_sectors = set()
        steps = 0
        while track != 0 and steps < 4096:    # 4096 = generous cap
            if (track, sector) in seen_sectors:
                # Cycle in the chain - stop. Some homebrew images do
                # this on purpose to confuse rippers; we just bail.
                break
            seen_sectors.add((track, sector))
            try:
                self._fh.seek(offset_fn(track, sector))
            except ValueError:
                break
            data = self._fh.read(256)
            if len(data) < 256:
                break
            next_t, next_s = data[0], data[1]
            for i in range(8):
                e = data[i * 32:(i + 1) * 32]
                if e[2] != 0:    # type byte 0 = empty slot
                    entry = self._make_entry(e)
                    entry.dir_track = track
                    entry.dir_sector = sector
                    entry.dir_slot = i
                    self.entries.append(entry)
            track, sector = next_t, next_s
            steps += 1
        # Remember the directory chain as visited so validate has
        # something to compare against.
        self._dir_chain = list(seen_sectors)

    # ---- Per-entry struct -> CbmDirEntry ----
    def _make_entry(self, e: bytes) -> CbmDirEntry:
        """Convert a raw 32-byte directory entry into the high-level
        CbmDirEntry the rest of this module uses.

        Layout (offset within the 32-byte entry):
            0x02 : file type byte
                     bits 0-3 = type (0=DEL, 1=SEQ, 2=PRG, 3=USR, 4=REL)
                     bit  6   = closed (1) / open (0)
                     bit  7   = locked (1) on >= V2 file types
            0x03 : start track  of file data
            0x04 : start sector of file data
            0x05..0x14 : 16-byte PETSCII filename ($A0-padded)
            0x1E..0x1F : block count (low/high byte)
        """
        ftb = e[2]
        type_code = ftb & 0x0F
        # Lock is bit 6 - independent of the closed (splat) bit.
        # Earlier code required (ftb & 0xC0) == 0xC0 which made
        # 'locked' falsely report false on splat-set files. The
        # 1541 firmware itself only checks bit 6 for write-protect.
        locked = bool(ftb & 0x40)
        type_labels = {0: 'DEL', 1: 'SEQ', 2: 'PRG', 3: 'USR', 4: 'REL'}
        label = type_labels.get(type_code, '???')
        name_bytes = bytes(e[5:5 + 16])
        return CbmDirEntry(
            name_petscii=name_bytes,
            name_ascii=_petscii_filename_to_ascii(name_bytes),
            type_code=type_code,
            type_label=label,
            blocks=e[0x1E] + e[0x1F] * 256,
            start_track=e[3],
            start_sector=e[4],
            locked=locked,
            # Splat = closed bit (0x80) is CLEARED. Real-disk files
            # have closed=1, splatted ones (interrupted writes,
            # cracker '*PRG' tags) have closed=0.
            splat=not bool(ftb & 0x80),
        )

    # ---- Pretty-print directory the way tools.py does ----
    def _normalize_petscii_for_display(self, b: int) -> int:
        """Render-time fixup: shifted-space -> regular space; ASCII
        lowercase -> PETSCII upper. Preserves graphics chars."""
        if b == 0xA0:               return 0x20
        if 0x61 <= b <= 0x7A:        return b - 0x20
        return b

    def _format_header_line(self) -> bytes:
        out = bytearray()
        out.append(0x30)       # '0'
        while len(out) < 2: out.append(0x20)
        out.append(0x22)       # opening quote
        for b in self.disk_name_raw[:16]:
            out.append(self._normalize_petscii_for_display(b))
        out.append(0x22)       # closing quote
        out.append(0x20)
        for b in self.disk_id_raw[:5]:
            out.append(self._normalize_petscii_for_display(b))
        return bytes(out)

    def _format_entry_line(self, ent: CbmDirEntry) -> bytes:
        out = bytearray()
        for ch in str(ent.blocks):
            out.append(ord(ch))
        while len(out) < 5: out.append(0x20)
        out.append(0x22)
        for b in ent.name_petscii:
            # Inside filenames we keep $60-$7F graphics intact (typical
            # of "directory art" tunes) - only $A0 is collapsed to space.
            out.append(0x20 if b == 0xA0 else b)
        out.append(0x22)
        # Real-1541 listing: SP for normal, '*' for splat (unclosed)
        # immediately before the type label. Then optional '<' for
        # locked after the label.
        out.append(ord('*') if ent.splat else 0x20)
        for ch in ent.type_label:
            out.append(ord(ch))
        if ent.locked:
            out.append(ord('<'))
        return bytes(out)

    def _format_blocks_free(self) -> bytes:
        out = bytearray()
        for ch in str(self.blocks_free):
            out.append(ord(ch))
        out.append(0x20)
        for ch in 'BLOCKS FREE.':
            out.append(ord(ch))
        return bytes(out)

    # =========================================================
    # Image mutation: sector R/W + dir edit + BAM validate
    # =========================================================
    # All write operations go through _read_sector / _write_sector
    # so that in-memory images (LNX-converted, is_temp=True) and
    # on-disk images use the same path. After any mutation, the
    # caller must call self.refresh() so entries / dir_lines /
    # blocks_free reflect the new state.

    def _read_sector(self, track: int, sector: int) -> bytes:
        """Read one 256-byte sector. Works for both file-backed and
        in-memory (is_temp) readers."""
        if not self._offset_fn:
            raise RuntimeError(
                "no offset function - reader format unsupported "
                "for sector R/W")
        off = self._offset_fn(track, sector)
        if self._temp_bytes is not None:
            return bytes(self._temp_bytes[off:off + 256])
        # File-backed - seek and read.
        if self._fh is None:
            raise RuntimeError("reader not open")
        self._fh.seek(off)
        return self._fh.read(256)

    def _write_sector(self, track: int, sector: int, data: bytes):
        """Write 256 bytes back to the image. For in-memory readers
        we have to swap the immutable bytes for a mutable bytearray
        on the first write, then keep updating it. For file-backed
        readers we seek and write through the open file handle."""
        if not self._offset_fn:
            raise RuntimeError("no offset function set")
        if len(data) != 256:
            raise ValueError(
                f"sector write must be 256 bytes (got {len(data)})")
        off = self._offset_fn(track, sector)
        if self._temp_bytes is not None:
            # Promote to bytearray on first mutation so we can
            # splice in place. After this all reads still go via
            # the same path - bytes(bytearray) is a copy each time
            # but the volume is small (max ~800 KB for a D81) so
            # nobody will notice.
            if not isinstance(self._temp_bytes, bytearray):
                self._temp_bytes = bytearray(self._temp_bytes)
            self._temp_bytes[off:off + 256] = data
            return
        if self._fh is None:
            raise RuntimeError("reader not open")
        # On-disk: file must be opened r+b for writing. The default
        # open() above opens 'rb' - we re-open here on the first
        # write attempt so we don't pay the cost on read-only use.
        if self._fh.mode != 'r+b':
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = open(self.path, 'r+b')
        self._fh.seek(off)
        self._fh.write(data)
        self._fh.flush()

    def refresh(self):
        """Re-parse the directory after a mutation. Resets entries +
        dir_lines + blocks_free + disk_name fields. Use this after
        any rename / delete / insert / validate-with-fix call.

        For in-memory images we have to rebuild the BytesIO wrapper
        because BytesIO took a copy of the original bytes, and
        subsequent _write_sector calls only update self._temp_bytes
        (which IS the live mutable bytearray after promotion). If
        we don't rebuild, refresh re-reads stale bytes and the user
        sees the OLD name / OLD slot.

        For file-backed images the same rebuild keeps things tidy:
        flush + rewind so the next read picks up our writes even
        with non-sync-safe filesystems.
        """
        old_offset = self._offset_fn
        # Rebuild the BytesIO so it reflects current bytes.
        if self._temp_bytes is not None:
            import io
            self._fh = io.BytesIO(bytes(self._temp_bytes))
        elif self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass
            self._fh.seek(0)
        self.entries = []
        self.dir_lines = []
        if self.kind == 'd64':
            self._read_d64()
        elif self.kind == 'd71':
            self._read_d71()
        elif self.kind == 'd81':
            self._read_d81()
        else:
            raise RuntimeError(
                f"refresh unsupported for kind {self.kind!r}")
        # Restore offset_fn in case the format-specific reader
        # didn't (it does, but defensive doesn't hurt).
        if not self._offset_fn:
            self._offset_fn = old_offset

    # ---- Rename ----
    def rename_entry(self, entry: CbmDirEntry, new_name_petscii: bytes):
        """Change the 16-byte PETSCII filename of an entry in place.

        The new name is right-padded with $A0 (shifted-space) which
        is the canonical CBM padding char and matches what the 1541
        firmware writes. Names longer than 16 bytes are truncated.
        """
        # Pad/truncate to exactly 16 bytes.
        name = bytes(new_name_petscii[:16])
        name = name + b'\xA0' * (16 - len(name))
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        sec[slot_off + 5:slot_off + 5 + 16] = name
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    # ---- File type / lock byte ----
    #
    # The type byte at offset 0x02 packs three things into one octet.
    # The exact bit mapping in this codebase mirrors what _make_entry
    # already extracts:
    #     bits 0-3   : file type    (0=DEL, 1=SEQ, 2=PRG, 3=USR, 4=REL)
    #     bit  6     : locked flag  (1 = locked '<')
    #     bit  7     : closed flag  (1 = closed, 0 = splat '*')
    #
    # 1541 firmware writes (0x80 | type) for a normally-closed file:
    # PRG = 0x82, locked PRG = 0xC2, splat PRG = 0x02 (loading it
    # crashes the C64 because the save was never finalised).
    #
    # Cracker-tagged disks frequently use splat entries with type DEL
    # to make specific entries unloadable but still visible.

    TYPE_NAMES = ('DEL', 'SEQ', 'PRG', 'USR', 'REL')

    def change_file_type(self, entry: CbmDirEntry, new_type: int):
        """Set the low nibble (file type 0..4) of an entry.

        Preserves bits 4-7 (closed/locked flags). Caller passes
        0 for DEL, 1 for SEQ, 2 for PRG, 3 for USR, 4 for REL.
        """
        if not 0 <= new_type <= 4:
            raise ValueError(
                f"file type must be 0..4, got {new_type}")
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old = sec[slot_off + 2]
        sec[slot_off + 2] = (old & 0xF0) | (new_type & 0x0F)
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    # Older callsite alias - some UI code uses set_file_type, the
    # cleaner name is change_file_type. Keep both pointing at the
    # same logic so we don't have to update every caller.
    set_file_type = change_file_type

    def set_locked(self, entry: CbmDirEntry, locked: bool):
        """Toggle the locked flag (bit 6) of the type byte. Locked
        files render with '<' after their type label in the
        directory; on a real 1541 they reject SCRATCH commands."""
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old = sec[slot_off + 2]
        if locked:
            sec[slot_off + 2] = old | 0x40
        else:
            sec[slot_off + 2] = old & ~0x40
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    def set_splat(self, entry: CbmDirEntry, splat: bool):
        """Set / clear the splat marker (bit 7 of type byte). splat=
        True clears the closed-flag, marking the file as never-fully-
        written. Real-1541 listings show '*' before the type label
        and the file fails to load. Used as a decoration on cracker
        disks to mark inactive / placeholder entries."""
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old = sec[slot_off + 2]
        if splat:
            sec[slot_off + 2] = old & ~0x80    # clear closed -> splat
        else:
            sec[slot_off + 2] = old | 0x80     # set closed -> normal
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    def set_disk_name(self, new_name_petscii: bytes,
                        new_id_petscii: bytes = None):
        """Change the disk name (16 bytes) and optionally the disk
        ID (5 bytes). Both are PETSCII, padded with $A0.

        Disk name lives at:
          D64/D71  : track 18 sector 0, offset 0x90..0x9F
          D81      : track 40 sector 0, offset 0x04..0x13
        """
        name = bytes(new_name_petscii[:16])
        name = name + b'\xA0' * (16 - len(name))
        if self.kind in ('d64', 'd71'):
            bam = bytearray(self._read_sector(18, 0))
            bam[0x90:0x90 + 16] = name
            if new_id_petscii is not None:
                pid = bytes(new_id_petscii[:5])
                pid = pid + b'\xA0' * (5 - len(pid))
                bam[0xA2:0xA2 + 5] = pid
            self._write_sector(18, 0, bytes(bam))
        elif self.kind == 'd81':
            hdr = bytearray(self._read_sector(40, 0))
            hdr[0x04:0x04 + 16] = name
            if new_id_petscii is not None:
                pid = bytes(new_id_petscii[:5])
                pid = pid + b'\xA0' * (5 - len(pid))
                hdr[0x16:0x16 + 5] = pid
            self._write_sector(40, 0, bytes(hdr))
        else:
            raise RuntimeError(
                f"set_disk_name unsupported for {self.kind!r}")

    # ---- Block count "lies" ----
    #
    # The 16-bit block count at offsets 0x1E..0x1F of a directory
    # entry is what the C64 displays in the directory listing. The
    # firmware fills it in based on the actual sector chain length
    # when the file is closed, but nothing on the C64 side cross-
    # checks it later. So we can lie about it.
    #
    # Same with the BAM's per-track free counts: these get summed up
    # to produce "BLOCKS FREE." in the listing. The actual bitmap is
    # the source of truth (the firmware ignores the sum and walks
    # the bits when looking for a free sector). So we can lie about
    # the displayed total too without breaking allocation.
    #
    # These two lies are popular on cracker / demo disks for visual
    # effect ("664 BLOCKS FREE" on a half-full disk is funnier when
    # the real value would be 312, etc.). We support them as direct
    # writes to the appropriate bytes; a "validate" pass later would
    # normalize them away which is exactly what scene users want.

    def set_block_count(self, entry: CbmDirEntry, blocks: int):
        """Overwrite the displayed block count of one entry. The
        actual sector chain is unaffected - this only changes the
        number shown in the listing.

        Range is 0..65535 (16-bit). Values >999 won't display
        correctly on the C64 (the listing routine assumes 3-digit
        decimal) but the bytes go in fine.
        """
        if not 0 <= blocks <= 0xFFFF:
            raise ValueError(
                f"block count must fit in 16 bits, got {blocks}")
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        sec[slot_off + 0x1E] = blocks & 0xFF
        sec[slot_off + 0x1F] = (blocks >> 8) & 0xFF
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    # ---- Delete ----
    def delete_entry(self, entry: CbmDirEntry):
        """Scratch (delete) a file from the disk image.

        Three steps, mirroring what the 1541 firmware does on a
        SCRATCH command:
          1) Walk the file's data sector chain and free those blocks
             in the BAM (clear bitmap bit + bump per-track free count)
          2) Zero out the entry's type byte (offset 2) - same as a
             full slot wipe but minimum-invasive; some tools rely on
             the rest of the entry being intact for "undelete"
          3) Recompute the global blocks_free count after refresh()

        SEQ/USR/PRG files use the linked-sector chain (next_t, next_s
        in the first two bytes of each data sector). REL files have
        a side-sector structure on top - we still walk the main
        chain here, side sectors leak but it's better than refusing
        to delete REL files entirely.
        """
        # Free the data sectors first so partial-failure case doesn't
        # leave a wholly orphaned file.
        if entry.start_track:
            self._free_chain(entry.start_track, entry.start_sector)
        # Zero out type byte to mark the slot deleted.
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        sec[slot_off + 2] = 0
        self._write_sector(entry.dir_track, entry.dir_sector, bytes(sec))

    def _free_chain(self, track: int, sector: int, max_steps: int = 4096):
        """Walk a sector chain and mark each visited sector as free
        in the BAM. Stops cleanly on cycles. Used by delete_entry."""
        seen = set()
        t, s = track, sector
        steps = 0
        while t != 0 and steps < max_steps:
            if (t, s) in seen:
                break
            seen.add((t, s))
            try:
                data = self._read_sector(t, s)
            except (ValueError, RuntimeError):
                break
            if len(data) < 2:
                break
            nt, ns = data[0], data[1]
            self._bam_free(t, s)
            t, s = nt, ns
            steps += 1

    def _bam_free(self, track: int, sector: int):
        """Mark a single sector as free in the BAM. Format-specific:
        D64/D71/D81 each have their own BAM layout."""
        if self.kind == 'd64':
            self._bam_free_d64(track, sector)
        elif self.kind == 'd71':
            self._bam_free_d71(track, sector)
        elif self.kind == 'd81':
            self._bam_free_d81(track, sector)
        # Other formats: leave BAM untouched (we still wipe the dir
        # slot, the file just stays "allocated" in the BAM until a
        # proper validate fixes it).

    def _bam_free_d64(self, track: int, sector: int):
        """1541 BAM at track 18 sector 0. Each track entry is 4 bytes
        starting at 0x04: byte 0 = free count, bytes 1-3 = bitmap
        (LSB first, so sector 0 = bit 0 of byte 1).
        Setting a sector bit to 1 = free. We also bump the count if
        the bit was 0 before, otherwise we'd double-count."""
        if track < 1 or track > 35:
            return
        bam = bytearray(self._read_sector(18, 0))
        entry = 0x04 + (track - 1) * 4
        bit = sector
        byte_off = entry + 1 + (bit // 8)
        bit_mask = 1 << (bit % 8)
        if not (bam[byte_off] & bit_mask):
            bam[byte_off] |= bit_mask
            bam[entry] = (bam[entry] + 1) & 0xFF
            self._write_sector(18, 0, bytes(bam))

    def _bam_free_d71(self, track: int, sector: int):
        """1571 BAM is split: side 1 (tracks 1-35) lives in the same
        track-18 sector-0 layout as a D64; side 2 (tracks 36-70) has
        its free counts at 0xDD..0xFF in the SAME sector and the
        bitmap proper in track 53 sector 0 at 0x00..0x8B (3 bytes per
        track entry, no count byte - the count is in the side-1 BAM).
        """
        if track < 1 or track > 70:
            return
        if track <= 35:
            # Use D64 path on track-18 sector-0.
            self._bam_free_d64(track, sector)
            return
        # Side 2: free count in track-18 sector-0 at 0xDD + (track-36)
        bam = bytearray(self._read_sector(18, 0))
        cnt_off = 0xDD + (track - 36)
        # Bitmap is in track 53 sector 0 at 0x00 + (track-36)*3
        side2 = bytearray(self._read_sector(53, 0))
        bmp_off = (track - 36) * 3
        bit = sector
        byte_off = bmp_off + (bit // 8)
        bit_mask = 1 << (bit % 8)
        if byte_off < len(side2) and not (side2[byte_off] & bit_mask):
            side2[byte_off] |= bit_mask
            self._write_sector(53, 0, bytes(side2))
            bam[cnt_off] = (bam[cnt_off] + 1) & 0xFF
            self._write_sector(18, 0, bytes(bam))

    def _bam_free_d81(self, track: int, sector: int):
        """1581 BAM lives at track 40 sectors 1+2. Each track entry
        is 6 bytes starting at 0x10: byte 0 = free count, bytes 1-5 =
        bitmap. Tracks 1-40 in sector 1, tracks 41-80 in sector 2."""
        if track < 1 or track > 80:
            return
        bam_t, bam_s = 40, (1 if track <= 40 else 2)
        bam = bytearray(self._read_sector(bam_t, bam_s))
        rel = track if bam_s == 1 else (track - 40)
        entry = 0x10 + (rel - 1) * 6
        bit = sector
        byte_off = entry + 1 + (bit // 8)
        bit_mask = 1 << (bit % 8)
        if byte_off < len(bam) and not (bam[byte_off] & bit_mask):
            bam[byte_off] |= bit_mask
            bam[entry] = (bam[entry] + 1) & 0xFF
            self._write_sector(bam_t, bam_s, bytes(bam))

    # ---- Per-entry attribute setters ----
    def set_file_type(self, entry: CbmDirEntry, new_type: int):
        """Change the file-type code (low nibble of byte 2) of an
        entry without touching anything else.

        new_type accepts 0..4:
          0 = DEL  1 = SEQ  2 = PRG  3 = USR  4 = REL

        We preserve the closed bit (0x80) and locked bit (0x40)
        from the existing byte so flipping the type doesn't also
        unlock the file or mark it open.
        """
        if not (0 <= new_type <= 4):
            raise ValueError(
                f"file type must be 0..4 (got {new_type})")
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old_byte = sec[slot_off + 2]
        # Replace low nibble; keep high nibble (closed/locked bits).
        sec[slot_off + 2] = (old_byte & 0xF0) | (new_type & 0x0F)
        self._write_sector(entry.dir_track, entry.dir_sector,
                            bytes(sec))

    def set_locked(self, entry: CbmDirEntry, locked: bool):
        """Set or clear the locked flag. The lock bit is bit 6 of
        the file-type byte; on a 1541 it shows as a '<' next to the
        type label (SAVE@-write-protected).

        Some references call this 'closed' but on a real disk the
        closed-bit (0x80) is always set for a properly written
        file - it's the lock-bit (0x40) that the user typically
        flips with the LOCK command.
        """
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old = sec[slot_off + 2]
        if locked:
            sec[slot_off + 2] = old | 0x40
        else:
            sec[slot_off + 2] = old & ~0x40
        self._write_sector(entry.dir_track, entry.dir_sector,
                            bytes(sec))

    def set_splat(self, entry: CbmDirEntry, splat: bool):
        """Set or clear the splat ('*') flag, which is the inverted
        closed-bit. A file is shown with a '*' next to its type if
        the closed bit (0x80) is CLEARED - signals an unclosed /
        truncated file (typical when a write was aborted, e.g.
        power loss during SAVE).

        splat=True  -> clear bit 7 -> '*' shown in directory
        splat=False -> set   bit 7 -> normal closed file
        """
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        old = sec[slot_off + 2]
        if splat:
            sec[slot_off + 2] = old & ~0x80
        else:
            sec[slot_off + 2] = old | 0x80
        self._write_sector(entry.dir_track, entry.dir_sector,
                            bytes(sec))

    def set_block_count(self, entry: CbmDirEntry, blocks: int):
        """Override the block count shown for an entry. Pure
        cosmetic edit - bytes 0x1E/0x1F of the slot, low/high. Does
        NOT touch the BAM or recompute anything; just changes what
        the directory listing displays.

        Useful for cleaning up "weird block counts" on cracked
        intros or for matching a known cracker's 'fake' block
        numbering.
        """
        if not (0 <= blocks <= 65535):
            raise ValueError("blocks must fit in 16 bits (0..65535)")
        sec = bytearray(self._read_sector(
            entry.dir_track, entry.dir_sector))
        slot_off = entry.dir_slot * 32
        sec[slot_off + 0x1E] = blocks & 0xFF
        sec[slot_off + 0x1F] = (blocks >> 8) & 0xFF
        self._write_sector(entry.dir_track, entry.dir_sector,
                            bytes(sec))

    def set_disk_blocks_free(self, blocks_free: int):
        """Override the disk-wide blocks-free count without
        actually freeing or allocating any sectors.

        On a real 1541 the count shown in 'N BLOCKS FREE.' is the
        sum of the per-track free counters in the BAM. We do the
        cosmetic equivalent: scale every per-track free-count
        proportionally so the visible total matches `blocks_free`,
        WITHOUT touching the actual bitmap bits. Reading a file's
        chain still returns the same data; it's purely the BAM
        free-count fields that change.

        The result is the same 'fake disk-full' look as on cracker
        intros that report '0 BLOCKS FREE.' or '666 BLOCKS FREE.'
        even though the sectors are physically present.

        Format-specific: D64 has one BAM, D71 has split free
        counts (side 1 in track 18 sector 0, side 2 starting at
        0xDD in same sector), D81 has 6-byte entries split across
        two sectors.
        """
        if blocks_free < 0:
            raise ValueError("blocks_free must be >= 0")
        if self.kind == 'd64':
            self._set_blocks_free_d64(blocks_free)
        elif self.kind == 'd71':
            self._set_blocks_free_d71(blocks_free)
        elif self.kind == 'd81':
            self._set_blocks_free_d81(blocks_free)
        else:
            raise RuntimeError(
                f"set_disk_blocks_free unsupported for {self.kind!r}")

    def _set_blocks_free_d64(self, target: int):
        """Distribute `target` blocks across the per-track free
        counts. Track 18 stays 0 (it's the dir track, conventionally
        all-allocated).

        We do NOT clamp per-track to its physical sector count -
        the on-disk counter is just a byte at the BAM entry, and
        crackers routinely set it to absurd values like 666 to
        signal a custom format. The user's target is honoured
        verbatim up to the byte limit (255 per track entry, ~8000
        total).
        """
        if target > 255 * 34:
            target = 255 * 34
        bam = bytearray(self._read_sector(18, 0))
        usable_tracks = [t for t in range(1, 36) if t != 18]
        per = target // len(usable_tracks)
        rem = target - per * len(usable_tracks)
        for i, t in enumerate(usable_tracks):
            slot = per + (1 if i < rem else 0)
            slot = min(slot, 255)    # one-byte field
            entry = 0x04 + (t - 1) * 4
            bam[entry] = slot & 0xFF
        self._write_sector(18, 0, bytes(bam))

    def _set_blocks_free_d71(self, target: int):
        """Same idea as D64 but split across both sides (1571 BAM
        keeps side-1 free counts in track 18 sector 0 main BAM
        bytes, side-2 free counts at offsets 0xDD..0xFF in same
        sector). No realism cap - the per-track byte is what the
        directory listing sums, and crackers use that for fake
        values."""
        bam = bytearray(self._read_sector(18, 0))
        side1_tracks = [t for t in range(1, 36) if t != 18]
        side2_tracks = [t for t in range(36, 71) if t != 53]
        all_tracks = side1_tracks + side2_tracks
        target = min(target, 255 * len(all_tracks))
        per = target // len(all_tracks)
        rem = target - per * len(all_tracks)
        # Side 1 tracks
        for i, t in enumerate(side1_tracks):
            slot = per + (1 if i < rem else 0)
            slot = min(slot, 255)
            entry = 0x04 + (t - 1) * 4
            bam[entry] = slot & 0xFF
        # Side 2 tracks (offset relative to remaining rem)
        side1_used = min(rem, len(side1_tracks))
        for i, t in enumerate(side2_tracks):
            slot = per + (
                1 if (side1_used + i) < rem else 0)
            slot = min(slot, 255)
            cnt_off = 0xDD + (t - 36)
            bam[cnt_off] = slot & 0xFF
        self._write_sector(18, 0, bytes(bam))

    def _set_blocks_free_d81(self, target: int):
        """1581 BAM: 6 bytes per track (byte 0 = free count).
        Tracks 1-40 in BAM sector 1, 41-80 in sector 2. Skip
        track 40 (system track). No realism cap (see D64 note)."""
        bam1 = bytearray(self._read_sector(40, 1))
        bam2 = bytearray(self._read_sector(40, 2))
        usable = [t for t in range(1, 81) if t != 40]
        target = min(target, 255 * len(usable))
        per = target // len(usable)
        rem = target - per * len(usable)
        for i, t in enumerate(usable):
            slot = per + (1 if i < rem else 0)
            slot = min(slot, 255)
            if t <= 40:
                entry = 0x10 + (t - 1) * 6
                bam1[entry] = slot & 0xFF
            else:
                entry = 0x10 + (t - 41) * 6
                bam2[entry] = slot & 0xFF
        self._write_sector(40, 1, bytes(bam1))
        self._write_sector(40, 2, bytes(bam2))

    # ---- Insert separator ----
    def insert_separator_raw(self, name_petscii: bytes,
                                file_type: int = 0,
                                closed: bool = True):
        """Same as insert_separator() but takes raw PETSCII bytes
        for the filename (16 bytes, padded with $A0). Used by the
        graphics-aware separator editor that builds the byte string
        directly from the picker UI - we don't want it round-
        tripping through ASCII because that would destroy the
        graphics characters.
        """
        if self.kind not in ('d64', 'd71', 'd81'):
            raise RuntimeError(
                f"insert_separator unsupported for {self.kind!r}")
        target_track, target_sector, target_slot = (
            self._find_free_dir_slot())
        if target_track == 0:
            raise RuntimeError(
                "directory is full - cannot insert separator")
        ent = bytearray(32)
        ent[0] = 0
        ent[1] = 0xFF
        # File type byte: closed bit + low nibble.
        type_byte = (file_type & 0x0F)
        if closed:
            type_byte |= 0x80
        ent[2] = type_byte
        ent[3] = 0
        ent[4] = 0
        # Pad/truncate name_petscii to 16 bytes with $A0.
        name = bytes(name_petscii[:16])
        name = name + b'\xA0' * (16 - len(name))
        ent[5:5 + 16] = name
        sec = bytearray(self._read_sector(target_track, target_sector))
        slot_off = target_slot * 32
        if target_slot == 0:
            ent[0] = sec[0]
            ent[1] = sec[1]
        sec[slot_off:slot_off + 32] = ent
        self._write_sector(target_track, target_sector, bytes(sec))

    def insert_separator(self, label: str = "----------------",
                            after_entry: Optional[CbmDirEntry] = None):
        """Add a 'fake' DEL-type directory entry with a label like
        '----------------' or '== SECTION ==' to act as a visual
        separator in the directory listing.

        The entry uses file type 0 (DEL) with the closed bit set
        (0x80) so it shows as 'DEL<' in the listing - that's the
        idiom most cracker intros / collections use. Block count is
        0, start track/sector both 0 (no data behind it).

        We have to find a free slot first. If the existing chain has
        room (any 32-byte slot with type byte == 0 in a current dir
        sector), use it. Otherwise extend the chain by allocating a
        fresh dir sector via the BAM - more involved, so for now if
        no free slot exists we raise. (Practical .d64 dir holds 144
        entries; 99% of tagged disks don't fill that.)
        """
        if self.kind not in ('d64', 'd71', 'd81'):
            raise RuntimeError(
                f"insert_separator unsupported for {self.kind!r}")
        # Find first free slot in the existing dir chain.
        target_track, target_sector, target_slot = self._find_free_dir_slot()
        if target_track == 0:
            raise RuntimeError(
                "directory is full - cannot insert separator")
        # Build the 32-byte entry.
        # File type byte: 0x80 | 0x00 = closed, type DEL = 0x80.
        # That renders as "DEL<" in PETSCII listings.
        ent = bytearray(32)
        ent[0] = 0       # next-entry track (only used at slot 0)
        ent[1] = 0xFF    # next-entry sector (slot 0 marker)
        ent[2] = 0x80    # file type: closed DEL
        ent[3] = 0       # start track
        ent[4] = 0       # start sector
        # Filename: PETSCII-uppercase the ASCII label, pad to 16
        # bytes with $A0 (CBM space). Truncate at 16. We don't
        # reverse-video here - that's a render-time decision and
        # depends on the PETSCII-control char $12. The user can
        # use a label like "  ===== HEAD =====" or all dashes;
        # both look right.
        name = self._ascii_to_petscii_filename(label)
        ent[5:5 + 16] = name
        # REL fields (0x15..0x17) and reserved bytes left zero.
        # Block count low/high (0x1E..0x1F) = 0 - that's what tools
        # like DirMaster show for separator entries.
        # Splice it into the dir sector.
        sec = bytearray(self._read_sector(target_track, target_sector))
        slot_off = target_slot * 32
        # Preserve the chain link bytes if this is slot 0.
        if target_slot == 0:
            ent[0] = sec[0]
            ent[1] = sec[1]
        sec[slot_off:slot_off + 32] = ent
        self._write_sector(target_track, target_sector, bytes(sec))

    def _find_free_dir_slot(self):
        """Return (track, sector, slot) for the first 32-byte slot
        in the dir chain whose type byte is 0 (= empty). Returns
        (0, 0, 0) if no free slot is available without extending
        the chain."""
        chain = self._dir_chain or []
        for (t, s) in chain:
            data = self._read_sector(t, s)
            for slot in range(8):
                slot_off = slot * 32
                if data[slot_off + 2] == 0:
                    return t, s, slot
        return 0, 0, 0

    @staticmethod
    def _ascii_to_petscii_filename(s: str) -> bytes:
        """Convert an ASCII label to a 16-byte PETSCII filename
        padded with $A0. Lowercase ASCII -> PETSCII upper; printable
        ASCII passes through; everything else becomes a space."""
        out = bytearray()
        for ch in s[:16]:
            o = ord(ch)
            if 0x61 <= o <= 0x7A:        # a-z -> A-Z (PETSCII upper)
                out.append(o - 0x20)
            elif 0x41 <= o <= 0x5A:      # A-Z -> shifted A-Z (PETSCII lower row)
                out.append(o + 0x80)
            elif 0x20 <= o <= 0x60 or 0x7B <= o <= 0x7E:
                out.append(o)
            else:
                out.append(0x20)
        # Pad with $A0 to 16.
        while len(out) < 16:
            out.append(0xA0)
        return bytes(out)

    # ---- Validate ----
    def validate(self, fix: bool = False) -> dict:
        """Walk every directory entry, follow each file's sector
        chain, and check that every reachable sector is marked as
        ALLOCATED in the BAM. Also flags the inverse: any sector
        marked allocated but not reachable from the directory.

        Returns a dict with:
          'unallocated': [(track, sector), ...]   (data sectors not in BAM)
          'orphaned':    [(track, sector), ...]   (BAM-allocated but not in dir)
          'fixed':       int    # number of BAM bits flipped (when fix=True)
          'errors':      [str, ...]    # walking errors

        With fix=False this is read-only - just the report. With
        fix=True we mark the unallocated sectors as allocated AND
        the orphaned ones as free, mirroring 1541-DOS VALIDATE.
        """
        report = {
            'unallocated': [],
            'orphaned': [],
            'fixed': 0,
            'errors': [],
        }
        if self.kind not in ('d64', 'd71', 'd81'):
            report['errors'].append(
                f"validate unsupported for {self.kind!r}")
            return report

        # Set of sectors we expect to be allocated: dir chain itself
        # plus every file's data chain. The BAM's own sector (track
        # 18 / 40 plus side 2 for D71) is implicitly allocated and
        # not represented in any free count, so we add it manually.
        reachable = set()
        for (t, s) in self._dir_chain or []:
            reachable.add((t, s))
        # BAM/header sectors per format
        if self.kind == 'd64':
            reachable.add((18, 0))
        elif self.kind == 'd71':
            reachable.add((18, 0))
            reachable.add((53, 0))
        elif self.kind == 'd81':
            reachable.add((40, 0))
            reachable.add((40, 1))
            reachable.add((40, 2))
        # Walk each entry's chain
        for ent in self.entries:
            if ent.start_track == 0:
                continue
            t, s = ent.start_track, ent.start_sector
            seen = set()
            steps = 0
            while t != 0 and steps < 4096:
                if (t, s) in seen:
                    report['errors'].append(
                        f"chain loop in {ent.name_ascii}")
                    break
                seen.add((t, s))
                reachable.add((t, s))
                try:
                    data = self._read_sector(t, s)
                except Exception as e:
                    report['errors'].append(
                        f"{ent.name_ascii}: read fail at "
                        f"{t}/{s}: {e}")
                    break
                t, s = data[0], data[1]
                steps += 1

        # Now walk the BAM and find allocated-but-not-reachable.
        all_sectors = self._all_disk_sectors()
        for (t, s) in all_sectors:
            allocated = self._bam_is_allocated(t, s)
            in_dir = (t, s) in reachable
            if in_dir and not allocated:
                # File chain visits a sector the BAM thinks is free.
                report['unallocated'].append((t, s))
                if fix:
                    self._bam_alloc(t, s)
                    report['fixed'] += 1
            elif allocated and not in_dir:
                # BAM thinks it's used but no file references it.
                report['orphaned'].append((t, s))
                if fix:
                    self._bam_free(t, s)
                    report['fixed'] += 1
        return report

    def _all_disk_sectors(self):
        """Generate (track, sector) for every sector on the image."""
        if self.kind == 'd64':
            for t in range(1, 36):
                for s in range(_d64_sectors_in_track(t)):
                    yield (t, s)
        elif self.kind == 'd71':
            for t in range(1, 36):
                for s in range(_d64_sectors_in_track(t)):
                    yield (t, s)
            for t in range(36, 71):
                # D71 uses the same per-track sector counts mirrored
                # for tracks 36..70.
                for s in range(_d64_sectors_in_track(t - 35)):
                    yield (t, s)
        elif self.kind == 'd81':
            for t in range(1, 81):
                for s in range(40):
                    yield (t, s)

    def _bam_is_allocated(self, track: int, sector: int) -> bool:
        """Read one bit from the BAM. True = allocated."""
        if self.kind == 'd64':
            if track < 1 or track > 35:
                return True
            bam = self._read_sector(18, 0)
            entry = 0x04 + (track - 1) * 4
            byte_off = entry + 1 + (sector // 8)
            bit_mask = 1 << (sector % 8)
            return not bool(bam[byte_off] & bit_mask)
        if self.kind == 'd71':
            if track <= 35:
                bam = self._read_sector(18, 0)
                entry = 0x04 + (track - 1) * 4
                byte_off = entry + 1 + (sector // 8)
                bit_mask = 1 << (sector % 8)
                return not bool(bam[byte_off] & bit_mask)
            side2 = self._read_sector(53, 0)
            bmp_off = (track - 36) * 3 + (sector // 8)
            bit_mask = 1 << (sector % 8)
            if bmp_off >= len(side2):
                return True
            return not bool(side2[bmp_off] & bit_mask)
        if self.kind == 'd81':
            bam_s = 1 if track <= 40 else 2
            bam = self._read_sector(40, bam_s)
            rel = track if bam_s == 1 else (track - 40)
            entry = 0x10 + (rel - 1) * 6
            byte_off = entry + 1 + (sector // 8)
            bit_mask = 1 << (sector % 8)
            if byte_off >= len(bam):
                return True
            return not bool(bam[byte_off] & bit_mask)
        return True

    def _bam_alloc(self, track: int, sector: int):
        """Mark a sector as allocated in the BAM (inverse of _bam_free)."""
        if self.kind == 'd64' and 1 <= track <= 35:
            bam = bytearray(self._read_sector(18, 0))
            entry = 0x04 + (track - 1) * 4
            byte_off = entry + 1 + (sector // 8)
            bit_mask = 1 << (sector % 8)
            if bam[byte_off] & bit_mask:
                bam[byte_off] &= ~bit_mask
                if bam[entry]:
                    bam[entry] -= 1
                self._write_sector(18, 0, bytes(bam))
        elif self.kind == 'd71':
            if track <= 35:
                bam = bytearray(self._read_sector(18, 0))
                entry = 0x04 + (track - 1) * 4
                byte_off = entry + 1 + (sector // 8)
                bit_mask = 1 << (sector % 8)
                if bam[byte_off] & bit_mask:
                    bam[byte_off] &= ~bit_mask
                    if bam[entry]:
                        bam[entry] -= 1
                    self._write_sector(18, 0, bytes(bam))
            else:
                bam = bytearray(self._read_sector(18, 0))
                cnt_off = 0xDD + (track - 36)
                side2 = bytearray(self._read_sector(53, 0))
                bmp_off = (track - 36) * 3 + (sector // 8)
                bit_mask = 1 << (sector % 8)
                if (bmp_off < len(side2)
                        and (side2[bmp_off] & bit_mask)):
                    side2[bmp_off] &= ~bit_mask
                    self._write_sector(53, 0, bytes(side2))
                    if bam[cnt_off]:
                        bam[cnt_off] -= 1
                    self._write_sector(18, 0, bytes(bam))
        elif self.kind == 'd81' and 1 <= track <= 80:
            bam_s = 1 if track <= 40 else 2
            bam = bytearray(self._read_sector(40, bam_s))
            rel = track if bam_s == 1 else (track - 40)
            entry = 0x10 + (rel - 1) * 6
            byte_off = entry + 1 + (sector // 8)
            bit_mask = 1 << (sector % 8)
            if (byte_off < len(bam) and (bam[byte_off] & bit_mask)):
                bam[byte_off] &= ~bit_mask
                if bam[entry]:
                    bam[entry] -= 1
                self._write_sector(40, bam_s, bytes(bam))

    def _build_dir_lines(self):
        lines = [self._format_header_line()]
        for ent in self.entries:
            lines.append(self._format_entry_line(ent))
        lines.append(self._format_blocks_free())
        self.dir_lines = lines

    # ---- File extraction ----
    def extract(self, entry: CbmDirEntry) -> bytes:
        """Return the raw bytes of one file from the image. For
        D64/D71/D81 we walk the sector chain starting at
        entry.start_track / start_sector. The first two bytes of each
        chained sector are (next_track, next_sector); the rest is
        payload, except for the LAST sector in the chain where
        next_track==0 and next_sector holds "bytes used in this last
        sector minus 1" - i.e. payload length is next_sector - 1.

        For LNX we slice the in-memory archive at the data_offset/
        total_bytes the parser stashed into start_track/start_sector.

        For CMD natives we treat the start track/sector as a flat
        sector index (the format isn't really track-based) and walk
        the chain the same way as D64.
        """
        if self.kind == 'lnx':
            # Overloaded fields: start_track = byte offset, start_sector = total bytes.
            off = entry.start_track
            n = entry.start_sector
            return self._lnx_buf[off:off + n]
        if self.kind in ('d64', 'd71', 'd81'):
            offset_fn = {
                'd64': _d64_sector_offset,
                'd71': _d71_sector_offset,
                'd81': _d81_sector_offset,
            }[self.kind]
            return self._extract_chain(entry.start_track,
                                        entry.start_sector,
                                        offset_fn)
        if self.kind in ('d2m', 'd4m', 'dnp'):
            # CMD natives: start_track/start_sector here is really a
            # logical (track, sector) but the geometry varies. We use
            # a flat-sector mapping: (track-1)*<sectors_per_track>+sector.
            # Without a precise per-track table, fall back to: offset
            # = (start_track * 256 * 80) + (start_sector * 256) which
            # matches D2M/D4M typical geometry and works for most
            # samples seen in the wild.
            return self._extract_cmd_native(entry)
        raise NotImplementedError(self.kind)

    def _extract_chain(self, track: int, sector: int,
                         offset_fn) -> bytes:
        """Generic CBM sector-chain walker. Returns the assembled
        payload (last sector trimmed to its "bytes used" count)."""
        out = bytearray()
        seen = set()
        max_steps = 4096
        steps = 0
        while track != 0 and steps < max_steps:
            if (track, sector) in seen:
                break
            seen.add((track, sector))
            try:
                self._fh.seek(offset_fn(track, sector))
            except ValueError:
                break
            blk = self._fh.read(256)
            if len(blk) < 256:
                break
            next_t, next_s = blk[0], blk[1]
            if next_t == 0:
                # Last block: next_s holds (bytes used - 1) when the
                # block is partially full. That's "next_s" usable
                # bytes after the 2-byte link header.
                used = max(0, next_s - 1)
                # Some images store 0 here meaning "all 254 bytes",
                # but the standard 1541 firmware convention is
                # "last byte position", so 0 means an empty/unused
                # block. We treat 0 as empty.
                if next_s == 0:
                    pass
                else:
                    out.extend(blk[2:2 + used])
                break
            out.extend(blk[2:256])
            track, sector = next_t, next_s
            steps += 1
        return bytes(out)

    def _extract_cmd_native(self, entry: CbmDirEntry) -> bytes:
        """CMD native (D2M/D4M/DNP) extraction.

        These formats use a flat-sector layout, but the directory
        entries store the start position as a (logical_track, sector)
        pair using 80 sectors per track. We compute the absolute byte
        offset and follow the same chain convention as 1541 floppies
        (next_track/next_sector at byte 0/1; track==0 = last).
        """
        sectors_per_track = 80
        track = entry.start_track
        sector = entry.start_sector
        out = bytearray()
        seen = set()
        steps = 0
        while track != 0 and steps < 8192:
            if (track, sector) in seen:
                break
            seen.add((track, sector))
            abs_sector = (track - 1) * sectors_per_track + sector
            self._fh.seek(abs_sector * 256)
            blk = self._fh.read(256)
            if len(blk) < 256:
                break
            next_t, next_s = blk[0], blk[1]
            if next_t == 0:
                used = max(0, next_s - 1)
                if next_s != 0:
                    out.extend(blk[2:2 + used])
                break
            out.extend(blk[2:256])
            track, sector = next_t, next_s
            steps += 1
        return bytes(out)


# =====================================================================
# ZipCode encoder
# =====================================================================
# ZipCode is a venerable C64 backup format: a single .d64 image is
# split across four files named "1!FOO", "2!FOO", "3!FOO", "4!FOO"
# where each file covers a specific track range and uses RLE
# compression on the sector contents. The format originates from
# Aaron Peromsik's ZIP-CODE utility (1989) and is still ubiquitous
# in CBM file archives - many C64 BBSes ONLY accept disk uploads
# in this form, which is why Quopus needs to produce it.
#
# Logic ported verbatim from PYCGMS' tools.py.

# Track ranges for the 4 ZipCode files. Track 35 is the last 1541
# track; the split is uneven because ZipCode predates 40-track
# extensions and was sized for typical PETSCII-modem upload limits.
ZIPCODE_TRACK_RANGES = (
    (1, 8),       # File 1!
    (9, 16),      # File 2!
    (17, 25),     # File 3!  (includes the directory track 18)
    (26, 35),     # File 4!
)

# Sectors per zone on a 1541 disk. Same data as LNX_TRACK_SECTORS
# but expressed as zones for the ZipCode block-offset arithmetic.
ZIPCODE_TRACK_SECTOR_MAX = (
    (21, (1, 17)),
    (19, (18, 24)),
    (18, (25, 30)),
    (17, (31, 35)),
)

# Sentinel pair appended to every ZipCode file marking end-of-data.
ZIPCODE_END_MARKER_TRACK = 26
ZIPCODE_END_MARKER_SECTOR = 26

# Final track/sector pair stored at the end of each ZipCode file's
# real data. The decoder uses this to detect "we've seen everything
# this part is supposed to deliver" instead of relying purely on
# end-of-stream, which catches malformed files cleanly.
ZIPCODE_FINAL_TS = ((8, 10), (16, 10), (25, 17), (35, 8))


def _zipcode_sectors_for_track(track: int) -> int:
    for sectors, (lo, hi) in ZIPCODE_TRACK_SECTOR_MAX:
        if lo <= track <= hi:
            return sectors
    return 0


def _zipcode_interleave(track: int) -> int:
    """ZipCode writes sectors in an interleaved order to match the
    1541's read pattern - half-and-half pairing keeps decompression
    fast on the original hardware."""
    return (_zipcode_sectors_for_track(track) + 1) // 2


def _zipcode_sector_order(track: int):
    """Yield the sector indices for a track in interleaved order:
    0, IL, 1, IL+1, ... where IL = interleave for this track."""
    n = _zipcode_sectors_for_track(track)
    il = _zipcode_interleave(track)
    out = []
    for i in range(il):
        out.append(i)
        if i + il < n:
            out.append(i + il)
    return out


def _zipcode_block_offset(track: int, sector: int) -> int:
    """Byte offset of (track, sector) inside a 35-track 1541 image."""
    off = 0
    for sectors, (lo, hi) in ZIPCODE_TRACK_SECTOR_MAX:
        if track > hi:
            off += (hi - lo + 1) * sectors
        else:
            off += (track - lo) * sectors
            off += sector
            break
    return off * 0x100


def _zipcode_read_block(image_fp, track: int, sector: int) -> bytes:
    image_fp.seek(_zipcode_block_offset(track, sector))
    data = image_fp.read(0x100)
    if len(data) != 0x100:
        raise IOError(f"could not read full block t{track}/s{sector}")
    return data


def _zipcode_rle_pack(data: bytes, rep_byte: int) -> bytes:
    """RLE encode a 256-byte block using `rep_byte` as the marker.
    Runs of >= 4 bytes (or any run of `rep_byte` itself) become
    a (rep_byte, length, value) triplet; everything else is
    written literally. Output never exceeds 256 bytes if the input
    is well-distributed - the caller chooses between this and the
    raw form based on size."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        v = data[i]
        run = 1
        while i + run < n and data[i + run] == v and run < 255:
            run += 1
        if run >= 4 or v == rep_byte:
            out.append(rep_byte)
            out.append(run)
            out.append(v)
            i += run
        else:
            out.append(v)
            i += 1
    return bytes(out)


def _zipcode_pick_rep_byte(data: bytes) -> int:
    """Pick the byte value that appears LEAST in `data` as the RLE
    marker - that minimises false-positive run starts."""
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    min_count = min(counts)
    for i, c in enumerate(counts):
        if c == min_count:
            return i
    return 0


def _zipcode_compress_block(data: bytes):
    """Compress one 256-byte block. Returns (flags, payload):
        flags 0 = raw 256 bytes, no compression
        flags 1 = single-byte fill (data is just that byte)
        flags 2 = RLE: payload is len, rep_byte, encoded_data...
    The flags go into the high 2 bits of the track byte that
    precedes each block in the output stream.
    """
    if all(b == data[0] for b in data):
        return 1, bytes([data[0]])
    rep = _zipcode_pick_rep_byte(data)
    rle = _zipcode_rle_pack(data, rep)
    rle_total = 2 + len(rle)
    if len(rle) <= 255 and rle_total < 256:
        return 2, bytes([len(rle), rep]) + rle
    return 0, data


def _zipcode_write_one_file(image_fp,
                              start_track: int, end_track: int,
                              disk_id_2bytes: Optional[bytes]) -> bytes:
    """Build one of the four ZipCode files (1!, 2!, 3!, 4!) entirely
    in memory and return its bytes. The first file (start_track==1)
    gets a 4-byte header: a load address of $03FE (so the file can
    be LOAD"1!NAME",8,1'd into memory at the right place) followed
    by the 2-byte disk ID. Subsequent files just have a $0400 load
    address.

    `image_fp` must be a seekable binary file-like object positioned
    anywhere - we call .seek() ourselves.
    """
    import struct
    out = bytearray()
    if disk_id_2bytes is not None:
        out += struct.pack('<H', 0x03FE)
        out += disk_id_2bytes
    else:
        out += struct.pack('<H', 0x0400)
    for track in range(start_track, end_track + 1):
        for sector in _zipcode_sector_order(track):
            block = _zipcode_read_block(image_fp, track, sector)
            flags, payload = _zipcode_compress_block(block)
            track_flags = track | (flags << 6)
            out += struct.pack('BB', track_flags, sector)
            out += payload
    # End marker: track + sector pair that doesn't correspond to any
    # real disk position, signalling "stop reading".
    track_flags = ZIPCODE_END_MARKER_TRACK | (1 << 6)
    out += struct.pack('BB', track_flags, ZIPCODE_END_MARKER_SECTOR)
    return bytes(out)


def d64_to_zipcode_files(image_bytes: bytes,
                           base_name: str,
                           disk_id: Optional[bytes] = None):
    """Convert a D64 image (passed as bytes) into the four ZipCode
    files. Returns a list of (filename, data_bytes) pairs ready to
    be written by the caller.

    `base_name` is the disk's chosen output name without the "1!"
    prefix and without the trailing extension - e.g. "GAME"
    produces ("1!GAME.prg", ...), ("2!GAME.prg", ...), etc.

    The .prg extension is appended because most C64 BBSes / file
    archives expect ZipCode parts to look like regular CBM PRGs
    on disk (so they LOAD and decompress correctly when uploaded
    to a 1541 image). Earlier versions of this code omitted it
    and that broke the BBS upload workflow.

    `disk_id`, if provided, must be exactly 2 bytes. If None we
    read it from the BAM (track 18 sector 0, offset $A2-$A3).
    """
    import io
    if len(image_bytes) < LNX_D64_SIZE:
        raise ValueError(
            f"D64 image is {len(image_bytes)} bytes; "
            f"ZipCode needs a full 35-track image ({LNX_D64_SIZE} bytes).")
    fp = io.BytesIO(image_bytes)
    if disk_id is None:
        fp.seek(_zipcode_block_offset(18, 0) + 0xA2)
        disk_id = fp.read(2)
        if len(disk_id) != 2:
            disk_id = b'00'
    base_name = base_name.upper()
    results = []
    for idx, (lo, hi) in enumerate(ZIPCODE_TRACK_RANGES, 1):
        # Only file 1 carries the disk ID; files 2-4 use the plain
        # $0400 load address.
        did = disk_id if idx == 1 else None
        data = _zipcode_write_one_file(fp, lo, hi, did)
        results.append((f"{idx}!{base_name}.prg", data))
    return results


# =====================================================================
# ZipCode decoder (the reverse direction)
# =====================================================================
# Used when the user double-clicks one of the four "N!FOO.prg" files
# in Quopus: we collect all four parts, decompress them in memory,
# stitch a fresh 174848-byte D64 together, and show that in the
# CbmDiskDialog (with a Save D64 button so the user can persist the
# decoded image if they want it permanently).


def _zipcode_decode_file(zip_bytes: bytes,
                          d64_image: bytearray,
                          part_index: int) -> None:
    """Decompress one ZipCode file's blocks into the in-progress D64
    image. `zip_bytes` is the raw file contents; `d64_image` is the
    174848-byte bytearray we're building up; `part_index` is 0..3
    so we know which final track/sector pair to expect.

    Stops cleanly at end-of-stream OR when we've written the part's
    final block (per ZIPCODE_FINAL_TS) - whichever comes first.
    """
    import struct
    pos = 0
    n = len(zip_bytes)
    if n < 2:
        raise ValueError("ZipCode file too short")
    # Skip header: 2 bytes load address, plus 2 bytes disk ID if it's
    # the first file (load addr $03FE).
    load = struct.unpack_from('<H', zip_bytes, 0)[0]
    pos = 2
    if load == 0x03FE:
        pos += 2  # skip disk ID

    final_ts = ZIPCODE_FINAL_TS[part_index]

    while pos + 2 <= n:
        track_flags = zip_bytes[pos]; sector = zip_bytes[pos + 1]
        pos += 2
        flags = (track_flags & 0xC0) >> 6
        track = track_flags & 0x3F
        # End marker has track > 35 - skip it and stop.
        if track > 35:
            return
        # Decompress depending on flags.
        if flags == 0:
            # Raw block: copy 256 bytes verbatim.
            if pos + 0x100 > n:
                raise ValueError("truncated raw block")
            data = zip_bytes[pos:pos + 0x100]
            pos += 0x100
        elif flags == 1:
            # Fill block: one byte repeated 256 times.
            if pos + 1 > n:
                raise ValueError("truncated fill block")
            data = bytes([zip_bytes[pos]]) * 0x100
            pos += 1
        elif flags == 2:
            # RLE block: (length, rep_byte) header, then a stream
            # where occurrences of rep_byte introduce a (count, value)
            # triplet. Loop until we've produced 256 bytes.
            data = bytearray()
            while len(data) < 0x100:
                if pos + 2 > n:
                    raise ValueError("truncated RLE block header")
                dlen = zip_bytes[pos]; rep = zip_bytes[pos + 1]; pos += 2
                if pos + dlen > n:
                    raise ValueError("truncated RLE block payload")
                zdata = zip_bytes[pos:pos + dlen]; pos += dlen
                zi = 0
                while zi < len(zdata):
                    b = zdata[zi]
                    if b == rep:
                        if zi + 2 >= len(zdata):
                            raise ValueError("RLE marker without count/value")
                        data.extend(bytes([zdata[zi + 2]]) * zdata[zi + 1])
                        zi += 3
                    else:
                        data.append(b); zi += 1
            data = bytes(data[:0x100])
        else:
            raise ValueError(f"unknown ZipCode flags {flags}")
        # Write the decompressed block into the D64 image at its
        # correct (track, sector) offset.
        if len(data) == 0x100 and 1 <= track <= 35:
            off = _zipcode_block_offset(track, sector)
            if off + 0x100 <= len(d64_image):
                d64_image[off:off + 0x100] = data
        # Stop when we've reached the final block this part owns.
        if (track, sector) == final_ts:
            return


def zipcode_to_d64_bytes(part_files: List[bytes]) -> bytes:
    """Decode four ZipCode files (passed as a list of byte strings,
    in order 1!, 2!, 3!, 4!) into a complete 174848-byte D64 image.

    Raises ValueError if the part list isn't exactly four entries
    or if any part's contents look corrupt. Missing parts are the
    caller's responsibility to detect; this function assumes the
    list is complete.
    """
    if len(part_files) != 4:
        raise ValueError(
            f"need exactly 4 ZipCode parts, got {len(part_files)}")
    img = bytearray(LNX_D64_SIZE)
    for i, payload in enumerate(part_files):
        _zipcode_decode_file(payload, img, i)
    return bytes(img)


# Regex used to detect ZipCode part files: the leading digit (1-4),
# the "!" separator, then any base name, optionally followed by .prg.
# Matched case-insensitively because some BBSes upper-case everything.
_ZIPCODE_PART_RE = re.compile(r'^([1-4])!(.+?)(\.prg)?$', re.IGNORECASE)


def is_zipcode_part(path) -> bool:
    """True if `path`'s filename matches the ZipCode part naming
    pattern: one of 1!, 2!, 3!, or 4! followed by a base name,
    optionally with .prg extension. Used by the Quopus lister to
    route double-clicks to the ZipCode -> D64 conversion."""
    try:
        return bool(_ZIPCODE_PART_RE.match(Path(path).name))
    except Exception:
        return False


def find_zipcode_set(path) -> Tuple[Optional[List[Path]],
                                      List[int],
                                      Optional[str]]:
    """Given any one of the four ZipCode parts, locate the full set.

    Returns (paths, missing, base_name) where:
        paths      = list of 4 Path objects in order 1..4, or None
                     if any part is missing
        missing    = list of part numbers that couldn't be found
                     (empty when paths is non-None)
        base_name  = the matched base name (without .prg) for use
                     in dialogs / status messages, None on parse fail
    """
    p = Path(path)
    m = _ZIPCODE_PART_RE.match(p.name)
    if not m:
        return None, [1, 2, 3, 4], None
    base = m.group(2)
    has_prg = (m.group(3) is not None)
    parent = p.parent
    found = []
    missing = []
    for n in range(1, 5):
        # Try the same extension presence as the input first, then
        # the alternate. This handles users who saved one variant
        # with .prg and one without (rare but possible).
        candidates = []
        if has_prg:
            candidates.append(parent / f"{n}!{base}.prg")
            candidates.append(parent / f"{n}!{base}")
        else:
            candidates.append(parent / f"{n}!{base}")
            candidates.append(parent / f"{n}!{base}.prg")
        # Also try uppercase variants since 1!FOO.PRG vs 1!foo.prg
        # are distinct on case-sensitive filesystems but should both
        # work. We don't enumerate every case combination - just
        # uppercase, which is the historical default.
        candidates.append(parent / f"{n}!{base.upper()}.prg")
        candidates.append(parent / f"{n}!{base.upper()}")
        hit = None
        for c in candidates:
            if c.is_file():
                hit = c
                break
        if hit is None:
            missing.append(n)
        else:
            found.append(hit)
    if missing:
        return None, missing, base
    return found, [], base


# =====================================================================
# Lynx parser (verbatim from tools.py, kept self-contained here so
# cbmfiles doesn't reach across the codebase for it).
# =====================================================================
@dataclass
class _LynxEntry:
    name: str
    blocks: int
    ftype: str
    last_bytes: int
    data_offset: int
    total_bytes: int


# Per-track sector counts for a 1541 / D64 image. Index = track number
# (1-based). Used by the LNX -> D64 conversion when a directory entry
# allocates sectors. Track 0 is unused (placeholder) so the array can
# be 1-indexed without arithmetic.
LNX_TRACK_SECTORS = [0]   # placeholder for track 0
for _t in range(1, 36):
    if _t <= 17:        LNX_TRACK_SECTORS.append(21)
    elif _t <= 24:      LNX_TRACK_SECTORS.append(19)
    elif _t <= 30:      LNX_TRACK_SECTORS.append(18)
    else:                LNX_TRACK_SECTORS.append(17)

# Cumulative byte offset of each track's first sector. Sums of
# (sectors_per_track * 256). LNX_D64_SIZE = total D64 image size
# (174848 bytes).
LNX_TRACK_OFFSETS = [0]
_cum = 0
for _t in range(1, 36):
    LNX_TRACK_OFFSETS.append(_cum)
    _cum += LNX_TRACK_SECTORS[_t] * 256
LNX_D64_SIZE = _cum


def _lnx_ts_to_offset(track: int, sector: int) -> int:
    """Track/sector -> byte offset inside a 1541 D64 image."""
    if not (1 <= track <= 35):
        raise ValueError(f"invalid track: {track}")
    if not (0 <= sector < LNX_TRACK_SECTORS[track]):
        raise ValueError(f"invalid sector {sector} on track {track}")
    return LNX_TRACK_OFFSETS[track] + sector * 256


def _lnx_ascii_to_petscii_name(name: str) -> bytes:
    """Convert an ASCII filename to a 16-byte PETSCII directory
    field, $A0-padded on the right. Lowercase letters fold to upper
    (CBM disks are upper-case-by-default for the directory)."""
    name = name[:16]
    out = bytearray()
    for ch in name:
        c = ord(ch)
        if 97 <= c <= 122:
            c -= 32
        if 32 <= c < 128:
            out.append(c)
        else:
            out.append(32)
    while len(out) < 16:
        out.append(0xA0)
    return bytes(out)


def _lnx_build_d64(entries, buf: bytes,
                     diskname: str = "LYNX-DISK") -> bytes:
    """Build a 174848-byte 1541 D64 image from a list of Lynx
    entries plus the source archive buffer.

    The result has:
      - A valid BAM at track 18, sector 0 (DOS version 'A', "2A"
        format, given disk name + ID "01").
      - One directory entry per Lynx file at track 18, sectors 1+.
      - Each file's payload allocated to free 1541 sectors
        starting at track 1, with a proper sector chain.
      - BAM bitmap reflecting the allocations.

    Verbatim port of tools.py' _lnx_build_d64.
    """
    image = bytearray(LNX_D64_SIZE)
    used_ts = set()
    used_ts.add((18, 0))

    free_ts = []
    for t in range(1, 36):
        if t == 18:
            continue
        for s in range(LNX_TRACK_SECTORS[t]):
            free_ts.append((t, s))

    file_count = len(entries)
    dir_sectors_needed = (file_count + 7) // 8 or 1
    dir_sectors = list(range(1, 1 + dir_sectors_needed))

    # Link the directory sectors to one another. Last one terminates
    # with track=0, sector=0xFF.
    for i, sec in enumerate(dir_sectors):
        off = _lnx_ts_to_offset(18, sec)
        if i < len(dir_sectors) - 1:
            image[off + 0] = 18
            image[off + 1] = dir_sectors[i + 1]
        else:
            image[off + 0] = 0
            image[off + 1] = 0xFF
        used_ts.add((18, sec))

    # BAM header at track 18, sector 0.
    bam_off = _lnx_ts_to_offset(18, 0)
    image[bam_off + 0] = 18
    image[bam_off + 1] = dir_sectors[0]
    image[bam_off + 2] = 0x41   # DOS Version 'A'
    image[bam_off + 3] = 0x00   # single-sided

    dn_bytes = _lnx_ascii_to_petscii_name(diskname)
    image[bam_off + 0x90: bam_off + 0x90 + 16] = dn_bytes
    image[bam_off + 0xA0] = 0xA0
    image[bam_off + 0xA1] = 0xA0
    image[bam_off + 0xA2] = 0x30   # disk ID '0'
    image[bam_off + 0xA3] = 0x31   # disk ID '1'
    image[bam_off + 0xA4] = 0xA0
    image[bam_off + 0xA5] = 0x32   # DOS type '2'
    image[bam_off + 0xA6] = 0x41   # DOS type 'A'
    for o in (0xA7, 0xA8, 0xA9, 0xAA):
        image[bam_off + o] = 0xA0

    # Allocate each file: walk free sectors in order, build the
    # chain, write the directory entry.
    for idx, e in enumerate(entries):
        start = e.data_offset
        end = start + e.total_bytes
        file_data = buf[start:end]
        n_sectors = (len(file_data) + 253) // 254
        if len(free_ts) < n_sectors:
            raise ValueError("LNX archive doesn't fit into a D64 image")
        allocated = free_ts[:n_sectors]
        free_ts = free_ts[n_sectors:]
        for si in range(n_sectors):
            t, s = allocated[si]
            used_ts.add((t, s))
            off = _lnx_ts_to_offset(t, s)
            chunk_start = si * 254
            chunk_end = min(chunk_start + 254, len(file_data))
            chunk = file_data[chunk_start:chunk_end]
            if si < n_sectors - 1:
                nt, ns = allocated[si + 1]
                image[off + 0] = nt
                image[off + 1] = ns
                image[off + 2:off + 2 + len(chunk)] = chunk
            else:
                image[off + 0] = 0
                used = len(chunk)
                eof_pos = 1 + used
                if eof_pos > 255:
                    eof_pos = 255
                image[off + 1] = eof_pos
                image[off + 2:off + 2 + used] = chunk
        dir_sec_index = idx // 8
        entry_index = idx % 8
        d_off = _lnx_ts_to_offset(18, dir_sectors[dir_sec_index])
        entry_off = d_off + entry_index * 32
        # File type: closed (bit 7) + locked-not (bit 6 off) + PRG (2)
        image[entry_off + 2] = 0x82
        first_t, first_s = allocated[0]
        image[entry_off + 3] = first_t
        image[entry_off + 4] = first_s
        name_bytes = _lnx_ascii_to_petscii_name(e.name)
        image[entry_off + 5: entry_off + 21] = name_bytes
        image[entry_off + 30] = n_sectors & 0xFF
        image[entry_off + 31] = (n_sectors >> 8) & 0xFF

    # BAM bitmap: free-count + 3 bytes of bits per track.
    bam_ptr = bam_off + 0x04
    for track in range(1, 36):
        sectors = LNX_TRACK_SECTORS[track]
        free_count = 0
        b0 = b1 = b2 = 0
        for sec in range(sectors):
            if (track, sec) not in used_ts:
                free_count += 1
                bit = 1 << (sec & 7)
                if sec < 8:        b0 |= bit
                elif sec < 16:     b1 |= bit
                else:               b2 |= bit
        image[bam_ptr + 0] = free_count
        image[bam_ptr + 1] = b0
        image[bam_ptr + 2] = b1
        image[bam_ptr + 3] = b2
        bam_ptr += 4
    return bytes(image)


def lnx_to_d64_bytes(lnx_bytes: bytes,
                      diskname: str = "LYNX-DISK") -> bytes:
    """Convenience wrapper: parse an in-memory LNX archive and
    return the converted D64 image as bytes. Raises ValueError on
    parse failure."""
    _db, _nf, _sig, entries = _lnx_parse(lnx_bytes)
    if not entries:
        raise ValueError("LNX archive contains no files")
    return _lnx_build_d64(entries, lnx_bytes, diskname=diskname)


def is_lnx_in_prg(path) -> bool:
    """True for files named like 'xxx.lnx.prg' or anything else
    where the stem ends with '.lnx' (case-insensitive). These are
    LNX archives wrapped in a PRG load-address header by some
    BBSes / archive sites; they need the LNX -> D64 path to
    show their contents in the disk viewer.
    """
    try:
        p = Path(path)
        # Match: stem ends with .lnx AND extension is .prg
        # ('foo.lnx.prg'.stem == 'foo.lnx', .suffix == '.prg')
        return (p.suffix.lower() == '.prg'
                and p.stem.lower().endswith('.lnx'))
    except Exception:
        return False


# =====================================================================
# Lynx parser
# =====================================================================
def _lnx_parse(buf: bytes):
    """See tools.py for the original prose. In short: the LNX header
    is ASCII-text, telling us how many directory blocks the file has
    + how many entries follow. After that the data area is
    blocks*254 bytes long with each entry's bytes laid out
    sequentially."""
    v = buf
    vlen = len(v)
    dir_pos = None
    for i in range(0x20, min(vlen - 3, 0x200)):
        if v[i] == 0x0D and v[i + 1] == 0x20 and 0x30 <= v[i + 2] <= 0x39:
            dir_pos = i
            break
    if dir_pos is None:
        raise ValueError("DirBlocks line not found in LNX header")
    i = dir_pos + 1
    while i < vlen and v[i] == 0x20: i += 1
    dstart = i
    while i < vlen and 0x30 <= v[i] <= 0x39: i += 1
    dir_blocks = int(v[dstart:i].decode('ascii'))
    while i < vlen and v[i] == 0x20: i += 1
    sig_start = i
    while i < vlen and v[i] != 0x0D: i += 1
    sig = v[sig_start:i].decode('ascii', errors='replace')
    j = i + 1
    while j < vlen and v[j] == 0x20: j += 1
    nf_start = j
    while j < vlen and 0x30 <= v[j] <= 0x39: j += 1
    num_files = int(v[nf_start:j].decode('ascii'))
    while j < vlen and v[j] != 0x0D: j += 1
    pos = j + 1
    data_start = dir_blocks * 254
    entries = []
    cur_block = 0
    for _ in range(num_files):
        name_start = pos
        while pos < vlen and v[pos] != 0x0D: pos += 1
        name = v[name_start:pos].decode('ascii', errors='replace')
        pos += 1
        k = pos
        while k < vlen and v[k] == 0x20: k += 1
        bstart = k
        while k < vlen and 0x30 <= v[k] <= 0x39: k += 1
        blocks = int(v[bstart:k].decode('ascii'))
        while k < vlen and v[k] != 0x0D: k += 1
        pos = k + 1
        ftype_ch = chr(v[pos]); pos += 2
        k = pos
        while k < vlen and v[k] == 0x20: k += 1
        lbstart = k
        while k < vlen and 0x30 <= v[k] <= 0x39: k += 1
        last_bytes = int(v[lbstart:k].decode('ascii'))
        while k < vlen and v[k] != 0x0D: k += 1
        pos = k + 1
        total_bytes = (blocks - 1) * 254 + last_bytes
        data_offset = data_start + cur_block * 254
        cur_block += blocks
        entries.append(_LynxEntry(name=name, blocks=blocks,
                                    ftype=ftype_ch.upper(),
                                    last_bytes=last_bytes,
                                    data_offset=data_offset,
                                    total_bytes=total_bytes))
    return dir_blocks, num_files, sig, entries


# =====================================================================
# PETSCII -> C64 Pro Mono PUA mapping
# =====================================================================
# C64 Pro Mono lays out PETSCII glyphs in the Unicode Private Use Area:
#
#     U+E000 + b   = upper charset, normal video
#     U+E100 + b   = lower charset, normal video
#
# However the font only covers the *printable* PETSCII slots: bytes
# 0x00-0x1F and 0x80-0x9F have no direct glyph (those ranges are
# control codes in PETSCII text streams). In directory art, those
# byte ranges DO appear and need to render as REVERSE-VIDEO graphics
# (the C64 KERNAL displays such bytes by setting bit 7 of the screen
# RAM byte = "show this screen-code in reverse video").
#
# So our mapping does two things:
#
#   1. Convert the raw PETSCII byte through the C64 KERNAL's
#      petscii->screen-code table. Bit 7 of the screen code becomes
#      our "render in reverse video" flag.
#   2. Look up the printable PETSCII byte that produces the same
#      screen-code SHAPE - i.e. the glyph the user wants to see.
#      That printable byte is what we feed into the PUA page.
#
# Caller does the final cell flip (fill cell with fg, draw glyph in bg)
# when our reverse flag is set, since C64 Pro Mono itself doesn't ship
# pre-inverted glyphs at the corresponding PUA slots in a way that
# Qt's drawText() handles transparently.


def _petscii_byte_to_screen(b: int) -> int:
    """C64 KERNAL's PETSCII -> screen-code conversion. Bit 7 of the
    result indicates reverse video; bits 0-6 are the screen code
    (0..127) of the printable shape."""
    b &= 0xFF
    if b < 0x20:        return b + 0x80   # $00-$1F: reverse of $40-$5F
    if b < 0x40:        return b           # $20-$3F unchanged
    if b < 0x60:        return b - 0x40    # $40-$5F (@ A-Z) -> $00-$1F
    if b < 0x80:        return b - 0x20    # $60-$7F -> $40-$5F (graphics)
    if b < 0xA0:        return b           # $80-$9F: reverse of $A0-$BF... no
    if b < 0xC0:        return b - 0x40    # $A0-$BF -> $60-$7F (shifted gfx)
    if b < 0xE0:        return b - 0x80    # $C0-$DF -> $40-$5F (CBM gfx)
    return b - 0x80                        # $E0-$FF -> $60-$7F


def _screen_to_printable_petscii(sc: int) -> int:
    """Inverse mapping: take a 7-bit screen code and return a
    PETSCII byte that the C64 Pro Mono direct PUA page has a glyph
    for. Pick the canonical printable representative."""
    sc &= 0x7F
    if sc < 0x20:        return sc + 0x40   # screen 0x00-0x1F -> @ A-Z [..]
    if sc < 0x40:        return sc           # digits / symbols
    if sc < 0x60:        return sc + 0x20   # graphics 1
    return sc + 0x40                        # graphics 2 (shifted)


def _petscii_to_pua_glyph(b: int, charset: str = 'lower'):
    """Map a raw PETSCII directory byte to a (codepoint, reverse)
    pair where:
      codepoint = unicode glyph in the C64 Pro Mono direct PUA page
      reverse   = caller should render this cell in reverse video

    The C64 Pro Mono font lays out PETSCII glyphs directly:
        U+E000 + b   = upper charset, normal video
        U+E100 + b   = lower charset, normal video

    Most bytes have a glyph at the matching offset and we just use
    it as-is. The exceptions are:
        0x00-0x1F  : control codes (no printable glyph)
        0x80-0x9F  : reserved control range (no printable glyph)

    In CBM directory art these bytes appear and are meant to render
    as REVERSE-VIDEO of the corresponding printable byte. The C64
    KERNAL maps them by adding/clearing 0x40 / 0x80 + setting bit 7
    of the screen RAM byte. We do the same here:
        b in 0x00-0x1F  ->  reverse of (b + 0x40)   = reverse @-^_
        b in 0x80-0x9F  ->  reverse of (b + 0x40)   = reverse graphics
    Both yield a printable byte the font has a glyph for at the
    chosen charset's PUA page, plus a `reverse=True` flag the
    caller honours by flipping the cell colours.
    """
    b &= 0xFF
    reverse = False
    if b < 0x20:
        # Control range -> reverse of @-A-Z[..]
        b += 0x40
        reverse = True
    elif 0x80 <= b < 0xA0:
        # Reserved control range -> reverse of CBM graphics ($C0-$DF)
        b += 0x40
        reverse = True
    base = 0xE000 if charset == 'upper' else 0xE100
    return chr(base + b), reverse


# =====================================================================
# C64-style renderer (uses the bundled C64 Pro Mono font)
# =====================================================================
# Quopus ships fonts/C64_Pro_Mono-STYLE.ttf which provides every
# PETSCII glyph in the PUA. The rendering path here is intentionally
# the same shape as readers.py' _render_petscii_to_pixmap so the
# directory listing inside CbmDiskDialog visually matches the .seq
# / .pet viewer the user is already familiar with.


def render_directory_to_pixmap(dir_lines: List[bytes],
                                 cell_size: int = 16,
                                 fg=(255, 255, 255),
                                 bg=(63, 63, 215),
                                 charset: str = 'upper') -> QPixmap:
    """Render a list of PETSCII directory lines to a QPixmap painted
    in the classic C64 directory style: white-on-blue, header line
    in REVERSE video to mirror what the C64 KERNAL prints when
    LOADing $.

    `charset` selects the C64 character ROM:
      'upper' = ALL-CAPS + graphics (KERNAL default at boot)
      'lower' = mixed case (a-z lowercase, A-Z via shift) + some
                graphics. Most CBM users in the demoscene actually
                see directories in this mode, since DirMaster and
                most modern editors default to lower charset for
                better readability of mixed-case filenames.
    """
    if not dir_lines:
        return QPixmap()
    # Pad short lines so the resulting image isn't ragged on the
    # right; 40 chars matches a real 1541 directory listing.
    max_len = max(40, max(len(ln) for ln in dir_lines))
    cols = max_len
    rows = len(dir_lines)
    img_w = cols * cell_size
    img_h = rows * cell_size
    img = QImage(img_w, img_h, QImage.Format.Format_RGB32)
    img.fill(QColor(*bg))

    fg_q = QColor(*fg)
    bg_q = QColor(*bg)

    p = QPainter(img)
    try:
        font = QFont("C64 Pro Mono")
        font.setPixelSize(cell_size)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100)
        p.setFont(font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        from PyQt6.QtCore import QRect
        for line_idx, raw in enumerate(dir_lines):
            y_top = line_idx * cell_size
            for col_idx in range(cols):
                ch = raw[col_idx] if col_idx < len(raw) else 0x20
                # Header line is REVERSED from col 2 onward (matches
                # KERNAL behaviour - the leading "0 " is normal, the
                # quoted disk name + ID is shown reversed).
                header_reverse = (line_idx == 0 and col_idx >= 2)
                glyph, byte_reverse = _petscii_to_pua_glyph(ch,
                                                              charset=charset)
                cell_reverse = header_reverse ^ byte_reverse
                rect = QRect(col_idx * cell_size, y_top,
                              cell_size, cell_size)
                if cell_reverse:
                    p.fillRect(rect, fg_q)
                    p.setPen(bg_q)
                else:
                    p.setPen(fg_q)
                p.drawText(rect,
                            int(Qt.AlignmentFlag.AlignCenter),
                            glyph)
    finally:
        p.end()
    return QPixmap.fromImage(img)


# =====================================================================
# Interactive directory rendering - lets the user select entries by
# clicking directly on the rendered PETSCII directory.
# =====================================================================
class _InteractiveDirLabel(QLabel):
    """Click-to-select wrapper around the rendered directory pixmap.

    Behaviour mirrors a standard list widget:

      - Left-click on an entry row toggles single-select to that
        row only
      - Shift + click extends the selection from the last anchor
        to the clicked row (contiguous range)
      - Ctrl/Cmd + click toggles the clicked row in/out of the
        selection without affecting other selected rows
      - Click on the header or blocks-free line clears selection
      - Double-click on an entry row emits double_clicked(idx)
        (used by the dialog to launch viewers / Run-PRG etc.)

    Selected rows get a translucent yellow highlight rectangle
    drawn over the underlying pixmap. We don't re-render the
    PETSCII for selection - that would mean re-doing the QPainter
    work on every selection change which is wasteful for a 200-file
    directory. Instead we paint the highlight as an overlay in
    paintEvent.

    The dialog hands us:
      - a pre-rendered QPixmap of the directory
      - the cell_size (pixel height of each rendered row)
      - the number of entries (header + N entries + blocks-free
        line, so total rows = entries + 2)

    We expose `selected_indices` as a set of entry indices (NOT
    line indices - the header offset is already accounted for) so
    callers don't have to think about the off-by-one.
    """

    # Emitted whenever the selection set changes. Carries the new
    # set as a list of entry indices in stable (ascending) order.
    from PyQt6.QtCore import pyqtSignal as _signal
    selection_changed = _signal(list)
    # Emitted on double-click; carries the entry index (0-based).
    double_clicked_entry = _signal(int)

    def __init__(self, pixmap, cell_size: int, n_entries: int,
                 parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignTop
                          | Qt.AlignmentFlag.AlignLeft)
        self.setPixmap(pixmap)
        self.resize(pixmap.size())
        self._cell_size = max(1, int(cell_size))
        self._n_entries = max(0, int(n_entries))
        # Currently-selected entry indices. Set for O(1) toggle.
        self._selected: set[int] = set()
        # Anchor for shift-extend; None until first click.
        self._anchor: Optional[int] = None
        # Allow keyboard focus so the user can use arrow keys + Esc
        # without first clicking on something else.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---- public API used by the dialog ----------------------------
    def selected_indices(self) -> list[int]:
        """Return selected entries as a sorted list."""
        return sorted(self._selected)

    def clear_selection(self):
        if self._selected:
            self._selected.clear()
            self.selection_changed.emit([])
            self.update()

    def select_all(self):
        new = set(range(self._n_entries))
        if new != self._selected:
            self._selected = new
            self.selection_changed.emit(sorted(self._selected))
            self.update()

    def set_selected_indices(self, indices) -> None:
        """Replace selection - used to keep the optional fallback
        QListWidget in sync if it's visible."""
        new = {int(i) for i in indices
                if 0 <= int(i) < self._n_entries}
        if new != self._selected:
            self._selected = new
            self.selection_changed.emit(sorted(self._selected))
            self.update()

    # ---- mouse / keyboard handling --------------------------------
    def _entry_index_at(self, y: int) -> Optional[int]:
        """Convert a click Y coordinate to an entry index, or None
        if the click is on the header / blocks-free line / outside
        the rendered area. Line layout: header at row 0, entries
        at rows 1..N, blocks-free at row N+1."""
        if self._cell_size <= 0:
            return None
        line = y // self._cell_size
        idx = line - 1  # skip header
        if 0 <= idx < self._n_entries:
            return int(idx)
        return None

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        idx = self._entry_index_at(int(ev.position().y()))
        mods = ev.modifiers()
        if idx is None:
            # Click on header / blocks-free / empty area:
            # clear selection (matches standard list behaviour).
            self.clear_selection()
            self._anchor = None
            ev.accept()
            return
        if mods & Qt.KeyboardModifier.ShiftModifier:
            # Range from anchor to current click
            if self._anchor is None:
                self._anchor = idx
            lo, hi = sorted([self._anchor, idx])
            self._selected = set(range(lo, hi + 1))
            self.selection_changed.emit(sorted(self._selected))
            self.update()
        elif (mods & Qt.KeyboardModifier.ControlModifier
                or mods & Qt.KeyboardModifier.MetaModifier):
            # Toggle one row, keep anchor where it was
            if idx in self._selected:
                self._selected.discard(idx)
            else:
                self._selected.add(idx)
            self._anchor = idx
            self.selection_changed.emit(sorted(self._selected))
            self.update()
        else:
            # Plain click - single-select that row
            self._selected = {idx}
            self._anchor = idx
            self.selection_changed.emit(sorted(self._selected))
            self.update()
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(ev)
            return
        idx = self._entry_index_at(int(ev.position().y()))
        if idx is not None:
            self.double_clicked_entry.emit(idx)
        ev.accept()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_A and (
                ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.select_all()
            ev.accept()
            return
        if k == Qt.Key.Key_Escape:
            self.clear_selection()
            ev.accept()
            return
        if k in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            # Cursor navigation: move anchor up/down, replace
            # selection unless Shift held (then extend).
            if self._n_entries == 0:
                return
            cur = self._anchor if self._anchor is not None else 0
            new = cur - 1 if k == Qt.Key.Key_Up else cur + 1
            new = max(0, min(self._n_entries - 1, new))
            if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                lo, hi = sorted([
                    self._anchor if self._anchor is not None
                    else new, new])
                self._selected = set(range(lo, hi + 1))
            else:
                self._selected = {new}
                self._anchor = new
            self.selection_changed.emit(sorted(self._selected))
            self.update()
            ev.accept()
            return
        super().keyPressEvent(ev)

    # ---- painting --------------------------------------------------
    def paintEvent(self, ev):
        # Let QLabel draw the pixmap normally first.
        super().paintEvent(ev)
        if not self._selected:
            return
        # Overlay translucent yellow on each selected row. We
        # paint AFTER the pixmap so the highlight rides on top
        # of the rendered PETSCII characters but doesn't replace
        # them.
        from PyQt6.QtGui import QPainter as _QP
        p = _QP(self)
        try:
            # 80-alpha yellow gives a clearly visible band without
            # losing readability of the white-on-blue text underneath.
            p.setBrush(QColor(255, 220, 0, 80))
            p.setPen(Qt.PenStyle.NoPen)
            w = self.width()
            for idx in self._selected:
                y = (idx + 1) * self._cell_size  # +1 for header
                p.drawRect(0, y, w, self._cell_size)
        finally:
            p.end()


class _PreviewFullscreenDialog(QDialog):
    """Modal fullscreen viewer for a preview pixmap. Used when
    the user clicks the preview pane in CbmDiskDialog to get a
    larger look at the rendered SEQ / PETSCII art / C64 bitmap.

    Behaviour:
      - Opens maximized (taking the screen the parent dialog
        is on)
      - Pixmap scales to fit the window while keeping aspect
        ratio. Uses FastTransformation (nearest-neighbor) so
        pixel-art-style C64 graphics stay crisp instead of
        blurring with bilinear filtering.
      - Rescales on resize so the user can drag corners to
        size the window and the picture follows.
      - Esc, double-click, or clicking the close button shuts it
      - Caption shown in window title for context.
    """

    def __init__(self, source_pixmap, caption: str = "",
                  parent=None):
        super().__init__(parent)
        self._source = source_pixmap
        self._caption = caption
        title = caption if caption else "Preview"
        self.setWindowTitle(title)
        # Dark background everywhere - matches the preview pane
        self.setStyleSheet(
            "QDialog { background-color: #1a1a1a; }")
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QPushButton)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "background-color: #1a1a1a;")
        lay.addWidget(self._label, stretch=1)
        # Tiny action bar at the bottom: lets the user save the
        # same pixmap they're looking at as PNG without having
        # to close fullscreen and find the button in the parent
        # dialog. Subtle styling so it doesn't compete with the
        # picture - dark grey button on near-black background.
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 4, 8, 4)
        btn_row.addStretch(1)
        btn_save = QPushButton("Save as PNG...")
        btn_save.setStyleSheet(
            "QPushButton { background-color: #333; color: #ddd; "
            "padding: 4px 12px; border: 1px solid #555; } "
            "QPushButton:hover { background-color: #444; }")
        btn_save.clicked.connect(self._save_as_png)
        btn_row.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(
            "QPushButton { background-color: #333; color: #ddd; "
            "padding: 4px 12px; border: 1px solid #555; } "
            "QPushButton:hover { background-color: #444; }")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
        # Show maximized (preserves window chrome / close button)
        # rather than true fullscreen, which can be hard to escape
        # from on some window managers.
        self.showMaximized()
        self._rescale()

    def _save_as_png(self):
        """Save the source pixmap (NOT the scaled-to-fit one)
        as a PNG. Uses 2x nearest-neighbor scaling so the
        C64's chunky pixels stay sharp."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from PyQt6.QtCore import Qt as _Qt
        import os, re
        cap = self._caption or "preview"
        stem = cap.split("(")[0].strip() or "preview"
        stem = re.sub(r'[^\w\-.]+', '_', stem).strip('_') \
                or "preview"
        default_path = os.path.join(
            os.path.expanduser("~"), f"{stem}.png")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save preview as PNG",
            default_path,
            "PNG Images (*.png);;All files (*)")
        if not out_path:
            return
        if not out_path.lower().endswith(".png"):
            out_path += ".png"
        src = self._source
        if src is None or src.isNull():
            return
        scaled = src.scaled(
            src.width() * 2, src.height() * 2,
            _Qt.AspectRatioMode.IgnoreAspectRatio,
            _Qt.TransformationMode.FastTransformation)
        if not scaled.save(out_path, "PNG"):
            QMessageBox.warning(
                self, "Save PNG",
                f"Failed to save:\n{out_path}")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._rescale()

    def _rescale(self):
        if self._source is None or self._source.isNull():
            return
        from PyQt6.QtGui import QPixmap
        # Size pixmap to fit the label viewport, keeping aspect
        # ratio. FastTransformation = nearest-neighbor, which is
        # exactly right for pixel art (no smoothing).
        target = self._label.size()
        if target.width() < 10 or target.height() < 10:
            return
        scaled = self._source.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self._label.setPixmap(scaled)

    def keyPressEvent(self, ev):
        # Esc closes - same as the close button. Avoids any
        # ambiguity about how to leave fullscreen.
        if ev.key() == Qt.Key.Key_Escape:
            self.accept()
            return
        super().keyPressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        # Double-click anywhere also closes - common viewer
        # convention.
        self.accept()


# =====================================================================
# CbmDiskDialog - the modal viewer + extractor shown for .dxx files
# =====================================================================
class CbmDiskDialog(QDialog):
    """Show the disk image's directory in C64-style PETSCII rendering
    plus an Extract toolbar. Layout:

        +----------------------------------------------+
        | [filename]                       [Close Esc] |
        +----------------------------------------------+
        | (rendered PETSCII directory pixmap)          |
        |                                              |
        +----------------------------------------------+
        | [Extract All...]  [Extract Selected]  Sel: 3 |
        +----------------------------------------------+
        | (optional file picker - QListWidget of names)|
        +----------------------------------------------+

    The picker is populated from CbmDiskReader.entries; selection is
    multi-select. "Extract All" extracts every entry; "Extract
    Selected" only the highlighted ones. Both prefer the OTHER Quopus
    panel as the destination (same convention as ArchiveViewer).
    """

    def __init__(self, path, parent=None, *, reader=None):
        """Open a CBM disk dialog.

        Normal usage: pass `path` and a fresh CbmDiskReader is built
        for it. Internal usage: pass `reader=` (an already-opened
        reader, e.g. from from_lnx_prg below) to skip the open and
        reuse an in-memory image.

        `self.source_path` is always the file the user double-clicked
        on (for an LNX-PRG that's the .lnx.prg, not the in-memory
        .d64). Save operations use it as the directory anchor so the
        converted image lands next to its source.
        """
        super().__init__(parent)
        # Track the on-disk source separately from the displayed
        # name. `self.path` is what we show in the title; for direct
        # disk-image opens this is the actual file. For the LNX-PRG
        # path, the from_lnx_prg() classmethod passes the real file
        # path here even though the reader is in-memory.
        self.source_path = Path(path) if path is not None else None
        if reader is not None:
            self.reader = reader
            # Display name comes from the reader (e.g. 'game.d64'
            # for an LNX-PRG conversion). The window title shows
            # this so the user sees what kind of image is open.
            self.path = Path(reader.display_name)
            title_name = reader.display_name
        else:
            self.path = Path(path)
            title_name = self.path.name
            try:
                self.reader = CbmDiskReader(self.path)
                self.reader.open()
            except Exception as e:
                QMessageBox.warning(self, "CBM Disk",
                                      f"Failed to open {self.path.name}:\n{e}")
                self.reader = None
                return
        # Title indicates "[temp]" if the image lives only in memory
        # (LNX-converted) so the user knows it isn't on disk yet.
        suffix = "  [temp]" if self.reader.is_temp else ""
        self.setWindowTitle(f"CBM Disk: {title_name}{suffix}")
        self.resize(900, 700)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "cbm_disk")
        self.setModal(True)
        # Charset state for the directory rendering. 'upper' is the
        # CBM boot-time default (caps + graphics), and the only
        # charset where CBM directory art (T-Rex logos and friends)
        # renders with proper block graphics. The user can flip to
        # 'lower' (mixed case) via the [Aa] button on the toolbar
        # for files where the lowercase look is preferred.
        self._charset = 'upper'
        self._cell_size = 16
        self._build_layout()
        QShortcut(QKeySequence("Esc"), self, self.close)

    def closeEvent(self, ev):
        """Release the disk image's file handle when the dialog
        closes. Without this the underlying .d64/.d71/.d81 stays
        open for the lifetime of the dialog object, which on Windows
        means other programs (and the user) can't delete or move the
        file until Quopus exits. CbmDiskReader.close() is safe to
        call repeatedly and on in-memory (LNX/ZipCode) readers too.
        """
        try:
            if getattr(self, "reader", None) is not None:
                self.reader.close()
        except Exception:
            pass
        super().closeEvent(ev)

    @classmethod
    def from_lnx_prg(cls, path, parent=None) -> "CbmDiskDialog":
        """Open a .lnx.prg file: parse it as a Lynx archive, build a
        D64 image in memory, and show that. The dialog gains a
        "Save D64..." button so the user can persist the converted
        image to disk if they want it permanently.
        """
        path = Path(path)
        try:
            data = path.read_bytes()
            d64_bytes = lnx_to_d64_bytes(
                data, diskname=path.stem.upper()[:16] or "LYNX-DISK")
        except Exception as e:
            QMessageBox.warning(
                parent, "LNX -> D64",
                f"Could not convert {path.name} to a D64 image:\n{e}")
            return None
        # Build the reader on top of the in-memory bytes. Use a
        # display name that reflects the original .lnx.prg so the
        # window title makes sense.
        out_name = path.stem + ".d64"   # e.g. game.lnx -> game.lnx.d64
        reader = CbmDiskReader.from_bytes(d64_bytes, 'd64',
                                            display_name=out_name)
        reader.open()
        return cls(path=path, parent=parent, reader=reader)

    @classmethod
    def from_zipcode(cls, path, parent=None) -> "CbmDiskDialog":
        """Open one of the four ZipCode parts (1!FOO.prg, 2!FOO.prg,
        3!FOO.prg, 4!FOO.prg) - we locate the rest of the set,
        decode all four into a temporary D64 image in memory, and
        show that. The dialog gains a "Save D64" button so the user
        can persist the decoded image to disk.

        If any part is missing, displays an error and returns None.
        """
        path = Path(path)
        paths, missing, base = find_zipcode_set(path)
        if paths is None:
            QMessageBox.warning(
                parent, "ZipCode -> D64",
                f"ZipCode Files missing.\n\n"
                f"Could not find part(s) "
                f"{', '.join(str(m) + '!' for m in missing)}"
                f"{base or ''} in {path.parent}.\n\n"
                f"All four parts (1!, 2!, 3!, 4!) must be present "
                f"in the same directory to decode the disk image.")
            return None
        try:
            payloads = [p.read_bytes() for p in paths]
            d64_bytes = zipcode_to_d64_bytes(payloads)
        except Exception as e:
            QMessageBox.warning(
                parent, "ZipCode -> D64",
                f"Could not decode ZipCode set {base!r}:\n{e}")
            return None
        out_name = (base or path.stem) + ".d64"
        reader = CbmDiskReader.from_bytes(d64_bytes, 'd64',
                                            display_name=out_name)
        reader.open()
        # Use the FIRST part as the source path so Save D64 lands
        # in the same directory.
        return cls(path=paths[0], parent=parent, reader=reader)

    def _build_layout(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        # Top: interactive PETSCII directory. The user can click
        # any entry row directly to select it - the same as if
        # they'd clicked the corresponding row in the old list
        # widget. Selection is reflected with a translucent
        # highlight bar painted over the row.
        #
        # We keep the QListWidget around but HIDDEN by default
        # for two reasons:
        #   1. Accessibility - users who prefer keyboard tab
        #      navigation can toggle "Show file list" to get
        #      the ASCII grid back.
        #   2. Fallback - if the renderer fails on some exotic
        #      image format (zero entries, broken header etc.)
        #      the list still works.
        self._dir_pix = render_directory_to_pixmap(
            self.reader.dir_lines,
            cell_size=self._cell_size,
            charset=self._charset)
        self._dir_label = _InteractiveDirLabel(
            self._dir_pix,
            cell_size=self._cell_size,
            n_entries=len(self.reader.entries))
        self._dir_label.setStyleSheet("background-color: #3F3FD7;")
        self._dir_label.selection_changed.connect(
            self._on_label_selection_changed)
        self._dir_label.double_clicked_entry.connect(
            self._on_label_double_clicked)
        scroll = QScrollArea()
        scroll.setWidget(self._dir_label)
        scroll.setWidgetResizable(False)
        scroll.setStyleSheet("QScrollArea { border: 1px solid #888; }")

        # Right side: Preview pane. Renders the selected entry as:
        #   - PETSCII text (for SEQ files - they're almost always
        #     text dumps from Wordpro / SpeedScript / similar)
        #   - C64 bitmap graphic (for PRG files whose load address
        #     and size match a known graphics format - Koala,
        #     Art Studio, AAS, Doodle, FLI, etc.)
        #   - Friendly "no preview" placeholder otherwise
        # The split is a QSplitter so the user can drag the
        # divider to give more or less room to either side.

        # Header label above the preview area showing what
        # format was detected. Useful both as feedback ("yes,
        # that's a Koala") and as a diagnostic for guessed
        # formats ("Bitmap (guess)") where the user might want
        # to know we're not 100% sure.
        self._preview_format_label = QLabel("No file selected")
        self._preview_format_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._preview_format_label.setStyleSheet(
            "background-color: #2a2a2a; color: #ddd; "
            "border: 1px solid #444; padding: 4px; "
            "font-weight: bold;")
        self._preview_format_label.setMinimumHeight(24)

        # Clickable QLabel subclass - clicks on the preview
        # area pop up a fullscreen/maximized view of whatever
        # pixmap is currently shown. The original (unscaled)
        # pixmap is stashed in ._preview_original_pixmap by
        # the various render paths so the fullscreen view can
        # scale fresh from the source without quality loss.
        class _ClickablePreviewLabel(QLabel):
            def __init__(self, parent_dlg):
                super().__init__()
                self._dlg = parent_dlg
                # Pointer cursor as visual feedback that this
                # area is interactive. Set conditionally - we
                # only want the pointer when there's actually
                # a pixmap to expand.
                self._interactive = False
            def mousePressEvent(self, ev):
                if (ev.button() == Qt.MouseButton.LeftButton
                        and self._interactive):
                    self._dlg._open_fullscreen_preview()
                super().mousePressEvent(ev)

        self._preview_label = _ClickablePreviewLabel(self)
        self._preview_label.setText("Click a file to preview")
        self._preview_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setStyleSheet(
            "background-color: #1a1a1a; color: #888; "
            "border: 1px solid #444; padding: 8px;")
        self._preview_label.setMinimumWidth(330)
        self._preview_label.setWordWrap(True)
        # Tooltip hints at the click-to-expand behaviour. Only
        # shows when a pixmap is loaded (interactive=True).
        self._preview_original_pixmap = None
        self._preview_caption = ""
        # Wrap the preview in its own scrollarea so big bitmaps
        # (or long SEQ text dumps) don't blow up the dialog.
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidget(self._preview_label)
        self._preview_scroll.setWidgetResizable(False)
        self._preview_scroll.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._preview_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #888; }")

        # Pack format label + scroll-area into a vertical
        # container that the splitter treats as one pane.
        from PyQt6.QtWidgets import QSplitter, QWidget
        right_pane = QWidget()
        right_lay = QVBoxLayout(right_pane)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(2)
        right_lay.addWidget(self._preview_format_label)
        right_lay.addWidget(self._preview_scroll, stretch=1)

        # Button bar below the preview: lets the user export the
        # currently-shown bitmap as a PNG to anywhere on disk.
        # Enabled state is driven by _set_preview_pixmap (sets
        # _preview_original_pixmap) and _clear_preview (clears
        # it). For text-only previews (SEQ PETSCII transcripts,
        # hex peek) the button stays grey because there's
        # nothing image-shaped to save.
        from PyQt6.QtWidgets import QHBoxLayout, QPushButton
        prev_btn_row = QHBoxLayout()
        prev_btn_row.setContentsMargins(0, 0, 0, 0)
        prev_btn_row.setSpacing(4)
        self._btn_save_preview_png = QPushButton("Save as PNG...")
        self._btn_save_preview_png.setToolTip(
            "Export the currently-shown bitmap as a PNG file.\n"
            "Useful for sharing Koala / Hi-Res / FLI etc. "
            "previews without having to load the disk in a\n"
            "separate viewer.")
        self._btn_save_preview_png.clicked.connect(
            self._save_preview_as_png)
        self._btn_save_preview_png.setEnabled(False)
        prev_btn_row.addWidget(self._btn_save_preview_png)
        prev_btn_row.addStretch(1)
        right_lay.addLayout(prev_btn_row)

        # Horizontal splitter: directory on the left, preview on
        # the right. Initial sizes give the directory most of
        # the space; user can resize.
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(scroll)
        split.addWidget(right_pane)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([520, 360])
        outer.addWidget(split, stretch=4)

        # Optional ASCII fallback list. Hidden initially; the
        # user can toggle it with the "Show file list" checkbox
        # in the toolbar. Selection is two-way-synced with the
        # interactive label.
        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        from PyQt6.QtGui import QFont as _QFont
        list_font = _QFont("Topaz-8")
        list_font.setStyleHint(_QFont.StyleHint.TypeWriter)
        list_font.setFamilies(["Topaz-8", "Topaz",
                                  "Courier New", "Consolas",
                                  "DejaVu Sans Mono", "monospace"])
        list_font.setPointSize(10)
        self._list.setFont(list_font)
        for ent in self.reader.entries:
            name = ent.name_ascii
            if len(name) > 18:
                name = name[:18]
            txt = (f"{ent.blocks:>4}  {name:<18} "
                    f"{ent.type_label}{'<' if ent.locked else ' '}")
            it = QListWidgetItem(txt)
            it.setData(Qt.ItemDataRole.UserRole, ent)
            self._list.addItem(it)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.itemSelectionChanged.connect(
            self._on_list_selection_changed)
        # Hidden by default; toggleable via the "Show file list"
        # checkbox in the toolbar below.
        self._list.setVisible(False)
        outer.addWidget(self._list, stretch=2)
        # Flag used by the sync handlers to suppress re-firing
        # when we set selection programmatically from the other
        # widget's signal.
        self._syncing_selection = False

        # Bottom: action toolbar.
        bar = QHBoxLayout()
        b_all = QPushButton("Extract All...")
        b_all.clicked.connect(self._extract_all)
        bar.addWidget(b_all)
        b_sel = QPushButton("Extract Selected")
        b_sel.clicked.connect(self._extract_selected)
        bar.addWidget(b_sel)
        # ZipCode encoder: only meaningful for 35-track 1541 images
        # (the format is hardcoded for that geometry). For D71/D81/CMD
        # natives we hide the button - those formats are bigger than
        # ZipCode can represent. The bytes-source check covers both
        # plain .d64 files AND the in-memory D64 we get from LNX-PRG
        # conversion.
        if self.reader.kind == 'd64':
            b_zc = QPushButton("Save as ZipCode")
            b_zc.setToolTip("Write the four ZipCode files "
                              "(1!, 2!, 3!, 4!) next to the source")
            b_zc.clicked.connect(self._save_as_zipcode)
            bar.addWidget(b_zc)
        # Save button only present when the image is in-memory only
        # (LNX-converted). Persists the bytes to a user-chosen path.
        if self.reader.is_temp:
            b_save = QPushButton("Save D64")
            b_save.setToolTip("Save the converted D64 image to disk")
            b_save.clicked.connect(self._save_temp_d64)
            bar.addWidget(b_save)
        b_charset = QPushButton("Aa")
        b_charset.setFixedWidth(34)
        b_charset.setToolTip("Toggle upper/lower character set")
        b_charset.clicked.connect(self._toggle_charset)
        bar.addWidget(b_charset)
        b_zoom_in = QPushButton("+")
        b_zoom_in.setFixedWidth(30)
        b_zoom_in.setToolTip("Zoom in directory rendering")
        b_zoom_in.clicked.connect(lambda: self._zoom(+4))
        bar.addWidget(b_zoom_in)
        b_zoom_out = QPushButton("-")
        b_zoom_out.setFixedWidth(30)
        b_zoom_out.setToolTip("Zoom out directory rendering")
        b_zoom_out.clicked.connect(lambda: self._zoom(-4))
        bar.addWidget(b_zoom_out)
        # Edit buttons - only available for the formats whose
        # mutation paths we've implemented (D64/D71/D81). The CMD
        # native + LNX paths are read-only.
        if self.reader.kind in ('d64', 'd71', 'd81'):
            b_rename = QPushButton("Edit")
            b_rename.setToolTip(
                "Edit the selected entry's name, type, lock flag, "
                "splat flag, and block count.")
            b_rename.clicked.connect(self._rename_selected)
            bar.addWidget(b_rename)
            b_delete = QPushButton("Delete")
            b_delete.setToolTip(
                "Scratch (delete) the selected entries from the "
                "image. Frees their data sectors in the BAM.")
            b_delete.clicked.connect(self._delete_selected)
            bar.addWidget(b_delete)
            b_type = QPushButton("Type")
            b_type.setToolTip(
                "Change the file type (DEL/SEQ/PRG/USR/REL) of the "
                "selected entry. Quick path - the same is also "
                "available via Edit.")
            b_type.clicked.connect(self._change_type_selected)
            bar.addWidget(b_type)
            b_lock = QPushButton("Lock")
            b_lock.setToolTip(
                "Toggle the locked flag '<' of the selected entry. "
                "Locked files refuse SCRATCH on a real 1541.")
            b_lock.clicked.connect(self._toggle_lock_selected)
            bar.addWidget(b_lock)
            b_splat = QPushButton("Splat")
            b_splat.setToolTip(
                "Toggle the splat marker '*' of the selected entry. "
                "Splat = file is shown as unclosed.")
            b_splat.clicked.connect(self._toggle_splat_selected)
            bar.addWidget(b_splat)
            b_sep = QPushButton("Sep+")
            b_sep.setToolTip(
                "Insert a separator entry (DEL< type) into the "
                "directory with PETSCII graphics + live preview.")
            b_sep.clicked.connect(self._insert_separator)
            bar.addWidget(b_sep)
            b_bfree = QPushButton("BFree")
            b_bfree.setToolTip(
                "Set the disk-wide BLOCKS FREE count cosmetically "
                "(0 = full, 666 = cracker classic).")
            b_bfree.clicked.connect(self._set_blocks_free_dialog)
            bar.addWidget(b_bfree)
            b_val = QPushButton("Validate")
            b_val.setToolTip(
                "Walk every file's sector chain and check for "
                "unallocated / orphaned sectors. Equivalent of "
                "the 1541 DOS VALIDATE command.")
            b_val.clicked.connect(self._validate_image)
            bar.addWidget(b_val)
        # Optional show/hide for the ASCII file list. Off by
        # default - the interactive PETSCII directory above is
        # the primary selection UI. Users who prefer the older
        # behaviour or who want to verify filenames in ASCII
        # can toggle this back on.
        from PyQt6.QtWidgets import QCheckBox as _QChk
        self._chk_show_list = _QChk("Show file list")
        self._chk_show_list.setToolTip(
            "Show / hide the ASCII file list beneath the PETSCII\n"
            "directory rendering. Selection stays in sync between\n"
            "the two views. Off by default because you can now\n"
            "click rows directly in the rendered directory above.")
        self._chk_show_list.setChecked(False)
        self._chk_show_list.toggled.connect(
            self._on_toggle_show_list)
        bar.addWidget(self._chk_show_list)
        bar.addStretch(1)
        b_close = QPushButton("Close (Esc)")
        b_close.clicked.connect(self.close)
        bar.addWidget(b_close)
        outer.addLayout(bar)

        # Status line shows how many files we parsed + image kind.
        n = len(self.reader.entries)
        kind_label = self.reader.kind.upper()
        if self.reader.is_temp:
            kind_label += " (in memory)"
        self._status = QLabel(
            f"  {n} file(s)   ·   {kind_label}   ·   "
            f"{self.reader.blocks_free} blocks free   ·   "
            f"charset: {self._charset}")
        outer.addWidget(self._status)

    # ---- Selection sync between PETSCII label and list widget -----
    def _on_label_selection_changed(self, indices):
        """Selection happened in the PETSCII label. Mirror it into
        the (possibly-hidden) ASCII list so other code paths that
        still read self._list.selectedItems() see the same set."""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            self._list.clearSelection()
            for idx in indices:
                if 0 <= idx < self._list.count():
                    self._list.item(idx).setSelected(True)
        finally:
            self._syncing_selection = False
        # Refresh preview pane to match the new selection. Skip
        # for multi-select - the pane shows one file at a time
        # and rendering the "first of many" would mislead.
        self._maybe_update_preview(indices)

    def _on_list_selection_changed(self):
        """Selection happened in the ASCII list. Mirror it back
        to the PETSCII label so the highlighted rows on the
        rendered directory match what's selected in the list."""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            indices = [self._list.row(it)
                       for it in self._list.selectedItems()]
            self._dir_label.set_selected_indices(indices)
        finally:
            self._syncing_selection = False
        self._maybe_update_preview(indices)

    def _maybe_update_preview(self, indices):
        """Trigger preview if exactly one entry is selected,
        or try logo+charset rendering when exactly two are
        selected. Empty selection or 3+ selected clears the
        pane back to the placeholder.

        Two-file rendering is a BBS/demoscene convention:
        a custom charset (typically 2048 bytes = 256 chars *
        8 bytes) is loaded alongside a small screen-data file
        (typically 200-1000 bytes of screencodes). The smaller
        file is the screen, the bigger one is the charset.
        """
        if not hasattr(self, "_preview_label"):
            return  # called before _build_layout, ignore
        if len(indices) == 1:
            idx = indices[0]
            if 0 <= idx < len(self.reader.entries):
                self._update_preview(self.reader.entries[idx])
                return
        if len(indices) == 2:
            # Try logo+charset pairing
            valid = [i for i in indices
                      if 0 <= i < len(self.reader.entries)]
            if len(valid) == 2:
                ents = [self.reader.entries[i] for i in valid]
                if self._try_render_logo_charset(ents):
                    return
            # Fall through to multi-select placeholder if pair
            # didn't match the logo+charset heuristic
        # 0 or 3+ selected (or pair didn't qualify) - reset to
        # default state
        self._preview_label.setPixmap(QPixmap())
        if len(indices) == 0:
            self._set_format_label("No file selected")
            self._preview_label.setText(
                "Click a file to preview")
        else:
            self._set_format_label(
                f"{len(indices)} files selected")
            if len(indices) == 2:
                self._preview_label.setText(
                    "Two files selected - to render as "
                    "logo+charset, one should be ~2KB (charset) "
                    "and the other smaller (screen data).\n\n"
                    "Select one file to preview that file alone."
                )
            else:
                self._preview_label.setText(
                    "Select one file to preview\n"
                    "(or two files for logo+charset rendering)")
        self._preview_label.adjustSize()

    def _try_render_logo_charset(self, entries) -> bool:
        """Attempt to render two files as a screen + custom
        charset combo. Returns True on success, False if the
        sizes don't look like a charset + screen pair so the
        caller can fall back to the multi-select placeholder.

        Heuristic:
          - Bigger file in [1024..3000] bytes -> custom charset
            (a real C64 charset is 2048 bytes for 256 chars, but
            partial sets of 128/256 chars at 1024/2048 are both
            common in BBS art)
          - Smaller file in [40..4000] bytes -> screen data
            (just screencodes, possibly with embedded color RAM)

        Charset is laid out as 8 bytes per char (top row first,
        each bit = 1 pixel left-to-right). Screen is just an
        array of char codes (0..255) read row-major at 40 chars
        per line.
        """
        try:
            data_a = self.reader.extract(entries[0])
            data_b = self.reader.extract(entries[1])
        except Exception:
            return False
        if not data_a or not data_b:
            return False
        # Strip PRG load addresses if both look like PRGs
        body_a = data_a[2:] if len(data_a) > 2 else data_a
        body_b = data_b[2:] if len(data_b) > 2 else data_b
        # Decide which is which based on size
        if len(body_a) >= len(body_b):
            charset_body = body_a
            charset_ent = entries[0]
            screen_body = body_b
            screen_ent = entries[1]
        else:
            charset_body = body_b
            charset_ent = entries[1]
            screen_body = body_a
            screen_ent = entries[0]
        # Sanity checks - charset must be at least 1024 (128
        # chars), screen must be at least 40 bytes (one line)
        # and not bigger than 4000 (BBS logos rarely exceed
        # full 40x25 = 1000 plus color RAM)
        if not (1024 <= len(charset_body) <= 4096):
            return False
        if not (40 <= len(screen_body) <= 4000):
            return False
        # Render and show. The renderer returns (preview, src)
        # where preview is 2x-scaled for the inline pane and
        # src is the 1x original we hand the fullscreen viewer
        # so it can do its own clean nearest-neighbor scale-up.
        try:
            preview_pix, src_pix = (
                self._render_logo_with_charset(
                    screen_body, charset_body))
        except Exception as e:
            self._set_preview_text(
                f"Logo render failed:\n{e}")
            self._set_format_label("Logo+Charset render error")
            return True  # treated as 'handled' so we don't fall through
        if preview_pix.isNull():
            return False
        self._reset_preview_style()
        # Format label
        n_chars = min(256, len(charset_body) // 8)
        # Try to figure out the screen dimensions for the label
        screen_cells = len(screen_body)
        # Most BBS logos are 40 wide, height = ceil(cells/40)
        height = (screen_cells + 39) // 40
        caption = (f"Logo + Charset: {screen_ent.name_ascii} "
                    f"+ {charset_ent.name_ascii} (40x{height})")
        self._set_preview_pixmap(preview_pix, caption=caption)
        # Replace the auto-stashed scaled pixmap with the
        # unscaled source so the fullscreen viewer can blow it
        # up cleanly.
        self._preview_original_pixmap = src_pix
        self._set_format_label(
            f"Logo + Charset  -  "
            f"{screen_ent.name_ascii} ({screen_cells} chars) "
            f"+ {charset_ent.name_ascii} ({n_chars} char set)  "
            f"-  rendered as 40x{height}")
        return True

    def _render_logo_with_charset(self, screen_data: bytes,
                                     charset_data: bytes):
        """Render a screen + custom charset combo to two pixmaps:
        (preview_pix, src_pix). preview_pix is 2x-scaled for the
        inline preview pane; src_pix is the unscaled source for
        the fullscreen viewer's own scaling.

        screen_data: array of screencodes, 40 per line.
        charset_data: 8 bytes per char, top row first; each bit
                      = 1 pixel left-to-right (MSB = leftmost).

        Returns 40-cell-wide, ceil(len/40)-cell-tall image at
        2x zoom. Default colors are white-on-black to match the
        most common BBS-logo color scheme. We don't have color
        RAM info from this two-file setup, so per-cell color is
        not possible - all cells get the same fg/bg.
        """
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import Qt
        # Layout
        cells_per_line = 40
        cells = list(screen_data)
        n_lines = (len(cells) + cells_per_line - 1) // cells_per_line
        if n_lines == 0:
            return QPixmap()
        # Pad incomplete final line with spaces ($20 = screen
        # code for space)
        padded = cells + [0x20] * (
            n_lines * cells_per_line - len(cells))
        # Build a flat 8-bit pixel buffer
        px_w = cells_per_line * 8  # 320
        px_h = n_lines * 8
        buf = bytearray(px_w * px_h)
        # Char count in the charset (typically 128 or 256)
        n_chars = len(charset_data) // 8
        for cy in range(n_lines):
            for cx in range(cells_per_line):
                code = padded[cy * cells_per_line + cx]
                if code >= n_chars:
                    # Code outside the supplied charset - draw
                    # a solid block as visual feedback
                    glyph_bytes = b'\xff' * 8
                else:
                    glyph_bytes = charset_data[
                        code * 8:code * 8 + 8]
                # Each of the 8 bytes is one pixel row
                for row in range(8):
                    rb = glyph_bytes[row] if row < len(
                        glyph_bytes) else 0
                    y = cy * 8 + row
                    base = y * px_w + cx * 8
                    for col in range(8):
                        # MSB first - bit 7 is leftmost pixel
                        if rb & (0x80 >> col):
                            buf[base + col] = 1
        # Build a 2-color QImage. Index 0 = bg (black),
        # index 1 = fg (light blue/white feel)
        ct = [
            (0xFF << 24) | 0x000000,  # bg: black
            (0xFF << 24) | 0xFFFFFF,  # fg: white
        ]
        img = QImage(bytes(buf), px_w, px_h, px_w,
                      QImage.Format.Format_Indexed8)
        img.setColorTable(ct)
        img = img.convertToFormat(QImage.Format.Format_RGB888)
        # Return TWO pixmaps: the 1x unscaled source (for the
        # fullscreen viewer to scale up cleanly later) and a 2x
        # preview-sized version so 8x8 cells become 16x16 in
        # the side pane - that's the sweet spot for readability
        # without dominating the dialog. Caller picks whichever
        # it needs.
        src_pix = QPixmap.fromImage(img)
        preview_pix = src_pix.scaled(
            px_w * 2, px_h * 2,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        return preview_pix, src_pix

    def _on_label_double_clicked(self, idx):
        """Double-click on a rendered entry row - same effect as
        double-clicking the corresponding row in the ASCII list."""
        if 0 <= idx < self._list.count():
            it = self._list.item(idx)
            # Make sure the selection lines up first (some down-
            # stream code reads _list selection rather than the
            # double-clicked item)
            self._list.clearSelection()
            it.setSelected(True)
            self._on_double_click(it)

    def _on_toggle_show_list(self, checked: bool):
        """Show / hide the ASCII fallback list."""
        self._list.setVisible(bool(checked))

    # ---- Edit operations ---------------------------------------------
    def _selected_entries(self):
        """Return the list of CbmDirEntry objects the user has
        selected. Reads from the interactive PETSCII label, which
        is the primary selection UI now; falls back to the list
        widget if the label hasn't been built yet (defensive)."""
        out = []
        if hasattr(self, "_dir_label") and isinstance(
                self._dir_label, _InteractiveDirLabel):
            for idx in self._dir_label.selected_indices():
                if 0 <= idx < len(self.reader.entries):
                    out.append(self.reader.entries[idx])
            if out:
                return out
        # Fallback: read from the list widget (used if the label
        # selection is empty or the label isn't an interactive one
        # for some reason).
        for it in self._list.selectedItems():
            ent = it.data(Qt.ItemDataRole.UserRole)
            if ent is not None:
                out.append(ent)
        return out

    def _refresh_after_mutation(self):
        """Common cleanup after rename / delete / insert. Re-reads
        the directory, rebuilds the rendered pixmap + the file
        picker list, and updates the status line."""
        try:
            self.reader.refresh()
        except Exception as e:
            QMessageBox.warning(
                self, "Refresh failed",
                f"Could not re-read directory:\n{e}")
            return
        # Rebuild file picker
        self._list.clear()
        for ent in self.reader.entries:
            name = ent.name_ascii
            if len(name) > 18:
                name = name[:18]
            txt = (f"{ent.blocks:>4}  {name:<18} "
                    f"{ent.type_label}{'<' if ent.locked else ' '}")
            it = QListWidgetItem(txt)
            it.setData(Qt.ItemDataRole.UserRole, ent)
            self._list.addItem(it)
        # Rebuild PETSCII pixmap
        self._rerender()
        # Update status line
        n = len(self.reader.entries)
        kind_label = self.reader.kind.upper()
        if self.reader.is_temp:
            kind_label += " (in memory)"
        if hasattr(self, '_status'):
            self._status.setText(
                f"  {n} file(s)   ·   {kind_label}   ·   "
                f"{self.reader.blocks_free} blocks free   ·   "
                f"charset: {self._charset}")

    def _change_type_selected(self):
        """Quick path for changing file type without opening the
        full edit dialog. Pops a small ComboBox dialog."""
        sel = self._selected_entries()
        if not sel:
            QMessageBox.information(self, "Type",
                "Select a file in the list above first.")
            return
        entry = sel[0]
        types = ['DEL', 'SEQ', 'PRG', 'USR', 'REL']
        new_label, ok = QInputDialog.getItem(
            self, "Change file type",
            f"New type for {entry.name_ascii!r}\n"
            f"(currently {entry.type_label}):",
            types, current=entry.type_code, editable=False)
        if not ok:
            return
        new_code = types.index(new_label)
        if new_code == entry.type_code:
            return
        try:
            self.reader.set_file_type(entry, new_code)
        except Exception as e:
            QMessageBox.warning(self, "Type failed", f"{e}")
            return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _toggle_lock_selected(self):
        """Toggle the locked flag for every selected entry. Multi-
        select supported; if the selection is mixed (some locked,
        some not), all become locked."""
        sel = self._selected_entries()
        if not sel:
            QMessageBox.information(self, "Lock",
                "Select one or more files first.")
            return
        # If ANY are unlocked, lock them all. Otherwise unlock all.
        any_unlocked = any(not e.locked for e in sel)
        target = any_unlocked
        for entry in sel:
            try:
                self.reader.set_locked(entry, target)
            except Exception as e:
                QMessageBox.warning(self, "Lock failed",
                    f"{entry.name_ascii}: {e}")
                return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _toggle_splat_selected(self):
        """Toggle the splat marker for every selected entry."""
        sel = self._selected_entries()
        if not sel:
            QMessageBox.information(self, "Splat",
                "Select one or more files first.")
            return
        any_normal = any(not e.splat for e in sel)
        target = any_normal
        for entry in sel:
            try:
                self.reader.set_splat(entry, target)
            except Exception as e:
                QMessageBox.warning(self, "Splat failed",
                    f"{entry.name_ascii}: {e}")
                return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _rename_selected(self):
        """Open a multi-field edit dialog for the selected entry.

        Edits, all in one dialog: filename (PETSCII), file type
        (DEL/SEQ/PRG/USR/REL), lock flag, splat flag, block count
        (cosmetic). Each field is independent - changing one
        doesn't reset the others. Cancel rolls back nothing
        (mutations are atomic per OK click).
        """
        sel = self._selected_entries()
        if not sel:
            QMessageBox.information(self, "Edit",
                "Select a file in the list above first.")
            return
        if len(sel) > 1:
            QMessageBox.information(self, "Edit",
                "Edit works on one file at a time. "
                "Pick exactly one entry.")
            return
        entry = sel[0]
        dlg = _CbmEntryEditDialog(entry, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.result()
        try:
            # Apply each changed field. Order matters: rename first
            # so the slot identity stays clear; other writes go to
            # the same slot regardless.
            if result['name_petscii'] != entry.name_petscii:
                self.reader.rename_entry(entry, result['name_petscii'])
            if result['type_code'] != entry.type_code:
                self.reader.set_file_type(entry, result['type_code'])
            if result['locked'] != entry.locked:
                self.reader.set_locked(entry, result['locked'])
            if result['splat'] != entry.splat:
                self.reader.set_splat(entry, result['splat'])
            if result['blocks'] != entry.blocks:
                self.reader.set_block_count(entry, result['blocks'])
        except Exception as e:
            QMessageBox.warning(
                self, "Edit failed", f"{e}")
            return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _delete_selected(self):
        """Confirm + scratch every selected entry. Confirmation
        dialog lists the names so the user can sanity-check what's
        about to vanish."""
        sel = self._selected_entries()
        if not sel:
            QMessageBox.information(self, "Delete",
                "Select one or more files in the list above first.")
            return
        names = "\n".join(f"  • {e.name_ascii}  ({e.type_label})"
                            for e in sel)
        ans = QMessageBox.question(
            self, "Confirm delete",
            f"Delete {len(sel)} file(s) from the disk image?\n\n"
            f"{names}\n\n"
            f"This frees their sectors in the BAM. There is no "
            f"undo button.",
            QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        # Delete in reverse order of dir slot so higher-slot deletes
        # don't shift the indices we'd need for the rest. (In our
        # implementation slots aren't compacted on delete, but
        # being defensive is cheap.)
        sel_sorted = sorted(
            sel,
            key=lambda e: (e.dir_track, e.dir_sector, e.dir_slot),
            reverse=True)
        errs = []
        for ent in sel_sorted:
            try:
                self.reader.delete_entry(ent)
            except Exception as e:
                errs.append(f"{ent.name_ascii}: {e}")
        if errs:
            QMessageBox.warning(
                self, "Some deletions failed",
                "\n".join(errs))
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _insert_separator(self):
        """Open the separator-editor dialog with a PETSCII graphics
        picker and live preview of the rendered separator line.
        On OK, inserts the typed bytes as a closed-DEL entry.

        Multi-row PNG imports return multiple 16-byte rows via
        result_petscii_rows(); we insert them one after another so a
        single Apply produces a stack of DirArt separator entries.
        """
        dlg = _CbmSeparatorEditorDialog(parent=self,
                                          charset=self._charset)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # Prefer the multi-row API; fall back to legacy single-row
        if hasattr(dlg, "result_petscii_rows"):
            rows = dlg.result_petscii_rows()
        else:
            rows = [dlg.result_petscii()]
        if not rows or not rows[0]:
            return
        n_inserted = 0
        n_skipped = 0
        first_err = None
        for sep_bytes in rows:
            if not sep_bytes:
                continue
            try:
                self.reader.insert_separator_raw(sep_bytes)
                n_inserted += 1
            except Exception as e:
                if first_err is None:
                    first_err = str(e)
                n_skipped += 1
                # Stop after the first failure - typically means the
                # directory is full so further inserts won't help.
                break
        if n_inserted == 0:
            QMessageBox.warning(
                self, "Insert failed",
                f"Could not insert separator:\n{first_err or '?'}")
            return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()
        if n_skipped > 0:
            QMessageBox.warning(
                self, "Partial insert",
                f"Inserted {n_inserted} of {n_inserted + n_skipped} "
                f"separator rows. The remaining rows did not fit:\n"
                f"{first_err}")

    def _set_blocks_free_dialog(self):
        """Pop a dialog asking for a new disk-wide blocks-free
        value, then apply it. The value is shown verbatim in the
        directory listing's 'N BLOCKS FREE.' line; it does NOT
        reflect actual sector usage. Used for cracker-intro style
        '0 BLOCKS FREE.' or '666 BLOCKS FREE.'.
        """
        cur = self.reader.blocks_free
        new_val, ok = QInputDialog.getInt(
            self, "Set blocks free",
            f"New disk-wide blocks-free count\n"
            f"(current: {cur} - 0 = full, 664 = empty 1541 disk,\n"
            f" or any cosmetic value like 666):",
            value=cur, min=0, max=8670)
        if not ok:
            return
        try:
            self.reader.set_disk_blocks_free(new_val)
        except Exception as e:
            QMessageBox.warning(
                self, "Set failed", f"{e}")
            return
        self._refresh_after_mutation()
        self._save_image_if_file_backed()

    def _validate_image(self):
        """Run a read-only validate first. If issues are found, ask
        the user whether to actually fix them. Mirrors the 1541's
        VALIDATE: 'I' command (preview = nothing) followed by
        'V' (commit fixes)."""
        try:
            report = self.reader.validate(fix=False)
        except Exception as e:
            QMessageBox.warning(
                self, "Validate failed", f"{e}")
            return
        ua = len(report['unallocated'])
        orph = len(report['orphaned'])
        errs = report['errors']
        if not ua and not orph and not errs:
            QMessageBox.information(
                self, "Validate",
                "Image is clean: every directory file's sectors "
                "are allocated and no orphaned BAM entries exist.")
            return
        # Build summary
        lines = []
        if ua:
            lines.append(
                f"• {ua} sector(s) used by files but marked free "
                f"in the BAM")
        if orph:
            lines.append(
                f"• {orph} sector(s) marked allocated but unreachable "
                f"from any file")
        if errs:
            lines.append("• Walking errors:")
            for e in errs[:10]:
                lines.append(f"    {e}")
        summary = "\n".join(lines)
        ans = QMessageBox.question(
            self, "Validate report",
            f"Validate found:\n\n{summary}\n\n"
            f"Fix the BAM?",
            QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        report = self.reader.validate(fix=True)
        self._refresh_after_mutation()
        QMessageBox.information(
            self, "Validate",
            f"Fixed {report['fixed']} BAM entries.")
        self._save_image_if_file_backed()

    def _save_image_if_file_backed(self):
        """For in-memory images we just hold the mutated bytes in
        self.reader._temp_bytes; the user has to hit 'Save D64' to
        get them onto disk. For file-backed images we already wrote
        through to disk on every _write_sector call, so nothing to
        do here. We keep this hook for future formats where commit
        is more involved."""
        # File-backed: no-op (already on disk).
        # In-memory: do nothing - user keeps editing, presses Save
        # D64 button (already in toolbar) when ready to persist.
        pass

    # ---- charset toggle ----
    def _toggle_charset(self):
        """Flip between upper and lower charset rendering. Lower is
        the default (mixed-case) and matches DirMaster's default;
        upper is the C64 boot-time CAPS+graphics mode."""
        self._charset = 'upper' if self._charset == 'lower' else 'lower'
        self._rerender()

    def _rerender(self):
        """Repaint the directory pixmap using current charset and
        cell size. Called from charset toggle and zoom buttons."""
        self._dir_pix = render_directory_to_pixmap(
            self.reader.dir_lines,
            cell_size=self._cell_size,
            charset=self._charset)
        self._dir_label.setPixmap(self._dir_pix)
        self._dir_label.resize(self._dir_pix.size())
        # Keep the interactive label's geometry hints in sync
        # with the freshly-rendered pixmap. Without this, after a
        # zoom in/out the click-to-entry math would be off because
        # _cell_size in the label still points at the old size,
        # and after an insert / delete the row count would be
        # wrong so clicks on the last few entries would miss.
        if isinstance(self._dir_label, _InteractiveDirLabel):
            self._dir_label._cell_size = self._cell_size
            self._dir_label._n_entries = len(
                self.reader.entries)
            # Clamp any out-of-range selection - if rows were
            # removed by a delete operation, the selected set
            # might still reference indices that no longer exist.
            keep = {i for i in self._dir_label._selected
                    if i < len(self.reader.entries)}
            if keep != self._dir_label._selected:
                self._dir_label._selected = keep
                self._dir_label.selection_changed.emit(
                    sorted(keep))
            self._dir_label.update()
        # Update status charset text in-place.
        n = len(self.reader.entries)
        kind_label = self.reader.kind.upper()
        if self.reader.is_temp:
            kind_label += " (in memory)"
        self._status.setText(
            f"  {n} file(s)   ·   {kind_label}   ·   "
            f"{self.reader.blocks_free} blocks free   ·   "
            f"charset: {self._charset}")

    # ---- zoom: re-render the pixmap at a new cell size ----
    def _zoom(self, delta: int):
        self._cell_size = max(8, min(48, self._cell_size + delta))
        self._rerender()

    # ---- save the temp D64 to disk ----
    def _save_temp_d64(self):
        """Persist an in-memory (LNX-converted) D64 image to disk.

        Saves silently next to the original .lnx.prg file with the
        '.d64' suffix - no file picker, no overwrite prompt. Same
        pattern as the other one-shot save routines in Quopus
        (extract-to-other-panel, archive-extract, etc.): predictable
        target, status-line feedback. If the target already exists
        we still overwrite, which matches the user's intent (re-
        running the conversion always produces the same output).
        """
        if not self.reader.is_temp or self.reader._temp_bytes is None:
            return
        if self.source_path is None:
            QMessageBox.warning(self, "Save D64",
                                  "Cannot determine where to save - "
                                  "no source file path is known.")
            return
        # Target = same directory as the original source file, with
        # the format-specific naming junk stripped:
        #   foo.lnx.prg     -> foo.d64    (LNX-PRG conversion)
        #   1!gamename.prg  -> gamename.d64    (ZipCode set)
        #   1!gamename      -> gamename.d64    (ZipCode without .prg)
        # If neither rule matches we still produce a sensible name
        # by stripping the final extension and adding .d64.
        src = self.source_path
        stem = src.stem        # 'foo.lnx.prg' -> 'foo.lnx'
        if stem.lower().endswith('.lnx'):
            stem = stem[:-4]
        # ZipCode parts: filenames look like '1!base' / '2!base' etc.
        # The base name was already stripped of the '.prg' by .stem
        # above; we still need to drop the leading 'N!' prefix that
        # find_zipcode_set used to locate the four parts. Without
        # this, saving the converted disk would produce '1!foo.d64'
        # which is misleading - the .d64 isn't a ZipCode part itself.
        import re as _re
        m = _re.match(r'^[1-4]!(.+)$', stem)
        if m:
            stem = m.group(1)
        # Trim trailing whitespace - some BBS-uploaded ZipCode parts
        # have trailing spaces in their names.
        stem = stem.strip()
        if not stem:
            stem = "disk"    # last-resort fallback
        out = src.parent / (stem + ".d64")
        try:
            out.write_bytes(self.reader._temp_bytes)
        except Exception as e:
            QMessageBox.warning(self, "Save D64",
                                  f"Could not write {out}:\n{e}")
            return
        self._status.setText(f"  Saved D64 to {out.name}")
        # Refresh the panel that holds the source file so the new
        # .d64 shows up immediately. Also try the other panel in
        # case the user is browsing from there.
        self._refresh_source_panel()
        self._refresh_other_panel()

    def _refresh_source_panel(self):
        """Refresh whichever Quopus panel currently shows the source
        directory. Best-effort: silent if not running inside Quopus."""
        try:
            mw = self.parent().window() if self.parent() else None
            if mw is None: return
            for attr in ('left_lister', 'right_lister'):
                lister = getattr(mw, attr, None)
                if lister is None: continue
                try:
                    if Path(lister.current_path) == self.source_path.parent:
                        lister.refresh()
                except Exception:
                    pass
        except Exception:
            pass

    # ---- save as ZipCode: write the 4 1!/2!/3!/4! files ----
    def _save_as_zipcode(self):
        """Convert the open D64 image to ZipCode and write the four
        files (1!NAME, 2!NAME, 3!NAME, 4!NAME) next to the source.
        Same no-picker, overwrite-silently convention as Save D64.

        Source bytes come from either:
          - The reader's in-memory buffer (LNX-PRG conversion).
          - The on-disk .d64 file (everything else - we re-read it).

        The base name for the four output files is derived from
        whichever source is available, with extensions stripped:
          foo.d64           -> 1!FOO, 2!FOO, 3!FOO, 4!FOO
          tra123.lnx.prg    -> 1!TRA123, 2!TRA123, ...
        """
        if not self.reader or self.reader.kind != 'd64':
            QMessageBox.information(
                self, "Save as ZipCode",
                "ZipCode encoding only works on 35-track .d64 images.")
            return
        # Get the raw 174848-byte image. For temp readers it sits in
        # the reader's buffer; for on-disk D64s we just read the
        # file again (cheap - 170 KB).
        if self.reader._temp_bytes is not None:
            d64_bytes = self.reader._temp_bytes
        elif self.source_path is not None and self.source_path.exists():
            try:
                d64_bytes = self.source_path.read_bytes()
            except Exception as e:
                QMessageBox.warning(self, "Save as ZipCode",
                                      f"Could not read source D64:\n{e}")
                return
        else:
            QMessageBox.warning(self, "Save as ZipCode",
                                  "No source D64 available.")
            return

        # Decide the output directory + base name. Source path wins
        # when it exists on disk; otherwise we fall back to the
        # reader's display name.
        if self.source_path is not None and self.source_path.exists():
            anchor = self.source_path
            stem = anchor.stem
            if stem.lower().endswith('.lnx'):
                stem = stem[:-4]
            out_dir = anchor.parent
        else:
            stem = Path(self.reader.display_name).stem
            if stem.lower().endswith('.lnx'):
                stem = stem[:-4]
            out_dir = Path.cwd()

        # Run the encoder. Output filenames are uppercased ('1!FOO')
        # to match historical ZipCode tradition.
        try:
            files = d64_to_zipcode_files(d64_bytes, base_name=stem)
        except Exception as e:
            QMessageBox.warning(self, "Save as ZipCode",
                                  f"Encoding failed:\n{e}")
            return

        # Write all four files. We accumulate the actual filenames
        # written so the status line can report something meaningful
        # if the user asked for a base name with weird characters.
        written = []
        try:
            for fname, payload in files:
                out_path = out_dir / fname
                out_path.write_bytes(payload)
                written.append(out_path.name)
        except Exception as e:
            QMessageBox.warning(self, "Save as ZipCode",
                                  f"Could not write {fname}:\n{e}")
            return

        sizes = [len(p) for _, p in files]
        total = sum(sizes)
        self._status.setText(
            f"  Wrote {len(written)} ZipCode files in {out_dir.name}/  "
            f"({total} bytes total)")
        self._refresh_source_panel()
        self._refresh_other_panel()

    # ---- double-click in list = single-file extract ----
    # ===================================================================
    # File preview pane
    # ===================================================================

    def _update_preview(self, ent: "CbmDirEntry"):
        """Try to render a preview for the selected entry into
        self._preview_label. Dispatches based on the file type
        and (for PRG) the load-address + size signature.

        Three outcomes:
          - SEQ / USR: render as PETSCII text (most SEQ files
            are ASCII/PETSCII text dumps)
          - PRG that looks like a C64 bitmap: render via the
            retro_gfx_decoders pipeline
          - Anything else: friendly "no preview available"
            message with a quick hex header for the curious

        Errors are swallowed and reported in the preview area
        as text, so a malformed file never crashes the dialog.
        """
        if not hasattr(self, "_preview_label"):
            return
        try:
            data = self.reader.extract(ent)
        except Exception as e:
            self._set_format_label("Read error")
            self._set_preview_text(
                f"Could not read '{ent.name_ascii}':\n{e}")
            return
        if not data:
            self._set_format_label("Empty")
            self._set_preview_text(
                f"'{ent.name_ascii}' is empty.")
            return
        tlabel = (ent.type_label or "").upper()
        # SEQ / USR: text dump. The first 2 bytes of PRG are the
        # load address but SEQ/USR don't have that - the whole
        # file is content. Render the PETSCII as a pixmap so the
        # graphics characters look right (regular fonts can't
        # render most PETSCII glyphs).
        if tlabel in ("SEQ", "USR"):
            self._set_format_label(
                f"{tlabel}  -  PETSCII text "
                f"({len(data)} bytes)")
            self._render_seq_petscii(ent, data)
            return
        # PRG: try graphics detection. If that fails, fall back
        # to "no preview" with a hex peek so the user can spot
        # SID files / charsets / sprites by their bytes.
        if tlabel == "PRG":
            ok = self._render_prg_graphics(ent, data)
            if ok:
                return
            # _render_prg_graphics handles the label only on
            # success - on failure we set it here for the hex
            # peek view.
            load = data[0] | (data[1] << 8) if len(data) >= 2 \
                else 0
            self._set_format_label(
                f"PRG  -  Load ${load:04X}  "
                f"({len(data)} bytes)  -  no graphics preview")
            self._render_prg_hex_peek(ent, data)
            return
        # DEL / REL / unknown - no preview pipeline yet.
        self._set_format_label(f"{tlabel}  -  no preview")
        self._set_preview_text(
            f"{ent.name_ascii} ({tlabel})\n\n"
            f"No preview for this file type.")

    def _set_format_label(self, text: str):
        """Update the format-detection label above the preview
        pane. Single source of truth for what's shown there so
        every render path can drop the right text."""
        if hasattr(self, "_preview_format_label"):
            self._preview_format_label.setText(text)

    def _set_preview_text(self, text: str):
        """Drop a plain text message into the preview pane and
        clear any pixmap. Used for error states and 'nothing to
        preview' notices."""
        self._reset_preview_style()
        self._preview_label.setPixmap(QPixmap())
        self._preview_label.setText(text)
        self._preview_label.adjustSize()
        # No pixmap = no fullscreen affordance
        self._preview_original_pixmap = None
        self._preview_caption = ""
        if hasattr(self._preview_label, "_interactive"):
            self._preview_label._interactive = False
        self._preview_label.setCursor(Qt.CursorShape.ArrowCursor)
        self._preview_label.setToolTip("")
        # Nothing image-shaped to save - disable the PNG button.
        if hasattr(self, '_btn_save_preview_png'):
            self._btn_save_preview_png.setEnabled(False)

    def _set_preview_pixmap(self, pix: QPixmap,
                              caption: str = ""):
        """Install a pixmap into the preview label and remember
        the source so clicking the preview can open a fullscreen
        upscaled version. The caption is shown in the fullscreen
        window's title bar.

        All render paths (SEQ PETSCII, PRG graphics, hex peek
        does NOT count, two-file logo+charset) should call this
        instead of setPixmap-ing the label directly, so the
        click-to-expand affordance is consistent everywhere a
        preview image is shown.
        """
        self._preview_label.setText("")
        self._preview_label.setPixmap(pix)
        self._preview_label.adjustSize()
        # Remember source pixmap + caption for fullscreen
        self._preview_original_pixmap = pix
        self._preview_caption = caption
        # Activate click affordance
        if hasattr(self._preview_label, "_interactive"):
            self._preview_label._interactive = True
        self._preview_label.setCursor(
            Qt.CursorShape.PointingHandCursor)
        # Enable the Save-PNG button so the user can export this
        # render to disk. We pass the caption through to the
        # save handler via the _preview_caption attribute that
        # is already being set above.
        if hasattr(self, '_btn_save_preview_png'):
            self._btn_save_preview_png.setEnabled(
                not pix.isNull())
        self._preview_label.setToolTip(
            "Click to view fullscreen (Esc to close)")

    def _save_preview_as_png(self):
        """Export the currently-shown preview pixmap to a PNG
        file. Triggered by the "Save as PNG..." button below
        the preview pane.

        Sensibly defaults the filename to the source-file's
        stem + ".png" (e.g. "MOONSPIRE" -> "MOONSPIRE.png")
        so the user just hits Enter for a quick export. The
        save dialog opens at the LAST-used directory from
        a previous PNG save, or the user's home if first
        time - we stash the directory on the instance so it
        persists for the dialog session but doesn't pollute
        quopus.cfg.

        For maximum sharability we save at 2x scale (640x400)
        so the C64's "fat pixels" don't get blurred by image
        viewers' bilinear scaling. A 1x save would look weird
        on modern hi-DPI screens.
        """
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QPixmap as _QPixmap
        import os, re

        pix = self._preview_original_pixmap
        if pix is None or pix.isNull():
            return
        # Build a reasonable default filename from the caption
        # the renderer left for the fullscreen-preview title.
        # The caption typically looks like "FILENAME (format,
        # N blocks)" - strip everything after the first
        # parenthesis and any unsafe chars.
        cap = self._preview_caption or "preview"
        stem = cap.split("(")[0].strip() or "preview"
        # Replace anything that would be awkward in a filename
        # with underscore: spaces, shifted-space artifacts,
        # punctuation. CBM file names can be wild.
        stem = re.sub(r'[^\w\-.]+', '_', stem).strip('_') \
                or "preview"
        # Last-used dir defaulted to home; persisted on the
        # instance so back-to-back exports remember the path.
        last_dir = getattr(self, '_last_png_export_dir', None) \
                    or os.path.expanduser("~")
        default_path = os.path.join(last_dir, f"{stem}.png")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save preview as PNG",
            default_path,
            "PNG Images (*.png);;All files (*)")
        if not out_path:
            return
        # Remember directory for next time
        try:
            self._last_png_export_dir = os.path.dirname(out_path)
        except Exception:
            pass
        # Add .png extension if user didn't type one - prevents
        # confusing "file saved but Windows doesn't recognize it"
        # situations on systems with hidden extensions.
        if not out_path.lower().endswith(".png"):
            out_path += ".png"
        # Save at 2x nearest-neighbor scale. C64 pixel art looks
        # awful with bilinear interpolation - FastTransformation
        # keeps the original pixel grid intact.
        src_w = pix.width()
        src_h = pix.height()
        scaled = pix.scaled(
            src_w * 2, src_h * 2,
            _Qt.AspectRatioMode.IgnoreAspectRatio,
            _Qt.TransformationMode.FastTransformation)
        if not scaled.save(out_path, "PNG"):
            QMessageBox.warning(
                self, "Save PNG",
                f"Failed to save:\n{out_path}\n\n"
                f"Check that the directory exists and is "
                f"writable.")
            return
        # Quiet success - tooltip on the button updates so the
        # user sees "where did the last save go?" without a
        # popup interrupting their workflow.
        try:
            self._btn_save_preview_png.setToolTip(
                f"Last saved to:\n{out_path}\n\n"
                f"Click to export again.")
        except Exception:
            pass

    def _open_fullscreen_preview(self):
        """Pop up a maximized window showing the current preview
        pixmap scaled up to fit the screen. Uses
        FastTransformation (nearest-neighbor) so pixel-art-style
        PETSCII and C64 bitmap previews stay sharp - no
        antialiased blur.

        Esc closes the window. Clicking anywhere also closes
        (matches the "click to open" affordance). Resizing the
        window rescales the pixmap to fit.
        """
        pix = self._preview_original_pixmap
        if pix is None or pix.isNull():
            return
        try:
            _PreviewFullscreenDialog(
                pix, self._preview_caption, self).exec()
        except Exception:
            pass

    def _reset_preview_style(self):
        """Restore the preview label to its default 'show a
        pixmap centered on dark background' state. Needed
        whenever a previous render left it in a special
        configuration - particularly the hex-peek path which
        switches to a monospace font + top-left alignment +
        word-wrap off for readable hex dumps. Without this
        reset, the next SEQ text or bitmap inherits that
        styling and looks wrong.

        Padding is intentionally 0 here. Qt's CSS padding on a
        QLabel interacts badly with displayed pixmaps - the
        widget can decide to fractionally scale the pixmap to
        fit, which re-introduces the 1-pixel-seam-between-cells
        artifact in PETSCII renders. The dark background colour
        alone is enough visual framing.
        """
        if not hasattr(self, "_preview_label"):
            return
        self._preview_label.setStyleSheet(
            "background-color: #1a1a1a; color: #888; "
            "border: 1px solid #444; padding: 0px;")
        self._preview_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        # Make sure pixmap isn't auto-scaled to fit the label -
        # that would also fractional-scale and re-introduce seams.
        self._preview_label.setScaledContents(False)
        # Restore the default font (no monospace override)
        from PyQt6.QtGui import QFont as _QF
        self._preview_label.setFont(_QF())
        # Re-enable word-wrap for plain-text messages and
        # default placeholder ("Click a file to preview"). The
        # hex-peek renderer turns this back off explicitly.
        self._preview_label.setWordWrap(True)

    def _render_seq_petscii(self, ent, data: bytes):
        """Render SEQ/USR file content as a full-fidelity PETSCII
        canvas. The internal seq/text reader uses
        parse_petscii() which honours every PETSCII control
        byte - cursor moves, color switches, reverse-video,
        charset toggle, screen-background change, etc. - so
        BBS art (mostly seq files with embedded $05/$1C/$9F
        color codes and $12/$92 reverse-video bytes) renders
        with all the right colors and the right glyphs.

        The previous implementation used the directory renderer,
        which only knew about printable PETSCII glyphs and
        dropped every control byte. That's fine for plain
        SpeedScript text but smears every BBS graphic into a
        flat white-on-blue mess. This version goes through
        the same code path the standalone Text Reader uses.
        """
        # Reset style first - previous selection may have left
        # the label configured for hex-peek (monospace font,
        # top-left aligned). PETSCII renders as a pixmap that
        # should be centered on the dark background.
        self._reset_preview_style()
        try:
            from .encodings import parse_petscii
            from .readers import render_petscii_grid_to_pixmap
            from .palette import has_c64_pro_mono, get_c64_font
        except ImportError as e:
            # If any of these aren't available, fall back to the
            # old simple directory-renderer path so the user
            # still sees something. Better than blanking out.
            self._render_seq_petscii_simple_fallback(ent, data)
            return
        try:
            # Width 40 matches a real C64 screen. parse_petscii
            # auto-wraps at column 40 just like the C64 KERNAL.
            result = parse_petscii(data,
                                     width=40,
                                     initial_charset=self._charset)
        except Exception as e:
            self._set_preview_text(
                f"PETSCII parse failed:\n{e}")
            return
        grid = result.get("grid") or []
        if not grid:
            self._set_preview_text(
                f"'{ent.name_ascii}' has no PETSCII content "
                f"to render.")
            return
        screen_bg = result.get("screen_bg", "#000000")
        # Pick a cell size that gives a readable preview without
        # eating the whole pane. The default 14px works well in
        # the standalone reader; we mirror that here.
        cell_size = 14
        # Use the C64 Pro Mono font if available, else fall back
        # to whatever has glyphs for petscii_byte_to_unicode.
        use_pua = has_c64_pro_mono()
        c64f = get_c64_font(cell_size)
        try:
            pix = render_petscii_grid_to_pixmap(
                grid,
                default_bg=screen_bg,
                font_family=c64f.family(),
                cell_size=cell_size,
                use_pua=use_pua)
        except Exception as e:
            self._set_preview_text(
                f"PETSCII render failed:\n{e}")
            return
        if pix.isNull():
            self._set_preview_text(
                f"'{ent.name_ascii}' has no content to render.")
            return
        self._set_preview_pixmap(
            pix, caption=f"SEQ: {ent.name_ascii}")

    def _render_seq_petscii_simple_fallback(self, ent, data: bytes):
        """Fallback SEQ renderer used when the full PETSCII
        parser pipeline can't be imported (e.g. encodings or
        readers module unavailable for some reason). Splits on
        CR and uses the directory renderer - dropped support
        for color/reverse/charset control codes but at least
        the printable text shows up.
        """
        lines = data.split(b'\r')
        if len(lines) > 200:
            lines = lines[:200] + [b"... (truncated for preview)"]
        capped_lines = [(ln[:80] if len(ln) > 80 else ln)
                          for ln in lines]
        try:
            pix = render_directory_to_pixmap(
                capped_lines, cell_size=12,
                charset=self._charset)
        except Exception as e:
            self._set_preview_text(
                f"Preview render failed:\n{e}")
            return
        if pix.isNull():
            self._set_preview_text(
                f"'{ent.name_ascii}' has no content to render.")
            return
        self._set_preview_pixmap(
            pix, caption=f"SEQ: {ent.name_ascii}")

    def _render_prg_graphics(self, ent, data: bytes) -> bool:
        """Try to decode the PRG as a known C64 graphics format.

        Returns True on successful render, False if no decoder
        matches (caller should fall back to hex peek).

        Approach: write the bytes to a tempfile with a plausible
        extension based on the load-address signature, then call
        the existing retro_gfx_decoders pipeline. That gives us
        all the format support that already exists (Koala,
        Art Studio, Advanced Art Studio, Doodle, FLI, AFLI,
        etc.) without duplicating decode logic here.

        Filename hints also matter: BBS art naming conventions
        prefix files with format markers like "[B]NAME" for
        Amica/Botticelli pics or "[K]NAME" for Koala. We look
        at the raw PETSCII filename to honour these even when
        the load-address heuristic alone wouldn't catch them.
        """
        if len(data) < 3:
            return False
        # Try to map load-address + size + filename hint to a
        # format. The mapping is heuristic - some formats share
        # a load address - so we try the decoders in priority
        # order and keep the first one that doesn't blow up.
        load = data[0] | (data[1] << 8)
        size = len(data)
        candidates = self._guess_graphics_format(
            load, size, name_petscii=ent.name_petscii)
        if not candidates:
            return False
        import tempfile, os as _os
        try:
            from .retro_gfx_decoders import (
                decode_koala, decode_art_studio,
                decode_advanced_art_studio, decode_doodle,
                decode_amica,
                C64_PALETTE)
        except ImportError:
            return False
        # The decoders take a file path - dump to tempfile.
        # Cleaned up in `finally`. Each candidate gets a fresh
        # try since some decoders raise on a bad match.
        fd, tmp_path = tempfile.mkstemp(prefix="quopus_gfx_",
                                          suffix=".prg")
        try:
            with _os.fdopen(fd, "wb") as f:
                f.write(data)
            result = None
            chosen_name = ""
            for fname, fn in candidates:
                try:
                    result = fn(tmp_path)
                    chosen_name = fname
                    break
                except Exception:
                    continue
            if result is None:
                return False
            # Render the pixel buffer to QImage/QPixmap. Same
            # pipeline as retro_gfx_viewer._render() - indexed
            # 8-bit with the 16-color C64 palette.
            w = result['width']
            h = result['height']
            pixels = result['pixels']
            ct = [(0xFF << 24) | (r << 16) | (g << 8) | b
                   for r, g, b in C64_PALETTE]
            img = QImage(bytes(pixels), w, h, w,
                          QImage.Format.Format_Indexed8)
            img.setColorTable(ct)
            # Convert to RGB so subsequent scaling stays clean
            img = img.convertToFormat(
                QImage.Format.Format_RGB888)
            # Scale 1.5x for visibility - C64 pixels are
            # noticeably non-square (multicolor is even more
            # squashed), but a uniform 1.5x is a decent
            # compromise for a preview pane.
            # Build the unscaled source pixmap first. We pass
            # this to _set_preview_pixmap so the fullscreen
            # viewer can do its own nearest-neighbor scaling on
            # the original 320x200 (or similar) image instead of
            # upscaling the already-1.5x preview.
            src_pix = QPixmap.fromImage(img)
            # Reset style so a previous hex-peek view doesn't
            # leave monospace font + top-left align in effect.
            self._reset_preview_style()
            # Scale to 1.5x for the inline preview pane - tight
            # enough to fit in the side pane, big enough to be
            # readable. Fullscreen viewer rescales from the
            # original.
            preview_pix = src_pix.scaled(
                int(w * 1.5), int(h * 1.5),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
            self._set_preview_pixmap(
                preview_pix,
                caption=f"{chosen_name}: {ent.name_ascii}")
            # The pixmap we stash for fullscreen is the unscaled
            # source - much better quality when blown up later.
            self._preview_original_pixmap = src_pix
            # Show what format we identified as a tooltip so
            # the user can sanity-check the guess.
            self._preview_label.setToolTip(
                f"Detected: {chosen_name}\n"
                f"Load: ${load:04X}  Size: {size}\n"
                f"Click to view fullscreen (Esc to close)")
            # And in the header label above the preview pane,
            # which is more discoverable than a tooltip.
            self._set_format_label(
                f"{chosen_name}  -  Load ${load:04X}  "
                f"({size} bytes)")
            return True
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass

    def _guess_graphics_format(self, load: int, size: int,
                                  name_petscii: bytes = b""):
        """Return a list of (name, decoder_fn) candidates that
        plausibly match the given load-address + size signature.

        Multiple candidates can be returned for ambiguous cases
        - the caller tries them in order and keeps the first
        successful decode. Common load addresses:
            $6000 -> Koala / FLI / AFLI / IFLI (size disambiguates)
            $2000 -> Art Studio / Advanced Art Studio
            $4000 -> Amica Paint (RLE) / FLI
            $5C00 -> Doodle
            $3F00 -> some Hires/AFLI variants

        BBS scene file naming conventions also help. Many groups
        prefix the filename with a format-tag in square brackets:
            "[B]NAME"  -> Amica/Botticelli (RLE-packed multicolor)
            "[K]NAME"  -> Koala
            "[A]NAME"  -> Advanced Art Studio
            "[D]NAME"  -> Doodle
            "[F]NAME"  -> FLI
        We honour these tags before the load-address heuristic
        so a packed Amica saved at a non-standard load address
        still gets decoded correctly.
        """
        from .retro_gfx_decoders import (
            decode_koala, decode_art_studio,
            decode_advanced_art_studio, decode_doodle,
            decode_amica)
        out = []

        # --- Filename-prefix tag detection -----------------
        # Look at the raw PETSCII filename for the [X] prefix
        # convention used in BBS/demoscene packs, plus a few
        # single-character markers also common in the wild:
        #   leading $73 / $D3 (heart glyph) = Koala. This is
        #   the convention some art packs use instead of the
        #   verbose "[K]" prefix - one character of filename
        #   real estate goes a long way on a CBM disk.
        prefix_tag = None
        if name_petscii:
            # Strip $A0 (shifted-space) padding from the right
            # and look for [X] at the start. PETSCII [ = $5B,
            # ] = $5D, and any uppercase letter A-Z = $41-$5A.
            stripped = name_petscii.rstrip(b'\xa0\x00')
            if (len(stripped) >= 3
                    and stripped[0] == 0x5B
                    and stripped[2] == 0x5D):
                t = stripped[1]
                # Map PETSCII upper-case tag bytes to chars
                if 0x41 <= t <= 0x5A:
                    prefix_tag = chr(t)
                elif 0x61 <= t <= 0x7A:
                    prefix_tag = chr(t).upper()
            # Single-char heart marker = Koala. Some packs
            # (especially older CSDB demos) use the heart
            # glyph as the first byte of the filename to
            # signify "this is a Koala". PETSCII byte $73 is
            # the heart in the lower charset; $D3 is the same
            # glyph in upper charset (CBM-shifted).
            elif (len(stripped) >= 1
                    and stripped[0] in (0x73, 0xD3)):
                prefix_tag = 'K'

        if prefix_tag == 'B':
            # Amica / Botticelli - RLE-packed multicolor. The
            # decoder is forgiving so we put it first regardless
            # of load address.
            out.append(("Amica Paint [B]", decode_amica))
        elif prefix_tag == 'K':
            out.append(("Koala Painter [K]", decode_koala))
        elif prefix_tag == 'A':
            out.append(
                ("Advanced Art Studio [A]",
                 decode_advanced_art_studio))
        elif prefix_tag == 'D':
            out.append(("Doodle [D]", decode_doodle))
        elif prefix_tag == 'H':
            out.append(("Art Studio (Hires) [H]", decode_art_studio))

        # --- Load-address + size heuristic ----------------
        # Even when a prefix tag matched we keep the heuristic
        # candidates as fallbacks - if the [B] decoder somehow
        # blows up we still try Koala next.
        # Koala: load $6000, body 10003 (with load) = 10001 stripped.
        # Some Koalas are slightly smaller/larger. Accept 8000..11000.
        if load == 0x6000 and 9000 <= size <= 11000:
            out.append(("Koala Painter", decode_koala))
        # Generic large enough payload at $6000 - try Koala anyway,
        # since many demo files use $6000 even when ext is just .prg
        elif load == 0x6000 and size >= 9000:
            out.append(("Koala Painter (guess)", decode_koala))
        # Art Studio: load $2000, ~9009 bytes
        if load == 0x2000 and 8500 <= size <= 9500:
            out.append(("Art Studio (Hires)", decode_art_studio))
        # Advanced Art Studio: load $2000, ~10018 bytes
        if load == 0x2000 and 9500 <= size <= 11000:
            out.append(
                ("Advanced Art Studio",
                 decode_advanced_art_studio))
        # Doodle: load $5C00, 9218 bytes
        if load == 0x5C00 and 8500 <= size <= 10000:
            out.append(("Doodle", decode_doodle))
        # Amica Paint: load $4000, RLE-packed. Sizes vary
        # wildly with the picture content - simple gradients
        # pack to ~2-3K, complex images closer to 10K. Accept
        # anything 1500..11000 bytes at $4000. Some Amica files
        # use a different load address (e.g. $4000 is canonical
        # but $1FFE / $2000 also seen in the wild) so also
        # accept those when the size range fits.
        if load == 0x4000 and 1500 <= size <= 11000:
            out.append(("Amica Paint", decode_amica))
        elif (load in (0x1FFE, 0x2000, 0x6000)
                and 1500 <= size <= 11000
                and prefix_tag != 'B'):
            # Soft Amica candidate - only as fallback, since
            # these load addresses also belong to other formats.
            # Skip if we already added [B]-tagged Amica above.
            out.append(("Amica Paint (alt load)", decode_amica))

        # Fallback: anything 10K-ish with load $4000-$8000 could
        # be a Koala-like - try decode_koala which is the most
        # forgiving format and pads short payloads.
        if (not out and 9500 <= size <= 11000
                and 0x4000 <= load <= 0x8000):
            out.append(
                ("Bitmap (guess)", decode_koala))
        return out

    def _render_prg_hex_peek(self, ent, data: bytes):
        """Fallback for PRG files we can't render graphically.
        Show the load address + a short hex/ASCII peek so the
        user has SOME information to identify the file by.

        This is essentially a tiny hex dumper - 8 rows of 16
        bytes is enough to spot file signatures (SID, FLI,
        charset, sprites, BASIC, etc.) without dragging in the
        full hex reader.
        """
        if len(data) < 2:
            self._set_preview_text(
                f"{ent.name_ascii}\n\nFile too short.")
            return
        load = data[0] | (data[1] << 8)
        body = data[2:]
        lines = []
        lines.append(f"{ent.name_ascii}  ({ent.type_label}, "
                      f"{ent.blocks} blocks)")
        lines.append(f"Load address: ${load:04X}")
        lines.append(f"Size: {len(data)} bytes  "
                      f"(body: {len(body)} bytes)")
        lines.append("")
        lines.append("Hex peek:")
        for row in range(8):
            offs = row * 16
            chunk = body[offs:offs + 16]
            if not chunk:
                break
            hexpart = " ".join(f"{b:02X}" for b in chunk)
            # ASCII view: printable 0x20-0x7E only, else dot.
            asc = "".join(
                chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            # Show the real C64 memory address (load + offset)
            # instead of the in-file offset. A PRG with load
            # $C400 displays $C400/$C410/... rather than +0000/
            # +0010/... - that's what disassemblers and the
            # actual machine see, and it matches the "Load
            # address: $C400" line above. Wraps cleanly past
            # $FFFF for the 64KB case via the & 0xFFFF mask.
            addr = (load + offs) & 0xFFFF
            lines.append(
                f"  ${addr:04X}  {hexpart:<47}  {asc}")
        text = "\n".join(lines)
        self._preview_label.setPixmap(QPixmap())
        # Switch to a monospace font for the hex peek so the
        # columns line up. Reset on next text-state preview.
        from PyQt6.QtGui import QFont as _QF
        f = _QF("Topaz-8")
        f.setFamilies(["Topaz-8", "Topaz", "Courier New",
                        "Consolas", "monospace"])
        f.setStyleHint(_QF.StyleHint.TypeWriter)
        f.setPointSize(9)
        self._preview_label.setFont(f)
        # Lighter color since we're showing structured text
        self._preview_label.setStyleSheet(
            "background-color: #1a1a1a; color: #ccc; "
            "border: 1px solid #444; padding: 8px; "
            "font-family: 'Topaz-8', monospace;")
        self._preview_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
             | Qt.AlignmentFlag.AlignTop)
        # CRITICAL: turn off word-wrap for the hex peek. The
        # default `setWordWrap(True)` (set in _build_layout for
        # the placeholder text) would wrap each hex row mid-line
        # when the pane is narrower than the row width. The hex
        # column + ASCII column have to stay on one line for the
        # layout to make sense - the user can horizontally
        # scroll if the pane is too narrow.
        self._preview_label.setWordWrap(False)
        self._preview_label.setText(text)
        # adjustSize uses the unwrapped text width so the label
        # widget becomes as wide as the longest line; the
        # surrounding QScrollArea then provides a horizontal
        # scrollbar if needed.
        self._preview_label.adjustSize()

    def _find_main_window(self):
        """Walk up from self.parent() to find the Quopus main
        window. self.window() doesn't work because the
        CbmDiskDialog IS a top-level Qt window (modal dialog),
        so self.window() just returns the dialog. The parent
        widget passed to CbmDiskDialog(__init__) is usually the
        lister; its window() gives us the actual MainWindow
        that holds the config dict.
        """
        p = self.parent()
        while p is not None:
            # The Quopus main window stashes its config dict
            # on itself. Walk up until we find one with that
            # attribute - works for direct parent-is-MainWindow
            # AND for parent-is-lister-inside-MainWindow.
            if hasattr(p, 'config') and isinstance(
                    getattr(p, 'config', None), dict):
                return p
            # window() on a non-top-level widget gives the
            # containing top-level window. From a lister
            # widget that's the MainWindow.
            if hasattr(p, 'window'):
                w = p.window()
                if (w is not None and w is not p
                        and hasattr(w, 'config')):
                    return w
            p = p.parent() if hasattr(p, 'parent') else None
        return None

    def _get_config(self):
        """Return the live config dict from the main window, or
        load it from disk as a fallback if we can't find the
        main window from here. Reading directly from disk means
        Run-on-U64 / Run-in-VICE still work even if the dialog
        was opened in a way that loses the parent chain.
        """
        mw = self._find_main_window()
        if mw is not None:
            cfg = getattr(mw, 'config', None)
            if isinstance(cfg, dict):
                return cfg
        # Fallback: load fresh from disk. Doesn't share state
        # with the running app's config dict, but for read-only
        # access to emulator/U64 settings that's fine.
        try:
            from .config import load_config
            return load_config() or {}
        except Exception:
            return {}

    def _on_double_click(self, item: QListWidgetItem):
        """Double-click brings up a small action picker so the
        user can choose Extract / Run on U64 / Run in VICE
        without having to right-click through a menu. Saves a
        click and matches what the asm64 browser does.

        Non-PRG entries (SEQ / USR / REL) only get the Extract
        option since the C64 'run' workflow assumes a runnable
        program. They can still be extracted to disk.
        """
        ent: CbmDirEntry = item.data(Qt.ItemDataRole.UserRole)
        if not ent:
            return
        self._show_action_menu(ent)

    def _show_action_menu(self, ent: "CbmDirEntry"):
        """Pop up a QMenu next to the cursor with the action
        options for this entry. Tied into the existing
        _extract_entries / U64 push / VICE launch paths so all
        three entry points (this menu, the toolbar buttons, and
        the future right-click context menu) share one impl.
        """
        from PyQt6.QtGui import QCursor, QAction
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        is_prg = (ent.type_label or "").upper() == "PRG"
        act_extract = QAction(
            f"Extract '{ent.name_ascii}'...", menu)
        act_extract.triggered.connect(
            lambda: self._extract_entries([ent]))
        menu.addAction(act_extract)
        if is_prg:
            menu.addSeparator()
            act_u64 = QAction(
                f"Run '{ent.name_ascii}' on U64", menu)
            act_u64.setToolTip(
                "Push this PRG to the active Ultimate-64 and "
                "start it via DMA. Skips writing a temp file.")
            act_u64.triggered.connect(
                lambda: self._run_entry_on_u64(ent))
            menu.addAction(act_u64)
            act_vice = QAction(
                f"Run '{ent.name_ascii}' in VICE", menu)
            act_vice.setToolTip(
                "Extract the PRG to a temp file and launch it "
                "in the configured C64 emulator (VICE / x64sc).")
            act_vice.triggered.connect(
                lambda: self._run_entry_in_vice(ent))
            menu.addAction(act_vice)
            menu.addSeparator()
            act_disasm = QAction(
                f"Disassemble '{ent.name_ascii}'", menu)
            act_disasm.setToolTip(
                "Open this PRG in the 6502 / 6510 disassembler "
                "without having to extract it first.")
            act_disasm.triggered.connect(
                lambda: self._disasm_entry(ent))
            menu.addAction(act_disasm)
        # Show-as-PETSCII works for every file type, not just
        # PRG. Useful for spotting embedded scene marker
        # strings, FILE_ID.DIZ text inside binary data, EAPI
        # signatures, etc. - things that hex dumps obscure
        # but jump out when the bytes get rendered as C64
        # PETSCII glyphs. SEQ/USR files already preview as
        # PETSCII automatically, but the dialog gives a bigger
        # canvas plus charset Lo/Hi toggle.
        menu.addSeparator()
        act_petscii = QAction(
            f"Show '{ent.name_ascii}' as PETSCII", menu)
        act_petscii.setToolTip(
            "Render the file content as a grid of C64 PETSCII "
            "glyphs. Adjustable charset (Lo/Hi), cell size and "
            "columns. Save the rendering as PNG.")
        act_petscii.triggered.connect(
            lambda: self._show_entry_as_petscii(ent))
        menu.addAction(act_petscii)
        # Show-as-picture: for any .bin/.col/.dat file we look
        # for the matching companions on the same disk and render
        # them as a single multicolor bitmap image. Used by the
        # War of the Worlds C64 family of games (and similar
        # adventure games of the era) that store images as a
        # .bin + .col pair, with an associated .dat carrying
        # the room's text/vocabulary data. Multi-selecting all
        # three lets the user inspect the picture AND the text
        # script in one dialog.
        picture_files = self._collect_picture_files(ent)
        if picture_files is not None:
            bin_ent, col_ent, dat_ent = picture_files
            label = f"Show '{ent.name_ascii}' as picture"
            selected_items = (
                self._list.selectedItems()
                if hasattr(self, "_list") else [])
            if len(selected_items) > 1:
                # User has a multi-selection - if it covers the
                # scene's bin+col(+dat) trio, mention that in
                # the menu label so it's clear which files will
                # be combined into the picture.
                sel_names = []
                for it in selected_items:
                    e = it.data(Qt.ItemDataRole.UserRole)
                    if e is not None:
                        sel_names.append(e.name_ascii)
                if len(sel_names) >= 2:
                    label = (f"Show {len(sel_names)} selected "
                              f"files as picture")
            act_pic = QAction(label, menu)
            act_pic.setToolTip(
                "Render this scene's .bin + .col as a multicolor "
                "bitmap, plus extract any text strings from the "
                ".dat file (adventure-game vocabulary / room "
                "descriptions).")
            act_pic.triggered.connect(
                lambda: self._show_entry_as_picture(ent))
            menu.addAction(act_pic)
        menu.exec(QCursor.pos())

    def _collect_picture_files(self, ent):
        """For a .bin/.col/.dat entry, locate the full set of
        companion files that make up the picture. Returns
        (bin_ent, col_ent, dat_ent) - any of which may be None
        if the corresponding file isn't on the disk - or None
        if `ent` doesn't look like part of a picture set at all.

        The naming scheme is BASENAME.{bin,col,dat} (case
        insensitive, '.' separator). The .bin holds the C64
        multicolor bitmap, the .col holds the screen RAM +
        color RAM + background byte, and the .dat - for
        adventure games like War of the Worlds - holds the
        room's text strings and vocabulary. The .dat doesn't
        contribute pixels but its text can be extracted and
        shown alongside the picture, which is why we include
        it here.
        """
        name = (ent.name_ascii or "").lower()
        m = None
        for ext in (".bin", ".col", ".dat"):
            if name.endswith(ext):
                base = name[:-4]
                break
        else:
            return None
        bin_ent = None
        col_ent = None
        dat_ent = None
        for e in self.reader.entries:
            n = (e.name_ascii or "").lower()
            if n == base + ".bin":
                bin_ent = e
            elif n == base + ".col":
                col_ent = e
            elif n == base + ".dat":
                dat_ent = e
        # Without a bitmap AND colour data, there's nothing to
        # render. We could fall back to a colour-only render
        # but it wouldn't show anything useful.
        if bin_ent is None or col_ent is None:
            return None
        return (bin_ent, col_ent, dat_ent)

    def _show_entry_as_picture(self, ent):
        """Read the scene's .bin + .col (+ optional .dat) and
        render the combined picture in a modal dialog. The
        bitmap and color come from the .bin/.col pair the same
        way the C64 displays them; the .dat - if present -
        gets scanned for printable text strings which are
        listed alongside (they're the adventure room's words /
        descriptions, not pixel data).
        """
        from PyQt6.QtWidgets import QMessageBox
        files = self._collect_picture_files(ent)
        if files is None:
            QMessageBox.warning(
                self, "Show as picture",
                f"No matching .bin + .col pair for "
                f"'{ent.name_ascii}'.")
            return
        bin_ent, col_ent, dat_ent = files
        try:
            bin_raw = self._read_entry_bytes(bin_ent)
            col_raw = self._read_entry_bytes(col_ent)
            dat_raw = (self._read_entry_bytes(dat_ent)
                       if dat_ent is not None else None)
        except Exception as e:
            QMessageBox.warning(
                self, "Show as picture",
                f"Could not read files:\n{e}")
            return
        dlg = _WotwImageDialog(
            self, bin_ent.name_ascii, col_ent.name_ascii,
            bin_raw, col_raw,
            dat_name=(dat_ent.name_ascii
                      if dat_ent is not None else None),
            dat_raw=dat_raw)
        dlg.show()

    def _show_entry_as_petscii(self, ent):
        """Read the entry's bytes and pop up a modal dialog
        showing them rendered as a C64 PETSCII glyph grid. The
        dialog provides the same controls the CRT viewer's
        PETSCII tab does - charset Lo/Hi, cell size +/-, column
        count - plus a Save-PNG button. Works for every file
        type the disk image has (PRG/SEQ/USR/DEL/REL), not just
        PRG.
        """
        from PyQt6.QtWidgets import QMessageBox
        try:
            data = self._read_entry_bytes(ent)
        except Exception as e:
            QMessageBox.warning(
                self, "Show as PETSCII",
                f"Could not read '{ent.name_ascii}':\n{e}")
            return
        if not data:
            QMessageBox.information(
                self, "Show as PETSCII",
                f"'{ent.name_ascii}' is empty - nothing to "
                f"render.")
            return
        # For PRG files the first 2 bytes are the load address;
        # skip them so the PETSCII view shows the actual
        # program data starting at byte 0 instead of a leading
        # garbage cell.
        if (ent.type_label or "").upper() == "PRG" \
                and len(data) >= 2:
            display_bytes = data[2:]
            load_addr = data[0] | (data[1] << 8)
        else:
            display_bytes = data
            load_addr = None
        dlg = _CbmPetsciiDialog(
            self, ent.name_ascii, display_bytes,
            load_addr=load_addr,
            initial_charset=self._charset)
        dlg.show()

    def _read_entry_bytes(self, ent):
        """Read the raw bytes of a directory entry. Shared by
        the PETSCII preview and the disassembler launch path
        so they always see the exact same data."""
        if hasattr(self, "reader") and self.reader is not None:
            return self.reader.extract(ent)
        return getattr(ent, "_data", b"")

    def _disasm_entry(self, ent: "CbmDirEntry"):
        """Extract the entry's PRG bytes from the disk image,
        write them to a temp .prg file and open the disassembler
        on it. We use a temp file because C64DisasmViewer expects
        a path - matches how 'Run in VICE' handles the same
        problem.
        """
        try:
            data = self.reader.extract(ent)
        except Exception as e:
            QMessageBox.warning(self, "Disassemble",
                f"Could not extract '{ent.name_ascii}':\n{e}")
            return
        if not data:
            QMessageBox.warning(self, "Disassemble",
                f"'{ent.name_ascii}' is empty - nothing to "
                "disassemble.")
            return
        # Write to a temp file under the system temp dir. Reuse
        # the standard tempfile mkstemp so the path is unique and
        # the OS will clean it up eventually. The disassembler
        # window keeps the path open for its lifetime; once the
        # user closes it the file is orphaned but harmless.
        import tempfile, os
        from pathlib import Path as _Path
        safe_name = "".join(
            c if c.isalnum() or c in "._-" else "_"
            for c in ent.name_ascii) or "unnamed"
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"quopus_disasm_{safe_name}_",
            suffix=".prg")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception as e:
            QMessageBox.warning(self, "Disassemble",
                f"Could not write temp file:\n{e}")
            return
        try:
            from .c64_disasm import C64DisasmViewer
            dlg = C64DisasmViewer(_Path(tmp_path), self.window())
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.setWindowTitle(
                f"Disassembler - {ent.name_ascii}")
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self, "Disassemble",
                f"Could not open disassembler:\n{e}")

    def _run_entry_on_u64(self, ent: "CbmDirEntry"):
        """Extract the entry's PRG bytes from the disk image and
        push them straight to the active U64 via the runners:
        run_prg endpoint. No temp file - the bytes go directly
        over HTTP.
        """
        try:
            data = self.reader.extract(ent)
        except Exception as e:
            QMessageBox.warning(self, "Run on U64",
                f"Could not extract '{ent.name_ascii}':\n{e}")
            return
        if not data:
            QMessageBox.warning(self, "Run on U64",
                f"'{ent.name_ascii}' is empty - nothing to run.")
            return
        # Pull the active U64 device's connection details. Uses
        # the same multi-device-aware picker as the asm64 browser
        # so the user gets a chooser if multiple U64s are
        # configured.
        try:
            from .u64_devices import pick_device
        except ImportError:
            QMessageBox.warning(self, "Run on U64",
                "U64 device support is not available in this "
                "build.")
            return
        # Find the main window so we can read the config + show
        # the device chooser parented correctly.
        cfg = self._get_config()
        device = pick_device(
            self, cfg,
            title=f"Run '{ent.name_ascii}' on U64",
            prompt="Choose which Ultimate-64 should run this "
                   "PRG:")
        if device is None:
            return
        host = (device.get('host', '') or '').strip()
        port = int(device.get('http_port', 80) or 80)
        password = device.get('http_password', '') or ''
        if not host:
            QMessageBox.warning(self, "Run on U64",
                "Selected U64 device has no host configured.")
            return
        # Fire the run. u64_run_prg returns (ok, msg).
        try:
            from .u64_streamer import u64_run_prg
        except ImportError:
            QMessageBox.warning(self, "Run on U64",
                "U64 streamer module not available.")
            return
        ok, msg = u64_run_prg(host, data, password=password,
                               port=port)
        if ok:
            self._status.setText(
                f"  Sent '{ent.name_ascii}' to U64 at {host}")
        else:
            QMessageBox.warning(self, "Run on U64",
                f"Failed to send '{ent.name_ascii}' to "
                f"{host}:\n{msg}")

    def _run_entry_in_vice(self, ent: "CbmDirEntry"):
        """Extract the entry's PRG bytes to a temp file in the
        system temp dir and launch the configured C64 emulator
        on it. The temp file isn't cleaned up - VICE may still
        have it open after this function returns, and stale
        files in temp are not a real problem.
        """
        try:
            data = self.reader.extract(ent)
        except Exception as e:
            QMessageBox.warning(self, "Run in VICE",
                f"Could not extract '{ent.name_ascii}':\n{e}")
            return
        if not data:
            QMessageBox.warning(self, "Run in VICE",
                f"'{ent.name_ascii}' is empty - nothing to run.")
            return
        # Pick a safe temp filename. Keep the original name as
        # a prefix so the user sees it in VICE's title bar.
        import tempfile
        import re as _re
        safe_name = _re.sub(
            r"[^a-zA-Z0-9._-]+", "_", ent.name_ascii)[:32] \
            or "extracted"
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f"quopus_{safe_name}_",
                suffix=".prg")
            import os as _os
            with _os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError as e:
            QMessageBox.warning(self, "Run in VICE",
                f"Could not write temp PRG file:\n{e}")
            return
        # Launch via the shared emulator-launcher helper. Uses
        # the same config knob (c64_emulator + c64_emulator_args)
        # that the lister Run-in-emulator and DB browser use, so
        # there's one place to configure VICE/x64sc/etc.
        try:
            from .c64_disasm import run_in_c64_emulator
            from .config import save_config
        except ImportError:
            QMessageBox.warning(self, "Run in VICE",
                "Emulator launcher not available in this "
                "build.")
            return
        from pathlib import Path
        cfg = self._get_config()
        try:
            launched = run_in_c64_emulator(
                Path(tmp_path), self, cfg,
                lambda: save_config(cfg) if cfg else None)
            if launched:
                self._status.setText(
                    f"  Launched '{ent.name_ascii}' "
                    f"in C64 emulator")
        except Exception as e:
            QMessageBox.warning(self, "Run in VICE",
                f"Could not launch emulator:\n{e}")

    # ---- buttons ----
    def _extract_all(self):
        self._extract_entries(list(self.reader.entries))

    def _extract_selected(self):
        items = self._list.selectedItems()
        if not items:
            QMessageBox.information(self, "Extract",
                                      "Nothing selected in the list.")
            return
        ents = [it.data(Qt.ItemDataRole.UserRole) for it in items]
        self._extract_entries(ents)

    # ---- destination picker (mirrors ArchiveViewer's logic) ----
    def _other_panel_path(self) -> Optional[Path]:
        """Return the active Quopus other-side panel's current path,
        or None if not running inside Quopus / other side is remote."""
        try:
            src = self.parent()
            if src is None: return None
            mw = src.window()
            if (mw is None
                    or not hasattr(mw, 'left_lister')
                    or not hasattr(mw, 'right_lister')):
                return None
            other = (mw.right_lister if src is mw.left_lister
                      else mw.left_lister)
            if getattr(other.fs, 'kind', None) != 'local':
                return None
            return Path(other.current_path)
        except Exception:
            return None

    def _refresh_other_panel(self):
        try:
            src = self.parent()
            mw = src.window()
            other = (mw.right_lister if src is mw.left_lister
                      else mw.left_lister)
            other.refresh()
        except Exception:
            pass

    def _pick_target(self, n_items: int) -> Optional[Path]:
        other = self._other_panel_path()
        if other is None:
            d = QFileDialog.getExistingDirectory(self, "Extract to...")
            return Path(d) if d else None
        box = QMessageBox(self)
        box.setWindowTitle("Extract")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Extract {n_items} file(s) to the other panel?\n\n"
                     f"Target: {other}")
        b_ok = box.addButton("Extract here",
                              QMessageBox.ButtonRole.AcceptRole)
        b_browse = box.addButton("Browse...",
                                  QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel",
                       QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_ok)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_ok:
            return other
        if clicked is b_browse:
            d = QFileDialog.getExistingDirectory(self, "Extract to...",
                                                   str(other))
            return Path(d) if d else None
        return None

    # ---- the actual extraction ----
    def _extract_entries(self, entries: List[CbmDirEntry]):
        if not entries:
            return
        target = self._pick_target(len(entries))
        if target is None:
            return
        target.mkdir(parents=True, exist_ok=True)
        ok = 0
        skipped = 0
        for ent in entries:
            try:
                data = self.reader.extract(ent)
            except Exception as e:
                QMessageBox.warning(self, "Extract",
                                      f"Failed to extract "
                                      f"'{ent.name_ascii}': {e}")
                skipped += 1
                continue
            if not data:
                skipped += 1
                continue
            # Filename: <ascii_name>.<type_label> so the user sees
            # both the original name and the CBM file type. Could be
            # made configurable later (ext-only / type-only / etc).
            ext = ent.type_label.lower()
            fname = f"{ent.name_ascii}.{ext}"
            # Avoid clobbering existing files - append _NN if needed.
            out = target / fname
            n = 1
            while out.exists() and n < 1000:
                out = target / f"{ent.name_ascii}_{n}.{ext}"
                n += 1
            try:
                out.write_bytes(data)
                ok += 1
            except Exception as e:
                QMessageBox.warning(self, "Extract",
                                      f"Could not write '{out}': {e}")
                skipped += 1
        msg = f"Extracted {ok} file(s) to {target}"
        if skipped:
            msg += f" ({skipped} skipped)"
        self._status.setText("  " + msg)
        self._refresh_other_panel()


# =====================================================================
# CbmEntryEditDialog: edit single entry (name, type, lock, splat, blocks)
# =====================================================================
class _CbmEntryEditDialog(QDialog):
    """Compact edit dialog for one CbmDirEntry. Lets the user set:
       - filename (ASCII; converted to PETSCII on save)
       - file type (DEL/SEQ/PRG/USR/REL via combobox)
       - locked checkbox
       - splat checkbox (clear closed-bit)
       - block count (cosmetic, 0..65535)

    Returns the updated values as a dict via .result() after
    accept(). Caller decides which fields actually got changed
    by comparing against the original entry.
    """

    TYPE_LABELS = ['DEL', 'SEQ', 'PRG', 'USR', 'REL']

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {entry.name_ascii}")
        self._entry = entry
        from PyQt6.QtWidgets import (
            QFormLayout, QLineEdit, QComboBox, QCheckBox, QSpinBox,
            QDialogButtonBox,
        )
        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setContentsMargins(10, 10, 10, 10)

        self.le_name = QLineEdit(entry.name_ascii)
        self.le_name.setMaxLength(16)
        self.le_name.setToolTip(
            "16 chars max. ASCII letters become PETSCII upper "
            "automatically.")
        form.addRow("Filename:", self.le_name)

        self.cb_type = QComboBox()
        for i, lbl in enumerate(self.TYPE_LABELS):
            self.cb_type.addItem(lbl, i)
        self.cb_type.setCurrentIndex(entry.type_code)
        self.cb_type.setToolTip(
            "DEL = scratched, SEQ = sequential, PRG = program, "
            "USR = user, REL = relative")
        form.addRow("File type:", self.cb_type)

        self.cb_locked = QCheckBox("Locked (write-protected, '<' splat)")
        self.cb_locked.setChecked(entry.locked)
        self.cb_locked.setToolTip(
            "Locked entries show '<' next to the type in the "
            "directory and refuse SCRATCH on a real 1541.")
        form.addRow("", self.cb_locked)

        self.cb_splat = QCheckBox("Splat ('*' = unclosed file)")
        self.cb_splat.setChecked(entry.splat)
        self.cb_splat.setToolTip(
            "Splat-marked entries show '*' next to the type. "
            "Real disks get this when a SAVE was interrupted; "
            "crackers use it as a visual marker.")
        form.addRow("", self.cb_splat)

        self.sp_blocks = QSpinBox()
        self.sp_blocks.setRange(0, 65535)
        self.sp_blocks.setValue(entry.blocks)
        self.sp_blocks.setToolTip(
            "Cosmetic block count - what the directory shows. "
            "Does NOT affect BAM or actual file data.")
        form.addRow("Blocks:", self.sp_blocks)

        outer.addLayout(form)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def result(self):
        """Return the new values as a dict. The caller diffs against
        the original entry to decide which setters to call."""
        # Build PETSCII bytes from the current name field.
        # Reference _ascii_to_petscii_filename via the entry's
        # parent class - same module, no circular import dance.
        name_pet = CbmDiskReader._ascii_to_petscii_filename(
            self.le_name.text())
        return {
            'name_petscii':  name_pet,
            'type_code':     int(self.cb_type.currentData()),
            'locked':        bool(self.cb_locked.isChecked()),
            'splat':         bool(self.cb_splat.isChecked()),
            'blocks':        int(self.sp_blocks.value()),
        }


# =====================================================================
# CbmSeparatorEditorDialog: PETSCII-aware separator builder
# =====================================================================
class _CbmSeparatorEditorDialog(QDialog):
    """Build a separator filename byte-by-byte using a PETSCII
    graphics character picker, with a live-rendered preview that
    looks exactly like the directory listing will.

    Layout:
        +--------------------------------------------+
        | [name field]  (16 chars max - PETSCII)     |
        | (live preview rendered as PETSCII tile)    |
        +--------------------------------------------+
        | Picker grid (16 cols x N rows of glyphs)   |
        |   click to insert at the current cursor    |
        +--------------------------------------------+
        | Quick fills: dashes, equals, blocks, ...   |
        +--------------------------------------------+
        | [OK]  [Cancel]                             |
        +--------------------------------------------+

    The 'name field' is a sequence of 16 byte boxes. Each box can
    hold one PETSCII byte. The picker grid shows all 256 PETSCII
    code points (rendered via the same render_directory_to_pixmap
    path as the main directory) - clicking inserts that byte at
    the current edit position. Backspace removes the last byte.
    """

    def __init__(self, parent=None, charset='lower'):
        super().__init__(parent)
        self.setWindowTitle("Edit separator")
        self.resize(640, 540)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "cbm_sep_editor")
        self._charset = charset
        # Internal byte buffer - exactly 16 PETSCII bytes, $A0
        # padded by default. Modified by typing, picker clicks,
        # quick-fill buttons.
        self._buf = bytearray(b'\xA0' * 16)
        self._cursor = 0  # next insert position
        # Extra rows from a multi-row PNG import. The first row is
        # always in self._buf; rows 2..N (if any) live here and get
        # picked up by the outer CBM disk dialog when it commits the
        # separator entry, so larger DirArt can be imported in one shot.
        self._sep_extra_rows = []

        from PyQt6.QtWidgets import (
            QGridLayout, QDialogButtonBox, QFrame,
        )
        outer = QVBoxLayout(self)
        outer.setSpacing(6)

        # Header
        hdr = QLabel("Click PETSCII glyphs below to insert into "
                     "the separator. Backspace removes the last "
                     "char. Use quick-fills for common patterns.")
        hdr.setWordWrap(True)
        outer.addWidget(hdr)

        # Live preview - render 1 line of "directory entry"
        # pixmap so the user sees exactly what shows up later.
        self._preview = QLabel()
        self._preview.setStyleSheet(
            "background-color: #3F3FD7; padding: 4px;")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignLeft
                                      | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._preview)

        # Quick-fill buttons.
        qf = QHBoxLayout()
        qf.setSpacing(4)
        qf.addWidget(QLabel("Quick fill:"))
        for label, fill_func in [
            ("Dashes",  lambda: self._fill(0x2D)),    # '-'
            ("Equals",  lambda: self._fill(0x3D)),    # '='
            ("Stars",   lambda: self._fill(0x2A)),    # '*'
            ("Blocks",  lambda: self._fill(0xA0)),    # solid block
            ("HLine",   lambda: self._fill(0xC3)),    # PETSCII top-left horiz
            ("VBar",    lambda: self._fill(0xDD)),    # PETSCII vertical line
            ("Diamond", lambda: self._fill(0x5A)),    # PETSCII diamond (Z gfx)
            ("Heart",   lambda: self._fill(0x53)),    # PETSCII heart (S gfx)
            ("Spade",   lambda: self._fill(0x41)),    # PETSCII spade (A gfx)
            ("Clear",   lambda: self._fill(0xA0)),    # blank
        ]:
            b = QPushButton(label)
            b.setMaximumWidth(60)
            b.clicked.connect(fill_func)
            qf.addWidget(b)
        qf.addSpacing(8)
        b_png = QPushButton("PNG...")
        b_png.setToolTip(
            "Import a PNG and convert it to a 16-character "
            "separator (scaled to 128x8 pixels, threshold-quantized "
            "to PETSCII glyphs).")
        b_png.setMaximumWidth(70)
        b_png.clicked.connect(self._import_png)
        qf.addWidget(b_png)
        qf.addStretch(1)
        outer.addLayout(qf)

        # Sample separators - clickable preset list with rendered
        # previews, like the "Separators" pane in DirMaster. Each
        # entry is a 16-byte PETSCII pattern that crackers /
        # collectors commonly use. Clicking sets the buffer to that
        # pattern.
        sample_label = QLabel("Sample separators (click to use):")
        sample_label.setStyleSheet(
            "QLabel { font-weight: bold; padding-top: 4px; }")
        outer.addWidget(sample_label)
        self._samples_list = self._build_samples_list()
        outer.addWidget(self._samples_list)

        # Picker grid: 16 cols x 16 rows = 256 codepoints.
        # Each cell is a small button rendering the glyph.
        # We use the existing render path so charset toggling
        # would work consistently.
        self._picker = self._build_picker_grid()
        outer.addWidget(self._picker, stretch=1)

        # Backspace button (couldn't bind Qt.Key_Backspace cleanly
        # to the dialog while picker has focus, so a button is
        # simpler).
        ctrl = QHBoxLayout()
        b_bs = QPushButton("Backspace")
        b_bs.setMaximumWidth(100)
        b_bs.clicked.connect(self._backspace)
        ctrl.addWidget(b_bs)
        b_clr = QPushButton("Clear all")
        b_clr.setMaximumWidth(100)
        b_clr.clicked.connect(self._clear_all)
        ctrl.addWidget(b_clr)
        ctrl.addStretch(1)
        outer.addLayout(ctrl)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._refresh_preview()

    # Pre-baked separator patterns à la DirMaster's "Separators" pane.
    # Each entry is a 16-byte PETSCII string that crackers and demo
    # disk authors commonly use to visually divide the directory. The
    # raw bytes use the C64 graphics codepoints directly:
    #   $40 = @ (used as filler in some patterns)
    #   $5B/$5D = [ ]  (brackets for box patterns)
    #   $A0 = shifted-space (CBM padding, also "solid block")
    #   $AC = lower-left block
    #   $AE = lower-right block
    #   $AF = lower-right corner thin
    #   $B0 = upper-right corner thin
    #   $B1 = upper-left corner thin
    #   $B2 = upper-half block
    #   $C0 = horizontal line (centre)
    #   $C2 = horizontal thicker
    #   $C3 = top horizontal line
    #   $C4 = ?
    #   $DD = vertical line
    #   $E2 = full block (also $A0 reversed)
    SAMPLE_SEPARATORS = [
        # Plain ASCII patterns
        ("Dashes",        b'-' * 16),
        ("Equals",        b'=' * 16),
        ("Stars",         b'*' * 16),
        ("Dots",          b'.' * 16),
        ("Underscores",   b'_' * 16),
        ("Hashes",        b'#' * 16),
        ("Plus",          b'+' * 16),

        # Boxed text style: brackets + dashes
        ("[ - dashes - ]",
         b'[' + b'-' * 14 + b']'),
        ("< - dashes - >",
         b'<' + b'-' * 14 + b'>'),
        ("== thick ==",
         b'=' * 4 + b' THICK ' + b'=' * 5),

        # PETSCII graphics patterns (all 16 bytes)
        ("Solid block (A0)",     bytes([0xA0]) * 16),
        ("Reverse block (E2)",   bytes([0xE2]) * 16),
        ("HLine top (C3)",       bytes([0xC3]) * 16),
        ("HLine mid (C0)",       bytes([0xC0]) * 16),
        ("VBars (DD)",           bytes([0xDD]) * 16),
        ("Half block top (B2)",  bytes([0xB2]) * 16),
        ("Half block bottom",    bytes([0xAC]) * 16),

        # Mixed graphics - the classic "boxy" looks
        ("Lower-half wave",
         bytes([0xAC, 0xAE]) * 8),
        ("Upper-half wave",
         bytes([0xB0, 0xB1]) * 8),
        ("Block alternating",
         bytes([0xA0, 0xE2]) * 8),
        ("HLine borders",
         bytes([0xB0]) + bytes([0xC3]) * 14 + bytes([0xAE])),
        ("Bracketed HLine",
         bytes([0xDD]) + bytes([0xC3]) * 14 + bytes([0xDD])),
        ("Diamond run",
         bytes([0x5A]) * 16),    # Z = PETSCII diamond
        ("Hearts",
         bytes([0x53]) * 16),    # S = PETSCII heart
        ("Spades",
         bytes([0x41]) * 16),    # A = PETSCII spade
        ("Clubs",
         bytes([0x58]) * 16),    # X = PETSCII club
        ("Circles",
         bytes([0x51]) * 16),    # Q = PETSCII circle
        ("Crosses",
         bytes([0x56]) * 16),    # V = PETSCII cross
    ]

    def _build_samples_list(self):
        """Build a QListWidget with one rendered preview per sample
        separator. Click selects + applies to the buffer.

        Each row is a small pixmap rendered through the same PETSCII
        path the directory uses, so the user sees exactly what the
        directory entry will look like. Rows are tall enough that
        the glyphs are legible (16-pixel cells)."""
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        from PyQt6.QtCore import QSize
        lst = QListWidget()
        lst.setStyleSheet(
            "QListWidget { background-color: #2a2a2a; }")
        lst.setIconSize(QSize(280, 18))
        lst.setSpacing(1)
        for name, pattern in self.SAMPLE_SEPARATORS:
            # Defensive: pad/truncate to exactly 16 PETSCII bytes
            pat = bytes(pattern[:16])
            pat = pat + b'\xA0' * (16 - len(pat))
            # Render the pattern as a single-line preview pixmap.
            # Use cell_size 16 for consistent appearance.
            pix = self._render_pattern_preview(pat)
            item = QListWidgetItem(QIcon(pix), name)
            item.setData(Qt.ItemDataRole.UserRole, pat)
            item.setToolTip(
                f"{name}\nPETSCII bytes: " +
                " ".join(f"{b:02X}" for b in pat))
            lst.addItem(item)
        lst.setMaximumHeight(180)
        lst.itemClicked.connect(self._on_sample_clicked)
        return lst

    def _render_pattern_preview(self, pattern: bytes,
                                  cell_size: int = 16) -> QPixmap:
        """Render a 16-byte PETSCII pattern as a one-row preview
        pixmap using the same font/colors as the live preview, so
        sample list and real preview match visually.

        Padded to 18 cells with a leading and trailing space to
        give the brackets/borders some visual breathing room.
        """
        from PyQt6.QtCore import QRect
        cells = 18
        img_w = cells * cell_size
        img_h = cell_size
        img = QImage(img_w, img_h, QImage.Format.Format_RGB32)
        bg_q = QColor(63, 63, 215)
        fg_q = QColor(255, 255, 255)
        img.fill(bg_q)
        font = QFont()
        font.setFamilies([
            "C64 Pro Mono", "C64 Pro", "PetMe 64", "PetMe",
            "Consolas", "DejaVu Sans Mono", "monospace",
        ])
        font.setPixelSize(cell_size)
        font.setLetterSpacing(
            QFont.SpacingType.PercentageSpacing, 100)
        p = QPainter(img)
        try:
            p.setFont(font)
            p.setRenderHint(
                QPainter.RenderHint.Antialiasing, False)
            p.setRenderHint(
                QPainter.RenderHint.TextAntialiasing, True)
            # First and last cell stay bg (border padding)
            for col_idx in range(16):
                glyph, byte_reverse = _petscii_to_pua_glyph(
                    pattern[col_idx], charset=self._charset)
                rect = QRect((col_idx + 1) * cell_size, 0,
                              cell_size, cell_size)
                if byte_reverse:
                    p.fillRect(rect, fg_q)
                    p.setPen(bg_q)
                else:
                    p.setPen(fg_q)
                p.drawText(
                    rect, int(Qt.AlignmentFlag.AlignCenter), glyph)
        finally:
            p.end()
        return QPixmap.fromImage(img)

    def _on_sample_clicked(self, item):
        """Apply the clicked sample to the buffer + reset cursor."""
        pattern = item.data(Qt.ItemDataRole.UserRole)
        if not pattern:
            return
        self._buf = bytearray(pattern[:16])
        if len(self._buf) < 16:
            self._buf += b'\xA0' * (16 - len(self._buf))
        self._cursor = 16    # at end - any further insert wraps via _insert
        self._refresh_preview()

    def _build_picker_grid(self):
        """16x16 grid of glyph buttons. Each button renders the
        PETSCII glyph at that codepoint as a small pixmap.

        Note: render_directory_to_pixmap pads every line to at
        least 40 columns, so calling it with `[bytes([code])]`
        gives a 40-cell-wide image where only the first cell is
        the glyph - the rest is bg padding. Setting that as a
        20x20 button icon shrinks the whole thing 32x and the
        glyph becomes invisible. Render single-cell pixmaps
        directly here instead.

        We use a 24-pixel cell so each glyph stays legible inside
        the 28x28 button. Font fallback chain: 'C64 Pro Mono'
        first (Quopus ships it for the directory rendering), then
        any monospaced system font as backup. Without one of
        these the picker shows blank rectangles - that's a
        font-installation issue, not a rendering bug.
        """
        from PyQt6.QtWidgets import QGridLayout, QFrame, QToolButton
        from PyQt6.QtCore import QRect
        wrap = QFrame()
        wrap.setStyleSheet("QFrame { background-color: #303030; }")
        grid = QGridLayout(wrap)
        grid.setSpacing(1)
        grid.setContentsMargins(2, 2, 2, 2)
        cell_size = 24
        # Use a font with explicit family fallbacks so the picker
        # still shows *something* (the unicode-PUA glyph as the
        # system's notdef) when C64 Pro Mono isn't installed. The
        # main directory-render code uses QFont("C64 Pro Mono")
        # exclusively, but for the picker we want graceful
        # degradation rather than 256 invisible cells.
        glyph_font = QFont()
        glyph_font.setFamilies([
            "C64 Pro Mono",
            "C64 Pro",
            "PetMe 64",
            "PetMe",
            "Consolas",
            "DejaVu Sans Mono",
            "monospace",
        ])
        glyph_font.setPixelSize(cell_size)
        glyph_font.setLetterSpacing(
            QFont.SpacingType.PercentageSpacing, 100)
        fg_q = QColor(255, 255, 255)
        bg_q = QColor(63, 63, 215)
        for code in range(256):
            row = code // 16
            col = code % 16
            btn = QToolButton()
            # Render this single PETSCII byte as a one-cell pixmap.
            img = QImage(cell_size, cell_size,
                          QImage.Format.Format_RGB32)
            img.fill(bg_q)
            glyph, byte_reverse = _petscii_to_pua_glyph(
                code, charset=self._charset)
            p = QPainter(img)
            try:
                p.setFont(glyph_font)
                p.setRenderHint(
                    QPainter.RenderHint.Antialiasing, False)
                p.setRenderHint(
                    QPainter.RenderHint.TextAntialiasing, True)
                rect = QRect(0, 0, cell_size, cell_size)
                if byte_reverse:
                    p.fillRect(rect, fg_q)
                    p.setPen(bg_q)
                else:
                    p.setPen(fg_q)
                p.drawText(
                    rect,
                    int(Qt.AlignmentFlag.AlignCenter),
                    glyph)
            finally:
                p.end()
            pix = QPixmap.fromImage(img)
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setFixedSize(28, 28)
            btn.setToolTip(
                f"PETSCII ${code:02X} ({code}) - click to insert")
            btn.clicked.connect(
                lambda _checked=False, c=code: self._insert(c))
            grid.addWidget(btn, row, col)
        return wrap

    def _insert(self, codepoint: int):
        """Insert one PETSCII byte at the cursor, advance cursor.
        Wraps around at 16 to overwrite from the start - means a
        16-byte filename can be fully built with 16 clicks. The
        wrap happens BEFORE the write so a cursor of 16 (set by
        _fill / end-of-buffer) doesn't IndexError."""
        self._cursor = self._cursor % 16
        self._buf[self._cursor] = codepoint & 0xFF
        self._cursor = (self._cursor + 1) % 16
        self._refresh_preview()

    def _backspace(self):
        """Remove the last inserted byte (set to $A0) and rewind
        the cursor. If the cursor is at 0, wrap to 15."""
        self._cursor = (self._cursor - 1) % 16
        self._buf[self._cursor] = 0xA0
        self._refresh_preview()

    def _clear_all(self):
        """Reset to all $A0 + cursor at 0."""
        self._buf = bytearray(b'\xA0' * 16)
        self._cursor = 0
        self._refresh_preview()

    def _fill(self, codepoint: int):
        """Quick-fill: replace the entire 16-byte buffer with the
        given codepoint. Useful for 'all dashes' / 'all blocks'
        type separators."""
        self._buf = bytearray([codepoint & 0xFF] * 16)
        self._cursor = 16    # at end - any further insert wraps
        self._refresh_preview()

    def _refresh_preview(self):
        """Render the current buffer using the same PETSCII path
        the directory listing uses, scale up so the user can see
        each glyph clearly."""
        # Build a fake "directory entry" line: 4 spaces (block#),
        # 2 spaces, the 16-byte name in quotes, then space + 'DEL<'
        # so the user sees exactly what will show up post-insert.
        # Format like a real directory line.
        line = bytearray()
        for ch in '   0  ':
            line.append(ord(ch))
        line.append(ord('"'))
        for b in self._buf:
            line.append(b)
        line.append(ord('"'))
        line.append(0x20)
        for ch in 'DEL<':
            line.append(ord(ch))
        pix = render_directory_to_pixmap(
            [bytes(line)],
            cell_size=20,    # 2.5x normal for readability
            charset=self._charset)
        self._preview.setPixmap(pix)
        self._preview.resize(pix.size())

    def result_petscii(self):
        """Return the 16-byte PETSCII buffer (already padded).
        Trailing $A0 is fine - that's the canonical CBM padding."""
        return bytes(self._buf)

    def result_petscii_rows(self):
        """Return the full list of 16-byte rows.

        For the standard single-line separator this is a 1-element
        list containing the same bytes as result_petscii(). When a
        multi-row PNG import was applied via _import_png(), this list
        carries the extra rows the caller should insert as additional
        separator entries (top to bottom). The caller is responsible
        for writing them in order.
        """
        rows = [bytes(self._buf)]
        extra = getattr(self, "_sep_extra_rows", None)
        if extra:
            rows.extend(extra)
        return rows

    def _import_png(self):
        """Importiere ein PNG und konvertiere es zu einem 16-PETSCII
        Separator.

        Workflow:
        - PNG laden (jede Groesse)
        - Skalieren auf 128x8 (Aspect Ratio ignoriert - so passt jedes
          Bild in eine Zeile)
        - Threshold 128 (weiss = FG, schwarz = BG)
        - In 16 Bloecke a 8x8 schneiden
        - Jeder Block: Hamming-Distance gegen alle 256 PETSCII-Glyphen
          (gerendert), naechster Match gewinnt
        - Buffer mit den 16 Bytes ueberschreiben + Preview-Update
        """
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        png_path, _ = QFileDialog.getOpenFileName(
            self, "Import PNG as separator",
            "", "PNG Images (*.png *.bmp *.jpg *.jpeg);;All Files (*)")
        if not png_path:
            return
        try:
            self._show_png_import_dialog(png_path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Import PNG",
                f"Failed to import:\n{e}")

    def _show_png_import_dialog(self, png_path):
        """Modaler Sub-Dialog mit Threshold/Invert/Fit-Optionen +
        Live-Preview, dann Apply schreibt in den Separator-Buffer.

        For multi-row PNG imports (Rows spinner > 1 in the dialog),
        row 1 goes into self._buf as usual; the remaining rows are
        stashed in self._sep_extra_rows so the outer disk-image
        dialog can write them as additional separator entries after
        the first one is committed."""
        src = QImage(png_path)
        if src.isNull():
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Import PNG",
                f"Could not load:\n{png_path}")
            return
        dlg = _SepPngImportDialog(png_path, src, self._charset, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rows = getattr(dlg, "result_rows", None)
            if rows and len(rows) >= 1:
                self._buf = bytearray(rows[0])
                self._sep_extra_rows = [bytes(r) for r in rows[1:]]
            else:
                # Backward compat fallback
                self._buf = bytearray(dlg.result_bytes)
                self._sep_extra_rows = []
            self._cursor = 16
            self._refresh_preview()


# -----------------------------------------------------------------
# Helper: render all 256 PETSCII glyphs as 8x8 bitmaps for matching.
# Cached per (charset_name) since rendering 256 glyphs takes ~200ms
# and we'd otherwise do it on every threshold-slider tick.
# -----------------------------------------------------------------

_PETSCII_GLYPH_8X8_CACHE = {}


def _petscii_to_screen_code(p):
    """Map PETSCII byte to its corresponding screen code (no reverse
    video involved - the ROM already stores reverse glyphs at screen
    codes $80-$FF).

    Standard mapping per sta.c64.org/cbm64pettoscr.html:
        $00-$1F: +$80  -> screen $80-$9F
        $20-$3F:  0    -> screen $20-$3F (same)
        $40-$5F: -$40  -> screen $00-$1F (@A-Z[..]_)
        $60-$7F: -$20  -> screen $40-$5F (graphics)
        $80-$9F: +$40  -> screen $C0-$DF (reverse of $80-$9F glyphs)
        $A0-$BF: -$40  -> screen $60-$7F (more graphics)
        $C0-$DF: -$80  -> screen $40-$5F (same as $60-$7F)
        $E0-$FE: -$80  -> screen $60-$7E
        $FF:           -> screen $5E (pi)

    Returns just the screen code (0-255). The character ROM has all
    256 codes including the pre-inverted $80-$FF reverse glyphs.
    """
    p &= 0xFF
    if p < 0x20:
        return p + 0x80
    if p < 0x40:
        return p
    if p < 0x60:
        return p - 0x40
    if p < 0x80:
        return p - 0x20
    if p < 0xA0:
        return p + 0x40
    if p < 0xC0:
        return p - 0x40
    if p < 0xE0:
        return p - 0x80
    if p < 0xFF:
        return p - 0x80
    return 0x5E    # PETSCII $FF = pi


def _build_petscii_glyph_bitmaps(charset='lower'):
    """Get all 256 PETSCII glyphs as 8-byte bitstrings, indexed
    directly from the bundled C64 chargen ROM.

    We use ROM-direct (PETSCII byte == ROM offset) instead of going
    through the C64 Pro Mono font path. Reasons:
    - ROM gives consistent results regardless of font installation
    - ROM byte $A0 is solid block (what users expect for separators),
      while the font's U+E0A0 codepoint is just an empty space
    - ROM matching is what makes PNG-import-to-PETSCII actually work
      visually: $A0 in the result renders to a visible block in the
      directory preview (via reverse-video of $20, the C64 hardware
      way) instead of empty.

    The bundled ROM is the original VICE chargen-906143-02.bin
    (4096 bytes = 2048 upper + 2048 lower).

    Cached per charset name ('upper' or 'lower').
    """
    if charset in _PETSCII_GLYPH_8X8_CACHE:
        return _PETSCII_GLYPH_8X8_CACHE[charset]

    glyphs = _build_glyphs_via_rom(charset)
    _PETSCII_GLYPH_8X8_CACHE[charset] = glyphs
    return glyphs


def _build_glyphs_via_rom(charset):
    """ROM-based fallback. Indexes the chargen ROM directly with the
    PETSCII byte as the screen code.

    The chargen ROM is *coincidentally* laid out so that this identity
    mapping gives a good match for what the C64 Pro Mono font shows
    (since both essentially follow the natural PETSCII-byte ordering).
    Specifically:
      $20 -> space
      $A0 -> solid block (was $20 inverted at ROM offset $A0)
      $E0 -> also solid block
      $41 -> spade (gfx 'A')
      ...

    For exact font-matched glyphs use _build_glyphs_via_font instead.
    This fallback is for when the C64 Pro Mono font isn't installed.
    """
    rom_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'c64_chargen.bin')
    if not os.path.isfile(rom_path):
        return [bytes(8)] * 256

    with open(rom_path, 'rb') as f:
        rom = f.read()
    base = 0 if charset == 'upper' else 2048

    result = []
    for petscii in range(256):
        # Identity: ROM offset == PETSCII byte. The ROM layout matches
        # the natural PETSCII byte ordering closely enough.
        glyph = rom[base + petscii * 8:base + petscii * 8 + 8]
        result.append(glyph)
    return result


class _SepPngImportDialog(QDialog):
    """PNG -> 16 PETSCII bytes converter for the separator editor.

    UI:
    - Source PNG preview (thresholded, scaled to 128x8)
    - Rendered preview using actual PETSCII glyphs
    - Threshold spinner + Invert checkbox + Fit-mode
    - OK applies the 16 bytes to the separator buffer
    """

    def __init__(self, png_path, src_image, charset_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            f"Import PNG as separator: {os.path.basename(png_path)}")
        self.resize(960, 720)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint)

        self._png_path = png_path
        self._src = src_image
        self._charset_name = charset_name
        # Custom charset.bin support. When set (path to a 2048-byte
        # raw charset binary), the matcher uses these glyphs instead
        # of the bundled C64 chargen ROM. Allows the user to match
        # against tools like tma@trsi.org's dirart-specific charsets
        # which yield far better visual quality than the generic ROM.
        self._custom_charset_path = ""
        self._custom_charset_bytes = None  # bytes of length 2048
        # Allowed char list - by default all 256 chars are matchable,
        # but the user can restrict to only DIR-legal codes for safer
        # output. None = no restriction.
        self._allowed_chars = None
        self._threshold = 128
        self._auto_threshold = True   # auto-compute via Otsu by default
        self._invert = False
        self._fit_mode = 'stretch'  # 'stretch' or 'fit' (aspect)
        # Multi-row support: rows >= 2 splits the image into N stacked
        # 16-char separator entries. Default is auto-computed from the
        # PNG aspect ratio so a 320x200 input gives ~10 rows.
        # result_bytes always covers the FIRST row (legacy behaviour);
        # rows >= 2 callers should use result_rows instead.
        sw, sh = src_image.width(), src_image.height()
        # Each char is 8x8 pixels, and each separator row is
        # 16 chars = 128 pixels wide. So natural row count is:
        #   rows = round(sh / (sw / 16))
        # For 320x200 -> 200 / 20 = 10 rows.
        if sw > 0 and sh > 0:
            auto_rows = max(1, round(sh / (sw / 16.0)))
        else:
            auto_rows = 1
        # Cap at 140 - a D64 holds 144 dir entries max, leave some
        # headroom for normal files. D71/D81 hold more so the cap
        # could be higher but 140 is a reasonable visual limit too.
        auto_rows = min(140, auto_rows)
        self._rows = auto_rows
        # Legacy single-row result (first row of result_rows)
        self.result_bytes = bytes([0xA0] * 16)
        # Multi-row result: list of 16-byte sequences. result_rows[0]
        # equals result_bytes for backward compat with single-row use.
        self.result_rows = [self.result_bytes]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        sw, sh = src_image.width(), src_image.height()
        info = QLabel(
            f"<b>Source:</b> {os.path.basename(png_path)} ({sw}x{sh})"
            f"<br>Image is converted to a monochrome <b>128 × (8 × N) "
            f"px</b> strip where <b>N = rows</b>. Each 8x8 block is "
            f"matched to the closest PETSCII glyph. With <b>Rows &gt; 1</b>, "
            f"the picture is imported as a stack of separator entries "
            f"(each one is a 16-char directory line) so larger DirArt "
            f"fits into the directory listing."
            f"<br>For your {sw}x{sh} input the natural row count is "
            f"<b>{self._rows}</b>.")
        info.setWordWrap(True)
        info.setStyleSheet("padding: 4px; background: #f0f0f0;")
        outer.addWidget(info)

        # Options
        from PyQt6.QtWidgets import QCheckBox, QSpinBox
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Rows:"))
        self.sp_rows = QSpinBox()
        self.sp_rows.setRange(1, 140)
        self.sp_rows.setValue(self._rows)
        self.sp_rows.setToolTip(
            "Number of separator entries to produce.\n"
            "Each row is 16 chars wide (= 128 pixels).\n"
            "Auto-detected from the PNG aspect ratio.\n"
            "When > 1, Apply writes N separator entries\n"
            "directly into the disk image (top to bottom).")
        self.sp_rows.valueChanged.connect(self._on_rows_changed)
        opts.addWidget(self.sp_rows)
        opts.addSpacing(12)
        self.cb_auto = QCheckBox("Auto threshold")
        self.cb_auto.setChecked(True)
        self.cb_auto.setToolTip(
            "Compute threshold automatically (Otsu's method). "
            "Disable to set manually below.")
        self.cb_auto.toggled.connect(self._on_auto_toggled)
        opts.addWidget(self.cb_auto)
        opts.addWidget(QLabel("Threshold:"))
        self.sp_thresh = QSpinBox()
        self.sp_thresh.setRange(1, 255)
        self.sp_thresh.setValue(128)
        self.sp_thresh.setSingleStep(8)
        self.sp_thresh.setEnabled(False)   # auto by default
        self.sp_thresh.valueChanged.connect(self._on_thresh_manual)
        opts.addWidget(self.sp_thresh)
        opts.addSpacing(12)
        self.cb_invert = QCheckBox("Invert")
        self.cb_invert.toggled.connect(self._on_changed)
        opts.addWidget(self.cb_invert)
        opts.addSpacing(12)
        opts.addWidget(QLabel("Fit:"))
        from PyQt6.QtWidgets import QComboBox
        self.cmb_fit = QComboBox()
        self.cmb_fit.addItem("Stretch to fit", "stretch")
        self.cmb_fit.addItem("Fit vertical (preserve aspect, pad/crop horiz)",
                              "fit_h")
        self.cmb_fit.addItem("Crop center (force 128 wide)", "crop")
        self.cmb_fit.setCurrentIndex(0)   # stretch is the sensible default
        self.cmb_fit.currentIndexChanged.connect(self._on_changed)
        opts.addWidget(self.cmb_fit)
        opts.addStretch(1)
        outer.addLayout(opts)

        # ---- Second options row: custom charset + allowed-chars ----
        # By default the matcher uses the bundled C64 chargen ROM,
        # which works for general-purpose separators but produces
        # mediocre output for proper DirArt. Loading a custom
        # charset.bin (e.g. tma@trsi.org's dirart-specific charset)
        # gives dramatically better matches because the matcher then
        # picks from glyphs that were already designed for DirArt.
        opts2 = QHBoxLayout()
        opts2.addWidget(QLabel("Charset:"))
        self.lbl_charset = QLabel("(built-in C64 chargen ROM)")
        self.lbl_charset.setStyleSheet(
            "padding: 2px 6px; background: #f5f5f5; "
            "border: 1px solid #ddd; min-width: 200px;")
        opts2.addWidget(self.lbl_charset, 1)
        self.btn_charset = QPushButton("Load charset.bin...")
        self.btn_charset.setToolTip(
            "Load a 2048-byte raw C64 charset binary. The matcher\n"
            "uses these 256 8x8 glyphs as the pool of candidates.\n"
            "For best DirArt results, use a charset designed for\n"
            "directory-listing art (e.g. tma@trsi.org's charset).")
        self.btn_charset.clicked.connect(self._on_load_charset)
        opts2.addWidget(self.btn_charset)
        self.btn_charset_reset = QPushButton("Reset")
        self.btn_charset_reset.setToolTip(
            "Reset to the built-in C64 chargen ROM.")
        self.btn_charset_reset.clicked.connect(self._on_reset_charset)
        self.btn_charset_reset.setEnabled(False)
        opts2.addWidget(self.btn_charset_reset)
        opts2.addSpacing(12)
        self.cb_dir_only = QCheckBox("DIR-safe chars only")
        self.cb_dir_only.setToolTip(
            "Restrict matcher output to bytes that are safe to use\n"
            "in a DEL filename (avoids the few control codes that\n"
            "can mess up directory parsers). Disable for max\n"
            "visual fidelity.")
        self.cb_dir_only.setChecked(False)
        self.cb_dir_only.toggled.connect(self._on_dir_only_toggled)
        opts2.addWidget(self.cb_dir_only)
        opts2.addStretch(1)
        outer.addLayout(opts2)

        # Previews: source + rendered, stacked since beide schmal
        # Previews: source threshold + ROM-rendered side-by-side
        # so the user can directly compare 'what the matcher sees'
        # to 'what it picked' at the same scale. The font-path
        # (1-row 'directory listing' style) preview goes below
        # as a thin strip for the legacy single-row use case.
        from PyQt6.QtWidgets import QSplitter
        prev_split = QSplitter(Qt.Orientation.Horizontal)
        prev_split.setChildrenCollapsible(False)

        prev_box = QGroupBox("Threshold preview (source)")
        pb_l = QVBoxLayout(prev_box)
        pb_l.setContentsMargins(4, 4, 4, 4)
        self.lbl_src = QLabel()
        self.lbl_src.setStyleSheet("background-color: #3F3FD7;")
        self.lbl_src.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pb_l.addWidget(self.lbl_src)
        prev_split.addWidget(prev_box)

        rom_box = QGroupBox(
            "ROM-rendered result (what the matcher picked)")
        rb_l = QVBoxLayout(rom_box)
        rb_l.setContentsMargins(4, 4, 4, 4)
        self.lbl_rom_render = QLabel()
        self.lbl_rom_render.setStyleSheet(
            "background-color: #3F3FD7;")
        self.lbl_rom_render.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        rb_l.addWidget(self.lbl_rom_render)
        prev_split.addWidget(rom_box)
        prev_split.setSizes([1, 1])
        outer.addWidget(prev_split, 1)

        # Font-path preview (single line) - shown for context when
        # rows == 1 since that's the classic separator use case
        out_box = QGroupBox(
            "Directory-listing preview (top row only)")
        ob_l = QVBoxLayout(out_box)
        ob_l.setContentsMargins(4, 4, 4, 4)
        self.lbl_out = QLabel()
        self.lbl_out.setStyleSheet("background-color: #3F3FD7;")
        ob_l.addWidget(self.lbl_out)
        # Hex display
        self.lbl_hex = QLabel()
        self.lbl_hex.setStyleSheet(
            f"font-family: 'Consolas', monospace; font-size: {scaled_font_px(11)}px; "
            "padding: 4px;")
        ob_l.addWidget(self.lbl_hex)
        outer.addWidget(out_box)

        # Buttons
        bar = QHBoxLayout()
        bar.addStretch(1)
        btn_ok = QPushButton("Apply to separator")
        btn_ok.setDefault(True)
        btn_ok.setStyleSheet("font-weight: bold;")
        btn_ok.clicked.connect(self.accept)
        bar.addWidget(btn_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        bar.addWidget(btn_cancel)
        outer.addLayout(bar)

        self._convert()

    def _on_changed(self):
        # Don't pick up the threshold spinbox here - that's only via
        # _on_thresh_manual. Auto-threshold is handled in _convert().
        self._invert = self.cb_invert.isChecked()
        self._fit_mode = self.cmb_fit.currentData()
        self._convert()

    def _on_rows_changed(self, v):
        self._rows = int(v)
        self._convert()

    def _on_load_charset(self):
        """Pop a file picker, load a 2048-byte raw C64 charset
        binary, then re-run conversion using these glyphs."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        # Default location: try the parent's _last_charset_dir if any
        start = ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load charset.bin",
            start,
            "Charset binaries (*.bin *.chr *.fnt *.64c);;"
            "All Files (*)")
        if not path:
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Load charset",
                f"Could not read:\n{e}")
            return
        # Accept several formats:
        # - 2048 bytes raw -> 256 8x8 chars
        # - 4096 bytes raw -> 512 chars (upper+lower), use first 256
        # - 2050 bytes .64c -> load address + 2048 data bytes
        if len(data) == 2050 and path.lower().endswith('.64c'):
            data = data[2:]
        if len(data) < 2048:
            QMessageBox.warning(self, "Load charset",
                f"Charset file is too small "
                f"({len(data)} bytes; need 2048).")
            return
        self._custom_charset_bytes = bytes(data[:2048])
        self._custom_charset_path = path
        import os
        self.lbl_charset.setText(os.path.basename(path))
        self.btn_charset_reset.setEnabled(True)
        self._convert()

    def _on_reset_charset(self):
        """Drop the custom charset and revert to bundled chargen ROM."""
        self._custom_charset_bytes = None
        self._custom_charset_path = ""
        self.lbl_charset.setText("(built-in C64 chargen ROM)")
        self.btn_charset_reset.setEnabled(False)
        self._convert()

    def _on_dir_only_toggled(self, on):
        if on:
            # Conservative whitelist of bytes that are safe in a DEL
            # filename and render visibly. This is the set used by
            # most existing DirArt tools.
            self._allowed_chars = set(
                # Space and graphical chars
                [0x20] + list(range(0x21, 0x40))   # ! " # ... 0-9 ; < = > ?
                + list(range(0x41, 0x5B))           # A-Z
                + list(range(0x60, 0x80))           # graphics
                + list(range(0xA0, 0xC0))           # more graphics
                + list(range(0xC0, 0xE0))           # uppercase + graphics
            )
        else:
            self._allowed_chars = None
        self._convert()

    def _get_glyphs(self):
        """Return the 256 glyph patterns the matcher should use.

        With a custom charset loaded, slice it into 256 x 8-byte
        strings; otherwise fall back to the bundled ROM."""
        if self._custom_charset_bytes is not None:
            data = self._custom_charset_bytes
            return [data[i*8:(i+1)*8] for i in range(256)]
        return _build_petscii_glyph_bitmaps(self._charset_name)

    def _on_auto_toggled(self, checked):
        self._auto_threshold = checked
        self.sp_thresh.setEnabled(not checked)
        self._convert()

    def _on_thresh_manual(self, value):
        # Only takes effect when auto is off
        if not self._auto_threshold:
            self._threshold = value
            self._convert()

    def _compute_otsu(self, gray_image):
        """Compute Otsu's optimal threshold for a Grayscale8 QImage."""
        w = gray_image.width()
        h = gray_image.height()
        histogram = [0] * 256
        for y in range(h):
            sl = gray_image.scanLine(y).asarray(w)
            for x in range(w):
                histogram[sl[x]] += 1
        total = w * h
        if total == 0:
            return 128
        sum_total = sum(i * histogram[i] for i in range(256))
        sum_b = 0.0
        w_b = 0
        max_var = 0.0
        best_t = 128
        for t in range(256):
            w_b += histogram[t]
            if w_b == 0:
                continue
            w_f = total - w_b
            if w_f == 0:
                break
            sum_b += t * histogram[t]
            m_b = sum_b / w_b
            m_f = (sum_total - sum_b) / w_f
            var = w_b * w_f * (m_b - m_f) ** 2
            if var > max_var:
                max_var = var
                best_t = t
        return best_t

    def _convert(self):
        """PNG -> 128 x (8*rows) bitmap -> rows * 16 bytes.

        For rows == 1 this is the legacy single-line separator (the
        existing CBM Sep+ workflow). For rows > 1 the image is treated
        as a stack of N separator entries, each 16 chars wide x 8 px
        tall, producing self.result_rows = list of N x 16-byte strings.
        """
        rows = max(1, int(self._rows))
        target_w = 128
        target_h = 8 * rows
        # 1. Scale to 128 x (8 * rows) depending on fit mode
        src = self._src
        w0, h0 = src.width(), src.height()
        if self._fit_mode == 'stretch':
            scaled = src.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        elif self._fit_mode == 'fit_h':
            # Preserve aspect, scale to target_h tall. If the result
            # is wider than 128 we center-crop; if narrower we center
            # and pad with BG. Either way the source's vertical
            # content is preserved 1:1 - this is the mode you want
            # for "natural-looking DirArt".
            ratio = float(target_h) / h0 if h0 > 0 else 1.0
            tw = max(1, int(w0 * ratio))
            scaled_full = src.scaled(
                tw, target_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            if tw <= target_w:
                # Pad horizontally to 128 with BG-black
                scaled = QImage(target_w, target_h,
                                  QImage.Format.Format_RGB888)
                scaled.fill(QColor(0, 0, 0))
                from PyQt6.QtGui import QPainter
                p = QPainter(scaled)
                try:
                    off_x = max(0, (target_w - tw) // 2)
                    p.drawImage(off_x, 0, scaled_full)
                finally:
                    p.end()
            else:
                # tw > 128: center-crop horizontally
                off_x = (tw - target_w) // 2
                scaled = scaled_full.copy(off_x, 0,
                                              target_w, target_h)
        else:  # crop - same as fit_h but always center-crops
            ratio = float(target_h) / h0 if h0 > 0 else 1.0
            tw = int(w0 * ratio)
            scaled_full = src.scaled(
                max(tw, target_w), target_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            off_x = max(0, (scaled_full.width() - target_w) // 2)
            scaled = scaled_full.copy(off_x, 0, target_w, target_h)

        # 2. Grayscale + Threshold
        gray = scaled.convertToFormat(QImage.Format.Format_Grayscale8)
        if self._auto_threshold:
            self._threshold = self._compute_otsu(gray)
            self.sp_thresh.blockSignals(True)
            self.sp_thresh.setValue(self._threshold)
            self.sp_thresh.blockSignals(False)

        bitmap = bytearray(target_w * target_h)
        for y in range(target_h):
            sl = gray.scanLine(y).asarray(target_w)
            line_bytes = bytes(sl)
            for x in range(target_w):
                v = line_bytes[x] >= self._threshold
                if self._invert:
                    v = not v
                bitmap[y * target_w + x] = 1 if v else 0

        # 3. Source threshold preview - display dimensions are
        # computed from the source PNG aspect ratio (not the
        # synthetic 128x(8*N) target), so the user sees the picture
        # in its real proportions and not stretched/squished. The
        # two previews are side-by-side now, so each gets ~430px max.
        src_img = QImage(target_w, target_h,
                          QImage.Format.Format_Grayscale8)
        for y in range(target_h):
            sl = src_img.scanLine(y).asarray(target_w)
            for x in range(target_w):
                sl[x] = 255 if bitmap[y * target_w + x] else 0
        # Target display size: respect source PNG aspect, cap at the
        # available dialog area so we don't overflow.
        max_disp_w = 430  # side-by-side: each half of dialog
        max_disp_h = 460  # vertical budget for previews
        if w0 > 0 and h0 > 0:
            src_aspect = w0 / h0   # width / height
        else:
            src_aspect = target_w / max(1, target_h)
        # Start at max_disp_w; if too tall, scale by height
        disp_w = max_disp_w
        disp_h = int(disp_w / src_aspect)
        if disp_h > max_disp_h:
            disp_h = max_disp_h
            disp_w = int(disp_h * src_aspect)
        disp_w = max(disp_w, 64)
        disp_h = max(disp_h, 16)
        pm_src = QPixmap.fromImage(src_img).scaled(
            disp_w, disp_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_src.setPixmap(pm_src)

        # 4. Match each 8x8 block to closest PETSCII glyph. Now we have
        # rows*16 blocks total.
        glyphs = self._get_glyphs()
        # Restrict candidate set if the user enabled "DIR-safe only"
        if self._allowed_chars is not None:
            candidates = [c for c in range(256)
                            if c in self._allowed_chars]
        else:
            candidates = list(range(256))
        all_rows = []
        for ry in range(rows):
            result = bytearray(16)
            for bx in range(16):
                target = bytearray(8)
                for r in range(8):
                    b = 0
                    for col in range(8):
                        if bitmap[(ry * 8 + r) * target_w
                                    + bx * 8 + col]:
                            b |= (0x80 >> col)
                    target[r] = b
                best_code = candidates[0]
                best_dist = 65
                for code in candidates:
                    gbits = glyphs[code]
                    d = 0
                    for i in range(8):
                        d += bin(target[i] ^ gbits[i]).count('1')
                        if d >= best_dist:
                            break
                    if d < best_dist:
                        best_dist = d
                        best_code = code
                        if d == 0:
                            break
                result[bx] = best_code
            all_rows.append(bytes(result))
        self.result_rows = all_rows
        self.result_bytes = all_rows[0]   # legacy single-row alias

        # 5. Rendered preview - stack all rows together. Font path
        # (proper rendered glyphs via render_directory_to_pixmap) is
        # only shown for row 1 to keep the dialog compact when N is
        # large; ROM path shows the full stack.
        try:
            line = bytearray()
            line.extend(b'   0  "')
            line.extend(all_rows[0])
            line.extend(b'" DEL<')
            pix = render_directory_to_pixmap(
                [bytes(line)],
                cell_size=20,
                charset=self._charset_name)
            self.lbl_out.setPixmap(pix)
        except Exception:
            pass

        # ROM path: full N-row stack using the same display
        # dimensions as the source preview so the user can compare
        # them 1:1.
        rom_render = QImage(target_w, target_h,
                              QImage.Format.Format_Grayscale8)
        rom_render.fill(0)
        for ry, row_bytes in enumerate(all_rows):
            for ci, b in enumerate(row_bytes):
                g = glyphs[b]
                for y in range(8):
                    sl = rom_render.scanLine(ry * 8 + y).asarray(target_w)
                    for x in range(8):
                        if g[y] & (0x80 >> x):
                            sl[ci * 8 + x] = 255
        pm_rom = QPixmap.fromImage(rom_render).scaled(
            disp_w, disp_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation)
        if hasattr(self, 'lbl_rom_render'):
            self.lbl_rom_render.setPixmap(pm_rom)

        # 6. Hex info - first row inline, rest as "(+N more rows)"
        hex_str = " ".join(f"{b:02X}" for b in all_rows[0])
        if rows > 1:
            self.lbl_hex.setText(
                f"PETSCII bytes (row 1 of {rows}): {hex_str}\n"
                f"Total: {rows * 16} bytes across {rows} separator "
                f"entries")
        else:
            self.lbl_hex.setText(f"PETSCII bytes: {hex_str}")


# ============================================================
# Database scanner helper
# ============================================================


from dataclasses import dataclass as _dataclass, field as _field
from typing import List as _List, Optional as _Optional
from .config import scaled_font_px


@_dataclass
class _DiskDbEntry:
    """One file entry packed for DB insertion. Simpler than
    CbmDirEntry - all we need is the fields the scanner will
    store, plus the MD5 of the extracted contents."""
    name: str
    file_type: str        # 'prg' / 'seq' / 'usr' / 'rel' / 'del'
    size_blocks: int
    size_bytes: _Optional[int]
    md5: _Optional[str]
    track: int
    sector: int


@_dataclass
class _DiskDbInfo:
    """Everything the database scanner wants to know about a
    disk image. Built by parse_disk_image() below."""
    disk_name: str
    disk_id: str
    dos_type: str
    image_type: str       # 'd64', 'd71', 'd81'
    track_count: int
    entries: _List[_DiskDbEntry] = _field(default_factory=list)


def parse_disk_image(data: bytes) -> _Optional[_DiskDbInfo]:
    """Parse a D64/D71/D81 byte buffer into a DiskDbInfo for
    the database scanner. Extracts the disk header and every
    directory entry, computes the MD5 of each PRG/SEQ/USR/REL.

    Returns None if the buffer doesn't look like a valid disk
    image (wrong size, can't determine type, etc).

    This is a thin wrapper around CbmDiskReader.from_bytes()
    that converts to the simpler dataclass form the DB needs.
    """
    import hashlib

    # Determine image type from size. The standard CBM image
    # sizes are:
    #   D64: 174848 (35 tracks no errors) or 175531 (with errors)
    #         or 196608 (40 tracks) or 197376 (40 + errors)
    #   D71: 349696 (70 tracks no errors) or 351062 (with errors)
    #   D81: 819200 (80 tracks no errors) or 822400 (with errors)
    size = len(data)
    image_type = None
    if size in (174848, 175531, 196608, 197376):
        image_type = "d64"
    elif size in (349696, 351062):
        image_type = "d71"
    elif size in (819200, 822400):
        image_type = "d81"
    elif size >= 8 and data[:8] == b"GCR-1541":
        # G64 GCR raw-track dump. We can't BAM-walk it because
        # the sectors are stored as GCR-encoded byte streams,
        # not flat blocks. Return None so the caller falls back
        # to "no directory" handling (file gets indexed by MD5,
        # contents marked as un-cataloguable).
        return None
    else:
        # Unknown size - return None rather than guessing.
        # Custom-sized images are rare and indexing them with
        # wrong sector math would corrupt the DB.
        return None

    # Build a reader on the in-memory bytes
    try:
        reader = CbmDiskReader.from_bytes(
            data, image_type, display_name="(scanned)")
        reader.open()
    except Exception:
        return None

    # Decode header fields. The raw bytes are PETSCII; we
    # decode to ASCII for display + DB storage but keep the
    # raw bytes available too.
    disk_name = _petscii_filename_to_ascii(reader.disk_name_raw)
    # disk_id is 5 bytes: 2 ID + space + 2 DOS, e.g. "ID 2A"
    disk_id_raw = reader.disk_id_raw or b""
    disk_id_decoded = _petscii_filename_to_ascii(disk_id_raw)
    # Split the DOS type out of the trailing 2 bytes - useful as
    # its own column even though it's also visible in disk_id
    dos_type = disk_id_decoded[-2:] if len(disk_id_decoded) >= 2 else ""
    # Track count is image-type dependent
    track_count = {"d64": 35, "d71": 70, "d81": 80}.get(image_type, 0)

    info = _DiskDbInfo(
        disk_name=disk_name,
        disk_id=disk_id_decoded,
        dos_type=dos_type,
        image_type=image_type,
        track_count=track_count,
        entries=[])

    # For each directory entry, extract the file content and
    # compute its MD5. PRG/SEQ/USR/REL only - Mario said
    # explicitly to skip DEL and SCRATCHED entries.
    for ent in reader.entries:
        type_label = ent.type_label.lower()
        if type_label not in ("prg", "seq", "usr", "rel"):
            continue
        # Try to extract. For very corrupt images this can fail
        # mid-file - we treat that as "no MD5" rather than
        # crashing the whole scan.
        try:
            file_bytes = reader.extract(ent)
            md5 = hashlib.md5(file_bytes).hexdigest()
            size_bytes = len(file_bytes)
        except Exception:
            file_bytes = None
            md5 = None
            size_bytes = None

        info.entries.append(_DiskDbEntry(
            name=ent.name_ascii,
            file_type=type_label,
            size_blocks=ent.blocks,
            size_bytes=size_bytes,
            md5=md5,
            track=ent.start_track,
            sector=ent.start_sector))

    reader.close()
    return info


class _CbmPetsciiDialog(QDialog):
    """Modal-ish PETSCII viewer dialog for CBM directory
    entries. Takes a raw byte block and renders it as a grid
    of C64 PETSCII glyphs, with the same Charset Lo/Hi toggle,
    cell-size +/- and column-count selector that the CRT
    toolkit's PETSCII tab provides. Used by the D64/D81
    viewer's right-click 'Show as PETSCII' menu entry.

    The bytes are rendered with the standard
    `render_directory_to_pixmap` helper (same one the disk
    directory display uses), which means the output looks
    identical to what the C64 would put on screen if it
    POKE'd those bytes into screen memory at $0400.
    """

    def __init__(self, parent, title: str, data: bytes,
                 load_addr=None, initial_charset="lower"):
        super().__init__(parent)
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
            QPushButton, QScrollArea,
        )
        self.setWindowTitle(f"PETSCII view - {title}")
        self.resize(900, 700)
        self.setWindowFlags(
            _Qt.WindowType.Window
            | _Qt.WindowType.WindowCloseButtonHint
            | _Qt.WindowType.WindowMinMaxButtonsHint)

        self._data = bytes(data)
        self._cell = 16
        self._cols = 64
        self._charset = initial_charset

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Header line: filename + size + optional load address.
        # Gives context inside the window so the user remembers
        # which file they're looking at when several are open.
        hdr_text = (f"{title}  -  {len(self._data)} bytes")
        if load_addr is not None:
            hdr_text += (
                f"  (PRG body, original load address "
                f"${load_addr:04X})")
        hdr = QLabel(hdr_text)
        hdr.setStyleSheet(
            "QLabel { color: #ddd; padding: 2px 6px; "
            "font-family: 'Courier New', monospace; }")
        lay.addWidget(hdr)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(4)
        bar.addWidget(QLabel("Charset:"))
        self._cb_charset = QComboBox()
        self._cb_charset.addItem("Lo (mixed case)", "lower")
        self._cb_charset.addItem("Hi (UPPER + graphics)", "upper")
        if initial_charset == "upper":
            self._cb_charset.setCurrentIndex(1)
        self._cb_charset.currentIndexChanged.connect(
            self._on_controls_changed)
        bar.addWidget(self._cb_charset)
        bar.addSpacing(12)

        bar.addWidget(QLabel("Cell:"))
        btn_smaller = QPushButton("-")
        btn_smaller.setMaximumWidth(28)
        btn_smaller.clicked.connect(
            lambda: self._adjust_cell(-2))
        bar.addWidget(btn_smaller)
        self._lbl_cell = QLabel("16 px")
        bar.addWidget(self._lbl_cell)
        btn_bigger = QPushButton("+")
        btn_bigger.setMaximumWidth(28)
        btn_bigger.clicked.connect(
            lambda: self._adjust_cell(+2))
        bar.addWidget(btn_bigger)
        bar.addSpacing(12)

        bar.addWidget(QLabel("Width:"))
        self._cb_cols = QComboBox()
        for c in (16, 32, 40, 48, 64, 80):
            self._cb_cols.addItem(f"{c} cols", c)
        self._cb_cols.setCurrentIndex(4)   # 64 cols default
        self._cb_cols.currentIndexChanged.connect(
            self._on_controls_changed)
        bar.addWidget(self._cb_cols)
        bar.addStretch(1)

        btn_save = QPushButton("Save PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        lay.addLayout(bar)

        # Pixmap area in a scroll view because a large file at
        # 16 px/cell can easily produce a 1024x4000+ image.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setStyleSheet(
            "QScrollArea { background: #3F3FD7; }")
        self._lbl_pixmap = QLabel()
        self._lbl_pixmap.setStyleSheet(
            "background-color: #3F3FD7;")
        self._scroll.setWidget(self._lbl_pixmap)
        lay.addWidget(self._scroll, 1)

        self._refresh()

    def _on_controls_changed(self, *_a):
        self._cols = self._cb_cols.currentData() or 64
        self._charset = (
            self._cb_charset.currentData() or "lower")
        self._refresh()

    def _adjust_cell(self, delta: int):
        self._cell = max(8, min(48, self._cell + delta))
        self._lbl_cell.setText(f"{self._cell} px")
        self._refresh()

    def _refresh(self):
        """Slice the byte buffer into rows of self._cols bytes
        and let render_directory_to_pixmap turn each row into
        a PETSCII line. We prepend an empty header row so the
        renderer's row-0 reverse-video logic doesn't trigger
        on real bytes (same trick the CRT viewer uses)."""
        lines = []
        for off in range(0, len(self._data), self._cols):
            lines.append(bytes(
                self._data[off:off + self._cols]))
        if not lines:
            self._lbl_pixmap.setText("(empty)")
            return
        padded = [b''] + lines
        try:
            pix = render_directory_to_pixmap(
                padded, cell_size=self._cell,
                charset=self._charset)
        except Exception as e:
            self._lbl_pixmap.setText(
                f"PETSCII render failed:\n{e}")
            return
        cropped = pix.copy(
            0, self._cell, pix.width(),
            pix.height() - self._cell)
        self._lbl_pixmap.setPixmap(cropped)
        self._lbl_pixmap.adjustSize()
        self._current_pixmap = cropped

    def _save_png(self):
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        pix = getattr(self, "_current_pixmap", None)
        if pix is None or pix.isNull():
            QMessageBox.warning(
                self, "Save PNG",
                "Nothing rendered yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PETSCII view as PNG",
            f"{self.windowTitle()}.png",
            "PNG Images (*.png)")
        if not path:
            return
        if not pix.save(path, "PNG"):
            QMessageBox.warning(
                self, "Save PNG",
                f"Could not save {path}.")


# C64 multicolor palette (Pepto's measured-from-CRT values).
# Same data the other Quopus viewers use; duplicated here so
# this module doesn't have to import from palette.py and pull
# in extra dependencies for what's a self-contained renderer.
_WOTW_C64_PALETTE = [
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF),
    (0x88, 0x39, 0x32), (0x67, 0xB6, 0xBD),
    (0x8B, 0x3F, 0x96), (0x55, 0xA0, 0x49),
    (0x40, 0x31, 0x8D), (0xBF, 0xCE, 0x72),
    (0x8B, 0x54, 0x29), (0x57, 0x42, 0x00),
    (0xB8, 0x69, 0x62), (0x50, 0x50, 0x50),
    (0x78, 0x78, 0x78), (0x94, 0xE0, 0x89),
    (0x78, 0x69, 0xC4), (0x9F, 0x9F, 0x9F),
]


def _wotw_decode_rle(stream, target):
    """Decompress the WotW .bin RLE stream into exactly `target`
    bytes. The format used by the game is `00 N B` = N copies
    of byte B, with anything else being a literal. The game's
    own loader stops as soon as it has produced `target` output
    bytes (size declared in the 2-byte header right before the
    stream) - so do we. Any input bytes past that point are
    auxiliary data the game uses for something else (sprites,
    palette tweaks, anim frames - the format is undocumented).

    Returns the decompressed bytes, padded with zeros if the
    stream runs short. Never raises - corrupt/unknown input
    just produces a noisy image, which is more useful for the
    viewer than an error dialog.
    """
    out = bytearray()
    i = 0
    L = len(stream)
    while i < L and len(out) < target:
        b0 = stream[i]
        if b0 == 0x00 and i + 2 < L:
            n = stream[i + 1]
            b = stream[i + 2]
            n = min(n, target - len(out))
            out.extend([b] * n)
            i += 3
        else:
            out.append(b0)
            i += 1
    while len(out) < target:
        out.append(0)
    return bytes(out)


def _wotw_decode_bin(raw):
    """Take a .bin PRG (with the 2-byte load address still on)
    and return (bitmap_bytes, load_addr, rows). The number of
    rows comes from the declared decompressed size - 8000 bytes
    is a full 25-row C64 bitmap, 6080 is a 19-row partial used
    by the in-game scene viewer.

    The load address tells the decoder which path to take:
        $2000 / $2FA0 / similar : raw uncompressed bitmap (the
            file is the bitmap, loaded directly into bitmap RAM)
        $9EA0 / other 'buffer'  : RLE compressed, the next 2
            bytes give the decompressed size, the rest is RLE
    """
    if len(raw) < 4:
        return b"", 0, 25
    load = raw[0] | (raw[1] << 8)
    body = raw[2:]
    # Files loaded at a real C64 bitmap base ($2000 / $2FA0) are
    # raw; everything else (most commonly $9EA0) is compressed.
    if load in (0x2000, 0x4000, 0x6000, 0x8000, 0xA000,
                  0xC000, 0xE000, 0x2FA0):
        bitmap = body
        size = len(bitmap)
    else:
        size = body[0] | (body[1] << 8)
        bitmap = _wotw_decode_rle(body[2:], size)
    # Decide row count from the decoded size.
    if size >= 8000:
        rows = 25
    else:
        rows = max(1, size // (40 * 8))
    return bitmap, load, rows


def _wotw_decode_col(raw, rows):
    """Parse a .col file into (screen_ram, color_ram, bg).
    Layout based on observed files:
        screen_ram   = rows * 40 bytes
                       (upper nibble = pixel-pair color 01,
                        lower nibble = pixel-pair color 10)
        color_ram    = rows * 40 bytes (lower nibble = color 11)
        background   = 1 byte (the $D021 register, color 00)
    """
    if len(raw) < 4:
        return b"", b"", 0
    body = raw[2:]   # skip 2-byte load address
    cells = rows * 40
    screen = bytes(body[0:cells])
    color = bytes(body[cells:cells * 2])
    bg = body[cells * 2] if len(body) > cells * 2 else 0
    return screen, color, bg & 0x0F


def _wotw_render_bitmap(bitmap, screen, color, bg,
                          cols=40, rows=19):
    """Render a multicolor bitmap into a QImage. C64 multicolor
    pairs every 2 horizontal bits as one pixel, doubled in
    width when shown - so the on-screen image is cols*8 wide
    (in pixels) but visually 4 'fat' pixels per char-cell.
    """
    from PyQt6.QtGui import QImage, qRgb
    w = cols * 8
    h = rows * 8
    img = QImage(w, h, QImage.Format.Format_RGB32)
    # Pre-resolve background colour once - it doesn't change.
    bg_rgb = qRgb(*_WOTW_C64_PALETTE[bg & 0x0F])
    for cr in range(rows):
        for cc in range(cols):
            ci = cr * cols + cc
            sb = screen[ci] if ci < len(screen) else 0
            cb = color[ci] if ci < len(color) else 0
            c01 = (sb >> 4) & 0x0F
            c10 = sb & 0x0F
            c11 = cb & 0x0F
            rgb01 = qRgb(*_WOTW_C64_PALETTE[c01])
            rgb10 = qRgb(*_WOTW_C64_PALETTE[c10])
            rgb11 = qRgb(*_WOTW_C64_PALETTE[c11])
            for cy in range(8):
                bidx = ci * 8 + cy
                if bidx >= len(bitmap):
                    byte = 0
                else:
                    byte = bitmap[bidx]
                py = cr * 8 + cy
                for px in range(4):
                    bits = (byte >> ((3 - px) * 2)) & 0x03
                    if bits == 0:
                        rgb = bg_rgb
                    elif bits == 1:
                        rgb = rgb01
                    elif bits == 2:
                        rgb = rgb10
                    else:
                        rgb = rgb11
                    x = cc * 8 + px * 2
                    img.setPixel(x, py, rgb)
                    img.setPixel(x + 1, py, rgb)
    return img


class _WotwImageDialog(QDialog):
    """Renderer for War of the Worlds (and related C64 games)
    that store images as a .bin (bitmap) + .col (screen RAM +
    color RAM + background) pair. The display reproduces what
    a C64 in multicolor bitmap mode would show.

    Compressed .bin files (those loaded at $9EA0 or similar
    buffer addresses) are decoded with the simple `00 N B`
    RLE scheme the game appears to use. The decoder is best-
    effort - the exact format isn't documented and some
    decompressed images may show small pixel-shift artifacts.
    Uncompressed .bin files (those loaded at a real bitmap
    base like $2000) come out pixel-perfect.
    """

    def __init__(self, parent, bin_name, col_name,
                 bin_raw, col_raw,
                 dat_name=None, dat_raw=None):
        super().__init__(parent)
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QScrollArea, QComboBox, QSplitter,
            QPlainTextEdit,
        )
        self.setWindowTitle(f"Picture - {bin_name}")
        # Wider default when we have a .dat panel to show, so
        # the text doesn't get squeezed against the bitmap.
        self.resize(1200 if dat_raw else 900, 650)
        self.setWindowFlags(
            _Qt.WindowType.Window
            | _Qt.WindowType.WindowCloseButtonHint
            | _Qt.WindowType.WindowMinMaxButtonsHint)

        self._bin_name = bin_name
        self._col_name = col_name
        self._dat_name = dat_name
        self._bin_raw = bytes(bin_raw)
        self._col_raw = bytes(col_raw)
        self._dat_raw = bytes(dat_raw) if dat_raw else None
        self._zoom = 2

        self._bitmap, self._load, self._rows = \
            _wotw_decode_bin(self._bin_raw)
        self._screen, self._color, self._bg = \
            _wotw_decode_col(self._col_raw, self._rows)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        hdr_parts = [
            f"<b>{bin_name}</b> ({len(self._bin_raw)} bytes, "
            f"load ${self._load:04X})",
            f"<b>{col_name}</b> ({len(self._col_raw)} bytes)",
        ]
        if self._dat_raw is not None:
            hdr_parts.append(
                f"<b>{dat_name}</b> "
                f"({len(self._dat_raw)} bytes, text data)")
        hdr_parts.append(
            f"{self._rows} rows  -  bg=${self._bg:X}")
        hdr = QLabel("  +  ".join(hdr_parts))
        hdr.setStyleSheet(
            "QLabel { color: #ddd; padding: 2px 6px; "
            "font-family: 'Courier New', monospace; }")
        lay.addWidget(hdr)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Zoom:"))
        for z in (1, 2, 3, 4):
            b = QPushButton(f"{z}x")
            b.setMaximumWidth(36)
            b.clicked.connect(
                lambda _c=False, zoom=z: self._set_zoom(zoom))
            bar.addWidget(b)
        bar.addStretch(1)
        btn_save = QPushButton("Save PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        lay.addLayout(bar)

        # If we have .dat data, split the working area between
        # the image (left) and the extracted text strings (right).
        # Otherwise just take the full width for the image.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setStyleSheet(
            "QScrollArea { background: #000; }")
        self._lbl = QLabel()
        self._lbl.setStyleSheet("background-color: #000;")
        self._scroll.setWidget(self._lbl)

        if self._dat_raw is not None:
            splitter = QSplitter(_Qt.Orientation.Horizontal)
            splitter.addWidget(self._scroll)
            # Side panel: extracted text strings + raw hex
            # preview. The .dat doesn't contribute pixels - in
            # WotW and similar adventure games it's the room's
            # vocabulary and text descriptions. We surface what
            # we can find without trying to fully decode the
            # game's tokenizer.
            side = QPlainTextEdit()
            side.setReadOnly(True)
            side.setStyleSheet(
                "QPlainTextEdit { background: #1e1e1e; "
                "color: #e0e0e0; "
                "font-family: 'Courier New', monospace; "
                "font-size: 11px; }")
            side.setPlainText(
                self._format_dat_panel(self._dat_raw))
            splitter.addWidget(side)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            lay.addWidget(splitter, 1)
        else:
            lay.addWidget(self._scroll, 1)

        self._refresh()

    def _format_dat_panel(self, dat_raw):
        """Format the .dat contents for the side panel. We show
        a header with the load address + body size, then any
        printable text strings we can find (likely the
        adventure-game vocabulary). This is informational only -
        no decoding of the game-specific binary format. The
        user can spot strings like 'POLICEMEN' or 'WHITE FLAG'
        and confirm what scene the file belongs to."""
        if len(dat_raw) < 4:
            return "(file too short)"
        load = dat_raw[0] | (dat_raw[1] << 8)
        body = dat_raw[2:]
        lines = []
        lines.append(
            f"Load address: ${load:04X}")
        lines.append(
            f"Body size:    {len(body)} bytes")
        lines.append("")
        lines.append("Extracted strings:")
        lines.append("-" * 32)
        # Find runs of printable PETSCII bytes ($20-$5F is
        # upper-case + digits + symbols; $60-$7F is the
        # lower-case range in PETSCII).
        in_run = False
        run_start = 0
        found = []
        for i, b in enumerate(body):
            if (0x20 <= b <= 0x5F) or (0x60 <= b <= 0x7F):
                if not in_run:
                    run_start = i
                    in_run = True
            else:
                if in_run and i - run_start >= 6:
                    s = ''.join(
                        chr(c) if 0x20 <= c < 0x7F else '.'
                        for c in body[run_start:i])
                    found.append((run_start, s))
                in_run = False
        # Catch a run that goes to the very end of the file.
        if in_run and len(body) - run_start >= 6:
            s = ''.join(
                chr(c) if 0x20 <= c < 0x7F else '.'
                for c in body[run_start:])
            found.append((run_start, s))
        if not found:
            lines.append(
                "(no printable strings of length >= 6)")
        else:
            for off, s in found[:200]:   # cap at 200 to be safe
                lines.append(f"${off:04X}  {s}")
            if len(found) > 200:
                lines.append(
                    f"... and {len(found) - 200} more")
        return "\n".join(lines)

    def _set_zoom(self, z):
        self._zoom = max(1, min(8, z))
        self._refresh()

    def _refresh(self):
        try:
            img = _wotw_render_bitmap(
                self._bitmap, self._screen, self._color,
                self._bg, cols=40, rows=self._rows)
        except Exception as e:
            self._lbl.setText(f"Render failed:\n{e}")
            return
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtCore import Qt as _Qt
        pix = QPixmap.fromImage(img)
        if self._zoom != 1:
            pix = pix.scaled(
                pix.width() * self._zoom,
                pix.height() * self._zoom,
                _Qt.AspectRatioMode.IgnoreAspectRatio,
                _Qt.TransformationMode.FastTransformation)
        self._lbl.setPixmap(pix)
        self._lbl.adjustSize()
        self._current_pixmap = pix

    def _save_png(self):
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        pix = getattr(self, "_current_pixmap", None)
        if pix is None or pix.isNull():
            QMessageBox.warning(
                self, "Save PNG", "Nothing rendered yet.")
            return
        # Strip the .bin extension for the default filename so
        # the user doesn't end up with "scene.bin.png".
        base = self._bin_name
        for ext in (".bin", ".BIN"):
            if base.endswith(ext):
                base = base[:-4]
                break
        path, _ = QFileDialog.getSaveFileName(
            self, "Save WotW image as PNG",
            f"{base}.png", "PNG Images (*.png)")
        if not path:
            return
        if not pix.save(path, "PNG"):
            QMessageBox.warning(
                self, "Save PNG",
                f"Could not save {path}.")
