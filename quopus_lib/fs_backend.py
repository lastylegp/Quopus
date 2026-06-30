# date_time: 2026-06-30 21:46
"""Filesystem abstraction for the lister.

LocalFs wraps pathlib operations, RemoteFs wraps an FtpBackend.
Both expose the same methods so the lister can switch between them
transparently.

API:
    fs.kind         -> 'local' or 'remote'
    fs.pwd()        -> current directory (string)
    fs.cd(path)     -> change directory
    fs.list()       -> list of FsEntry
    fs.make_dir(name)
    fs.delete(entry_path)      # removes file or empty dir
    fs.rename(old, new)
    fs.open_read(entry_path)   -> bytes (reads into memory)
    fs.write_bytes(name, data) # creates/overwrites a file
    fs.display_path()          -> human-readable path for the path bar
    fs.close()                 -> tear down (for remote)

FsEntry:
    name        -- basename
    path        -- full path string (URI-like for remote)
    is_dir      -- bool
    size        -- int (0 for dirs)
    mtime       -- float timestamp or None
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Optional


@dataclass
class FsEntry:
    name: str
    path: str          # for local: absolute path; for remote: POSIX path on server
    is_dir: bool
    size: int
    mtime: Optional[float]
    # Optional source-directory annotation. Populated only by the
    # SearchResultsFs backend so the lister can display a "Folder"
    # column showing where each match came from. None for ordinary
    # file listings.
    source_dir: Optional[str] = None


# ============================================================
# Local filesystem adapter
# ============================================================
class LocalFs:
    kind = 'local'

    def __init__(self, start_path=None):
        self._cwd = Path(start_path or Path.home()).resolve()

    def pwd(self):
        return str(self._cwd)

    @property
    def current_path(self) -> Path:
        """pathlib.Path object - kept for compatibility with lister code
        that was written against pathlib."""
        return self._cwd

    def cd(self, path):
        p = Path(path)
        if not p.is_absolute():
            p = self._cwd / p
        p = p.resolve()
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        self._cwd = p

    def list(self):
        out = []
        try:
            for entry in os.scandir(self._cwd):
                try:
                    st = entry.stat()
                    out.append(FsEntry(
                        name=entry.name,
                        path=entry.path,
                        is_dir=entry.is_dir(follow_symlinks=False),
                        size=st.st_size,
                        mtime=st.st_mtime,
                    ))
                except OSError:
                    continue
        except PermissionError:
            pass
        return out

    def make_dir(self, name):
        (self._cwd / name).mkdir(parents=True, exist_ok=False)

    def delete(self, path):
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    def rename(self, old, new):
        Path(old).rename(new)

    def open_read(self, path):
        return Path(path).read_bytes()

    def write_bytes(self, name, data, target_dir=None):
        """Write bytes to a file in `target_dir` (or cwd if None)."""
        d = Path(target_dir) if target_dir else self._cwd
        (d / name).write_bytes(data)

    def display_path(self):
        return str(self._cwd)

    def close(self):
        pass


# ============================================================
# Remote filesystem adapter (FTP/FTPS/SFTP)
# ============================================================
class RemoteFs:
    kind = 'remote'

    def __init__(self, backend, connection_label):
        self.backend = backend          # FtpBackend instance, already connected
        self.label = connection_label   # for display_path prefix
        # name -> datetime cache from the most recent list(); used to
        # stamp downloaded files with the server's modification date.
        self._mtime_cache: dict = {}
        try:
            self._cwd = self.backend.pwd()
        except Exception:
            self._cwd = "/"

    def pwd(self):
        return self._cwd

    def cd(self, path):
        self.backend.cwd(path)
        try:
            self._cwd = self.backend.pwd()
        except Exception:
            # paramiko SFTP may need special handling
            if path.startswith("/"):
                self._cwd = path
            elif path == "..":
                self._cwd = str(PurePosixPath(self._cwd).parent)
            else:
                self._cwd = str(PurePosixPath(self._cwd) / path)

    def list(self):
        out = []
        self._mtime_cache = {}
        for re in self.backend.list_dir():
            # Build POSIX-style remote path
            full = str(PurePosixPath(self._cwd) / re.name)
            mtime = re.mtime.timestamp() if re.mtime else None
            # Remember the raw datetime so download_to can apply the
            # server's modification date to the local copy.
            self._mtime_cache[re.name] = re.mtime
            out.append(FsEntry(
                name=re.name,
                path=full,
                is_dir=re.is_dir,
                size=re.size,
                mtime=mtime,
            ))
        return out

    def make_dir(self, name):
        self.backend.mkdir(name)

    def delete(self, path):
        name = PurePosixPath(path).name
        # Try rmdir first (works only for empty dirs); fall back to delete
        try:
            self.backend.rmdir(name)
        except Exception:
            self.backend.delete(name)

    def rename(self, old, new):
        old_name = PurePosixPath(old).name
        new_name = PurePosixPath(new).name
        self.backend.rename(old_name, new_name)

    def open_read(self, path, progress=None):
        """Download the remote file to memory and return bytes."""
        import tempfile
        name = PurePosixPath(path).name
        tmp = Path(tempfile.gettempdir()) / f"dopus_remote_{os.getpid()}_{name}"
        try:
            self.backend.download(name, str(tmp), progress=progress)
            return tmp.read_bytes()
        finally:
            try: tmp.unlink()
            except Exception: pass

    def download_to(self, remote_name, local_path, progress=None, size=None):
        """Save remote file directly to disk (avoids memory copy for large files)."""
        self.backend.download(remote_name, str(local_path),
                              progress=progress, size=size)
        self._apply_remote_mtime(remote_name, local_path)

    def _apply_remote_mtime(self, remote_name, local_path):
        """Stamp the freshly downloaded local file with the server's
        modification date, mirrored 1:1. Source order: the datetime
        cached from the last list() (matches what the pane shows),
        then an MDTM query as fallback. Whatever the server reports is
        applied verbatim - including epoch-0 / 1970 dates that some
        devices (e.g. the U64) store for files without an RTC stamp.
        Only a genuinely missing date (None) leaves the copy time."""
        name = PurePosixPath(remote_name).name
        ts = None
        m = self._mtime_cache.get(name)
        if m is not None:
            try: ts = m.timestamp()
            except Exception: ts = None
        if ts is None:
            fn = getattr(self.backend, "_remote_mtime", None)
            if callable(fn):
                try:
                    v = fn(name)
                    if v is not None:
                        ts = float(v)
                except Exception:
                    ts = None
        if ts is not None:
            try: os.utime(str(local_path), (ts, ts))
            except Exception: pass

    def download_tree(self, remote_dir_path, local_dir, progress=None):
        """Recursively download a remote directory into local_dir.

        remote_dir_path is the POSIX path of the remote folder (the
        .path of a directory FsEntry). The local tree is created and
        mirrored, each file stamped with the server's mtime (via
        download_to). Returns (n_files, n_dirs). The remote working
        directory is restored afterwards."""
        saved = self._cwd
        try:
            return self._download_tree_rec(
                remote_dir_path, Path(local_dir), progress)
        finally:
            try: self.cd(saved)
            except Exception: pass

    def _download_tree_rec(self, remote_dir_path, local_dir, progress):
        os.makedirs(local_dir, exist_ok=True)
        # cd refreshes _mtime_cache (via list) for this level so the
        # per-file date stamping in download_to has fresh data.
        self.cd(remote_dir_path)
        entries = self.list()
        files = [e for e in entries if not e.is_dir]
        dirs = [e for e in entries if e.is_dir]
        n_files = 0
        n_dirs = 1
        # Download this level's files first - cwd is the level dir and
        # the mtime cache is current. Recursing into sub-dirs only
        # afterwards keeps cwd correct for these basenames.
        for e in files:
            self.download_to(e.name, str(local_dir / e.name),
                             progress=progress, size=e.size)
            n_files += 1
        # Each recursion cd's by absolute path, so cwd drift is fine.
        for e in dirs:
            nf, nd = self._download_tree_rec(
                e.path, local_dir / e.name, progress)
            n_files += nf
            n_dirs += nd
        return n_files, n_dirs

    def upload_from(self, local_path, remote_name=None, progress=None):
        """Upload a local file to the current remote directory."""
        if remote_name is None:
            remote_name = Path(local_path).name
        self.backend.upload(str(local_path), remote_name, progress=progress)

    def download_tree(self, remote_dir, local_dir, on_file=None):
        """Recursively download a remote directory into local_dir.

        remote_dir is an absolute POSIX path on the server; local_dir is
        the local destination (Path/str), created if missing. on_file, if
        given, is called with each file's name just before it downloads
        (for progress / cancellation - raise from it to abort). The remote
        working directory is restored afterwards so the pane stays put."""
        saved = self._cwd
        try:
            self._download_tree_inner(remote_dir, Path(local_dir), on_file)
        finally:
            try: self.cd(saved)
            except Exception: pass

    def _download_tree_inner(self, remote_dir, local_dir, on_file):
        local_dir.mkdir(parents=True, exist_ok=True)
        self.cd(remote_dir)
        entries = self.list()          # snapshot before recursion clobbers state
        for e in entries:
            if e.is_dir:
                self._download_tree_inner(e.path, local_dir / e.name, on_file)
                self.cd(remote_dir)    # recursion moved cwd - restore for siblings
            else:
                if on_file is not None:
                    on_file(e.name)
                local_file = local_dir / e.name
                # Download directly via the backend and stamp the date
                # from this directory's snapshot. We don't go through
                # download_to here because its name->mtime cache gets
                # overwritten by the listing of any sub-directory, which
                # would mis-date files that follow a sub-folder.
                self.backend.download(e.name, str(local_file), size=e.size)
                if e.mtime is not None:
                    try: os.utime(str(local_file), (e.mtime, e.mtime))
                    except Exception: pass

    def write_bytes(self, name, data, target_dir=None):
        """Write in-memory bytes to remote. Goes via temp file."""
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"dopus_upload_{os.getpid()}_{name}"
        try:
            tmp.write_bytes(data)
            self.backend.upload(str(tmp), name)
        finally:
            try: tmp.unlink()
            except Exception: pass

    def display_path(self):
        proto = getattr(self.backend, 'PROTOCOL', 'ftp')
        return f"{proto}://{self.label}{self._cwd}"

    def close(self):
        try: self.backend.disconnect()
        except Exception: pass


# ============================================================
# Search-results filesystem (virtual)
# ============================================================
class SearchResultsFs:
    """Read-only virtual filesystem that exposes a flat list of files
    found by the FindDialog. The lister still feels like a normal
    folder view (sortable, double-click, drag, ops) but the entries
    span multiple real directories. Each entry carries its origin
    directory in `source_dir` for display in the Folder column.

    Limitations:
    - Read-only: make_dir/delete/rename raise. The user has to
      navigate to the actual location to modify files.
    - Not navigable: cd() raises - this isn't a tree, it's a flat
      result set. Going "back" to a real folder is via the lister's
      Close-search action, not via cd.
    """
    kind = 'search'

    def __init__(self, search_root: Path, label: str,
                  files: list):
        self._search_root = Path(search_root)
        # Human-readable label shown in the lister title
        self.label = label
        self._files = []
        for p in files:
            try:
                p = Path(p)
                if not p.exists():
                    continue
                st = p.stat()
                self._files.append(FsEntry(
                    name=p.name,
                    path=str(p),
                    is_dir=p.is_dir(),
                    size=st.st_size,
                    mtime=st.st_mtime,
                    source_dir=str(p.parent),
                ))
            except (OSError, PermissionError):
                continue

    def pwd(self):
        return f"<search>:{self.label}"

    @property
    def current_path(self) -> Path:
        # Return the search root as a sane fallback - some lister
        # code paths (drag&drop target, makedir, etc.) ask for this.
        # Search results are read-only so those paths shouldn't fire,
        # but we don't want to crash if they do.
        return self._search_root

    def cd(self, path):
        raise NotADirectoryError(
            "Search results are flat - cd not supported. "
            "Close search to navigate.")

    def list(self):
        return list(self._files)

    def make_dir(self, name):
        raise PermissionError("Read-only search results")

    def delete(self, path):
        # Allow deleting individual files in search results - the
        # path is real, just the listing is virtual. After deletion,
        # the entry will be filtered out on next refresh.
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        # Drop from our cached list
        self._files = [e for e in self._files if e.path != str(p)]

    def rename(self, old, new):
        Path(old).rename(new)
        # Update cached entry
        for e in self._files:
            if e.path == str(old):
                e.path = str(new)
                e.name = Path(new).name
                break

    def open_read(self, path):
        return Path(path).read_bytes()

    def write_bytes(self, name, data, target_dir=None):
        raise PermissionError("Read-only search results")

    def display_path(self):
        return f"🔎 Search: {self.label}"

    def close(self):
        pass
