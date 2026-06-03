# date_time: 2026-06-03 13:09
"""GitHub update checker for Quopus Commander.

Checks at startup whether the local working copy is behind the
remote main branch on GitHub, and offers to pull the latest changes
in place. The check runs in a background thread so the UI never
blocks, even if the network is slow or down.

Two backends, tried in this order:
  1. Local `git` binary (preferred). We run `git fetch origin main`
     followed by `git rev-list --count HEAD..origin/main`. This is
     authoritative and works even if the local clone uses a fork.
  2. GitHub REST API (fallback). Compares the local HEAD SHA (read
     from .git/HEAD without needing the git binary) against the
     branch's latest commit on api.github.com.

Either path produces an UpdateInfo with:
  - is_update_available
  - commits_behind (best-effort)
  - latest_commit_short, latest_commit_message
  - latest_commit_date
  - method ("git" / "api" / "fallback")

The "Update now" action runs `git pull --ff-only origin main` in
the same background thread and reports success or failure.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
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
    method: str = ""           # "git" / "api"
    repo_root: str = ""        # absolute path to the local clone


# ------------------------------------------------------------------
# Repo discovery
# ------------------------------------------------------------------
def _find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` to find the Quopus install root. Two
    markers we accept (first hit wins):

      - `.git/` directory  - proper git checkout
      - `quopus.py` next to `quopus_lib/` - ZIP-only install

    Either way we end up at the repo root."""
    p = (start or Path(__file__).resolve()).parent
    for _ in range(8):
        if (p / ".git").exists():
            return p
        if (p / "quopus.py").is_file() \
                and (p / "quopus_lib").is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent
    return None


def _git_binary_available() -> bool:
    """Probe for a working `git` executable. We don't trust PATH
    alone - some Windows installs (esp. when Git was uninstalled
    while a tab in PATH lingered) leave a stub that crashes on
    invocation."""
    try:
        r = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and "git version" in r.stdout
    except (FileNotFoundError, OSError,
            subprocess.TimeoutExpired):
        return False


# ------------------------------------------------------------------
# Local SHA - works without git binary by reading .git/HEAD
# ------------------------------------------------------------------
def _read_local_sha(repo_root: Path) -> Optional[str]:
    """Return the SHA the local working copy is at.

    Tries two sources, in order:
      1. .git/HEAD (works for proper git checkouts, no git binary
         needed - we read the file directly)
      2. config/installed_version.txt (written by pull_update on
         every successful update so even ZIP-only installs without
         a .git folder can be compared against the remote)"""
    head_file = repo_root / ".git" / "HEAD"
    if head_file.is_file():
        try:
            head_text = head_file.read_text(
                encoding="utf-8").strip()
        except Exception:
            head_text = ""
        if head_text.startswith("ref:"):
            ref_path = head_text[4:].strip()
            ref_file = repo_root / ".git" / ref_path
            if ref_file.is_file():
                try:
                    return ref_file.read_text(
                        encoding="utf-8").strip()
                except Exception:
                    pass
            # Packed refs fallback - common after `git gc`.
            packed = repo_root / ".git" / "packed-refs"
            if packed.is_file():
                try:
                    for line in packed.read_text(
                            encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") \
                                or line.startswith("^"):
                            continue
                        parts = line.split()
                        if len(parts) == 2 \
                                and parts[1] == ref_path:
                            return parts[0]
                except Exception:
                    pass
        elif re.fullmatch(r"[0-9a-fA-F]{40}", head_text):
            # Detached HEAD - HEAD itself is a SHA.
            return head_text
    # Fallback: previous Quopus update wrote the SHA here.
    installed = repo_root / "config" / "installed_version.txt"
    if installed.is_file():
        try:
            sha = installed.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                return sha
        except Exception:
            pass
    return None


# ------------------------------------------------------------------
# Backend 1 - local git
# ------------------------------------------------------------------
def _check_via_git(repo_root: Path) -> UpdateInfo:
    """Use the local git binary: fetch, then compare HEAD to
    origin/main. This is the most reliable check because it sees
    the user's actual branch and handles forks / detached HEADs."""
    info = UpdateInfo(repo_root=str(repo_root), method="git")
    try:
        # Refresh the local view of the remote without touching the
        # working tree. --quiet keeps the worker log small.
        fetch = subprocess.run(
            ["git", "fetch", "--quiet", "origin", DEFAULT_BRANCH],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=30)
        if fetch.returncode != 0:
            info.error = ("git fetch failed: %s"
                          % (fetch.stderr.strip() or "rc=%d"
                             % fetch.returncode))
            return info
        local = _run_git(
            repo_root, ["rev-parse", "HEAD"])
        remote = _run_git(
            repo_root, ["rev-parse", "origin/" + DEFAULT_BRANCH])
        if local is None or remote is None:
            info.error = "Couldn't read HEAD / origin SHA"
            return info
        info.local_sha = local
        info.latest_sha = remote
        if local == remote:
            info.ok = True
            return info
        # Count commits we're behind. If the local HEAD is ahead
        # (e.g. user has uncommitted local commits), commits_behind
        # will simply be 0 - which makes is_update_available False.
        behind = _run_git(
            repo_root,
            ["rev-list", "--count",
             "HEAD..origin/" + DEFAULT_BRANCH])
        try:
            info.commits_behind = int(behind or "0")
        except ValueError:
            info.commits_behind = 0
        # Latest commit details from the remote tip.
        subj = _run_git(
            repo_root,
            ["log", "-1", "--format=%s",
             "origin/" + DEFAULT_BRANCH])
        date = _run_git(
            repo_root,
            ["log", "-1", "--format=%ci",
             "origin/" + DEFAULT_BRANCH])
        info.latest_commit_short = remote[:7]
        info.latest_commit_message = subj or ""
        info.latest_commit_date = date or ""
        info.is_update_available = info.commits_behind > 0
        info.ok = True
        return info
    except subprocess.TimeoutExpired:
        info.error = "git operation timed out"
        return info
    except Exception as e:
        info.error = "git error: %s" % e
        return info


def _run_git(repo_root: Path, args: list) -> Optional[str]:
    """Run a short git command and return stdout stripped, or None
    on error. Used for one-shot queries where we don't need to
    surface the error individually."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


# ------------------------------------------------------------------
# Backend 2 - GitHub REST API
# ------------------------------------------------------------------
def _check_via_api(repo_root: Optional[Path]) -> UpdateInfo:
    """No git binary, or git fetch failed - fall back to the public
    REST API. Compares the local SHA (read from .git/HEAD) against
    the remote branch tip. Can't compute 'commits behind' reliably
    via the simple commits endpoint, so we report 0 or 1 instead."""
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
        info.ok = True
        if info.latest_sha and info.local_sha \
                and info.local_sha != info.latest_sha:
            info.is_update_available = True
            # Without compare API we don't know the count; treat
            # "1+" as the safe display value. The user-facing
            # dialog shows the commit subject anyway.
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
def _quick_remote_sha() -> Optional[str]:
    """A very lightweight 'what's the current remote SHA?' probe.
    Just hits the commits endpoint and reads the top-level `sha`
    field. Used to short-circuit the full check when the user has
    already seen this exact commit (config['update_last_seen_sha'])
    or already has it installed. Avoids the heavy `git fetch` step
    in the common 'nothing changed' case, which would otherwise
    download every new pack-object on the branch."""
    try:
        req = Request(API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT,
        })
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            sha = json.loads(
                resp.read().decode("utf-8")).get("sha", "")
        return sha or None
    except Exception:
        return None


def check_for_updates(
        known_remote_sha: str = "") -> UpdateInfo:
    """Blocking check - always call this from a worker thread.
    The UI helpers below already wrap it in QThread for you.

    Path selection:
      - If `known_remote_sha` matches the current remote tip we
        return immediately with is_update_available=False and
        method='cache' - no git fetch, no second API call. This
        is the fast path for repeated startups when nothing has
        changed upstream.
      - Proper git checkout + git binary -> `_check_via_git`
        (fastest accurate path, counts commits exactly).
      - Anything else -> `_check_via_api` (HTTP only, also works
        on ZIP-only installs).
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        return UpdateInfo(
            ok=False,
            error="Couldn't locate the Quopus install directory.")
    # Fast path: caller hands us the last remote SHA they saw;
    # if the branch tip hasn't moved, we can stop here without
    # doing a fetch or pulling commit metadata.
    if known_remote_sha:
        current = _quick_remote_sha()
        if current is not None and current == known_remote_sha:
            local = _read_local_sha(repo_root) or ""
            return UpdateInfo(
                ok=True,
                is_update_available=(local != "" and
                                     local != current),
                local_sha=local,
                latest_sha=current,
                latest_commit_short=current[:7],
                repo_root=str(repo_root),
                method="cache",
                # Mirror the count from previous runs as best-effort
                # - we genuinely don't know without a fetch.
                commits_behind=(1 if local != current
                                and local else 0),
            )
    has_git_dir = (repo_root / ".git").exists()
    if has_git_dir and _git_binary_available():
        info = _check_via_git(repo_root)
        if info.ok:
            return info
        api_info = _check_via_api(repo_root)
        if api_info.ok:
            return api_info
        return info
    return _check_via_api(repo_root)


def pull_update(repo_root: str, from_sha: str = "") -> tuple:
    """Apply the latest version from GitHub. Returns (success, msg).

    Two strategies are tried in order:

    1. **Incremental** (preferred when `from_sha` is known) - asks
       GitHub's compare API which files changed between `from_sha`
       and the branch tip, then downloads only those individual
       files via raw.githubusercontent.com. Typical small commit
       = a few KB instead of the ~86 MB full archive.

    2. **Full archive** - falls back to the branch ZIP when the
       incremental path fails (no `from_sha`, compare API
       unreachable, too many changed files, or the diff includes
       a rename we don't have enough info to reconstruct safely).

    Both paths protect config/, cache/, .git/, _master2publish/
    and friends, and back up every overwritten file into
    `<repo>/.update_backup/<timestamp>/`.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return (False, "Repo root doesn't exist: " + repo_root)
    # Incremental path - only worth attempting when we know
    # what we're updating from.
    if from_sha and re.fullmatch(r"[0-9a-fA-F]{40}", from_sha):
        ok, msg, fall_through = _pull_update_incremental(
            root, from_sha)
        if not fall_through:
            return (ok, msg)
        # else fall through to ZIP
    return _pull_update_zip(root)


# How many changed files we're willing to fetch one-by-one before
# admitting the full ZIP is the more efficient option. The compare
# API itself caps at 300 anyway, but even before that point the
# round-trip overhead of many tiny HTTPs starts to dominate.
INCREMENTAL_MAX_FILES = 60


def _pull_update_incremental(root: Path, from_sha: str) -> tuple:
    """Try the diff-and-fetch update path. Returns
    (success, message, fall_through_to_zip).

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
    head_sha = cmp_data.get("merge_base_commit", {}).get(
        "sha", "") or cmp_data.get("commits", [{}])[-1].get(
        "sha", "")
    # Actually, the head of the comparison is in the top-level
    # commits[-1] (last one in the range). But the SHA we want
    # to record is the branch tip - cmp_data also gives that as
    # the URL parameter; grab it from the response too.
    head_sha = ""
    try:
        # GitHub returns the merge_base, base_commit, and
        # commits[]. The "ahead by N" commit at index N-1 is the
        # tip we just compared to. If commits is empty, branch
        # tip == from_sha (no update).
        cmts = cmp_data.get("commits") or []
        if cmts:
            head_sha = cmts[-1].get("sha", "")
    except Exception:
        pass

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
    copied = removed = renamed = 0
    skipped_locked = []
    skipped_error = []

    for op, dest_rel, src_url, prev_rel in plan:
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
                        if dest.read_bytes() == data:
                            continue  # already current
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

    # 4) Record the new SHA so the next check can short-circuit.
    if head_sha:
        try:
            cfg_dir = root / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "installed_version.txt").write_text(
                head_sha + "\n", encoding="utf-8")
        except Exception:
            pass

    # 5) Build report.
    lines = [
        f"Incremental update applied "
        f"({cmp_data.get('ahead_by', '?')} commits, "
        f"{len(plan)} file change(s)).",
        f"  Wrote:   {copied}",
        f"  Removed: {removed}",
    ]
    if renamed:
        lines.append(f"  Renamed: {renamed}")
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
    if copied or removed or renamed:
        lines.append("")
        lines.append(f"Backup: {backup_dir}")
        lines.append("Restart Quopus to load the new code.")
    return (True, "\n".join(lines), False)


def _raw_url(path: str) -> str:
    """Compose the canonical raw.githubusercontent.com URL for a
    file at the tip of DEFAULT_BRANCH. Used as a fallback when the
    compare API didn't include `raw_url`."""
    return (f"https://raw.githubusercontent.com/"
            f"{REPO_OWNER}/{REPO_NAME}/{DEFAULT_BRANCH}/{path}")


def _pull_update_zip(root: Path) -> tuple:
    """Original full-archive update path. Used as the fallback for
    cases where the incremental path can't determine the right set
    of changes (no from_sha, huge diff, force-push, ...)."""
    from datetime import datetime
    import tempfile, zipfile, shutil

    archive_url = (
        f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
        f"/archive/refs/heads/{DEFAULT_BRANCH}.zip")

    # 1. Download
    try:
        req = Request(archive_url, headers={
            "User-Agent": HTTP_USER_AGENT})
        with urlopen(req, timeout=120) as r:
            data = r.read()
    except HTTPError as e:
        return (False,
                f"GitHub returned HTTP {e.code} while downloading "
                f"the update archive. Try again in a few minutes "
                f"or open the GitHub page manually.")
    except URLError as e:
        return (False, f"Network error: {e.reason}")
    except Exception as e:
        return (False, f"Download failed: {e}")
    if not data or len(data) < 1024:
        return (False, "Downloaded archive is empty or truncated.")

    # 2. Extract to a temp folder
    tmpdir = Path(tempfile.mkdtemp(prefix="quopus_update_"))
    try:
        zip_path = tmpdir / "main.zip"
        zip_path.write_bytes(data)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile as e:
            return (False, f"Downloaded archive is corrupt: {e}")

        # The ZIP wraps everything in a single top-level dir named
        # `<RepoName>-<branch>` (e.g. `Quopus-main/`). Find it -
        # GitHub's exact naming may shift with weird branch names.
        extracted_root = None
        for entry in tmpdir.iterdir():
            if entry.is_dir() and entry.name != "main.zip":
                extracted_root = entry
                break
        if extracted_root is None:
            return (False,
                    "ZIP layout unexpected (no top-level dir).")

        # 3. Copy files into place. Skip protected paths and the
        # backup tree itself (so re-running the update can't
        # cascade-overwrite previous backups).
        protect = {"config", "cache", ".git", "_master2publish",
                   ".update_backup", ".venv", "venv", "__pycache__"}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = root / ".update_backup" / stamp

        copied = 0
        unchanged = 0
        skipped_locked = []
        skipped_error = []

        for src in extracted_root.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(extracted_root)
            # First path component decides whether to protect.
            if rel.parts and rel.parts[0] in protect:
                continue
            # Also skip __pycache__ anywhere in the tree.
            if any(p == "__pycache__" for p in rel.parts):
                continue
            dst = root / rel
            try:
                # If destination exists and is byte-identical,
                # no need to touch it.
                if dst.is_file():
                    try:
                        if dst.read_bytes() == src.read_bytes():
                            unchanged += 1
                            continue
                    except Exception:
                        pass  # fall through to overwrite
                    # Back up the old version before overwriting.
                    bk = backup_dir / rel
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(dst, bk)
                    except Exception:
                        pass  # backup is best-effort
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except PermissionError:
                # On Windows, loaded DLLs (libsidwrapper.dll,
                # libopenmpt.dll, openmpt-*.dll, sidwrapper.dll)
                # can't be overwritten while Quopus runs.
                skipped_locked.append(str(rel))
            except Exception as e:
                skipped_error.append(f"{rel}: {e}")

        msg_lines = [
            f"Update applied.",
            f"  Updated: {copied} file(s)",
            f"  Unchanged: {unchanged} file(s)",
        ]
        if skipped_locked:
            msg_lines.append("")
            msg_lines.append(
                "Locked (close Quopus and re-run to update these):")
            for name in skipped_locked[:15]:
                msg_lines.append("  " + name)
            if len(skipped_locked) > 15:
                msg_lines.append(
                    f"  ... and {len(skipped_locked)-15} more")
        if skipped_error:
            msg_lines.append("")
            msg_lines.append("Errors:")
            for name in skipped_error[:10]:
                msg_lines.append("  " + name)
        if copied:
            msg_lines.append("")
            msg_lines.append(
                f"Backup of replaced files: {backup_dir}")
            msg_lines.append("")
            msg_lines.append(
                "Restart Quopus to load the new code.")
            # Note the new SHA so the next check knows what's
            # installed even without a .git directory.
            try:
                _write_installed_version(root)
            except Exception:
                pass

        success = copied > 0 or unchanged > 0
        return (success, "\n".join(msg_lines))
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _write_installed_version(repo_root: Path) -> None:
    """Persist the latest remote SHA into config/installed_version.txt
    so the next update check can compare even on installations
    without a .git directory (ZIP-only deployments)."""
    try:
        req = Request(API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT})
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            sha = json.loads(
                resp.read().decode("utf-8")).get("sha", "")
        if not sha:
            return
        cfg_dir = repo_root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "installed_version.txt").write_text(
            sha + "\n", encoding="utf-8")
    except Exception:
        pass


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
        Tries the incremental file-by-file path first when
        `from_sha` is known, falls back to the full ZIP archive
        otherwise (or when the incremental path can't safely apply
        the changes)."""
        done = pyqtSignal(bool, str)

        def __init__(self, repo_root: str, from_sha: str = ""):
            super().__init__()
            self._root = repo_root
            self._from_sha = from_sha or ""

        def run(self):
            ok, msg = pull_update(self._root,
                                  from_sha=self._from_sha)
            self.done.emit(ok, msg)

    def start_background_check(
            parent: Optional[QObject],
            callback: Callable,
            known_remote_sha: str = "") -> tuple:
        """Spin up a thread + worker, run the check, deliver the
        result to `callback`. If known_remote_sha is non-empty and
        still matches the remote tip, the worker returns almost
        immediately without a git fetch - the fast path for the
        common 'nothing changed since last visit' case.

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
            from_sha: str = "") -> tuple:
        """Run the update install in a worker thread. If from_sha
        is provided the worker can attempt the incremental diff
        path (way faster for small updates)."""
        thread = QThread(parent)
        worker = UpdatePullWorker(repo_root, from_sha=from_sha)
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
