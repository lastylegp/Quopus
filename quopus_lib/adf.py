"""Amiga Disk File (.adf) reader.

Pure-Python implementation of the AmigaDOS filesystem block layout
for reading directory structure and extracting files from ADF disk
images. Supports both OFS (Old File System) and FFS (Fast File
System), including INTL and dircache variants.

References used:
  - ADFlib FAQ: https://adflib.github.io/FAQ/adf_info.html
  - ADFOpus and ADFView source for cross-reference on field
    layout edge cases
  - "Amiga ROM Kernel Reference Manual: Devices" for the FFS
    block format addendum

Status: read-only. Directory listing, file extraction, recursive
walk. Write support (file add/delete/rename, bitmap update, new
disk creation) is a separate exercise - the on-disk format is
write-friendly but the bitmap reconstruction logic warrants its
own module so reading stays simple.

Design notes:
  - Everything is big-endian. The Amiga is m68k.
  - One ADF dump file represents one DD floppy: 80 cylinders x
    2 heads x 11 sectors x 512 bytes = 901120 bytes. HD floppies
    (1802240 bytes) are supported too.
  - The block size is always 512 bytes regardless of OFS/FFS.
  - Block numbers are 32-bit. Block 0 is the start of the file;
    block 880 is the rootblock for a standard DD ADF.

The public API:
  ADFImage(path)            - open an ADF, parse the rootblock
  img.list_dir(block=None)  - list directory entries at the
                              given header block (root if None)
  img.read_file(block)      - read all bytes of the file at the
                              given header block
  img.walk()                - recursive generator yielding
                              (path, entry) tuples for every
                              file in the disk
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional


# ===== Constants =====================================================

BLOCK_SIZE = 512

# Standard ADF sizes. Anything else is rejected as "not an ADF".
DD_FLOPPY_SIZE = 901_120     # 1760 blocks
HD_FLOPPY_SIZE = 1_802_240   # 3520 blocks

# Block types (longword 0 of header blocks)
T_HEADER = 2          # File or directory header, also rootblock
T_DATA = 8            # OFS data block (FFS has T=0)
T_LIST = 16           # File extension block
T_DIRCACHE = 33       # Directory cache block

# Secondary types (longword (block_size//4 - 1) of header blocks)
ST_ROOT = 1
ST_USERDIR = 2
ST_FILE = 0xFFFFFFFD  # actually -3 as int32
ST_LINKFILE = 0xFFFFFFFC  # -4
ST_LINKDIR = 4
ST_SOFTLINK = 3

# Disktype flag byte 3 of the bootblock
DOS_FLAG_FFS = 1
DOS_FLAG_INTL = 2
DOS_FLAG_DIRCACHE = 4


def _u32(data: bytes, offset: int) -> int:
    """Read a big-endian unsigned 32-bit int. Throws if offset is
    past the end of `data` - that's almost always a sign the disk
    image is truncated, so we'd rather raise loudly than silently
    return 0."""
    return struct.unpack_from(">I", data, offset)[0]


def _i32(data: bytes, offset: int) -> int:
    """Read a big-endian signed 32-bit int. Used for the secondary
    type field which encodes ST_FILE as -3, ST_LINKFILE as -4."""
    return struct.unpack_from(">i", data, offset)[0]


def _write_u32(buf: bytearray, offset: int, value: int) -> None:
    """Write a big-endian unsigned 32-bit int into `buf`."""
    struct.pack_into(">I", buf, offset, value & 0xFFFFFFFF)


def _checksum_block(buf: bytearray, sum_offset: int) -> None:
    """AmigaDOS standard block checksum used by header, root and
    OFS data blocks. Algorithm: zero the slot, sum all longwords,
    write -sum (mod 2^32). Verification expects total to be 0."""
    _write_u32(buf, sum_offset, 0)
    total = 0
    for i in range(0, BLOCK_SIZE, 4):
        total = (total + struct.unpack_from(">I", buf, i)[0]
                  ) & 0xFFFFFFFF
    _write_u32(buf, sum_offset, (-total) & 0xFFFFFFFF)


def _bitmap_checksum(buf: bytearray) -> None:
    """Bitmap blocks have the checksum at longword 0 instead of 5.
    Single helper so bitmap rewrites don't get the offset wrong."""
    _checksum_block(buf, 0)


def _amiga_hash_name(name: str, intl: bool = False) -> int:
    """AmigaDOS directory hash. 72 buckets. Algorithm: start with
    the name length, then for each character multiply by 13 and
    add the byte value, masked to 31 bits.

    For OFS (non-INTL) the name is just uppercased. For INTL (and
    FFS) the case folding covers the Latin-1 letter blocks
    0xE0..0xFE (excluding 0xF7) too."""
    if intl:
        out = []
        for c in name:
            cb = ord(c)
            if 0x61 <= cb <= 0x7A:
                out.append(cb - 0x20)
            elif 0xE0 <= cb <= 0xFE and cb != 0xF7:
                out.append(cb - 0x20)
            else:
                out.append(cb)
        bytes_ = bytes(out)
    else:
        bytes_ = name.upper().encode("latin-1", errors="replace")
    h = len(bytes_)
    for b in bytes_:
        h = ((h * 13) + b) & 0x7FFFFFFF
    return h % 72


def _write_bcpl_string(buf: bytearray, offset: int,
                         s: str, max_len: int) -> None:
    """Write a BCPL-style length-prefixed string into `buf` at
    `offset`. The byte at `offset` is the length, the next
    `length` bytes are the chars. Truncates if too long; zero-
    pads the remaining slot so we don't leak stale data from a
    previous name."""
    encoded = s.encode("latin-1", errors="replace")[:max_len]
    buf[offset] = len(encoded)
    for i, b in enumerate(encoded):
        buf[offset + 1 + i] = b
    for i in range(len(encoded), max_len):
        buf[offset + 1 + i] = 0


def _amiga_date(days: int, mins: int, ticks: int) -> datetime:
    """Convert the three-longword AmigaDOS timestamp to a Python
    datetime. The epoch is 1978-01-01, days/mins/ticks are an
    Amiga DateStamp struct. 1 tick = 1/50 sec (PAL frame).

    Returns the epoch itself for invalid (all-zero or otherwise
    nonsensical) timestamps rather than raising - some ADFs have
    zeroed timestamps and that's not worth blowing up over.
    """
    if days == 0 and mins == 0 and ticks == 0:
        return datetime(1978, 1, 1)
    try:
        return (datetime(1978, 1, 1)
                + timedelta(days=days, minutes=mins,
                              seconds=ticks / 50.0))
    except (OverflowError, ValueError):
        return datetime(1978, 1, 1)


def _date_to_amiga(when: datetime) -> tuple:
    """Inverse of _amiga_date. Returns a (days, mins, ticks)
    triplet relative to 1978-01-01. Pre-Amiga dates clamp to
    epoch."""
    delta = when - datetime(1978, 1, 1)
    if delta.total_seconds() < 0:
        return (0, 0, 0)
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    rem = total_seconds - days * 86400
    mins = rem // 60
    rem -= mins * 60
    ticks = rem * 50
    ticks += int(when.microsecond / 1_000_000 * 50)
    return (days, mins, ticks)


def _bcpl_string(data: bytes, offset: int,
                   max_len: int = 30) -> str:
    """Decode a BCPL-style length-prefixed string. AmigaDOS stores
    filenames as a length byte followed by up to max_len chars.

    Names use the Amiga's national character set (codepage 1, more
    or less Latin-1). Decode as latin-1 so non-ASCII chars round-
    trip without errors; we never write back so no normalization
    needed.
    """
    length = data[offset]
    if length > max_len:
        length = max_len   # corrupt headers happen, be lenient
    return data[offset + 1:offset + 1 + length].decode(
        "latin-1", errors="replace")


# ===== Header block dataclass ========================================

@dataclass
class ADFEntry:
    """One directory entry as parsed from a file/dir header block.

    We keep both the parsed values and the originating block
    number so callers can recurse (for directories) or seek to
    data (for files) without re-parsing.
    """
    block: int                       # header block number
    name: str
    is_dir: bool                     # True for directories
    is_softlink: bool = False
    is_hardlink: bool = False
    size_bytes: int = 0              # file byte count (header.byte_size)
    protection: int = 0              # the 8 HSPARWED protection bits
    comment: str = ""
    timestamp: datetime = field(
        default_factory=lambda: datetime(1978, 1, 1))
    # The first data block (for files) or hash table base
    # (for directories). The reader fills these in below.
    first_data_block: int = 0
    hash_table: list = field(default_factory=list)
    # Parent dir's header block; useful for walking back up.
    parent_block: int = 0
    # Next entry in the hash chain. Same hash bucket can host
    # multiple entries; we follow the chain by reading the
    # 'next_in_chain' (a.k.a. hash_chain) field of each header.
    next_in_chain: int = 0

    def __repr__(self):
        kind = 'd' if self.is_dir else 'f'
        return (f"<ADFEntry {kind} {self.name!r} "
                f"block={self.block} size={self.size_bytes}>")


# ===== The image ====================================================


class ADFError(Exception):
    """Raised for malformed images or unsupported variants."""


class ADFImage:
    """Open an ADF dump file for reading.

    Usage:
        img = ADFImage("workbench.adf")
        for entry in img.list_dir():
            print(entry)
        data = img.read_file(some_entry.block)

    The whole file is read into memory at construction time. ADF
    floppies cap at ~1.8 MB so this is fine; if we ever support
    multi-megabyte ADFs from real Amiga HDs we'd switch to a
    seek-on-demand reader.
    """

    def __init__(self, path):
        self.path = Path(path)
        # bytearray so write operations can patch blocks in place.
        # ADF images max out at 1.7 MB (HD) so keeping it all in
        # memory is fine; save() flushes to disk.
        self.data = bytearray(self.path.read_bytes())
        self.dirty = False    # set by any mutating method
        size = len(self.data)
        if size not in (DD_FLOPPY_SIZE, HD_FLOPPY_SIZE):
            # Some real-world dumps have a few junk bytes at the
            # end or are short by one block. Accept anything that
            # rounds to a sane block count, but warn via the
            # exception message that the size was unusual.
            blocks = size // BLOCK_SIZE
            if blocks < 100 or size % BLOCK_SIZE:
                raise ADFError(
                    f"{path}: file size {size} is not a known "
                    f"ADF layout (DD={DD_FLOPPY_SIZE}, "
                    f"HD={HD_FLOPPY_SIZE})")
        self.block_count = size // BLOCK_SIZE
        # Parse the bootblock to find the rootblock and figure
        # out OFS vs FFS.
        self._parse_bootblock()
        # Now parse the rootblock for the volume name.
        self._parse_rootblock()

    # --- bootblock and rootblock --------------------------------

    def _parse_bootblock(self):
        """Block 0+1 = the bootblock. First four bytes are 'DOSx'
        where x is the disk type flag (FFS / INTL / DIRCACHE).
        Block index of the rootblock is at offset 8 of block 0.
        Real disks usually use 880 (DD) regardless, so we don't
        trust the bootblock's pointer blindly - read it but fall
        back to the calculated middle-block if zeroed.
        """
        b0 = self.data[:BLOCK_SIZE]
        magic = b0[:3]
        if magic != b"DOS":
            raise ADFError(
                f"{self.path}: bootblock magic is {magic!r}, "
                f"not b'DOS' - probably not an ADF")
        flags = b0[3]
        self.is_ffs = bool(flags & DOS_FLAG_FFS)
        self.is_intl = bool(flags & (DOS_FLAG_INTL
                                       | DOS_FLAG_DIRCACHE))
        self.has_dircache = bool(flags & DOS_FLAG_DIRCACHE)
        # Bootblock's stored rootblock pointer
        boot_root = _u32(b0, 8)
        # For DD: 1760 blocks, middle = 880. For HD: 3520, middle
        # = 1760. The middle-block convention is rock-solid; the
        # boot pointer is sometimes 0 on disks formatted by
        # weird tools.
        default_root = self.block_count // 2
        if boot_root and 0 < boot_root < self.block_count:
            self.rootblock_num = boot_root
        else:
            self.rootblock_num = default_root

    def _parse_rootblock(self):
        """Block at self.rootblock_num is the volume header.
        Layout (longwords, 0-indexed, BLOCK_SIZE/4 = 128 entries):
            [0]  type = T_HEADER (2)
            [1]  header key (unused for root) = 0
            [2]  high seq = 0
            [3]  hash table size = (BLOCK_SIZE/4) - 56 = 72
            [4]  first data = 0
            [5]  checksum
            [6..78]  hash table (72 longwords)
            [79]  bm_flag (-1 means bitmap valid)
            [80..104]  bm_pages[25]   bitmap block pointers
            [105]  bm_ext (next bitmap-extension block)
            [106..108]  modification time of root
            [109]  name_len (BCPL byte)
            [110..138]  name bytes (max 30)
            [115]  ... padding
            [120..122]  disk creation time
            [123..125]  disk modification time
            [126]  next_hash = 0 for root
            [127]  parent dir = 0 for root
        Then secondary type at the LAST longword: ST_ROOT (1).
        """
        rb = self._block(self.rootblock_num)
        if _u32(rb, 0) != T_HEADER:
            raise ADFError(
                f"rootblock {self.rootblock_num} type is "
                f"{_u32(rb, 0)}, expected T_HEADER ({T_HEADER})")
        sec_type = _u32(rb, BLOCK_SIZE - 4)
        if sec_type != ST_ROOT:
            raise ADFError(
                f"rootblock secondary type {sec_type}, "
                f"expected ST_ROOT ({ST_ROOT})")
        self.disk_name = _bcpl_string(rb, 108 * 4)
        # Read modification time of the disk itself
        days = _u32(rb, 105 * 4)
        mins = _u32(rb, 106 * 4)
        ticks = _u32(rb, 107 * 4)
        self.disk_mtime = _amiga_date(days, mins, ticks)
        # Hash table at longwords 6..(6+ht_size-1)
        ht_size = _u32(rb, 12)
        if ht_size <= 0 or ht_size > 100:
            # Standard rootblock has ht_size = 72, but we've seen
            # weird disks. Cap to a safe range.
            ht_size = 72
        self.root_hashtable = [
            _u32(rb, (6 + i) * 4) for i in range(ht_size)
        ]
        self.root_entry = ADFEntry(
            block=self.rootblock_num,
            name=self.disk_name,
            is_dir=True,
            timestamp=self.disk_mtime,
            hash_table=self.root_hashtable,
            parent_block=0,
        )
        # Bitmap metadata for write support. Don't decode the
        # bitmap up-front (only needed when adding/removing
        # files) but grab the page pointers so later code can
        # find them.
        self.bm_valid = (_u32(rb, 79 * 4) == 0xFFFFFFFF)
        self.bm_pages = []
        for i in range(25):
            p = _u32(rb, (80 + i) * 4)
            if p == 0:
                break
            self.bm_pages.append(p)
        # bm_ext at longword 105 - HD floppies chain extension
        # blocks here. DD fits in the 25 above so bm_ext = 0.
        self.bm_ext = _u32(rb, 105 * 4)

    # --- low-level helpers --------------------------------------

    def _block(self, n: int) -> bytes:
        """Return a 512-byte block by index. Range-checks because
        a malformed disk could have a header pointing past the
        end of the image."""
        if n < 0 or n >= self.block_count:
            raise ADFError(
                f"block index {n} out of range "
                f"(0..{self.block_count - 1})")
        return self.data[n * BLOCK_SIZE:(n + 1) * BLOCK_SIZE]

    def _parse_header_block(self, block_num: int,
                              expected_dir: bool = False
                              ) -> ADFEntry:
        """Parse a file or user-dir header block at block_num.
        Returns an ADFEntry. Raises ADFError if the block isn't
        a valid header.

        Header layout (longwords from start, where idx = byte/4):
            [0]   type = T_HEADER
            [1]   own block number (self-reference)
            [2]   high seq (= block count - 1 for files)
            [3]   data size (longword count, for files only)
            [4]   first data block
            [5]   checksum
            [6..ht_end]  hash table (for dirs) / data block list (files)
            [-50]  byte_size (file size in bytes)
            [-49]  comment length (BCPL)
            [-48..-32]  comment bytes (max 79)
            [-23..-21]  timestamp days, mins, ticks
            [-20]  name_len (BCPL)
            [-19..-13]  name bytes (max 30)
            [-12]  protection (read as longword)
            ... and a few other fields we don't care about
            [-4]   next entry in same hash chain (sibling)
            [-3]   parent block
            [-1]   secondary type (ST_FILE / ST_USERDIR / ...)
        """
        b = self._block(block_num)
        if _u32(b, 0) != T_HEADER:
            raise ADFError(
                f"block {block_num} type {_u32(b, 0)} != "
                f"T_HEADER")
        sec_type = _i32(b, BLOCK_SIZE - 4)
        is_dir = sec_type in (ST_USERDIR, ST_LINKDIR)
        is_softlink = sec_type == ST_SOFTLINK
        is_hardlink = sec_type in (ST_LINKFILE, ST_LINKDIR)

        # File-size is at offset BLOCK_SIZE - 188 from the top
        # of the block (i.e. longword -47 from the end). The
        # AmigaDOS header is "back-aligned": metadata fields like
        # name, timestamp, parent, sec_type are anchored from
        # the END of the block, not the start. This is what lets
        # OFS and FFS share the same struct: data goes from the
        # start (longword 6+), metadata from the end.
        size_bytes = _u32(b, BLOCK_SIZE - 188)
        protection = _u32(b, BLOCK_SIZE - 192)

        # Name (BCPL, max 30) at offset -80 from end.
        # Comment (BCPL, max 79) at offset -184 from end.
        comment = _bcpl_string(b, BLOCK_SIZE - 184, max_len=79)
        name = _bcpl_string(b, BLOCK_SIZE - 80, max_len=30)

        # Timestamp (3 longwords) at -92 from end.
        days = _u32(b, BLOCK_SIZE - 92)
        mins = _u32(b, BLOCK_SIZE - 88)
        ticks = _u32(b, BLOCK_SIZE - 84)
        ts = _amiga_date(days, mins, ticks)

        # Hash chain / parent: last 4 longwords from end.
        next_in_chain = _u32(b, BLOCK_SIZE - 16)
        parent_block = _u32(b, BLOCK_SIZE - 12)

        # File-specific: first_data_block at longword 4.
        first_data = _u32(b, 16)

        # Directory-specific: hash table at longwords 6..77
        # (72 entries for the standard block size).
        hash_table = []
        if is_dir or sec_type == ST_ROOT:
            ht_count = 72
            hash_table = [
                _u32(b, (6 + i) * 4) for i in range(ht_count)
            ]

        return ADFEntry(
            block=block_num,
            name=name,
            is_dir=is_dir,
            is_softlink=is_softlink,
            is_hardlink=is_hardlink,
            size_bytes=size_bytes,
            protection=protection,
            comment=comment,
            timestamp=ts,
            first_data_block=first_data,
            hash_table=hash_table,
            parent_block=parent_block,
            next_in_chain=next_in_chain,
        )

    # --- public API ---------------------------------------------

    def list_dir(self,
                  block: Optional[int] = None) -> list:
        """Return the entries in the directory at `block`. If
        block is None, lists the root directory.

        The hash table can have up to 72 slots; each slot may
        chain multiple entries via next_in_chain. Walk every
        slot and every chain to surface the full set.
        """
        # Always re-read the parent's hash table from disk - the
        # cached self.root_entry.hash_table is from __init__ and
        # won't reflect writes performed since.
        if block is None or block == self.rootblock_num:
            rb = self._block(self.rootblock_num)
            hash_table = [
                _u32(rb, (6 + i) * 4) for i in range(72)
            ]
        else:
            parent = self._parse_header_block(block)
            if not parent.is_dir:
                raise ADFError(
                    f"block {block} is not a directory")
            hash_table = parent.hash_table
        results = []
        seen = set()    # avoid infinite loops on circular chains
        for slot in hash_table:
            cur = slot
            while cur and cur not in seen:
                seen.add(cur)
                try:
                    entry = self._parse_header_block(cur)
                except ADFError:
                    break
                results.append(entry)
                cur = entry.next_in_chain
        results.sort(key=lambda e: e.name.lower())
        return results

    def read_file(self, block: int) -> bytes:
        """Read the file whose header lives at `block` and return
        its byte content. Handles both OFS (data blocks have a
        24-byte header) and FFS (data blocks are all data) by
        switching on self.is_ffs.

        Files can span multiple data blocks chained via extension
        blocks (T_LIST). We walk the chain transparently.
        """
        header = self._parse_header_block(block)
        if header.is_dir:
            raise ADFError(
                f"block {block} ({header.name!r}) is a "
                f"directory, not a file")
        if header.first_data_block == 0 or header.size_bytes == 0:
            # Empty file - valid, nothing to read
            return b""
        out = bytearray()
        bytes_left = header.size_bytes
        # The header itself has space for the first few data
        # block pointers in its hash-table region (file data is
        # NOT in the hash table for files - they reuse those
        # slots as a data-block array). Build the list of all
        # data-block numbers via the data-block list + any
        # extension blocks.
        data_blocks = self._collect_data_blocks(header)
        for db in data_blocks:
            if bytes_left <= 0:
                break
            block_data = self._read_data_block(db, bytes_left)
            out.extend(block_data)
            bytes_left -= len(block_data)
        return bytes(out)

    def _collect_data_blocks(self,
                                header: ADFEntry) -> list:
        """Walk the file header's data-block list, following
        T_LIST extension blocks as needed, to return every data
        block in order.

        File header layout for the data-block list:
          The first 72 (BSIZE/4 - 56) data-block pointers live
          in the same region as the directory hash table (the
          AmigaDOS reused the slot range for file vs dir). They
          are stored in REVERSE ORDER - the file's LAST data
          block goes in slot 0, second-to-last in slot 1, etc.
          So we read the slots and reverse to get sequential
          order.

          When the file has more than 72 data blocks, the header
          stores a pointer to a 'file extension block' (T_LIST)
          which holds another 72 pointers, plus its own
          extension-block pointer. Chain those until you get
          a 0 extension pointer.
        """
        b = self._block(header.block)
        # high_seq at longword 2 = the number of data-block
        # pointers stored in this header. From the AmigaDOS
        # spec: "If one file has 7 datablocks, the first is at
        # datablock[71-0], the last at datablocks[71-6], and
        # highseq equals to 7." So the pointers live in the
        # TAIL of the hash-table region: slots
        #   [71], [70], ..., [72 - slots_used]
        # with slot [71] holding the file's FIRST data block.
        slots_used = _u32(b, 8)
        if slots_used > 72:
            slots_used = 72
        # Hash table runs from longword 6 (slot 0) to longword
        # 77 (slot 71). The data-block array fills slots
        # 71 downward. We want the file's data blocks in
        # SEQUENTIAL order, so we read from slot 71 down to
        # slot (72 - slots_used).
        ptrs = []
        for i in range(slots_used):
            slot_idx = 71 - i
            ptrs.append(
                _u32(b, (6 + slot_idx) * 4))
        # Now follow any extension chain. 'first_extension' is
        # at longword (BLOCK_SIZE/4 - 2) = BLOCK_SIZE - 8 bytes
        # from start.
        next_ext = _u32(b, BLOCK_SIZE - 8)
        seen_ext = set()    # avoid infinite loops on bad chains
        while next_ext and next_ext not in seen_ext:
            seen_ext.add(next_ext)
            eb = self._block(next_ext)
            if _u32(eb, 0) != T_LIST:
                break
            ext_slots = _u32(eb, 8)
            if ext_slots > 72:
                ext_slots = 72
            # Extension blocks use the same end-of-table
            # convention as the header block.
            for i in range(ext_slots):
                slot_idx = 71 - i
                ptrs.append(
                    _u32(eb, (6 + slot_idx) * 4))
            next_ext = _u32(eb, BLOCK_SIZE - 8)
        # Drop any 0 entries (defensive - sparse files don't
        # exist in AmigaDOS but bad disks happen).
        return [p for p in ptrs if p]

    def _read_data_block(self, block: int,
                            max_bytes: int) -> bytes:
        """Return up to max_bytes of payload from a single data
        block. OFS has 488 bytes of payload (after a 24-byte
        block header), FFS has the full 512 bytes. We trim to
        max_bytes for the last block of a file.
        """
        b = self._block(block)
        if self.is_ffs:
            payload = b
            # FFS uses all 512 bytes as data; max_bytes does
            # the trim.
        else:
            # OFS data block header is 24 bytes:
            #   [0] type = T_DATA (8)
            #   [4] header_key (= file header block)
            #   [8] seq_num (1-indexed within the file)
            #   [12] data_size (bytes of payload, max 488)
            #   [16] next_data_block (or 0 for last)
            #   [20] checksum
            payload = b[24:]
            data_size = _u32(b, 12)
            # Honour the block's own data_size field, capped at
            # 488; protects against corrupt headers reporting
            # values > 488.
            if data_size > 488:
                data_size = 488
            payload = payload[:data_size]
        if len(payload) > max_bytes:
            payload = payload[:max_bytes]
        return bytes(payload)

    def walk(self) -> Iterator:
        """Recursively yield (full_path_str, entry) for every
        file and directory on the disk. Directories are yielded
        BEFORE their contents (pre-order traversal).
        """
        def _recurse(parent_path: str, dir_block: int):
            for e in self.list_dir(dir_block):
                full = (parent_path + "/" + e.name
                          if parent_path else e.name)
                yield full, e
                if e.is_dir and not e.is_softlink:
                    # Recurse into subdirectory. Skip softlinks
                    # to avoid infinite loops on disks that have
                    # cyclic links.
                    yield from _recurse(full, e.block)
        yield from _recurse("", self.rootblock_num)

    # --- string formatting --------------------------------------

    def format_protection(self, prot: int) -> str:
        """Render the 8 protection bits as an AmigaDOS-style
        flag string: 'HSPARWED'. Convention: lowercase letter
        means the bit is SET (i.e. the right is granted, or for
        H/S/P the flag is in effect).

        Bit layout (Amiga convention, inverted for R/W/E/D):
          7 H  Hidden
          6 S  Script (re-executable)
          5 P  Pure (re-entrant)
          4 A  Archive (already saved by archiver)
          3 R  not Readable
          2 W  not Writable
          1 E  not Executable
          0 D  not Deletable
        AmigaDOS stores the lower 4 bits INVERTED, so a typical
        all-rights-granted file has protection = 0 (none of the
        upper 4 bits set, none of the lower 4 set, which means
        all of RWED are GRANTED).
        """
        # Default to all rights granted (RWED), no flags set
        chars = ['-'] * 8
        if prot & 0x80: chars[0] = 'h'
        if prot & 0x40: chars[1] = 's'
        if prot & 0x20: chars[2] = 'p'
        if prot & 0x10: chars[3] = 'a'
        # Lower 4 bits: SET means PROHIBITED, so we show the
        # letter when the bit is CLEAR.
        if not (prot & 0x08): chars[4] = 'r'
        if not (prot & 0x04): chars[5] = 'w'
        if not (prot & 0x02): chars[6] = 'e'
        if not (prot & 0x01): chars[7] = 'd'
        return ''.join(chars)

    # --- write primitives ---------------------------------------
    # Everything below mutates self.data (in-memory image). Nothing
    # hits disk until save() is called. Each mutator sets
    # self.dirty = True so callers can prompt to save on close.

    def _write_block(self, n: int, block_data: bytes) -> None:
        """Write a 512-byte block into the in-memory image."""
        if len(block_data) != BLOCK_SIZE:
            raise ADFError(
                f"block must be {BLOCK_SIZE} bytes, got "
                f"{len(block_data)}")
        if n < 0 or n >= self.block_count:
            raise ADFError(f"block {n} out of range")
        self.data[n * BLOCK_SIZE:(n + 1) * BLOCK_SIZE] = block_data
        self.dirty = True

    def save(self, path=None) -> None:
        """Flush the in-memory image to disk. Optional `path`
        argument lets you save-as; default rewrites the file we
        opened from."""
        out = Path(path) if path else self.path
        out.write_bytes(self.data)
        self.dirty = False

    # --- bitmap operations --------------------------------------
    # Bitmap: ONE bit per block. Bit 1 = block is FREE, 0 = USED.
    # Bitmap covers blocks 2 onwards (boot blocks 0+1 never
    # represented).
    # DD: 1758 data bits -> 220 bytes, fits in one bitmap block.
    # HD: 3518 bits -> 440 bytes, still fits in one block.
    # Bitmap block layout: [0..3] = checksum, [4..507] = bits.

    def _read_bitmap(self) -> bytearray:
        """Read the bitmap as a flat bytearray of (block_count-2)
        bits, LSB-first within each byte."""
        n_data_blocks = self.block_count - 2
        n_bytes = (n_data_blocks + 7) // 8
        bits = bytearray(n_bytes)
        offset = 0
        for page_num in self.bm_pages:
            page = self._block(page_num)
            payload = page[4:]
            take = min(len(payload), n_bytes - offset)
            bits[offset:offset + take] = payload[:take]
            offset += take
            if offset >= n_bytes:
                break
        return bits

    def _write_bitmap(self, bits: bytearray) -> None:
        """Persist bitmap back to its on-disk pages with fresh
        checksums, and mark the rootblock's bm_valid_flag."""
        n_data_blocks = self.block_count - 2
        n_bytes = (n_data_blocks + 7) // 8
        if len(bits) != n_bytes:
            raise ADFError(
                f"bitmap size mismatch: {len(bits)} vs "
                f"{n_bytes}")
        offset = 0
        for page_num in self.bm_pages:
            page = bytearray(BLOCK_SIZE)
            take = min(BLOCK_SIZE - 4, n_bytes - offset)
            page[4:4 + take] = bits[offset:offset + take]
            _bitmap_checksum(page)
            self._write_block(page_num, bytes(page))
            offset += take
            if offset >= n_bytes:
                break
        # Mark root's bm_flag as valid (-1)
        rb = bytearray(self._block(self.rootblock_num))
        _write_u32(rb, 79 * 4, 0xFFFFFFFF)
        _checksum_block(rb, 5 * 4)
        self._write_block(self.rootblock_num, bytes(rb))
        self.bm_valid = True

    def _block_is_free(self, bits: bytearray,
                          block_num: int) -> bool:
        idx = block_num - 2
        byte_idx, bit_idx = divmod(idx, 8)
        return bool(bits[byte_idx] & (1 << bit_idx))

    def _set_block_free(self, bits: bytearray, block_num: int,
                         free: bool) -> None:
        idx = block_num - 2
        byte_idx, bit_idx = divmod(idx, 8)
        mask = 1 << bit_idx
        if free:
            bits[byte_idx] |= mask
        else:
            bits[byte_idx] &= ~mask & 0xFF

    def _alloc_block(self, bits: bytearray,
                       protect: set = None) -> int:
        """Find the first free block, mark it used, return its
        absolute number. protect = block numbers to skip even
        if free (used to track in-progress reservations during
        multi-block file allocation)."""
        protect = protect or set()
        n_data_blocks = self.block_count - 2
        for idx in range(n_data_blocks):
            byte_idx, bit_idx = divmod(idx, 8)
            mask = 1 << bit_idx
            block_num = idx + 2
            if (bits[byte_idx] & mask) and (
                    block_num not in protect):
                bits[byte_idx] &= ~mask & 0xFF
                return block_num
        raise ADFError("disk full - no free blocks")

    def free_block_count(self) -> int:
        """Total free blocks. Used by the viewer's status bar
        and 'disk full' guards."""
        bits = self._read_bitmap()
        count = 0
        for b in bits:
            count += bin(b).count("1")
        n_data_blocks = self.block_count - 2
        if n_data_blocks % 8:
            # The last byte has only (n_data_blocks % 8) valid
            # bits; mask out the padding bits we just counted.
            last_byte = bits[-1]
            valid_in_last = n_data_blocks % 8
            stale = 0
            for i in range(valid_in_last, 8):
                if last_byte & (1 << i):
                    stale += 1
            count -= stale
        return count

    # --- disk-level operations ----------------------------------

    def set_disk_label(self, new_label: str) -> None:
        """Rename the volume. Writes the BCPL string into the
        rootblock and recomputes checksums. Also bumps the disk
        modification timestamp so Workbench redraws the icon."""
        if len(new_label) > 30:
            raise ADFError(
                f"disk label max 30 chars, got {len(new_label)}")
        rb = bytearray(self._block(self.rootblock_num))
        _write_bcpl_string(rb, 108 * 4, new_label, 30)
        days, mins, ticks = _date_to_amiga(datetime.now())
        _write_u32(rb, 105 * 4, days)
        _write_u32(rb, 106 * 4, mins)
        _write_u32(rb, 107 * 4, ticks)
        _checksum_block(rb, 5 * 4)
        self._write_block(self.rootblock_num, bytes(rb))
        self.disk_name = new_label
        self.root_entry.name = new_label
        self.disk_mtime = datetime.now()

    def set_bootblock(self, is_ffs: bool, is_intl: bool = False,
                        has_dircache: bool = False,
                        bootcode: bytes = b"") -> None:
        """Rewrite the bootblock with new disk-type flags.
        Optional bootcode (max ~1012 bytes) makes the disk
        bootable. Empty bootcode = non-bootable but still
        mountable (which is what most data disks are)."""
        flags = 0
        if is_ffs:        flags |= DOS_FLAG_FFS
        if is_intl:       flags |= DOS_FLAG_INTL
        if has_dircache:  flags |= DOS_FLAG_DIRCACHE
        bb = bytearray(BLOCK_SIZE * 2)
        bb[0:4] = b"DOS\x00"
        bb[3] = flags
        _write_u32(bb, 8, self.rootblock_num)
        if bootcode:
            if len(bootcode) > BLOCK_SIZE * 2 - 12:
                raise ADFError(
                    f"bootcode max {BLOCK_SIZE * 2 - 12} bytes")
            bb[12:12 + len(bootcode)] = bootcode
        # Bootblock checksum at offset 4, computed over both
        # blocks treated as one 1024-byte buffer.
        _write_u32(bb, 4, 0)
        total = 0
        for i in range(0, BLOCK_SIZE * 2, 4):
            total = (total
                      + struct.unpack_from(">I", bb, i)[0]
                      ) & 0xFFFFFFFF
        _write_u32(bb, 4, (-total) & 0xFFFFFFFF)
        self._write_block(0, bytes(bb[0:BLOCK_SIZE]))
        self._write_block(1, bytes(bb[BLOCK_SIZE:BLOCK_SIZE * 2]))
        self._parse_bootblock()

    # --- file/directory operations ------------------------------

    def _walk_all_used_blocks(self) -> set:
        """Walk every reachable block from the rootblock. Returns
        a set of block numbers. Used by validate() to find blocks
        the bitmap claims used but aren't actually reachable."""
        used = set()
        used.add(0)
        used.add(1)
        used.add(self.rootblock_num)
        for bp in self.bm_pages:
            used.add(bp)
        # Read rootblock hash table directly (not from cache - it
        # could be stale). The rootblock has a different layout
        # from regular header blocks but the hash table at
        # longwords 6..77 is identical.
        rb = self._block(self.rootblock_num)
        stack = []
        for i in range(72):
            slot = _u32(rb, (6 + i) * 4)
            if slot:
                stack.append(slot)
        seen = set()
        while stack:
            blk = stack.pop()
            if blk in seen:
                continue
            seen.add(blk)
            try:
                entry = self._parse_header_block(blk)
            except ADFError:
                continue
            used.add(blk)
            if entry.next_in_chain:
                stack.append(entry.next_in_chain)
            if entry.is_dir:
                for slot in entry.hash_table:
                    if slot:
                        stack.append(slot)
                continue
            try:
                data_blocks = self._collect_data_blocks(entry)
            except ADFError:
                continue
            for db in data_blocks:
                used.add(db)
            # Extension blocks
            b = self._block(entry.block)
            ext = _u32(b, BLOCK_SIZE - 8)
            seen_ext = set()
            while ext and ext not in seen_ext:
                seen_ext.add(ext)
                used.add(ext)
                try:
                    eb = self._block(ext)
                    ext = _u32(eb, BLOCK_SIZE - 8)
                except ADFError:
                    break
        return used

    def validate(self) -> dict:
        """Rebuild the bitmap from a full filesystem walk and
        report stats. Same effect as AmigaDOS's 'V0:' command.

        Returns dict with:
          fixed_bitmap: bool - did anything change
          freed_count: int  - blocks marked used but unreachable
          lost_count: int   - blocks reachable but marked free
        """
        n_data_blocks = self.block_count - 2
        n_bytes = (n_data_blocks + 7) // 8
        new_bits = bytearray(b"\xff" * n_bytes)
        # Clear bits beyond block_count (last byte may have
        # padding bits which must stay zero so the FS never
        # tries to allocate non-existent blocks).
        valid_in_last = n_data_blocks % 8
        if valid_in_last:
            mask = (1 << valid_in_last) - 1
            new_bits[-1] &= mask
        used = self._walk_all_used_blocks()
        for blk in used:
            if blk >= 2:
                self._set_block_free(new_bits, blk, False)
        old_bits = self._read_bitmap()
        freed = 0
        lost = 0
        for idx in range(n_data_blocks):
            byte_idx, bit_idx = divmod(idx, 8)
            mask = 1 << bit_idx
            old_free = bool(old_bits[byte_idx] & mask)
            new_free = bool(new_bits[byte_idx] & mask)
            if old_free and not new_free:
                lost += 1
            elif not old_free and new_free:
                freed += 1
        self._write_bitmap(new_bits)
        return {
            "fixed_bitmap": bool(freed or lost),
            "freed_count": freed,
            "lost_count": lost,
        }

    def _unlink_from_parent(self, parent_block: int,
                              entry: ADFEntry) -> None:
        """Splice `entry` out of its parent dir's hash chain.
        Updates either the parent's hash table slot or a
        sibling's next_in_chain pointer."""
        parent_buf = bytearray(self._block(parent_block))
        bucket = _amiga_hash_name(entry.name, self.is_intl)
        first_blk_offset = (6 + bucket) * 4
        first = _u32(parent_buf, first_blk_offset)
        if first == entry.block:
            _write_u32(parent_buf, first_blk_offset,
                         entry.next_in_chain)
            _checksum_block(parent_buf, 5 * 4)
            self._write_block(parent_block, bytes(parent_buf))
            return
        cur = first
        guard = 0
        while cur and guard < 1000:
            guard += 1
            cur_buf = bytearray(self._block(cur))
            nxt = _u32(cur_buf, BLOCK_SIZE - 16)
            if nxt == entry.block:
                _write_u32(cur_buf, BLOCK_SIZE - 16,
                             entry.next_in_chain)
                _checksum_block(cur_buf, 5 * 4)
                self._write_block(cur, bytes(cur_buf))
                return
            cur = nxt

    def _link_into_parent(self, parent_block: int,
                            new_block: int,
                            new_name: str) -> int:
        """Insert a freshly-created header into parent's hash
        table. Returns the value to put into new header's
        next_in_chain field."""
        parent_buf = bytearray(self._block(parent_block))
        bucket = _amiga_hash_name(new_name, self.is_intl)
        first_blk_offset = (6 + bucket) * 4
        old_first = _u32(parent_buf, first_blk_offset)
        _write_u32(parent_buf, first_blk_offset, new_block)
        # Bump parent dir's mtime
        days, mins, ticks = _date_to_amiga(datetime.now())
        if parent_block == self.rootblock_num:
            # Root has its mtime at longwords 105-107
            _write_u32(parent_buf, 105 * 4, days)
            _write_u32(parent_buf, 106 * 4, mins)
            _write_u32(parent_buf, 107 * 4, ticks)
        else:
            _write_u32(parent_buf, BLOCK_SIZE - 92, days)
            _write_u32(parent_buf, BLOCK_SIZE - 88, mins)
            _write_u32(parent_buf, BLOCK_SIZE - 84, ticks)
        _checksum_block(parent_buf, 5 * 4)
        self._write_block(parent_block, bytes(parent_buf))
        return old_first

    def delete_file(self, header_block: int) -> None:
        """Remove the file (or empty dir) whose header sits at
        header_block. Frees its data blocks, extension blocks
        and the header itself; unlinks from parent's hash chain.

        Non-empty directories are rejected - delete contents
        first."""
        entry = self._parse_header_block(header_block)
        if entry.is_dir:
            if any(slot for slot in entry.hash_table):
                raise ADFError(
                    f"directory {entry.name!r} is not empty")

        blocks_to_free = {header_block}
        if not entry.is_dir:
            try:
                blocks_to_free.update(
                    self._collect_data_blocks(entry))
            except ADFError:
                pass
            b = self._block(entry.block)
            ext = _u32(b, BLOCK_SIZE - 8)
            seen_ext = set()
            while ext and ext not in seen_ext:
                seen_ext.add(ext)
                blocks_to_free.add(ext)
                try:
                    eb = self._block(ext)
                    ext = _u32(eb, BLOCK_SIZE - 8)
                except ADFError:
                    break

        parent_block = entry.parent_block or self.rootblock_num
        self._unlink_from_parent(parent_block, entry)

        bits = self._read_bitmap()
        for blk in blocks_to_free:
            self._set_block_free(bits, blk, True)
        self._write_bitmap(bits)

        # Zero freed blocks so old data doesn't leak
        for blk in blocks_to_free:
            self._write_block(blk, bytes(BLOCK_SIZE))

    def add_file(self, parent_block: int, name: str,
                  data: bytes, protection: int = 0,
                  comment: str = "",
                  timestamp: Optional[datetime] = None
                  ) -> int:
        """Create a new file inside parent_block. data is the
        content bytes. Returns the new file's header block.

        Pre-allocates all block numbers via a 'protect' set
        before committing the bitmap, so a disk-full failure
        leaves no partial allocations on disk."""
        if name in ("", ".", "..") or len(name) > 30:
            raise ADFError(f"invalid filename: {name!r}")
        if "/" in name or ":" in name:
            raise ADFError(
                f"filename {name!r} contains illegal chars")

        bits = self._read_bitmap()
        protect = set()

        header_blk = self._alloc_block(bits, protect)
        protect.add(header_blk)

        if not data:
            data_blocks_needed = 0
        elif self.is_ffs:
            data_blocks_needed = (
                (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE)
        else:
            data_blocks_needed = (len(data) + 488 - 1) // 488

        data_block_nums = []
        for _ in range(data_blocks_needed):
            db = self._alloc_block(bits, protect)
            protect.add(db)
            data_block_nums.append(db)

        ext_block_nums = []
        remaining_after_header = max(0, data_blocks_needed - 72)
        n_ext_blocks = (
            (remaining_after_header + 72 - 1) // 72
            if remaining_after_header else 0)
        for _ in range(n_ext_blocks):
            eb = self._alloc_block(bits, protect)
            protect.add(eb)
            ext_block_nums.append(eb)

        ts = timestamp or datetime.now()

        # Write data blocks
        for i, db_num in enumerate(data_block_nums):
            block = bytearray(BLOCK_SIZE)
            if self.is_ffs:
                start = i * BLOCK_SIZE
                chunk = data[start:start + BLOCK_SIZE]
                block[:len(chunk)] = chunk
            else:
                # OFS data block:
                # [0] T_DATA=8 [4] header_key [8] seq_num
                # [12] data_size [16] next_data [20] cksum
                # [24..511] payload (max 488 bytes)
                _write_u32(block, 0, T_DATA)
                _write_u32(block, 4, header_blk)
                _write_u32(block, 8, i + 1)
                start = i * 488
                chunk = data[start:start + 488]
                _write_u32(block, 12, len(chunk))
                if i + 1 < len(data_block_nums):
                    _write_u32(block, 16, data_block_nums[i + 1])
                else:
                    _write_u32(block, 16, 0)
                for j, b in enumerate(chunk):
                    block[24 + j] = b
                _checksum_block(block, 5 * 4)
            self._write_block(db_num, bytes(block))

        # Write extension blocks
        for ext_idx, ext_num in enumerate(ext_block_nums):
            block = bytearray(BLOCK_SIZE)
            _write_u32(block, 0, T_LIST)
            _write_u32(block, 4, ext_num)
            first_idx_in_ext = 72 + ext_idx * 72
            last_idx_in_ext = min(
                first_idx_in_ext + 72,
                len(data_block_nums))
            block_ptrs = data_block_nums[
                first_idx_in_ext:last_idx_in_ext]
            n_in_this_ext = len(block_ptrs)
            _write_u32(block, 8, n_in_this_ext)
            # Same end-of-table convention as the header: FIRST
            # data pointer of this group at slot 71, decreasing.
            for i, ptr in enumerate(block_ptrs):
                slot_idx = 71 - i
                _write_u32(block, (6 + slot_idx) * 4, ptr)
            _write_u32(block, BLOCK_SIZE - 12, header_blk)
            next_ext = (ext_block_nums[ext_idx + 1]
                          if ext_idx + 1 < len(ext_block_nums)
                          else 0)
            _write_u32(block, BLOCK_SIZE - 8, next_ext)
            _write_u32(block, BLOCK_SIZE - 4, T_LIST)
            _checksum_block(block, 5 * 4)
            self._write_block(ext_num, bytes(block))

        # Link into parent
        next_in_chain = self._link_into_parent(
            parent_block, header_blk, name)

        # Write file header
        days, mins, ticks = _date_to_amiga(ts)
        header = bytearray(BLOCK_SIZE)
        _write_u32(header, 0, T_HEADER)
        _write_u32(header, 4, header_blk)
        n_data_in_header = min(data_blocks_needed, 72)
        _write_u32(header, 8, n_data_in_header)
        _write_u32(header, 12, 0)
        first_data = (data_block_nums[0]
                       if data_block_nums else 0)
        _write_u32(header, 16, first_data)
        # Data block pointers fill slots from the END of the
        # hash-table region downward: file's FIRST data block
        # goes in slot 71, second in slot 70, etc. This is the
        # AmigaDOS convention so appending to a file is a
        # cheap last-slot write rather than a full table
        # rewrite. The READER uses the same convention.
        in_header = data_block_nums[:72]
        for i, ptr in enumerate(in_header):
            slot_idx = 71 - i
            _write_u32(header, (6 + slot_idx) * 4, ptr)
        _write_u32(header, BLOCK_SIZE - 192, protection)
        _write_u32(header, BLOCK_SIZE - 188, len(data))
        _write_bcpl_string(
            header, BLOCK_SIZE - 184, comment, 79)
        _write_u32(header, BLOCK_SIZE - 92, days)
        _write_u32(header, BLOCK_SIZE - 88, mins)
        _write_u32(header, BLOCK_SIZE - 84, ticks)
        _write_bcpl_string(header, BLOCK_SIZE - 80, name, 30)
        _write_u32(header, BLOCK_SIZE - 16, next_in_chain)
        _write_u32(header, BLOCK_SIZE - 12, parent_block)
        first_ext = (ext_block_nums[0]
                       if ext_block_nums else 0)
        _write_u32(header, BLOCK_SIZE - 8, first_ext)
        _write_u32(header, BLOCK_SIZE - 4, ST_FILE)
        _checksum_block(header, 5 * 4)
        self._write_block(header_blk, bytes(header))

        # Commit bitmap with all the new allocations
        self._write_bitmap(bits)
        return header_blk

    def add_directory(self, parent_block: int,
                        name: str,
                        protection: int = 0,
                        comment: str = "",
                        timestamp: Optional[datetime] = None
                        ) -> int:
        """Create an empty subdirectory inside parent_block.
        Returns the header block of the new directory."""
        if name in ("", ".", "..") or len(name) > 30:
            raise ADFError(f"invalid directory name: {name!r}")
        if "/" in name or ":" in name:
            raise ADFError(
                f"name {name!r} contains illegal characters")

        bits = self._read_bitmap()
        header_blk = self._alloc_block(bits)
        next_in_chain = self._link_into_parent(
            parent_block, header_blk, name)

        ts = timestamp or datetime.now()
        days, mins, ticks = _date_to_amiga(ts)
        header = bytearray(BLOCK_SIZE)
        _write_u32(header, 0, T_HEADER)
        _write_u32(header, 4, header_blk)
        # Hash table all zero - empty directory
        _write_u32(header, BLOCK_SIZE - 192, protection)
        _write_bcpl_string(
            header, BLOCK_SIZE - 184, comment, 79)
        _write_u32(header, BLOCK_SIZE - 92, days)
        _write_u32(header, BLOCK_SIZE - 88, mins)
        _write_u32(header, BLOCK_SIZE - 84, ticks)
        _write_bcpl_string(header, BLOCK_SIZE - 80, name, 30)
        _write_u32(header, BLOCK_SIZE - 16, next_in_chain)
        _write_u32(header, BLOCK_SIZE - 12, parent_block)
        _write_u32(header, BLOCK_SIZE - 4, ST_USERDIR)
        _checksum_block(header, 5 * 4)
        self._write_block(header_blk, bytes(header))
        self._write_bitmap(bits)
        return header_blk

    def rename_entry(self, header_block: int,
                       new_name: str) -> None:
        """Change the name of a file or directory. Because the
        hash bucket is derived from the name, we unlink from the
        current chain and re-link under the new name's bucket."""
        if new_name in ("", ".", "..") or len(new_name) > 30:
            raise ADFError(f"invalid name: {new_name!r}")
        entry = self._parse_header_block(header_block)
        parent_block = entry.parent_block or self.rootblock_num
        self._unlink_from_parent(parent_block, entry)
        hb = bytearray(self._block(header_block))
        _write_bcpl_string(hb, BLOCK_SIZE - 80, new_name, 30)
        new_next = self._link_into_parent(
            parent_block, header_block, new_name)
        _write_u32(hb, BLOCK_SIZE - 16, new_next)
        _checksum_block(hb, 5 * 4)
        self._write_block(header_block, bytes(hb))

    def set_protection(self, header_block: int,
                          new_protection: int) -> None:
        """Change the 32-bit protection field. Low 4 bits are
        inverted-RWED; high 4 bits are HSPA. See
        format_protection() for layout."""
        hb = bytearray(self._block(header_block))
        _write_u32(hb, BLOCK_SIZE - 192,
                     new_protection & 0xFFFFFFFF)
        _checksum_block(hb, 5 * 4)
        self._write_block(header_block, bytes(hb))

    def set_comment(self, header_block: int,
                       new_comment: str) -> None:
        """Replace the file/directory comment (max 79 chars)."""
        if len(new_comment) > 79:
            raise ADFError(
                f"comment max 79 chars, got {len(new_comment)}")
        hb = bytearray(self._block(header_block))
        _write_bcpl_string(hb, BLOCK_SIZE - 184, new_comment, 79)
        _checksum_block(hb, 5 * 4)
        self._write_block(header_block, bytes(hb))


# ===== Module-level: blank disk creation ============================

def create_blank_adf(path,
                       label: str = "Empty",
                       ffs: bool = False,
                       intl: bool = False,
                       hd: bool = False) -> ADFImage:
    """Create a brand-new blank ADF at `path` and return an open
    ADFImage handle. The disk has a valid bootblock, rootblock,
    and bitmap; root directory is empty.

    ffs=False, intl=False => Workbench 1.3 compatible OFS disk
    ffs=True              => FFS (needs Kickstart 2.0+)
    intl=True             => international char handling
    hd=True               => 1.76 MB instead of 880 KB"""
    n_blocks = 3520 if hd else 1760
    rootblock_num = n_blocks // 2
    size = n_blocks * BLOCK_SIZE

    img_bytes = bytearray(size)

    # Bootblock (blocks 0+1)
    flags = 0
    if ffs:    flags |= DOS_FLAG_FFS
    if intl:   flags |= DOS_FLAG_INTL
    img_bytes[0:4] = b"DOS\x00"
    img_bytes[3] = flags
    struct.pack_into(">I", img_bytes, 8, rootblock_num)
    struct.pack_into(">I", img_bytes, 4, 0)
    total = 0
    for i in range(0, BLOCK_SIZE * 2, 4):
        total = (total
                  + struct.unpack_from(">I", img_bytes, i)[0]
                  ) & 0xFFFFFFFF
    struct.pack_into(">I", img_bytes, 4, (-total) & 0xFFFFFFFF)

    # Bitmap block at block N-1 (traditional placement). Wait -
    # historically AmigaDOS puts bitmap at rootblock+1 on DD
    # floppies. Use that placement.
    bitmap_block_num = rootblock_num + 1
    n_data_blocks = n_blocks - 2
    n_bytes = (n_data_blocks + 7) // 8
    bm = bytearray(BLOCK_SIZE)
    for i in range(4, 4 + n_bytes):
        bm[i] = 0xFF
    valid_in_last = n_data_blocks % 8
    if valid_in_last:
        mask = (1 << valid_in_last) - 1
        bm[4 + n_bytes - 1] &= mask
    # Mark rootblock + bitmap block as used
    for used in (rootblock_num, bitmap_block_num):
        idx = used - 2
        byte_idx, bit_idx = divmod(idx, 8)
        bm[4 + byte_idx] &= ~(1 << bit_idx) & 0xFF
    struct.pack_into(">I", bm, 0, 0)
    total = 0
    for i in range(0, BLOCK_SIZE, 4):
        total = (total
                  + struct.unpack_from(">I", bm, i)[0]
                  ) & 0xFFFFFFFF
    struct.pack_into(">I", bm, 0, (-total) & 0xFFFFFFFF)
    img_bytes[bitmap_block_num * BLOCK_SIZE
                :(bitmap_block_num + 1) * BLOCK_SIZE] = bm

    # Rootblock
    now = datetime.now()
    delta = now - datetime(1978, 1, 1)
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    rem = total_seconds - days * 86400
    mins = rem // 60
    rem -= mins * 60
    ticks = rem * 50

    rb = bytearray(BLOCK_SIZE)
    struct.pack_into(">I", rb, 0, T_HEADER)
    struct.pack_into(">I", rb, 8, 0)
    struct.pack_into(">I", rb, 12, 72)
    struct.pack_into(">I", rb, 79 * 4, 0xFFFFFFFF)
    struct.pack_into(">I", rb, 80 * 4, bitmap_block_num)
    struct.pack_into(">I", rb, 105 * 4, days)
    struct.pack_into(">I", rb, 106 * 4, mins)
    struct.pack_into(">I", rb, 107 * 4, ticks)
    # Disk label at offset 108*4 (length byte position)
    label_b = label.encode("latin-1", errors="replace")[:30]
    rb[108 * 4] = len(label_b)
    for i, b in enumerate(label_b):
        rb[108 * 4 + 1 + i] = b
    # Creation/modification times at 120-122 and 123-125
    for slot_base in (120, 123):
        struct.pack_into(">I", rb, slot_base * 4, days)
        struct.pack_into(">I", rb, (slot_base + 1) * 4, mins)
        struct.pack_into(">I", rb, (slot_base + 2) * 4, ticks)
    struct.pack_into(">I", rb, BLOCK_SIZE - 4, ST_ROOT)
    struct.pack_into(">I", rb, 5 * 4, 0)
    total = 0
    for i in range(0, BLOCK_SIZE, 4):
        total = (total
                  + struct.unpack_from(">I", rb, i)[0]
                  ) & 0xFFFFFFFF
    struct.pack_into(">I", rb, 5 * 4, (-total) & 0xFFFFFFFF)
    img_bytes[rootblock_num * BLOCK_SIZE
                :(rootblock_num + 1) * BLOCK_SIZE] = rb

    Path(path).write_bytes(img_bytes)
    return ADFImage(path)
