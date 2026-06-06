# date_time: 2026-06-06 10:15
"""Update manager for the frozen (.exe) build of Quopus.

Combines three update strategies:

  A. Sidecar qpe overlay
     The bundled .qpe modules can be hot-patched by dropping
     newer versions into qpe_updates/ next to the EXE.
     _qpe_loader.py's finder consults that folder first, so
     anything there overrides what's inside the EXE without
     touching the executable itself. This module just manages
     the folder - download + extract.

  B. Full EXE replacement
     For breaking changes (new Python deps, qt version bump
     etc.) the whole EXE has to be swapped. We download
     Quopus_NEW.exe alongside, drop a small .bat helper next to
     it, and tell the user to restart. On restart the helper
     deletes the running EXE and renames _NEW into place.

  C. qpe-bundle update via ZIP
     A small zip file ('quopus_qpe_<version>.zip') containing
     one or more .qpe files. We extract it into the sidecar
     folder. Same effect as A but as a one-click action that
     pulls from a GitHub release asset.

The check itself uses GitHub's release API (no auth needed for
public repos). The repo is hardcoded as Mario's
github.com/lastylegp/Quopus.

This module is dormant in source-tree dev mode: every entry
point bails out fast unless sys.frozen is True. The Python-
source build keeps using the existing update_checker.py
git-diff pipeline.
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import zipfile
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# GitHub repository to query for releases. Tagged releases are
# expected to have one or both of the following assets attached:
#   - Quopus_v<MAJOR.MINOR>.exe        (full EXE replace)
#   - quopus_qpe_<MAJOR.MINOR>.zip     (qpe-only patch bundle)
GITHUB_OWNER = "lastylegp"
GITHUB_REPO = "Quopus"
GITHUB_RELEASES_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/"
    f"{GITHUB_REPO}/releases/latest")

USER_AGENT = "Quopus-Updater/1.0"


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _quopus_lib_dir() -> Path:
    """The actual on-disk folder where the .qpe files live. In
    a PyInstaller --onedir build that's <build>/_internal/quopus_lib/
    (or whatever the build layout puts it at); we just ask the
    installed package for its __path__ instead of guessing.

    Used as the install target for qpe updates - we write the
    new files RIGHT NEXT TO the old ones so the import system
    finds them at the next launch without any sidecar logic.
    """
    import quopus_lib
    paths = list(quopus_lib.__path__)
    if not paths:
        return Path(__file__).resolve().parent
    return Path(paths[0]).resolve()


def _install_root() -> Path:
    """The user-visible install folder containing Quopus.exe
    (and, beneath it, _internal/quopus_lib/, fonts/, icons/
    etc.). Update bundles use this as the extraction root so
    arbitrary files - .qpe modules, icon assets, font files,
    config templates, anything that isn't the EXE itself -
    can be replaced with a single zip."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    # Source dev mode: the package's parent is the project
    # root. Less meaningful here since the update system is
    # frozen-only in practice, but lets us run the tests.
    return _quopus_lib_dir().parent


def _exe_dir() -> Path:
    """Folder containing the running EXE. Alias kept for the
    EXE-swap helper which talks about 'next to the EXE' in
    its docs/comments. Same as _install_root() in practice."""
    return _install_root()


# Extensions we refuse to overwrite from a qpe-style update
# bundle. These belong to the EXE-replace path because they
# either ARE the executable, are loaded by the Windows loader
# at startup (DLLs/PYDs), or are signed components that lose
# their signature on rewrite. The user is told to use the
# full-EXE update instead when these turn up in a bundle.
_LOCKED_FILE_SUFFIXES = (".exe", ".dll", ".pyd", ".so", ".dylib")


# ---------------------------------------------------------------
# GitHub release check
# ---------------------------------------------------------------

def fetch_latest_release_info(timeout: float = 10.0) -> dict:
    """Query GitHub's releases API for the latest tagged release.

    Returns a dict with keys:
        tag_name      "v1.1"
        name          "Quopus 1.1"
        body          markdown release notes
        assets        list of {name, browser_download_url, size}
        published_at  ISO timestamp

    Raises urllib.error.URLError or json.JSONDecodeError on
    network/parse failures - the caller is expected to wrap the
    call and translate to a user-facing error dialog.
    """
    req = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    # Trim asset metadata down to what we actually use - keeps
    # the cached dict small and makes test mocking easier.
    assets = [
        {"name": a.get("name", ""),
         "url": a.get("browser_download_url", ""),
         "size": int(a.get("size", 0))}
        for a in data.get("assets", [])
    ]
    return {
        "tag_name": data.get("tag_name", ""),
        "name": data.get("name", ""),
        "body": data.get("body", "") or "",
        "assets": assets,
        "published_at": data.get("published_at", ""),
    }


def _find_asset_by_pattern(
        assets: list, prefix: str, suffix: str) -> Optional[dict]:
    """Find an asset whose name starts with `prefix` AND ends
    with `suffix`. Used to locate the EXE vs the qpe zip among
    a release's attached files. Case-insensitive."""
    pre = prefix.lower()
    suf = suffix.lower()
    for a in assets:
        n = a.get("name", "").lower()
        if n.startswith(pre) and n.endswith(suf):
            return a
    return None


def find_exe_asset(release_info: dict) -> Optional[dict]:
    """The EXE replacement asset, if attached. Looks for any
    file starting with 'Quopus' and ending with '.exe'."""
    return _find_asset_by_pattern(
        release_info.get("assets", []), "Quopus", ".exe")


def find_qpe_bundle_asset(release_info: dict) -> Optional[dict]:
    """The qpe-bundle zip asset, if attached. Looks for any
    file starting with 'quopus_qpe' and ending with '.zip'."""
    return _find_asset_by_pattern(
        release_info.get("assets", []), "quopus_qpe", ".zip")


# ---------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------

def download_to_file(
        url: str, dst_path: Path,
        progress_cb=None,
        timeout: float = 30.0) -> int:
    """Stream-download `url` to `dst_path`. Returns bytes
    written. If `progress_cb` is supplied it gets called with
    (bytes_so_far, total_bytes_or_None) every chunk so callers
    can drive a progress bar.

    Writes to a .partial file and renames into place at the end
    so a half-downloaded file never looks complete to the rest
    of the update flow."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT})
    partial = dst_path.with_suffix(dst_path.suffix + ".partial")
    written = 0
    total = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = None
        partial.parent.mkdir(parents=True, exist_ok=True)
        with open(partial, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress_cb is not None:
                    try:
                        progress_cb(written, total)
                    except Exception:
                        # Progress callbacks must never abort
                        # the download - they're cosmetic.
                        pass
    # Atomic-ish rename: on Windows, replace() works even if
    # dst exists (unlike rename()).
    partial.replace(dst_path)
    return written


# ---------------------------------------------------------------
# A + C: qpe-bundle update
# ---------------------------------------------------------------

def apply_update_bundle(zip_path: Path) -> tuple:
    """Extract an update zip directly into the Quopus install
    folder. Files are written at the same relative paths they
    have inside the zip - so a zip entry
    '_internal/quopus_lib/actions.qpe' lands at
    <install_root>/_internal/quopus_lib/actions.qpe, and
    'fonts/topaz.ttf' lands at <install_root>/fonts/topaz.ttf.

    This means you can ship updates for ANY file in the build
    that isn't the executable itself: .qpe modules, icons,
    fonts, sound effects, default config templates, README
    files, whatever. Just put them in the zip with the right
    path and they get overwritten in place on next install.

    Files with extensions that the OS keeps locked while
    Quopus runs - .exe, .dll, .pyd, .so, .dylib - are
    skipped with a recorded warning. Those need the full-EXE
    update path because they can't be replaced while the
    process is running anyway.

    Locked .qpe files (very rare, only if a second Quopus is
    open or AV is scanning) get written to a .qpe.new sibling
    instead. The user is asked to restart and try again.

    Returns (installed_count, error_or_None). Path-traversal
    attempts (entries containing '..' or absolute paths) are
    silently dropped.
    """
    root = _install_root()
    root_resolved = root.resolve()
    installed = 0
    locked = 0
    skipped_locked_native = []
    last_error = None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                # Reject path escapes (.., absolute paths).
                # The resolved path must sit inside root.
                dest = (root / name).resolve()
                try:
                    dest.relative_to(root_resolved)
                except ValueError:
                    continue
                # Block native binaries - they need full EXE
                # update. Record the names so we can tell the
                # user why their bundle didn't fully apply.
                if dest.suffix.lower() in _LOCKED_FILE_SUFFIXES:
                    skipped_locked_native.append(name)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as src, \
                            open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    installed += 1
                except PermissionError:
                    # File locked - write a sibling .new and
                    # ask the user to restart. Only applies to
                    # .qpe files in practice; for everything
                    # else PermissionError means a real
                    # permission problem (Program Files etc.).
                    new_sib = dest.with_name(
                        dest.name + ".new")
                    try:
                        with zf.open(info) as src, \
                                open(new_sib, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        installed += 1
                        locked += 1
                    except Exception as ee:
                        last_error = str(ee)
    except Exception as e:
        last_error = str(e)
    notes = []
    if locked:
        notes.append(
            f"{locked} file(s) were locked and got installed "
            f"as .new siblings - restart Quopus and the install "
            f"should pick them up next time.")
    if skipped_locked_native:
        sample = ", ".join(skipped_locked_native[:3])
        if len(skipped_locked_native) > 3:
            sample += f", ... +{len(skipped_locked_native) - 3}"
        notes.append(
            f"Skipped {len(skipped_locked_native)} native "
            f"binary file(s): {sample}. These need the full-"
            f"EXE update path.")
    if notes and last_error is None:
        last_error = "  ".join(notes)
    return (installed, last_error)


# Backwards-compatibility shim: existing callers (and the UI)
# may still import apply_qpe_bundle. The semantics are now a
# superset - the function handles any file type, not just .qpe.
apply_qpe_bundle = apply_update_bundle


def install_qpe_update(
        url: str, progress_cb=None,
        timeout: float = 60.0) -> tuple:
    """End-to-end: download the qpe bundle from `url`, extract
    directly into quopus_lib/, delete the temp download.
    Returns (count_installed, error_message_or_None).

    Caller should display a 'restart Quopus to activate' nudge
    after this - the running EXE has the OLD modules loaded in
    memory; the on-disk replacements take effect only on next
    import (i.e. next launch)."""
    with tempfile.TemporaryDirectory(prefix="quopus_qpe_") as td:
        zip_path = Path(td) / "qpe_update.zip"
        download_to_file(
            url, zip_path, progress_cb=progress_cb,
            timeout=timeout)
        return apply_qpe_bundle(zip_path)


# ---------------------------------------------------------------
# B: Full EXE replacement
# ---------------------------------------------------------------

def download_exe_update(
        url: str, progress_cb=None,
        timeout: float = 120.0) -> Path:
    """Download a new EXE alongside the running one as
    Quopus_NEW.exe. The actual swap is deferred to
    prepare_exe_swap() + Quopus restart. Returns the path to
    the downloaded file."""
    dst = _exe_dir() / "Quopus_NEW.exe"
    download_to_file(
        url, dst, progress_cb=progress_cb, timeout=timeout)
    return dst


def prepare_exe_swap(new_exe: Optional[Path] = None) -> Path:
    """Write a tiny .bat helper next to the EXE that, when run:

        1. Waits ~2s for Quopus.exe to release file handles
        2. Deletes Quopus.exe (the running, soon-to-be-old one)
        3. Renames Quopus_NEW.exe to Quopus.exe
        4. Starts the new Quopus.exe
        5. Deletes itself

    Returns the path to the helper. The caller is responsible
    for spawning it (with shell=True, detached) immediately
    before exiting Quopus - the OS keeps the .bat alive after
    we're gone so it can do its work without the parent.

    Only meaningful on Windows. On other OSes a similar shell
    script would work but isn't implemented here because the
    frozen build is currently Windows-only."""
    if new_exe is None:
        new_exe = _exe_dir() / "Quopus_NEW.exe"
    exe_dir = _exe_dir()
    current_exe = Path(sys.executable).resolve()
    helper = exe_dir / "quopus_swap.bat"
    # Use absolute paths everywhere so a wonky working directory
    # during ShellExecute doesn't confuse the script.
    script = (
        "@echo off\r\n"
        "rem Quopus EXE swap helper - auto-generated\r\n"
        ":wait_loop\r\n"
        f'del "{current_exe}" >nul 2>&1\r\n'
        f'if exist "{current_exe}" (\r\n'
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait_loop\r\n"
        ")\r\n"
        f'move /Y "{new_exe}" "{current_exe}" >nul\r\n'
        f'start "" "{current_exe}"\r\n'
        f'(goto) 2>nul & del "{helper}"\r\n'
    )
    helper.write_text(script, encoding="ascii")
    return helper


def trigger_exe_swap_and_exit():
    """Run the swap helper detached, then exit this Quopus
    process so the helper can replace the EXE.

    Only call this AFTER the user has confirmed they want to
    apply the EXE update. The function does not return - it
    terminates the process via sys.exit(0).
    """
    helper = prepare_exe_swap()
    if sys.platform == "win32":
        # DETACHED_PROCESS = 0x00000008
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        import subprocess
        subprocess.Popen(
            ["cmd.exe", "/c", str(helper)],
            creationflags=0x00000008 | 0x00000200,
            close_fds=True)
    else:
        # Non-Windows: just spawn a shell. Unlikely to be hit
        # since frozen is currently Windows-only, but no reason
        # to crash if someone ports the build later.
        import subprocess
        subprocess.Popen(["bash", str(helper)],
                           start_new_session=True)
    sys.exit(0)


# ---------------------------------------------------------------
# High-level check
# ---------------------------------------------------------------

def parse_version(tag_or_text: str):
    """Crude version-parse: extract a tuple of ints from any
    'vMAJOR.MINOR[.PATCH]' substring. Returns (0, 0, 0) when
    the input has no recognisable version. Good enough for
    'greater-than' comparison between Quopus releases."""
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", tag_or_text or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)),
             int(m.group(3) or 0))


def check_for_updates(current_version: str) -> dict:
    """One-call summary for the UI: 'is there an update, and
    what kinds are available?'.

    Returns a dict:
        available     bool
        latest_tag    "v1.1"
        current       "v1.0"
        body          release notes markdown
        exe_asset     {name, url, size} or None
        qpe_asset     {name, url, size} or None
        error         str or None
    """
    out = {
        "available": False,
        "latest_tag": "",
        "current": current_version,
        "body": "",
        "exe_asset": None,
        "qpe_asset": None,
        "error": None,
    }
    try:
        info = fetch_latest_release_info()
    except urllib.error.URLError as e:
        out["error"] = f"network error: {e}"
        return out
    except json.JSONDecodeError as e:
        out["error"] = f"bad JSON from GitHub: {e}"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["latest_tag"] = info["tag_name"]
    out["body"] = info["body"]
    out["exe_asset"] = find_exe_asset(info)
    out["qpe_asset"] = find_qpe_bundle_asset(info)
    latest = parse_version(info["tag_name"])
    current = parse_version(current_version)
    if latest > current:
        out["available"] = True
    return out
