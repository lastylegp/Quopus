# date_time: 2026-06-03 18:41
"""GitHub update checker for Quopus Commander.

Checks at startup whether the local installation is behind the
remote main branch on GitHub, and offers to apply the latest
changes in place. The check runs in a background thread so the
UI never blocks, even if the network is slow or down.

NO git involvement anywhere - the checker behaves identically
on a developer's git clone and a standard-user ZIP install. The
single source of truth for the locally installed SHA is
`config/installed_version.txt`, written by `pull_update` after
a successful in-app update. If that file is missing, the local
SHA is treated as unknown and an update is offered so the user
gets a chance to register their version.

All HTTP work goes through `urllib`; no external dependencies.
Both the SHA probe (`_quick_remote_info`) and the full check
(`_check_via_api`) hit the public `/commits/<branch>` endpoint
on api.github.com.

UpdateInfo carries:
  - is_update_available (True when local SHA missing or differs)
  - commits_behind (1 in the API path, no diff arithmetic)
  - latest_sha, latest_commit_short, latest_commit_message
  - latest_commit_date
  - method ("api" / "cache")

The "Update now" action either downloads the changed files via
the compare API (incremental, typical few KB) or the full
branch ZIP as a fallback.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


# ------------------------------------------------------------------
# Configuration - tweak these if the repo moves or the project
# uses a different default branch in future.
# ------------------------------------------------------------------
REPO_OWNER = "lastylegp"
REPO_NAME = "Quopus"
DEFAULT_BRANCH = "main"
API_URL = (f"https://api.github.com/repos/"
           f"{REPO_OWNER}/{REPO_NAME}/commits/{DEFAULT_BRANCH}")
HTTP_TIMEOUT = 8  # seconds; the user's main window must not hang
HTTP_USER_AGENT = "Quopus-Commander-Update-Check/1.0"


@dataclass
class UpdateInfo:
    """What the checker learned about the current vs. remote state.

    A successful check always sets `ok=True`. When ok is False, the
    `error` field describes what went wrong and `is_update_available`
    is False to be safe (we never claim "you're up-to-date" or
    "update available" if the check itself failed)."""
    ok: bool = False
    error: str = ""
    is_update_available: bool = False
    commits_behind: int = 0
    local_sha: str = ""
    latest_sha: str = ""
    latest_commit_short: str = ""
    latest_commit_message: str = ""
    latest_commit_date: str = ""
    method: str = ""           # "api" / "cache"
    repo_root: str = ""        # absolute path to the local clone
    # True when this check just wrote installed_version.txt for
    # the first time (no prior local SHA). The UI uses this to
    # stay silent - it's a routine 'register the version'
    # operation, not a real update event.
    first_run_init: bool = False


# ------------------------------------------------------------------
# Repo discovery + local SHA - NO git involvement
# ------------------------------------------------------------------
# The update mechanism deliberately never touches a local .git
# directory. Standard end-users won't have git installed, won't
# have a .git/ folder, and we don't want two different code
# paths to maintain (dev-with-git vs user-without-git). One
# source of truth: config/installed_version.txt, written by
# pull_update after a successful in-app update. If it's missing,
# we don't know what version is installed - that's a deliberate
# signal that lets the UI prompt the user to update.

def _find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` looking for the Quopus install root.
    The marker is `quopus.py` sitting next to `quopus_lib/`.
    Stops as soon as that pair is found."""
    p = (start or Path(__file__).resolve()).parent
    for _ in range(8):
        if (p / "quopus.py").is_file() \
                and (p / "quopus_lib").is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent
    return None


def _read_local_sha(repo_root: Path) -> Optional[str]:
    """Return the SHA recorded in config/installed_version.txt,
    or None if the file isn't there / unreadable / malformed.
    This is the ONLY source the checker accepts as 'what's
    installed locally'. No git, no fallbacks."""
    installed = repo_root / "config" / "installed_version.txt"
    if not installed.is_file():
        return None
    try:
        sha = installed.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return sha
    return None


# ------------------------------------------------------------------
# Backend 2 - GitHub REST API
# ------------------------------------------------------------------
def _check_via_api(repo_root: Optional[Path]) -> UpdateInfo:
    """Hit the public REST API for the latest commit on the
    branch. The local SHA comes from config/installed_version.txt
    (via _read_local_sha). When that file is absent, local SHA
    is left empty - the caller treats that as 'update available'
    so the user is prompted on first run."""
    info = UpdateInfo(
        repo_root=str(repo_root) if repo_root else "",
        method="api")
    try:
        req = Request(API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT,
        })
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        info.latest_sha = data.get("sha", "")
        info.latest_commit_short = info.latest_sha[:7]
        commit = data.get("commit", {}) or {}
        info.latest_commit_message = (
            commit.get("message", "") or "").splitlines()[0] \
            if commit.get("message") else ""
        info.latest_commit_date = (
            commit.get("author", {}) or {}).get("date", "") or ""
        if repo_root is not None:
            local = _read_local_sha(repo_root)
            if local:
                info.local_sha = local
            elif info.latest_sha:
                # First run: no installed_version.txt yet. Register
                # the current remote SHA as 'installed' so the next
                # check has a comparison point. NO download, NO
                # dialog - we silently mark the user as up-to-date
                # against this exact commit. If the local files
                # are actually older, the next time someone pushes
                # to main the difference will surface and the
                # update dialog will fire normally.
                try:
                    _write_installed_version(
                        repo_root, info.latest_sha)
                    info.local_sha = info.latest_sha
                    info.first_run_init = True
                except Exception:
                    # Read-only install? Leave local_sha empty.
                    # The check_for_updates wrapper will treat it
                    # as 'update available' and the user gets
                    # prompted - not pretty, but not lethal.
                    pass
        info.ok = True
        if info.latest_sha:
            # Update is 'available' when local is genuinely behind
            # remote. After first_run_init local == latest so this
            # stays False; first-time users are silently aligned.
            if info.local_sha and info.local_sha != info.latest_sha:
                info.is_update_available = True
                info.commits_behind = 1
            elif not info.local_sha:
                # The write failed (permissions?). Surface the
                # update prompt so the user at least knows
                # something needs doing.
                info.is_update_available = True
                info.commits_behind = 1
        return info
    except HTTPError as e:
        info.error = "GitHub API HTTP %d" % e.code
    except URLError as e:
        info.error = "Network error: %s" % e.reason
    except Exception as e:
        info.error = "API error: %s" % e
    return info


# ------------------------------------------------------------------
# Public entry points
# ------------------------------------------------------------------
def _quick_remote_info() -> Optional[dict]:
    """Lightweight 'what's the current remote tip?' probe. Hits
    the same /commits endpoint as the full check but doesn't do
    any local git work afterwards. The commit message and date
    are virtually free (same JSON response), so we pull them too
    and pass them through - that way the cache-hit path can still
    display a meaningful 'Up to date - commit XXX <subject>' line
    instead of just the SHA."""
    try:
        req = Request(API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT,
        })
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sha = data.get("sha", "") or ""
        if not sha:
            return None
        commit = data.get("commit", {}) or {}
        return {
            "sha": sha,
            "message": (commit.get("message", "")
                        or "").splitlines()[0]
                if commit.get("message") else "",
            "date": (commit.get("author", {}) or {})
                .get("date", "") or "",
        }
    except Exception:
        return None


def _quick_remote_sha() -> Optional[str]:
    """Compatibility shim - just the SHA from _quick_remote_info."""
    info = _quick_remote_info()
    return info["sha"] if info else None


def check_for_updates(
        known_remote_sha: str = "") -> UpdateInfo:
    """Blocking check - always call this from a worker thread.
    The UI helpers below already wrap it in QThread for you.

    Two paths:
      * Cache fast path: if `known_remote_sha` is the same as the
        current remote tip, we skip everything and return what we
        already know. Used for repeated startups where nothing has
        moved on GitHub.
      * Full API check: hit /commits, compare with
        config/installed_version.txt.

    NO git involvement anywhere - we always behave like a no-git
    end-user install. Single source of truth for the local SHA is
    `config/installed_version.txt` (written by pull_update after
    a successful in-app update). When that file is absent, local
    SHA is unknown and `is_update_available` is True so the user
    gets prompted on first run.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        return UpdateInfo(
            ok=False,
            error="Couldn't locate the Quopus install directory.")
    # Fast path: caller hands us the last remote SHA they saw;
    # if the branch tip hasn't moved, we return immediately.
    if known_remote_sha:
        rinfo = _quick_remote_info()
        if rinfo is not None \
                and rinfo["sha"] == known_remote_sha:
            current = rinfo["sha"]
            local = _read_local_sha(repo_root) or ""
            # Same is_update_available rule as the full path: when
            # local is empty (no installed_version.txt yet), we
            # have no proof of what's installed, so it counts as
            # 'update available' to surface the dialog.
            update_available = (not local) or (local != current)
            return UpdateInfo(
                ok=True,
                is_update_available=update_available,
                local_sha=local,
                latest_sha=current,
                latest_commit_short=current[:7],
                latest_commit_message=rinfo["message"],
                latest_commit_date=rinfo["date"],
                repo_root=str(repo_root),
                method="cache",
                commits_behind=(1 if update_available else 0),
            )
    return _check_via_api(repo_root)


def pull_update(repo_root: str, from_sha: str = "",
                to_sha: str = "",
                progress_cb: Optional[Callable] = None) -> tuple:
    """Apply the latest version from GitHub. Returns (success, msg).

    `progress_cb`, when provided, is invoked with a short status
    string at each phase of the operation ('Checking diff...',
    'Downloading file 3/12...', 'Writing version file...'). The
    worker hooks it up to the status bar so the user can see
    what's happening - a multi-second per-file download stretch
    without any feedback looks identical to a frozen UI.

    Two strategies are tried in order. The full-archive ZIP path
    is intentionally NOT used; we always download individual
    files so the user only pays for what actually changed.

    1. **Compare API** (preferred, used when `from_sha` is in the
       branch history) - asks GitHub's `compare` endpoint which
       files changed between `from_sha` and the branch tip, then
       downloads only those individual files via
       raw.githubusercontent.com.

    2. **Tree diff** (universal fallback) - lists every blob in
       the remote tree, computes the git blob SHA-1 of each
       corresponding local file, and downloads only the ones
       where the hash differs (or files that don't exist locally).
       Works without any `from_sha` reference, so it covers the
       case where the local installation was set up from a ZIP
       drop or someone manually edited installed_version.txt to
       a SHA that's not on the branch.

    Both paths protect config/, cache/, .git/, _master2publish/
    and friends, and back up every overwritten file into
    `<repo>/.update_backup/<timestamp>/`. The destination SHA
    (`to_sha`) is recorded in `config/installed_version.txt` so
    subsequent checks know what's installed.
    """
    def _report(stage: str) -> None:
        """Push a stage label to the caller's progress callback,
        and also echo it to stderr so it shows up in the console
        log - handy when debugging update failures."""
        try:
            import sys
            print("[update]", stage, file=sys.stderr, flush=True)
        except Exception:
            pass
        if progress_cb is not None:
            try:
                progress_cb(stage)
            except Exception:
                pass

    _report("Locating install root...")
    root = Path(repo_root)
    if not root.is_dir():
        return (False, "Repo root doesn't exist: " + repo_root)

    # Resolve target SHA if caller didn't pass one. We need it
    # to record installed_version.txt at the end.
    if not to_sha or not re.fullmatch(
            r"[0-9a-fA-F]{40}", to_sha):
        _report("Asking GitHub for the latest commit SHA...")
        rinfo = _quick_remote_info()
        if rinfo is not None:
            to_sha = rinfo["sha"]
    if not to_sha:
        return (False,
                "Couldn't determine the target commit SHA from "
                "GitHub. Check your network and try again.")

    # Compare-API path. Only works when from_sha is reachable
    # from the branch tip on GitHub. fall_through=True means
    # 'compare endpoint returned 404 or didn't cover the range'
    # - we move on to the tree-diff fallback instead of ZIP.
    if from_sha and re.fullmatch(r"[0-9a-fA-F]{40}", from_sha):
        _report("Asking GitHub which files changed since "
                + from_sha[:7] + "...")
        ok, msg, fall_through = _pull_update_incremental(
            root, from_sha, to_sha, _report)
        if not fall_through:
            return (ok, msg)
        _report("Compare API unusable for this from_sha - "
                "switching to per-file tree diff...")
    else:
        _report("No usable from_sha - using per-file tree diff...")

    # Universal per-file diff via the git/trees API.
    return _pull_update_tree_diff(root, to_sha, _report)


# How many changed files we're willing to fetch one-by-one before
# admitting the full ZIP is the more efficient option. Below this
# threshold we always go incremental; the per-file HTTP overhead
# is irrelevant for a handful of files and we avoid the 86 MB
# archive download.
INCREMENTAL_MAX_FILES = 20


def _pull_update_incremental(
        root: Path, from_sha: str, to_sha: str,
        progress_cb: Optional[Callable] = None) -> tuple:
    """Try the diff-and-fetch update path. Returns
    (success, message, fall_through_to_zip).

    `to_sha` is the SHA we're updating to - the caller supplied
    it from the preceding update check, so we write it directly
    into installed_version.txt instead of doing a second API
    round-trip. `progress_cb`, if given, gets phase labels for
    the caller's status bar.

    fall_through_to_zip=True means 'I couldn't do this safely,
    caller should retry with the full ZIP'. fall_through=False
    means we either applied the incremental update successfully
    or hit a definitive failure that retrying with the ZIP
    wouldn't fix (e.g. network down)."""
    from datetime import datetime
    import shutil

    # 1) Fetch the diff between from_sha and the branch tip.
    compare_url = (f"https://api.github.com/repos/"
                   f"{REPO_OWNER}/{REPO_NAME}/compare/"
                   f"{from_sha}...{DEFAULT_BRANCH}")
    try:
        req = Request(compare_url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT})
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            cmp_data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        # 404 = the local from_sha isn't reachable from main (very
        # old base, force-push). Definitely fall through to ZIP.
        return (False, "", True) if e.code == 404 else \
            (False,
             f"Compare API HTTP {e.code}: try again in a few "
             "minutes (rate limit?).",
             False)
    except URLError as e:
        return (False,
                f"Network error fetching diff: {e.reason}",
                False)
    except Exception as e:
        return (False, "", True)  # fall through

    files = cmp_data.get("files") or []
    if cmp_data.get("status") == "identical" \
            or cmp_data.get("ahead_by", 0) == 0:
        return (True,
                "Already up to date - nothing to do.",
                False)
    if not files:
        # Branch moved but no file changes? Probably a metadata-
        # only change; nothing to apply locally.
        return (True,
                "No file changes between your version and the "
                "branch tip.",
                False)

    # GitHub's compare endpoint truncates `files` at 300. If
    # there's that many or more, we're better off with the ZIP -
    # the request count alone would make it slower.
    if len(files) >= INCREMENTAL_MAX_FILES \
            or cmp_data.get("total_commits", 0) > 30:
        return (False, "", True)

    # 2) Plan the operations. Each file in the diff has a
    # 'status': added, modified, removed, renamed, copied,
    # changed. Renames have a 'previous_filename'.
    protect = {"config", "cache", ".git", "_master2publish",
               ".update_backup", ".venv", "venv", "__pycache__"}

    def _is_protected(rel: str) -> bool:
        parts = rel.replace("\\", "/").split("/")
        return bool(parts) and (parts[0] in protect
                                or "__pycache__" in parts)

    plan = []  # list of (op, dest_rel, src_url, prev_rel)
    for f in files:
        name = f.get("filename", "")
        status = f.get("status", "")
        prev = f.get("previous_filename", "")
        if not name or _is_protected(name):
            continue
        # raw_url points at the *new* blob at the branch tip.
        raw_url = f.get("raw_url") or _raw_url(name)
        if status in ("added", "modified", "changed", "copied"):
            plan.append(("write", name, raw_url, ""))
        elif status == "removed":
            plan.append(("delete", name, "", ""))
        elif status == "renamed":
            plan.append(("write", name, raw_url, prev))
        else:
            # Unknown status - safest to bail to ZIP.
            return (False, "", True)

    if not plan:
        return (True,
                "All changes are in protected paths; nothing "
                "to apply.",
                False)

    # 3) Execute the plan.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = root / ".update_backup" / stamp
    copied = removed = renamed = unchanged = 0
    skipped_locked = []
    skipped_error = []
    total = len(plan)

    if progress_cb is not None:
        try:
            progress_cb(f"Incremental update: {total} file(s) "
                        f"to apply")
        except Exception:
            pass

    for idx, (op, dest_rel, src_url, prev_rel) in enumerate(
            plan, start=1):
        if progress_cb is not None:
            try:
                pct = (idx * 100) // total
                progress_cb(
                    f"{pct}%/100  ({idx}/{total}) "
                    f"{op}: {dest_rel}")
            except Exception:
                pass
        dest = root / dest_rel
        try:
            if op == "write":
                # Download the file content.
                try:
                    req = Request(src_url, headers={
                        "User-Agent": HTTP_USER_AGENT})
                    with urlopen(req, timeout=60) as r:
                        data = r.read()
                except Exception as de:
                    skipped_error.append(
                        f"{dest_rel}: download failed ({de})")
                    continue
                # Backup if exists.
                if dest.is_file():
                    try:
                        local = dest.read_bytes()
                        # Direct byte match OR same content after
                        # CRLF normalisation (Windows local + LF
                        # remote = different bytes, same logical
                        # file - don't rewrite).
                        if local == data or (
                                local.replace(b"\r\n", b"\n")
                                == data.replace(b"\r\n", b"\n")):
                            unchanged += 1
                            continue
                    except Exception:
                        pass
                    bk = backup_dir / dest_rel
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(dest, bk)
                    except Exception:
                        pass
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    dest.write_bytes(data)
                    copied += 1
                except PermissionError:
                    skipped_locked.append(dest_rel)
                # Rename: also remove the old file (after backup).
                if prev_rel and not _is_protected(prev_rel):
                    old = root / prev_rel
                    if old.is_file():
                        bk = backup_dir / prev_rel
                        bk.parent.mkdir(
                            parents=True, exist_ok=True)
                        try:
                            shutil.move(str(old), str(bk))
                            renamed += 1
                        except Exception as e:
                            skipped_error.append(
                                f"{prev_rel}: couldn't remove "
                                f"old file ({e})")
            elif op == "delete":
                if not dest.is_file():
                    continue
                bk = backup_dir / dest_rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(dest), str(bk))
                    removed += 1
                except PermissionError:
                    skipped_locked.append(dest_rel)
                except Exception as e:
                    skipped_error.append(
                        f"{dest_rel}: delete failed ({e})")
        except Exception as e:
            skipped_error.append(f"{dest_rel}: {e}")

    # 4) Record the new SHA so the next check has a reference.
    version_write_error = ""
    try:
        _write_installed_version(root, to_sha)
    except Exception as e:
        version_write_error = (
            "WARNING: could not write installed_version.txt "
            "(%s). The update was applied but Quopus won't "
            "be able to detect the next update without this "
            "file." % e)

    # 5) Build report.
    headline = (f"Incremental update applied "
                f"({cmp_data.get('ahead_by', '?')} commits, "
                f"{len(plan)} file change(s)).")
    if not copied and not removed and not renamed \
            and unchanged == len(plan) and unchanged > 0:
        # GitHub reported a diff but the local files are already
        # byte-identical to remote (or CRLF-equivalent). Common
        # after an out-of-band sync. Nothing to write, just bump
        # installed_version.txt.
        headline = (
            f"Already up to date by file content "
            f"({unchanged} of {len(plan)} files matched).")
    lines = [
        headline,
        f"  Wrote:     {copied}",
        f"  Unchanged: {unchanged}",
        f"  Removed:   {removed}",
    ]
    if renamed:
        lines.append(f"  Renamed:   {renamed}")
    if skipped_locked:
        lines.append("")
        lines.append("Locked (close Quopus and re-run "
                     "to finish these):")
        for n in skipped_locked[:15]:
            lines.append("  " + n)
        if len(skipped_locked) > 15:
            lines.append(
                f"  ... and {len(skipped_locked)-15} more")
    if skipped_error:
        lines.append("")
        lines.append("Errors:")
        for n in skipped_error[:10]:
            lines.append("  " + n)
    if copied or removed or renamed or unchanged:
        lines.append("")
        if copied or removed or renamed:
            lines.append(f"Backup: {backup_dir}")
            lines.append("")
        if version_write_error:
            lines.append(version_write_error)
        else:
            lines.append(
                f"Registered installed version: {to_sha[:7]}  "
                f"(config/installed_version.txt)")
        lines.append("")
        if copied or removed or renamed:
            lines.append("Restart Quopus to load the new code.")
    return (True, "\n".join(lines), False)


def _raw_url(path: str) -> str:
    """Compose the canonical raw.githubusercontent.com URL for a
    file at the tip of DEFAULT_BRANCH. Used as a fallback when the
    compare API didn't include `raw_url`."""
    return (f"https://raw.githubusercontent.com/"
            f"{REPO_OWNER}/{REPO_NAME}/{DEFAULT_BRANCH}/{path}")


def _pull_update_tree_diff(
        root: Path, to_sha: str,
        progress_cb: Optional[Callable] = None) -> tuple:
    """Universal per-file diff: list the entire remote tree on
    the branch, compute the git-blob SHA-1 of each corresponding
    local file, and download only the files that don't match.

    No assumption about the local state - works even when the
    user has no installed_version.txt at all, or has a SHA that
    isn't on the branch (e.g. they manually edited the file, or
    their old install never used Quopus's update tool).

    Returns (success, message). Same protection set as the
    compare-API path (config/, cache/, .git/, _master2publish,
    backup tree, virtualenvs). Each overwritten file is backed
    up to `<repo>/.update_backup/<timestamp>/`.
    """
    from datetime import datetime
    import hashlib, shutil

    def _report(stage: str) -> None:
        # NOTE: only forward to progress_cb. The outer _report in
        # pull_update() does the stderr print already - duplicating
        # it here was producing two lines per stage in the console.
        if progress_cb is not None:
            try:
                progress_cb(stage)
            except Exception:
                pass

    # 1) Fetch the recursive tree listing
    tree_url = (
        f"https://api.github.com/repos/"
        f"{REPO_OWNER}/{REPO_NAME}/git/trees/"
        f"{DEFAULT_BRANCH}?recursive=1")
    _report("Fetching file list from GitHub...")
    try:
        req = Request(tree_url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT})
        with urlopen(req, timeout=30) as resp:
            tree_data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return (False, f"Tree API HTTP {e.code}.")
    except URLError as e:
        return (False, f"Network error: {e.reason}")
    except Exception as e:
        return (False, f"Could not fetch tree: {e}")

    entries = tree_data.get("tree") or []
    if not entries:
        return (False, "GitHub returned an empty file list.")

    # 2) Hash each local file and build a download plan
    protect = {"config", "cache", ".git", "_master2publish",
               ".update_backup", ".venv", "venv",
               "__pycache__"}

    def _is_protected(rel: str) -> bool:
        parts = rel.replace("\\", "/").split("/")
        return bool(parts) and (parts[0] in protect
                                or "__pycache__" in parts)

    def _git_blob_sha_variants(filepath: Path):
        """Return (raw_sha, eol_normalized_sha) for the file.
        GitHub stores files with LF line endings and computes
        its blob SHA over that LF form. On Windows installs the
        local file is often CRLF, which gives a different raw
        hash even though the content is logically identical -
        so we always compute both and the caller accepts a match
        on either side. For binary files (no CRLF present) both
        values are identical, no harm done."""
        try:
            content = filepath.read_bytes()
        except Exception:
            return ("", "")
        raw_header = ("blob %d\0" % len(content)).encode("ascii")
        raw_sha = hashlib.sha1(raw_header + content).hexdigest()
        if b"\r\n" in content:
            norm = content.replace(b"\r\n", b"\n")
            norm_header = ("blob %d\0" % len(norm)).encode("ascii")
            norm_sha = hashlib.sha1(
                norm_header + norm).hexdigest()
            return (raw_sha, norm_sha)
        return (raw_sha, raw_sha)

    _report("Comparing local files...")
    plan = []  # list of (path, remote_blob_sha)
    for entry in entries:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        if not path or _is_protected(path):
            continue
        remote_sha = entry.get("sha", "")
        local_file = root / path
        if not local_file.is_file():
            plan.append((path, remote_sha))
            continue
        raw_sha, norm_sha = _git_blob_sha_variants(local_file)
        # Match on either: handles Windows CRLF locals vs the LF
        # blob GitHub stores. If neither matches, the file is
        # genuinely different and gets queued for download.
        if remote_sha != raw_sha and remote_sha != norm_sha:
            plan.append((path, remote_sha))

    if not plan:
        # Every file already matches - just record the SHA.
        try:
            _write_installed_version(root, to_sha)
            return (True,
                    "All files already match the remote.\n"
                    f"Registered installed version: {to_sha[:7]}"
                    " (config/installed_version.txt)")
        except Exception as e:
            return (True,
                    "All files match, but couldn't update "
                    f"installed_version.txt: {e}")

    _report(f"Downloading {len(plan)} updated file(s)...")

    # 3) Download each file from raw.githubusercontent.com at
    #    the exact destination SHA - pinning to a commit avoids
    #    a race where the branch moves mid-update.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = root / ".update_backup" / stamp
    copied = 0
    skipped_locked = []
    skipped_error = []

    for idx, (path, _) in enumerate(plan, start=1):
        pct = (idx * 100) // len(plan)
        _report(f"Downloading {pct}%/100  ({idx}/{len(plan)}): "
                f"{path}")
        url = (f"https://raw.githubusercontent.com/"
               f"{REPO_OWNER}/{REPO_NAME}/{to_sha}/{path}")
        try:
            req = Request(url, headers={
                "User-Agent": HTTP_USER_AGENT})
            with urlopen(req, timeout=60) as r:
                data = r.read()
        except Exception as e:
            skipped_error.append(
                f"{path}: download failed ({e})")
            continue

        dest = root / path
        try:
            if dest.is_file():
                bk = backup_dir / path
                bk.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(dest, bk)
                except Exception:
                    pass  # backup best-effort
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            copied += 1
        except PermissionError:
            skipped_locked.append(path)
        except Exception as e:
            skipped_error.append(f"{path}: {e}")

    # 4) Record installed version
    _report("Writing installed_version.txt...")
    version_write_error = ""
    try:
        _write_installed_version(root, to_sha)
    except Exception as e:
        version_write_error = (
            f"WARNING: could not write installed_version.txt "
            f"({e}). Update applied but the next check won't "
            "know what's installed.")

    # 5) Build report
    lines = [
        f"Update applied (tree diff).",
        f"  Wrote:   {copied} of {len(plan)} file(s)",
    ]
    if skipped_locked:
        lines.append("")
        lines.append("Locked (close Quopus and re-run "
                     "to finish these):")
        for n in skipped_locked[:15]:
            lines.append("  " + n)
        if len(skipped_locked) > 15:
            lines.append(
                f"  ... and {len(skipped_locked)-15} more")
    if skipped_error:
        lines.append("")
        lines.append("Errors:")
        for n in skipped_error[:10]:
            lines.append("  " + n)
    if copied:
        lines.append("")
        lines.append(f"Backup: {backup_dir}")
        lines.append("")
        if version_write_error:
            lines.append(version_write_error)
        else:
            lines.append(
                f"Registered installed version: {to_sha[:7]}  "
                f"(config/installed_version.txt)")
        lines.append("")
        lines.append("Restart Quopus to load the new code.")
    return (copied > 0, "\n".join(lines))


def _write_installed_version(repo_root: Path, sha: str) -> None:
    """Persist `sha` into config/installed_version.txt so the
    next update check can compare against it. Raises on failure
    so the caller can surface a clear error message instead of
    silently dropping the write."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha or ""):
        raise ValueError("Refusing to write malformed SHA: %r"
                         % (sha,))
    cfg_dir = repo_root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / "installed_version.txt"
    target.write_text(sha + "\n", encoding="utf-8")


# ------------------------------------------------------------------
# Qt worker - wraps check_for_updates / pull_update in a QThread
# so the main window can call them without blocking.
# ------------------------------------------------------------------
try:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal

    class UpdateCheckWorker(QObject):
        """Runs check_for_updates() once, emits `done` with the
        result. Designed to be moved onto its own QThread."""
        done = pyqtSignal(object)  # UpdateInfo

        def __init__(self, known_remote_sha: str = ""):
            super().__init__()
            self._known = known_remote_sha or ""

        def run(self):
            try:
                info = check_for_updates(
                    known_remote_sha=self._known)
            except Exception as e:
                info = UpdateInfo(ok=False,
                                  error="Internal: %s" % e)
            self.done.emit(info)

    class UpdatePullWorker(QObject):
        """Downloads + installs an update on the given repo root.
        Tries the compare-API incremental path first when
        `from_sha` is known and reachable, falls back to the
        tree-diff per-file path otherwise. Never downloads the
        full branch ZIP. `to_sha` is the destination commit and
        is recorded into installed_version.txt at the end.

        Emits `progress(str)` from the worker thread with short
        stage labels so the UI can show 'Fetching file list...',
        'Downloading 3/12: ...', etc. instead of looking frozen
        during the per-file download stretch."""
        done = pyqtSignal(bool, str)
        progress = pyqtSignal(str)

        def __init__(self, repo_root: str,
                     from_sha: str = "", to_sha: str = ""):
            super().__init__()
            self._root = repo_root
            self._from_sha = from_sha or ""
            self._to_sha = to_sha or ""

        def run(self):
            ok, msg = pull_update(
                self._root,
                from_sha=self._from_sha,
                to_sha=self._to_sha,
                progress_cb=lambda s: self.progress.emit(s))
            self.done.emit(ok, msg)

    def start_background_check(
            parent: Optional[QObject],
            callback: Callable,
            known_remote_sha: str = "") -> tuple:
        """Spin up a thread + worker, run the check, deliver the
        result to `callback`. If known_remote_sha is non-empty and
        still matches the remote tip, the worker returns almost
        immediately - the fast path for the common 'nothing
        changed since last visit' case.

        Returns (thread, worker) which the caller must keep
        references to until the thread quits - otherwise they get
        garbage collected mid-flight and Qt complains about
        destroyed QThread."""
        thread = QThread(parent)
        worker = UpdateCheckWorker(
            known_remote_sha=known_remote_sha)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(callback)
        # Self-cleanup so callers don't have to manage the lifecycle.
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return thread, worker

    def start_background_pull(
            parent: Optional[QObject],
            repo_root: str,
            callback: Callable,
            from_sha: str = "",
            to_sha: str = "") -> tuple:
        """Run the update install in a worker thread.

          from_sha - the SHA we're updating from. When known, the
              worker tries the incremental diff path (a few KB)
              before falling back to the full ZIP archive.
          to_sha - the SHA we're updating to. Recorded into
              installed_version.txt at the end so the next check
              has a reference. Caller normally has this from the
              preceding update check; pass it through."""
        thread = QThread(parent)
        worker = UpdatePullWorker(
            repo_root, from_sha=from_sha, to_sha=to_sha)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(callback)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return thread, worker

except ImportError:
    # PyQt6 not available (e.g. CLI usage / tests) - the synchronous
    # entry points above still work.
    pass


__all__ = [
    "REPO_OWNER", "REPO_NAME", "DEFAULT_BRANCH",
    "UpdateInfo", "check_for_updates", "pull_update",
    "UpdateCheckWorker", "UpdatePullWorker",
    "start_background_check", "start_background_pull",
]