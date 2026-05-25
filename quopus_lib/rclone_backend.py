"""Rclone integration for Quopus Commander.

Rclone (https://rclone.org) is a command-line tool that exposes
70+ cloud storage backends (Dropbox, Google Drive, OneDrive,
Mega, Box, pCloud, S3, B2, Azure, WebDAV, SFTP, ...) through a
unified interface. By shelling out to rclone we get all those
providers "for free" without having to implement each one's
OAuth / API quirks ourselves.

Architecture:

  RcloneManager           - high-level wrapper around the rclone
                            CLI. Methods like list_remotes(),
                            list_dir(remote, path), copy_to(...),
                            copy_from(...), delete(...).

  RcloneRemote            - one configured remote (name + type +
                            optional description). The user has
                            already set this up via 'rclone
                            config' on their machine.

  Worker QThreads         - long-running operations (large file
                            copies, sync, large dir listings)
                            run in background threads so the UI
                            stays responsive. They emit progress
                            and completion signals.

We deliberately don't try to reimplement rclone's config file
handling - the user sets up remotes through `rclone config` once
(this is a one-time interactive setup with OAuth browser flows
etc.) and we just query whatever remotes are present.

CLI commands we shell out to:

    rclone listremotes --long              List configured remotes
    rclone lsjson <remote>:<path>          List dir as JSON
    rclone copy <src> <dst> [--progress]   Copy file or dir
    rclone copyto <src> <dst>              Copy with rename
    rclone delete <remote>:<path>          Delete file
    rclone mkdir <remote>:<path>           Create directory
    rclone purge <remote>:<path>           Recursively delete dir
    rclone size <remote>:<path>            Get total size
    rclone about <remote>:                 Quota info
    rclone version                         Probe binary

We don't use the rclone "rc" remote-control daemon mode - the
one-shot CLI commands are simpler to reason about and don't
require a long-running background process.

Configuration:
    Path to rclone binary is in quopus_lib config under
    'rclone_path' (default: just 'rclone', found on $PATH).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------


@dataclass
class RcloneRemote:
    """One configured remote in the user's rclone config.

    `name` is what the user types as `<name>:path/in/cloud`.
    `type_` is the backend type (e.g. 'drive', 'dropbox',
    'onedrive', 's3') - used to pick an icon / friendly label.
    `description` is optional human text, only set on some
    backends.
    """
    name: str
    type_: str = ""
    description: str = ""

    @property
    def friendly_type(self) -> str:
        """Map rclone backend type to a friendlier display name.
        E.g. 'drive' -> 'Google Drive'. Falls back to the raw
        type name for backends we don't have a label for."""
        return _BACKEND_LABELS.get(self.type_, self.type_)


@dataclass
class RcloneEntry:
    """One entry returned by `rclone lsjson`.

    Mirrors the fields rclone emits per item. We only keep the
    ones we actually use - the full lsjson output has more
    fields like MimeType, Hashes, etc. that we ignore for now.
    """
    path: str = ""          # relative to the dir we listed
    name: str = ""          # basename
    size: int = 0           # bytes; -1 for unknown/dir
    is_dir: bool = False
    mod_time: str = ""      # ISO8601 string


# Maps rclone backend type strings to friendlier display labels.
# The full list (70+) is too much to enumerate; we cover the
# popular consumer + sysadmin backends and let the rest fall
# through to their raw type names. Sourced from rclone's docs
# (https://rclone.org/overview/) and the backend/ directory in
# the rclone source.
_BACKEND_LABELS = {
    "drive":         "Google Drive",
    "onedrive":      "Microsoft OneDrive",
    "dropbox":       "Dropbox",
    "box":           "Box",
    "pcloud":        "pCloud",
    "mega":          "Mega",
    "yandex":        "Yandex Disk",
    "jottacloud":    "Jottacloud",
    "googlecloudstorage": "Google Cloud Storage",
    "googlephotos":  "Google Photos",
    "s3":            "Amazon S3 / Compatible",
    "b2":            "Backblaze B2",
    "azureblob":     "Azure Blob",
    "azurefiles":    "Azure Files",
    "swift":         "OpenStack Swift",
    "ftp":           "FTP",
    "sftp":          "SFTP / SSH",
    "webdav":        "WebDAV",
    "http":          "HTTP (read-only)",
    "smb":           "SMB / CIFS",
    "storj":         "Storj DCS",
    "filefabric":    "Storage Made Easy",
    "hidrive":       "HiDrive",
    "koofr":         "Koofr",
    "linkbox":       "Linkbox",
    "mailru":        "Mail.ru Cloud",
    "opendrive":     "OpenDrive",
    "premiumizeme":  "Premiumize.me",
    "putio":         "Put.io",
    "seafile":       "Seafile",
    "sharefile":     "Citrix ShareFile",
    "uloz":          "Ulozto",
    "alias":         "(alias)",
    "crypt":         "(encrypted)",
    "union":         "(union)",
    "chunker":       "(chunker)",
    "compress":      "(compress)",
    "cache":         "(cache)",
    "local":         "Local filesystem",
    "memory":        "In-memory",
}


# ---------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------


class RcloneNotFoundError(Exception):
    """Raised when the rclone binary can't be located on PATH or
    at the configured rclone_path. The UI catches this and shows
    a friendly 'install rclone first' message with a link to the
    rclone download page."""
    pass


class RcloneError(Exception):
    """Raised when rclone returns a non-zero exit code or emits
    something we can't parse. Carries stderr for the UI to show."""

    def __init__(self, message: str, stderr: str = "",
                 returncode: int = 0):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------
# Manager
# ---------------------------------------------------------------


class RcloneManager:
    """Thin wrapper around the rclone CLI. All methods either
    return parsed results or raise RcloneError / subclass.

    Construct with the path to the rclone binary, or 'rclone'
    if it's on PATH. We probe via `rclone version` on first
    use - subsequent calls trust the cached binary path.
    """

    def __init__(self, rclone_path: str = "rclone",
                 config_path: Optional[str] = None,
                 timeout_short: float = 30.0,
                 timeout_long: float = 600.0,
                 bwlimit: str = "",
                 transfers: int = 0,
                 checkers: int = 0,
                 extra_args: str = "",
                 config_password: str = ""):
        self.rclone_path = rclone_path
        # Optional explicit config file path. None = let rclone
        # pick its default (~/.config/rclone/rclone.conf etc).
        self.config_path = config_path
        # Short timeout for metadata commands (listremotes,
        # lsjson, version). Long timeout for transfers which
        # might run for hours - the caller should consider those
        # interruptible and use a worker thread.
        self.timeout_short = timeout_short
        self.timeout_long = timeout_long
        # Transfer tuning - applied to every rclone command via
        # _build_cmd. Zero / empty means "use rclone default".
        self.bwlimit = bwlimit
        self.transfers = transfers
        self.checkers = checkers
        self.extra_args = extra_args
        # Password for encrypted rclone.conf. We pass it via the
        # RCLONE_CONFIG_PASS env var on each subprocess call so
        # it doesn't show up in `ps`/process listings. Stored
        # in memory only; never persisted to the Quopus config
        # file because that would defeat the encryption.
        self.config_password = config_password
        self._version_cache: Optional[str] = None

    # ----- Probing ----------------------------------------------

    def config_is_encrypted(self) -> bool:
        """Heuristic: peek at the rclone config file and see if
        it's encrypted. Rclone marks encrypted configs with a
        magic 'RCLONE_ENCRYPT_V0:' prefix on the first line; the
        rest is base64 ciphertext.

        Returns False on any error - the worst case is we don't
        prompt for a password and rclone itself fails with a
        readable error message.
        """
        try:
            path = self._resolve_config_path()
            if path is None or not os.path.isfile(path):
                return False
            with open(path, "rb") as f:
                head = f.read(64)
            return head.startswith(b"RCLONE_ENCRYPT_V0:")
        except Exception:
            return False

    def _resolve_config_path(self) -> Optional[str]:
        """Figure out where rclone would actually look for its
        config. If we set config_path explicitly, that's the
        answer. Otherwise we ask rclone itself via `config file`
        which prints the path it would use.
        """
        if self.config_path:
            return str(self.config_path)
        try:
            out = self._run(["config", "file"],
                            timeout=self.timeout_short,
                            allow_no_config=True)
        except Exception:
            return None
        # Output format: "Configuration file is stored at:\n  /path/to/rclone.conf"
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("Configuration"):
                return line
        return None

    def is_available(self) -> bool:
        """Returns True if rclone binary can be invoked. Doesn't
        raise - the UI uses this for greying out actions when
        rclone isn't installed."""
        try:
            self.version()
            return True
        except (RcloneNotFoundError, RcloneError, Exception):
            return False

    def version(self) -> str:
        """Returns the rclone version string, e.g. 'rclone v1.66.0'.
        Cached after first successful call so we don't fork a
        subprocess every time the UI rechecks availability.
        """
        if self._version_cache:
            return self._version_cache
        try:
            out = self._run(["version"], timeout=self.timeout_short)
        except FileNotFoundError:
            raise RcloneNotFoundError(
                f"rclone binary not found at: {self.rclone_path}")
        # First line is "rclone v1.xx.x"
        first = (out.splitlines() or [""])[0].strip()
        self._version_cache = first
        return first

    # ----- Remote management ------------------------------------

    def list_remotes(self) -> list[RcloneRemote]:
        """Returns the user's configured remotes. Empty list if
        no remotes are configured yet (typical for first-time
        users) - the UI shows a hint to run `rclone config` in
        a terminal.

        We use --long to get the type alongside the name:
            mydrive:        drive
            backup:         b2
            uplink:         sftp
        """
        try:
            out = self._run(
                ["listremotes", "--long"],
                timeout=self.timeout_short)
        except RcloneError as e:
            # Empty config file is fine, treat as no remotes.
            if "config file" in e.stderr.lower():
                return []
            raise
        remotes = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # Format is 'name:<spaces>type[<spaces>desc]'
            # The name part ends at the colon. Type is the next
            # whitespace-separated token. Description is rare
            # and only on some backends.
            if ":" not in line:
                continue
            name_part, rest = line.split(":", 1)
            name = name_part.strip()
            tokens = rest.split()
            if not tokens:
                remotes.append(RcloneRemote(name=name))
                continue
            type_ = tokens[0]
            desc = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            remotes.append(RcloneRemote(
                name=name, type_=type_, description=desc))
        return remotes

    # ----- Directory listing ------------------------------------

    def list_dir(self, remote: str, path: str = "",
                 max_items: int = 1000) -> list[RcloneEntry]:
        """List one directory level on the given remote.

        `remote` is the bare name without the colon ('mydrive',
        not 'mydrive:'). `path` is relative to the remote root.
        `max_items` caps the result to avoid memory blowups on
        directories with 100k+ items - the user can drill into
        a more specific path if they hit the cap.

        Uses `lsjson` (not `lsf`) so we get structured output
        and don't have to parse human-readable date columns.
        """
        target = self._make_target(remote, path)
        out = self._run(
            ["lsjson", "--no-modtime", target],
            timeout=self.timeout_short)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RcloneError(
                f"Couldn't parse rclone lsjson output: {e}",
                stderr=out[:500])
        entries = []
        for d in data[:max_items]:
            entries.append(RcloneEntry(
                path=str(d.get("Path", "")),
                name=str(d.get("Name", "")),
                size=int(d.get("Size", 0) or 0),
                is_dir=bool(d.get("IsDir", False)),
                mod_time=str(d.get("ModTime", "")),
            ))
        # Directories first, then alphabetical name. Matches the
        # convention in the local Quopus lister.
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    # ----- Quota / size -----------------------------------------

    def quota(self, remote: str) -> dict:
        """Get free / used quota for a remote. Returns a dict
        with the rclone-about fields: used, free, quota, trashed.
        All in bytes. Some backends don't expose this and will
        raise RcloneError - the UI just hides the quota row in
        that case rather than complaining."""
        out = self._run(
            ["about", "--json", f"{remote}:"],
            timeout=self.timeout_short)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}

    # ----- Transfers --------------------------------------------

    def copy_to_remote(self, local_path: str, remote: str,
                       remote_path: str,
                       progress_callback=None,
                       process_callback=None) -> int:
        """Copy a local file or directory to the remote. Streams
        rclone's stdout line-by-line so the progress_callback
        can update the UI - the callback receives raw lines, the
        UI can parse them or just display them.

        Optional `process_callback(proc)` is invoked once with
        the subprocess.Popen handle so a worker thread can keep
        a reference for terminate() on user-cancel.

        Returns rclone's exit code. The caller is responsible for
        checking it - we don't raise on non-zero because the
        worker thread wants to surface the error in the UI rather
        than crashing out of the QThread.

        Uses `rclone copyto` when the destination has a trailing
        filename (so user can rename during copy), `rclone copy`
        otherwise (preserves source basename in target dir).
        """
        target = self._make_target(remote, remote_path)
        if remote_path.endswith("/") or remote_path == "":
            cmd = ["copy", "--progress", local_path, target]
        else:
            cmd = ["copyto", "--progress", local_path, target]
        return self._run_streaming(
            cmd, progress_callback=progress_callback,
            process_callback=process_callback)

    def copy_from_remote(self, remote: str, remote_path: str,
                         local_path: str,
                         progress_callback=None,
                         process_callback=None) -> int:
        """Mirror of copy_to_remote. local_path is the host-side
        destination; will be the directory we copy into, or the
        exact file path to rename to."""
        target = self._make_target(remote, remote_path)
        if local_path.endswith(os.sep) or local_path == "":
            cmd = ["copy", "--progress", target, local_path]
        else:
            cmd = ["copyto", "--progress", target, local_path]
        return self._run_streaming(
            cmd, progress_callback=progress_callback,
            process_callback=process_callback)

    def sync_to_remote(self, local_path: str, remote: str,
                       remote_path: str,
                       progress_callback=None,
                       process_callback=None) -> int:
        """Sync local -> remote. Makes the remote location IDENTICAL
        to the local one: files only on the remote are DELETED.

        This is destructive - the UI must confirm with the user
        before calling. We don't add safety guards here because
        the user might legitimately want exactly this (clean
        backup with stale files removed).

        Implementation uses `rclone sync` rather than `copy`.
        sync's source is always a directory; for a single-file
        sync the user should use copy_to_remote instead.
        """
        target = self._make_target(remote, remote_path)
        cmd = ["sync", "--progress", local_path, target]
        return self._run_streaming(
            cmd, progress_callback=progress_callback,
            process_callback=process_callback)

    def sync_from_remote(self, remote: str, remote_path: str,
                         local_path: str,
                         progress_callback=None,
                         process_callback=None) -> int:
        """Sync remote -> local. DELETES local files that aren't
        in the remote. Same warning as sync_to_remote."""
        target = self._make_target(remote, remote_path)
        cmd = ["sync", "--progress", target, local_path]
        return self._run_streaming(
            cmd, progress_callback=progress_callback,
            process_callback=process_callback)

    def delete(self, remote: str, remote_path: str,
               recursive: bool = False) -> int:
        """Delete a single file (recursive=False) or recursively
        delete a directory (recursive=True via `rclone purge`).
        rclone's `delete` only acts on files, `purge` nukes a
        whole directory - we picked names that match the user's
        intent rather than rclone's.
        """
        target = self._make_target(remote, remote_path)
        cmd = ["purge", target] if recursive else ["deletefile",
                                                    target]
        self._run(cmd, timeout=self.timeout_long)
        return 0

    def mkdir(self, remote: str, remote_path: str) -> int:
        """Create a directory on the remote. No-op on backends
        that don't support empty directories (e.g. S3 prefixes
        are implicit) - rclone handles this transparently."""
        target = self._make_target(remote, remote_path)
        self._run(["mkdir", target], timeout=self.timeout_short)
        return 0

    def rename(self, remote: str, old_path: str,
               new_path: str) -> int:
        """Rename a file within the same remote. Implemented as
        `rclone moveto src dst` which does a server-side move
        on backends that support it (most do)."""
        src = self._make_target(remote, old_path)
        dst = self._make_target(remote, new_path)
        self._run(["moveto", src, dst], timeout=self.timeout_long)
        return 0

    # ----- Helpers ----------------------------------------------

    def _make_target(self, remote: str, path: str) -> str:
        """Build the `<remote>:<path>` string rclone expects.
        Normalises away the colon if the user accidentally
        included it in the remote name."""
        remote = remote.rstrip(":")
        path = path.lstrip("/")
        return f"{remote}:{path}" if path else f"{remote}:"

    def _build_cmd(self, args: list[str]) -> list[str]:
        """Compose the full rclone command-line argv. Adds the
        --config flag if the user has set a custom config path,
        plus any tuning flags (bandwidth limit, transfers,
        checkers, extra args) configured in Quopus settings.

        Tuning flags only matter for transfer commands (copy,
        sync) - rclone silently ignores them on metadata
        commands (listremotes, lsjson) so it's safe to add them
        unconditionally.
        """
        cmd = [self.rclone_path]
        if self.config_path:
            cmd.extend(["--config", str(self.config_path)])
        if self.bwlimit:
            cmd.extend(["--bwlimit", str(self.bwlimit)])
        if self.transfers and int(self.transfers) > 0:
            cmd.extend(["--transfers", str(int(self.transfers))])
        if self.checkers and int(self.checkers) > 0:
            cmd.extend(["--checkers", str(int(self.checkers))])
        if self.extra_args:
            # Split on whitespace OR newlines so the user can
            # write one flag per line in the textbox. We use
            # shlex so quoted values survive intact.
            try:
                cmd.extend(shlex.split(self.extra_args))
            except ValueError:
                # Unbalanced quotes - fall back to naive split
                cmd.extend(self.extra_args.split())
        cmd.extend(args)
        return cmd

    def _env_with_password(self) -> dict:
        """Return a copy of os.environ with RCLONE_CONFIG_PASS
        set if we have a password for an encrypted config. We
        pass it via env rather than CLI flag so the password
        doesn't appear in process listings or shell history."""
        env = os.environ.copy()
        if self.config_password:
            env["RCLONE_CONFIG_PASS"] = self.config_password
        return env

    def _run(self, args: list[str], timeout: float = 30.0,
             allow_no_config: bool = False) -> str:
        """Synchronous helper: run rclone, capture stdout/stderr,
        raise RcloneError on non-zero exit. Returns stdout as a
        string.

        Don't use this for long transfers - the timeout will
        kill them. Use _run_streaming() instead, which has no
        timeout and surfaces progress.

        `allow_no_config=True` is used for the `config file`
        probe that runs before we know whether there's a config
        at all - it suppresses the usual non-zero-exit raise so
        the caller can interpret the result themselves.
        """
        cmd = self._build_cmd(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout,
                env=self._env_with_password(),
                # Hide the console window flash on Windows when
                # we're called from a GUI process.
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
        except FileNotFoundError:
            raise RcloneNotFoundError(
                f"rclone binary not found: {self.rclone_path}")
        except subprocess.TimeoutExpired:
            raise RcloneError(
                f"rclone command timed out after {timeout}s: "
                f"{' '.join(args)}")
        if result.returncode != 0 and not allow_no_config:
            # Detect encrypted-config password errors and tag
            # them so the UI can re-prompt instead of just
            # showing the raw stderr.
            stderr = result.stderr or ""
            if ("RCLONE_CONFIG_PASS" in stderr
                    or "could not decrypt" in stderr.lower()
                    or "wrong password" in stderr.lower()):
                raise RcloneError(
                    "Encrypted rclone config: password missing "
                    "or wrong. Set it in Settings.",
                    stderr=stderr,
                    returncode=result.returncode)
            raise RcloneError(
                f"rclone {args[0] if args else '?'} failed "
                f"(exit {result.returncode}): "
                f"{stderr.strip()[:200]}",
                stderr=stderr,
                returncode=result.returncode)
        return result.stdout

    def _run_streaming(self, args: list[str],
                       progress_callback=None,
                       process_callback=None) -> int:
        """Run rclone and stream stdout line-by-line to the
        callback. Used for `copy --progress` where rclone emits
        periodic transfer stats we want to surface in the UI.

        `process_callback`, if given, is called once with the
        Popen object as soon as the subprocess has started. The
        worker thread uses this to keep a handle for terminate()
        when the user clicks Cancel.

        Returns the exit code. Doesn't raise on non-zero - the
        worker handles error display itself.
        """
        cmd = self._build_cmd(args)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env=self._env_with_password(),
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
        except FileNotFoundError:
            raise RcloneNotFoundError(
                f"rclone binary not found: {self.rclone_path}")
        if process_callback is not None:
            try:
                process_callback(proc)
            except Exception:
                pass
        try:
            for line in proc.stdout:
                if progress_callback is not None:
                    try:
                        progress_callback(line.rstrip())
                    except Exception:
                        # Don't let a callback exception kill
                        # the transfer
                        pass
        finally:
            proc.wait()
        return proc.returncode


# ---------------------------------------------------------------
# Module convenience: a singleton manager built from config
# ---------------------------------------------------------------


_MANAGER_CACHE: Optional[RcloneManager] = None


# ---------------------------------------------------------------
# Saved-paths (associated lists)
# ---------------------------------------------------------------
#
# For each remote the user can save a list of local paths that
# they sync to that remote frequently. Stored as JSON in the
# Quopus config dir so it survives across sessions and gets
# included in backups.
#
# Format:
#   {
#     "gdrive-private": [
#       {"local": "C:\\backup\\photos", "remote": "/photos"},
#       {"local": "C:\\backup\\docs", "remote": "/docs"}
#     ],
#     "dropbox-work": [...]
#   }
#
# We deliberately don't share the same JSON file with the rest
# of the Quopus config - this file is rclone-specific, the
# format may evolve, and keeping it separate keeps the main
# config file from growing huge for users with hundreds of
# saved paths.

_SAVED_PATHS_FILENAME = "rclone_saved_paths.json"


def saved_paths_file() -> Path:
    """Locate the saved-paths JSON. Lives in the Quopus config
    directory alongside the other persistent state. Created on
    first write; this function just returns the path, it does
    not touch disk."""
    try:
        from .config import CONFIG_DIR
        return Path(CONFIG_DIR) / _SAVED_PATHS_FILENAME
    except Exception:
        # Fallback: home dir under .quopus/. Lets the tests run
        # without a full Quopus install.
        return (Path.home() / ".quopus"
                / _SAVED_PATHS_FILENAME)


def load_saved_paths() -> dict:
    """Read the saved-paths JSON. Returns an empty dict if the
    file doesn't exist or is corrupt - the UI then shows an
    empty saved-paths list and the user can populate it."""
    p = saved_paths_file()
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_saved_paths(data: dict) -> None:
    """Write the saved-paths JSON. Creates the parent dir if
    needed. Atomic write (temp file + rename) so a crash mid-
    write doesn't corrupt the file."""
    p = saved_paths_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def add_saved_path(remote: str, local: str,
                   remote_path: str = "") -> None:
    """Add one local<->remote mapping for `remote`. If an entry
    with the same local path already exists, it's updated
    rather than duplicated."""
    data = load_saved_paths()
    entries = data.get(remote, [])
    # De-dupe on local path - if the user adds the same folder
    # twice we update the remote target instead of growing the
    # list with duplicates.
    found = False
    for e in entries:
        if e.get("local") == local:
            e["remote"] = remote_path
            found = True
            break
    if not found:
        entries.append({"local": local, "remote": remote_path})
    data[remote] = entries
    save_saved_paths(data)


def remove_saved_path(remote: str, local: str) -> None:
    """Remove an entry by local path."""
    data = load_saved_paths()
    entries = data.get(remote, [])
    entries = [e for e in entries if e.get("local") != local]
    if entries:
        data[remote] = entries
    else:
        data.pop(remote, None)
    save_saved_paths(data)


def get_saved_paths(remote: str) -> list:
    """Return the list of saved entries for one remote.
    Each entry is {'local': str, 'remote': str}."""
    data = load_saved_paths()
    return list(data.get(remote, []))



def get_manager(config: Optional[dict] = None) -> RcloneManager:
    """Returns a cached RcloneManager built from the Quopus
    config dict. Rebuilds if the rclone_path setting has changed.

    The caller passes the main_window's config dict; we don't
    import quopus_lib.config directly to avoid a circular dep.

    Tuning knobs read from the config:
      rclone_path           - explicit binary location
      rclone_config_path    - explicit rclone.conf location
      rclone_bwlimit        - --bwlimit value
      rclone_transfers      - --transfers value (concurrency)
      rclone_checkers       - --checkers value
      rclone_extra_args     - free-text extra args
    """
    global _MANAGER_CACHE
    cfg = config or {}
    rclone_path = (cfg.get("rclone_path", "")
                   or _detect_rclone_path() or "rclone")
    config_path = cfg.get("rclone_config_path") or None
    bwlimit = str(cfg.get("rclone_bwlimit", "") or "")
    transfers = int(cfg.get("rclone_transfers", 0) or 0)
    checkers = int(cfg.get("rclone_checkers", 0) or 0)
    extra_args = str(cfg.get("rclone_extra_args", "") or "")
    # Password for encrypted rclone.conf. Read from config but
    # never persisted there - the UI prompts on each session
    # and stores in the in-memory config dict only.
    config_password = str(cfg.get("rclone_config_password", "")
                          or "")
    needs_new = (
        _MANAGER_CACHE is None
        or _MANAGER_CACHE.rclone_path != rclone_path
        or _MANAGER_CACHE.config_path != config_path
        or _MANAGER_CACHE.bwlimit != bwlimit
        or _MANAGER_CACHE.transfers != transfers
        or _MANAGER_CACHE.checkers != checkers
        or _MANAGER_CACHE.extra_args != extra_args
        or _MANAGER_CACHE.config_password != config_password
    )
    if needs_new:
        _MANAGER_CACHE = RcloneManager(
            rclone_path=rclone_path,
            config_path=config_path,
            bwlimit=bwlimit,
            transfers=transfers,
            checkers=checkers,
            extra_args=extra_args,
            config_password=config_password)
    return _MANAGER_CACHE


def _detect_rclone_path() -> Optional[str]:
    """Best-effort detection of an installed rclone binary.

    Search order:
      1. Quopus's bundled external/ directory next to the
         executable - this is the recommended location, ships
         with Quopus without polluting the user's $PATH.
      2. System $PATH (shutil.which)
      3. Well-known install paths on Windows / macOS for users
         who installed rclone via the official installer,
         scoop, chocolatey, or Homebrew.

    Returns None if nothing was found - the caller falls back
    to the bare name "rclone" so the OS error message is
    informative.
    """
    # First: external/ in the Quopus install dir. Walk up from
    # this file's location: quopus_lib/rclone_backend.py is two
    # levels deep, so __file__/../.. is the install root.
    try:
        install_root = Path(__file__).resolve().parent.parent
        candidates_external = [
            install_root / "external" / "rclone.exe",
            install_root / "external" / "rclone",
        ]
        for c in candidates_external:
            if c.is_file():
                return str(c)
    except Exception:
        pass

    # Second: $PATH
    found = shutil.which("rclone")
    if found:
        return found

    # Third: OS-specific well-known paths
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\rclone\rclone.exe",
            r"C:\Program Files (x86)\rclone\rclone.exe",
            os.path.expandvars(
                r"%USERPROFILE%\scoop\apps\rclone\current\rclone.exe"),
            os.path.expandvars(
                r"%LOCALAPPDATA%\Programs\rclone\rclone.exe"),
            r"C:\rclone\rclone.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    elif sys.platform == "darwin":
        candidates = [
            "/opt/homebrew/bin/rclone",
            "/usr/local/bin/rclone",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None
