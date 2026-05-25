"""HVSC Songlengths.md5 parser.

The HVSC project ships a Songlengths.md5 file that maps each SID
file's MD5 hash to a list of per-subsong durations. We use this to
display real song time in the player and to auto-advance shuffle
play exactly when the current track ends.

File location: dopus_out/config/Songlengths.md5

Format (since HVSC #68):
    [Database]
    Version: 76
    abc123def456...=1:35 2:10 0:45
    abc123def456...=1:35.500

Each entry is either:
    MM:SS               (whole seconds)
    MM:SS.mmm           (with milliseconds)

Older format (pre-#68) had a different MD5 method but same line
format; libsidplayfp can compute both via its createMD5 / createMD5New
methods, and we try the new format first.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------
# Time string parsing
# ---------------------------------------------------------------------
# Matches "MM:SS" or "MM:SS.mmm" with M up to 2 digits, S 2 digits,
# milliseconds 1-3 digits optional.
_TIME_RE = re.compile(r'^(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?$')


def parse_time(s: str) -> Optional[int]:
    """Parse a single 'MM:SS' or 'MM:SS.mmm' duration string into
    integer milliseconds. Returns None if the format doesn't match."""
    m = _TIME_RE.match(s.strip())
    if not m:
        return None
    mm = int(m.group(1))
    ss = int(m.group(2))
    if ss >= 60:
        return None
    ms = m.group(3)
    if ms:
        # left-justify so '5' -> 500 ms, '50' -> 500 ms, '500' -> 500 ms
        ms_int = int(ms.ljust(3, '0')[:3])
    else:
        ms_int = 0
    return (mm * 60 + ss) * 1000 + ms_int


# ---------------------------------------------------------------------
# Songlengths database
# ---------------------------------------------------------------------
class SongLengthsDB:
    """Lazy loader for HVSC Songlengths.md5.

    Usage:
        db = SongLengthsDB(Path('config/Songlengths.md5'))
        ms_list = db.get(md5_hash)   # list[int] in milliseconds, or None
        ms = db.get_subsong(md5_hash, subsong=1)  # 1-based, in ms

    The file is parsed once on first access; subsequent lookups are
    O(1) dict accesses. Total file size is typically ~3 MB for the
    full HVSC database; parsed dict fits comfortably in memory.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: dict[str, list[int]] | None = None
        self._loaded = False

    @property
    def available(self) -> bool:
        """True if the songlengths file exists. Doesn't trigger
        loading - just checks the file."""
        return self.path.is_file()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._db = {}
        if not self.path.is_file():
            return
        try:
            # The HVSC file is ASCII so this is safe; use latin-1 as
            # a permissive fallback since some old entries may have
            # legacy encoded comments.
            with open(self.path, 'r', encoding='latin-1') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(';') \
                       or line.startswith('['):
                        continue
                    if 'Version:' in line:
                        continue
                    eq = line.find('=')
                    if eq < 0:
                        continue
                    md5 = line[:eq].strip().lower()
                    if len(md5) != 32:
                        continue
                    times_str = line[eq + 1:].strip()
                    times = []
                    for t in times_str.split():
                        ms = parse_time(t)
                        if ms is None:
                            # Malformed entry - skip the whole line
                            times = []
                            break
                        times.append(ms)
                    if times:
                        self._db[md5] = times
        except Exception:
            # Don't propagate - users without an HVSC file just get
            # no auto-skip information.
            pass

    def get(self, md5_hash: str) -> Optional[list[int]]:
        """Return the per-subsong duration list (ms) for the given
        MD5, or None if not found / db not loaded."""
        self._ensure_loaded()
        if not self._db or not md5_hash:
            return None
        return self._db.get(md5_hash.lower())

    def get_subsong(self, md5_hash: str,
                      subsong: int) -> Optional[int]:
        """Return the duration in ms for a specific subsong (1-based),
        or None if not found / out of range. The 1-based numbering
        matches what users see in the player UI and what libsidplayfp
        exposes as 'currentSong'."""
        lst = self.get(md5_hash)
        if not lst:
            return None
        idx = subsong - 1
        if 0 <= idx < len(lst):
            return lst[idx]
        return None

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._db) if self._db else 0


# ---------------------------------------------------------------------
# Auto-download from HVSC mirror
# ---------------------------------------------------------------------


# Primary HVSC Songlengths.md5 URL. The HVSC project distributes the
# database in zip-form on the project's official site, mirrored on
# c64.com. Format-version pinned so we can detect upgrades. The
# project moves URLs occasionally so we try a few candidates.
HVSC_SONGLENGTHS_URLS = [
    # Direct .md5 hosted by Geir Tjelta (HVSC maintainer)
    "https://www.transbyte.org/SID/hvsc/Songlengths.md5",
    # HVSC main hosted on hvsc.de
    "https://hvsc.de/download/Songlengths.md5",
    # c64.com mirror
    "https://www.c64.com/sid/Songlengths.md5",
]


def download_songlengths(dest_path,
                            progress_callback=None,
                            timeout: float = 60.0):
    """Download a fresh Songlengths.md5 from a HVSC mirror.

    `dest_path` is where to write the file. `progress_callback`,
    if provided, gets called with (bytes_read, total_bytes_or_None,
    status_string).

    Returns (True, path_str) on success, (False, error_str) on
    failure. Tries each mirror in turn; first that responds wins.
    """
    import urllib.request, urllib.error, os
    last_err = None
    for url in HVSC_SONGLENGTHS_URLS:
        try:
            if progress_callback:
                progress_callback(0, None, f"connecting to {url}")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Quopus/1.0 (HVSC fetch)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                total = None
                try:
                    total = int(r.headers.get('Content-Length', '0'))
                    if total <= 0:
                        total = None
                except (TypeError, ValueError):
                    total = None
                chunks = []
                read = 0
                chunk_size = 64 * 1024
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    read += len(chunk)
                    if progress_callback:
                        progress_callback(
                            read, total,
                            f"downloading from {url}")
                data = b"".join(chunks)
            # Sanity check: file should start with [Database] and
            # contain at least a few entries.
            head = data[:200].decode('latin-1', errors='replace')
            if "[Database]" not in head and "=" not in head:
                last_err = (
                    f"got {len(data)} bytes from {url} but it "
                    "doesn't look like a Songlengths.md5 file")
                continue
            # Write to dest
            os.makedirs(
                os.path.dirname(os.path.abspath(dest_path)),
                exist_ok=True)
            with open(dest_path, 'wb') as f:
                f.write(data)
            if progress_callback:
                progress_callback(
                    len(data), len(data),
                    f"saved {len(data) // 1024} KB")
            return True, str(dest_path)
        except urllib.error.URLError as e:
            last_err = f"{url}: {e}"
            continue
        except (OSError, ValueError) as e:
            last_err = f"{url}: {e}"
            continue
    return False, last_err or "no mirror responded"
