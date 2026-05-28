# date_time: 2026-05-28 22:06
"""
Wrapper around the real TAPClean command-line tool.

Rather than reimplementing TAPClean's 93 loader scanners in Python
(an enormous, never-quite-accurate undertaking), Quopus ships the
GPL TAPClean C source under external/tapclean/ and calls the
compiled binary. This gives 100% TAPClean-accurate loader
identification, file detection and PRG extraction.

This module:
  * locates a prebuilt tapclean binary, or builds it on first use
    (needs a C compiler + make; falls back gracefully if absent)
  * runs `tapclean -t <tap> -doprg` in a temp working dir
  * parses tcreport.txt into a structured report
  * collects the extracted prg/ files

If TAPClean can't be built or run (no compiler, Windows without
the bundled .exe, etc.), is_available() returns False and the
toolkit falls back to the built-in Python analyzer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Where the TAPClean source lives inside the Quopus tree.
def _quopus_root() -> Path:
    # this file is quopus_lib/tap_tapclean.py -> root is parent of
    # quopus_lib
    return Path(__file__).resolve().parent.parent


def _tapclean_src_dir() -> Path:
    return _quopus_root() / "external" / "tapclean" / "src"


def _binary_name() -> str:
    return "tapclean.exe" if os.name == "nt" else "tapclean"


def _binary_search_paths():
    """All locations where Quopus looks for a prebuilt tapclean
    binary. First hit wins. We accept all the places a user might
    reasonably drop it:

      1. external/tapclean/src/tapclean[.exe]  - canonical (next to
         the build output / where `make` would put it)
      2. external/tapclean/tapclean[.exe]      - sibling of the src
         folder (some users put it here after a manual build)
      3. external/tapclean[.exe]               - alongside the other
         external tools (recoil2png, nibconv, unlzx)
      4. system PATH                           - if tapclean is on
         the user's PATH from a system install
    """
    bn = _binary_name()
    ext_dir = _quopus_root() / "external"
    candidates = [
        ext_dir / "tapclean" / "src" / bn,
        ext_dir / "tapclean" / bn,
        ext_dir / bn,
    ]
    return candidates


def _find_existing_binary():
    """Return the first existing tapclean binary from the search
    paths, or None. Also checks PATH as a last resort.

    The lookup is case-insensitive on the filename so 'TAPClean.exe'
    or 'tapclean' both match - on Windows users sometimes preserve
    upstream capitalisation, and that mustn't break the wrapper."""
    bn = _binary_name().lower()
    for p in _binary_search_paths():
        # Direct match (covers the case-sensitive case on
        # Linux/macOS and the common case on Windows where
        # Path.is_file() is case-insensitive anyway).
        if p.is_file():
            return p
        # Case-insensitive sibling lookup: scan the parent
        # directory for any file whose lower-cased name matches.
        try:
            parent = p.parent
            if parent.is_dir():
                for entry in parent.iterdir():
                    if (entry.is_file()
                            and entry.name.lower() == bn):
                        return entry
        except OSError:
            pass
    onpath = shutil.which(_binary_name())
    if onpath:
        return Path(onpath)
    # also try Title case on PATH (some installs ship TAPClean.exe)
    if os.name == "nt":
        for nm in ("TAPClean.exe", "TAPClean"):
            onpath = shutil.which(nm)
            if onpath:
                return Path(onpath)
    return None


def _binary_path() -> Path:
    """Canonical build target (where `make` writes the binary).
    Used by the build step; for lookup use _find_existing_binary()
    which checks more locations."""
    return _tapclean_src_dir() / _binary_name()


# ---------------------------------------------------------------------
# Build / locate
# ---------------------------------------------------------------------

_build_attempted = False
_build_error = ""


def _try_build() -> bool:
    """Compile TAPClean from the bundled source. Returns True on
    success. Result is cached so we only try once per session.

    Building is intentionally restricted to non-Windows platforms.
    On Windows the MinGW toolchain is rarely available, and when
    a stale `make.exe` happens to be on PATH the build can fail
    catastrophically (DLL load errors like 0xc000007b that bubble
    up as a popup before we even see the return code). Windows
    users should drop a prebuilt `tapclean.exe` into one of the
    locations listed in `_binary_search_paths()`."""
    global _build_attempted, _build_error
    if _build_attempted:
        return _binary_path().exists()
    _build_attempted = True

    if os.name == "nt":
        _build_error = (
            "Auto-build is disabled on Windows. Drop a prebuilt "
            "tapclean.exe into external/tapclean/src/ (or the "
            "external/ folder) to enable the toolkit. Quopus will "
            "fall back to the built-in Python analyzer.")
        return False

    src = _tapclean_src_dir()
    if not src.is_dir():
        _build_error = f"TAPClean source not found at {src}"
        return False

    make = shutil.which("make")
    cc = shutil.which("gcc") or shutil.which("cc")
    if make is None or cc is None:
        _build_error = ("No C compiler/make available to build "
                        "TAPClean (need gcc + make)")
        return False

    try:
        proc = subprocess.run(
            [make], cwd=str(src), capture_output=True,
            text=True, timeout=300)
        if proc.returncode != 0:
            _build_error = (
                "TAPClean build failed:\n"
                + (proc.stderr or proc.stdout)[-2000:])
            return False
    except Exception as e:
        _build_error = f"TAPClean build error: {e}"
        return False

    if not _binary_path().exists():
        _build_error = "TAPClean built but binary not found"
        return False
    # make sure it's executable
    try:
        os.chmod(_binary_path(), 0o755)
    except OSError:
        pass
    return True


def is_available() -> bool:
    """True if a usable TAPClean binary exists or can be built."""
    # Check every plausible drop-in location first - if the user
    # placed a prebuilt binary anywhere reasonable, use it without
    # touching the build system.
    if _find_existing_binary() is not None:
        return True
    return _try_build()


def build_error() -> str:
    """Human-readable reason the binary isn't available (if any)."""
    return _build_error


# ---------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------

@dataclass
class TapCleanFile:
    """One file from the TAPClean report / prg extraction."""
    seq: int
    file_type: str
    location: str = ""
    load_addr: int = -1
    end_addr: int = -1
    size: int = 0
    name: str = ""
    checkbyte_actual: str = ""
    checkbyte_expected: str = ""
    checkbyte_pass: Optional[bool] = None
    read_errors: int = 0
    crc32: str = ""
    data_load_addr: int = -1     # for headers: the DATA file addr
    data_end_addr: int = -1
    file_id: str = ""            # FIRST / REPEAT
    prg_path: Optional[Path] = None   # extracted .prg on disk
    prg_name: str = ""


@dataclass
class TapCleanReport:
    raw_report: str = ""
    tap_name: str = ""
    tap_size: int = 0
    tap_version: int = 0
    computer: str = ""
    recognized_percent: float = 0.0
    data_files: int = 0
    pauses: int = 0
    gaps: int = 0
    magic_crc32: str = ""
    tap_time: str = ""
    bootable: str = ""
    loader_id: str = ""
    overall_result: str = ""
    header_test: str = ""
    recognition_test: str = ""
    checksum_test: str = ""
    read_test: str = ""
    optimization_test: str = ""
    files: list = field(default_factory=list)   # TapCleanFile
    prg_dir: Optional[Path] = None


# ---------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------

_GEN_FIELDS = {
    "TAP Name": "tap_name",
    "Computer type": "computer",
    "Bootable": "bootable",
    "Loader ID": "loader_id",
    "Overall Result": "overall_result",
    "Magic CRC32": "magic_crc32",
    "TAP Time": "tap_time",
}


def _parse_report(text: str) -> TapCleanReport:
    rep = TapCleanReport(raw_report=text)
    lines = text.splitlines()

    def val_after_colon(line):
        return line.split(":", 1)[1].strip() if ":" in line else ""

    for ln in lines:
        s = ln.strip()
        if s.startswith("TAP Size"):
            m = re.search(r"(\d+)\s*bytes", s)
            if m:
                rep.tap_size = int(m.group(1))
        elif s.startswith("TAP Version"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                rep.tap_version = int(m.group(1))
        elif s.startswith("Recognized"):
            m = re.search(r"(\d+)%", s)
            if m:
                rep.recognized_percent = float(m.group(1))
        elif s.startswith("Data Files"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                rep.data_files = int(m.group(1))
        elif s.startswith("Pauses"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                rep.pauses = int(m.group(1))
        elif s.startswith("Gaps"):
            m = re.search(r":\s*(\d+)", s)
            if m:
                rep.gaps = int(m.group(1))
        elif s.startswith("TAP Name"):
            rep.tap_name = val_after_colon(s)
        elif s.startswith("Computer type"):
            rep.computer = val_after_colon(s)
        elif s.startswith("Magic CRC32"):
            rep.magic_crc32 = val_after_colon(s)
        elif s.startswith("TAP Time"):
            rep.tap_time = val_after_colon(s)
        elif s.startswith("Bootable"):
            rep.bootable = val_after_colon(s)
        elif s.startswith("Loader ID"):
            rep.loader_id = val_after_colon(s)
        elif s.startswith("Overall Result"):
            rep.overall_result = val_after_colon(s)
        elif s.startswith("Header test"):
            rep.header_test = val_after_colon(s)
        elif s.startswith("Recognition test"):
            rep.recognition_test = val_after_colon(s)
        elif s.startswith("Checksum test"):
            rep.checksum_test = val_after_colon(s)
        elif s.startswith("Read test"):
            rep.read_test = val_after_colon(s)
        elif s.startswith("Optimization test"):
            rep.optimization_test = val_after_colon(s)

    # Parse the FILE DATABASE section into per-file blocks.
    rep.files = _parse_file_blocks(text)
    return rep


def _parse_file_blocks(text: str) -> list:
    files = []
    # Each file block starts with "Seq. no.:" and is separated by
    # the dashed line.
    blocks = re.split(r"-{10,}", text)
    for blk in blocks:
        if "Seq. no.:" not in blk:
            continue
        f = TapCleanFile(seq=0, file_type="")
        for ln in blk.splitlines():
            s = ln.strip()
            if s.startswith("Seq. no.:"):
                m = re.search(r":\s*(\d+)", s)
                if m:
                    f.seq = int(m.group(1))
            elif s.startswith("File Type:"):
                f.file_type = s.split(":", 1)[1].strip()
            elif s.startswith("Location:"):
                f.location = s.split(":", 1)[1].strip()
            elif s.startswith("LA:"):
                m = re.search(r"LA:\s*\$?([0-9A-Fa-f]+)\s+"
                              r"EA:\s*\$?([0-9A-Fa-f]+)\s+"
                              r"SZ:\s*(\d+)", s)
                if m:
                    f.load_addr = int(m.group(1), 16)
                    f.end_addr = int(m.group(2), 16)
                    f.size = int(m.group(3))
            elif s.startswith("File Name:"):
                f.name = s.split(":", 1)[1].strip()
            elif s.startswith("Checkbyte Actual/Expected:"):
                m = re.search(
                    r"\$?([0-9A-Fa-f]+)/\$?([0-9A-Fa-f]+),\s*"
                    r"(PASS|FAIL)", s)
                if m:
                    f.checkbyte_actual = m.group(1)
                    f.checkbyte_expected = m.group(2)
                    f.checkbyte_pass = (m.group(3) == "PASS")
            elif s.startswith("Read Errors:"):
                m = re.search(r":\s*(\d+)", s)
                if m:
                    f.read_errors = int(m.group(1))
            elif s.startswith("CRC32:"):
                f.crc32 = s.split(":", 1)[1].strip()
            elif "File ID :" in s:
                f.file_id = s.split(":", 1)[1].strip()
            elif "DATA FILE Load address" in s:
                m = re.search(r"\$([0-9A-Fa-f]+)", s)
                if m:
                    f.data_load_addr = int(m.group(1), 16)
            elif "DATA FILE End address" in s:
                m = re.search(r"\$([0-9A-Fa-f]+)", s)
                if m:
                    f.data_end_addr = int(m.group(1), 16)
        files.append(f)
    return files


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------

def analyze(tap_path, extract_prgs=True,
            extra_args=None) -> Optional[TapCleanReport]:
    """Run TAPClean on `tap_path`. Returns a TapCleanReport, or
    None if TAPClean isn't available.

    Runs in a temp dir so tcreport.txt and the prg/ folder don't
    clutter the user's filesystem. When extract_prgs is True the
    extracted .prg files are matched to their report entries via
    the prg_path field (caller can copy them wherever).
    """
    if not is_available():
        return None
    # Use whichever binary location actually exists - the user may
    # have dropped a prebuilt tapclean.exe into external/ rather
    # than external/tapclean/src/.
    binary = _find_existing_binary() or _binary_path()
    tap_path = Path(tap_path)
    if not tap_path.is_file():
        return None

    work = Path(tempfile.mkdtemp(prefix="quopus_tapclean_"))
    try:
        # TAPClean normally writes prg/ and tcreport.txt to the
        # CWD, so we run it in a fresh temp dir. Some Windows
        # builds of TAPClean instead write next to the executable
        # (or to whichever directory was current when subprocess
        # started Python) - we check both locations so the wrapper
        # works regardless of the binary's behaviour.
        args = [str(binary), "-t", str(tap_path.resolve())]
        if extract_prgs:
            args.append("-doprg")
        if extra_args:
            args.extend(extra_args)
        try:
            proc = subprocess.run(
                args, cwd=str(work), capture_output=True,
                text=True, timeout=120)
        except Exception:
            return None

        # Where to look for the outputs - in order of preference:
        # the temp work dir (the CWD we passed), and the directory
        # the binary lives in (some TAPClean builds write there).
        out_dirs = [work, binary.parent]
        report_txt = None
        prg_dir = None
        for d in out_dirs:
            cand_report = d / "tcreport.txt"
            if cand_report.is_file() and report_txt is None:
                report_txt = cand_report
            cand_prg = d / "prg"
            if cand_prg.is_dir() and prg_dir is None:
                prg_dir = cand_prg
            if report_txt and prg_dir:
                break

        if report_txt is not None:
            text = report_txt.read_text(errors="replace")
        else:
            # fall back to stdout
            text = proc.stdout
        rep = _parse_report(text)

        # Collect extracted PRGs and match to report files by seq.
        if extract_prgs and prg_dir is not None and prg_dir.is_dir():
            # persist the prg dir to a stable temp location so the
            # caller can read the files after this function returns
            keep = Path(tempfile.mkdtemp(prefix="quopus_prg_"))
            for p in sorted(prg_dir.iterdir()):
                if p.is_file():
                    shutil.copy2(p, keep / p.name)
            rep.prg_dir = keep
            _match_prgs_to_files(rep, keep)
            # If TAPClean wrote into the binary's directory rather
            # than our temp work dir, clean those droppings up so
            # we don't leave junk next to external/tapclean[.exe].
            if prg_dir.parent != work:
                try:
                    shutil.rmtree(prg_dir, ignore_errors=True)
                except Exception:
                    pass
        # Also remove a stray tcreport.txt that landed next to
        # the binary (same reason).
        if (report_txt is not None
                and report_txt.parent != work):
            try:
                report_txt.unlink()
            except Exception:
                pass
        return rep
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _match_prgs_to_files(rep: TapCleanReport, prg_dir: Path):
    """Match extracted prg files (named '003 (xxxx-yyyy) [name].prg')
    to their report entries via the leading sequence number."""
    prgs = {}
    for p in prg_dir.iterdir():
        if not p.is_file():
            continue
        m = re.match(r"(\d+)\s", p.name)
        if m:
            prgs[int(m.group(1))] = p
    for f in rep.files:
        if f.seq in prgs:
            f.prg_path = prgs[f.seq]
            f.prg_name = prgs[f.seq].name


def list_prgs(rep: TapCleanReport) -> list:
    """Return [(report_file, Path)] for every extracted PRG."""
    out = []
    for f in rep.files:
        if f.prg_path is not None:
            out.append((f, f.prg_path))
    return out
