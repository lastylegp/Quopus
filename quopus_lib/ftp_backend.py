"""FTP / FTPS / SFTP client backends.

Provides a uniform API for browsing remote filesystems. Used by the
FtpBrowserDialog to offer Lister-style file operations on a remote host.

Supports:
  - FTP (plain)
  - FTPS (explicit TLS, RFC 4217)
  - FTPS (implicit TLS, port 990)
  - SFTP (SSH File Transfer, via paramiko if installed)
"""
from __future__ import annotations

import ftplib
import io
import os
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable


@dataclass
class RemoteEntry:
    name: str
    size: int
    mtime: datetime | None
    is_dir: bool


class FtpBackend:
    """Base class for remote filesystem backends."""
    PROTOCOL = "ftp"

    def connect(self): raise NotImplementedError
    def disconnect(self): raise NotImplementedError
    def cwd(self, path): raise NotImplementedError
    def pwd(self): raise NotImplementedError
    def list_dir(self, path=None) -> list[RemoteEntry]: raise NotImplementedError
    def download(self, remote_name, local_path,
                 progress: Callable[[int, int], None] | None = None): raise NotImplementedError
    def upload(self, local_path, remote_name,
               progress: Callable[[int, int], None] | None = None): raise NotImplementedError
    def delete(self, remote_name): raise NotImplementedError
    def mkdir(self, name): raise NotImplementedError
    def rmdir(self, name): raise NotImplementedError
    def rename(self, old, new): raise NotImplementedError


# ============================================================
# Plain FTP / FTPS (explicit + implicit)
# ============================================================
class FtpTlsImplicit(ftplib.FTP_TLS):
    """FTP_TLS subclass that does implicit TLS on port 990."""
    def __init__(self, *args, **kwargs):
        self._sock = None
        super().__init__(*args, **kwargs)

    def connect(self, host='', port=0, timeout=-999, source_address=None):
        if host: self.host = host
        if port: self.port = port
        if timeout != -999: self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class FtpBackendStd(FtpBackend):
    """Plain FTP or explicit-TLS FTP (AUTH TLS)."""

    def __init__(self, host, port=21, user="anonymous",
                 password="anonymous@", use_tls=False, passive=True,
                 encoding="utf-8", timeout=20):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.passive = passive
        self.encoding = encoding
        self.timeout = timeout
        self.ftp: ftplib.FTP | None = None

    def connect(self):
        if self.use_tls:
            ctx = ssl.create_default_context()
            # allow self-signed certificates - BBS FTPs often use them
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.ftp = ftplib.FTP_TLS(context=ctx, timeout=self.timeout)
        else:
            self.ftp = ftplib.FTP(timeout=self.timeout)
        self.ftp.encoding = self.encoding
        self.ftp.connect(self.host, self.port)
        self.ftp.login(self.user, self.password)
        if self.use_tls:
            self.ftp.prot_p()  # encrypt data channel
        self.ftp.set_pasv(self.passive)

    def disconnect(self):
        if self.ftp is not None:
            try: self.ftp.quit()
            except Exception:
                try: self.ftp.close()
                except Exception: pass
            self.ftp = None

    def cwd(self, path): self.ftp.cwd(path)
    def pwd(self): return self.ftp.pwd()

    def list_dir(self, path=None):
        if path:
            self.ftp.cwd(path)
        lines = []
        self.ftp.retrlines("LIST", lines.append)
        return [e for e in (self._parse_line(ln) for ln in lines) if e]

    def _parse_line(self, line):
        """Parse UNIX-style FTP LIST line.
        Examples:
          drwxr-xr-x    2 owner group   4096 Apr  1 12:34 dirname
          -rw-r--r--    1 owner group  12345 Apr  1 12:34 filename.txt
        """
        parts = line.split(None, 8)
        if len(parts) < 9:
            return None
        perms = parts[0]
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        month, day, tme = parts[5], parts[6], parts[7]
        name = parts[8]
        if name in (".", ".."):
            return None
        # Parse date
        mtime = None
        try:
            now = datetime.now()
            if ":" in tme:
                # Time present → year is current
                mtime = datetime.strptime(
                    f"{now.year} {month} {day} {tme}",
                    "%Y %b %d %H:%M")
                if mtime > now:
                    mtime = mtime.replace(year=now.year - 1)
            else:
                # Year instead of time
                mtime = datetime.strptime(
                    f"{tme} {month} {day}", "%Y %b %d")
        except ValueError:
            mtime = None
        is_dir = perms.startswith("d") or perms.startswith("l")
        return RemoteEntry(name=name, size=size, mtime=mtime, is_dir=is_dir)

    def download(self, remote_name, local_path, progress=None, size=None,
                 mtime=None):
        # Support sub-directory paths (recursive folder downloads):
        # cd into the directory and fetch by basename, then restore cwd.
        saved_cwd = None
        if "/" in remote_name.strip("/"):
            saved_cwd = self.ftp.pwd()
            d, _, base = remote_name.rpartition("/")
            self.ftp.cwd(d or "/")
            remote_name = base
        try:
            # If caller didn't pass the expected size, try to query it.
            # SIZE command only works in binary (TYPE I) mode on most servers.
            if size is None or size <= 0:
                size = self._size_safe(remote_name)
            with open(local_path, "wb") as f:
                done = [0]
                def cb(chunk):
                    f.write(chunk)
                    done[0] += len(chunk)
                    if progress: progress(done[0], size or 0)
                self.ftp.retrbinary(f"RETR {remote_name}", cb)
            # Preserve the server's modification date on the local file.
            # Prefer the timestamp from the directory listing (matches what
            # the browser shows); fall back to an MDTM query.
            ts = None
            if mtime is not None:
                try: ts = mtime.timestamp()
                except Exception: ts = None
            if ts is None:
                ts = self._remote_mtime(remote_name)
            if ts is not None:
                try: os.utime(local_path, (ts, ts))
                except Exception: pass
        finally:
            if saved_cwd is not None:
                try: self.ftp.cwd(saved_cwd)
                except Exception: pass

    def upload(self, local_path, remote_name, progress=None):
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as f:
            done = [0]
            def wrapper(block, callback_orig=None):
                done[0] += len(block)
                if progress: progress(done[0], size)
                return block
            # Use storbinary - it reads from the file in chunks
            # We wrap the file to report progress.
            class ProgressFile:
                def __init__(self, fp, sz):
                    self.fp, self.sz = fp, sz; self.done = 0
                def read(self, n=-1):
                    d = self.fp.read(n)
                    self.done += len(d)
                    if progress: progress(self.done, self.sz)
                    return d
            self.ftp.storbinary(f"STOR {remote_name}",
                                ProgressFile(f, size))

    def delete(self, name): self.ftp.delete(name)
    def mkdir(self, name):  self.ftp.mkd(name)
    def rmdir(self, name):  self.ftp.rmd(name)
    def rename(self, old, new): self.ftp.rename(old, new)

    def _size_safe(self, name):
        """SIZE command only works in binary mode on many servers.
        Switch to TYPE I, query, and don't worry if it still fails."""
        try:
            self.ftp.voidcmd("TYPE I")
        except Exception:
            pass
        try:
            return self.ftp.size(name) or 0
        except Exception:
            return 0

    def _remote_mtime(self, name):
        """Query the server modification time via MDTM (RFC 3659).
        Returns POSIX timestamp (seconds, UTC) or None if unsupported."""
        try:
            resp = self.ftp.sendcmd(f"MDTM {name}")
        except Exception:
            return None
        parts = resp.split()
        if len(parts) < 2 or not parts[1][:14].isdigit():
            return None
        try:
            dt = datetime.strptime(parts[1][:14], "%Y%m%d%H%M%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None


class FtpBackendImplicitTls(FtpBackendStd):
    """FTPS with implicit TLS (port 990 by default)."""
    def __init__(self, host, port=990, **kw):
        kw['use_tls'] = True
        super().__init__(host, port, **kw)

    def connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ftp = FtpTlsImplicit(context=ctx, timeout=self.timeout)
        self.ftp.encoding = self.encoding
        self.ftp.connect(self.host, self.port)
        self.ftp.login(self.user, self.password)
        self.ftp.prot_p()
        self.ftp.set_pasv(self.passive)


# ============================================================
# SFTP (SSH) via paramiko
# ============================================================
class FtpBackendSftp(FtpBackend):
    PROTOCOL = "sftp"

    def __init__(self, host, port=22, user="", password="", keyfile=None,
                 timeout=20):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.keyfile = keyfile
        self.timeout = timeout
        self.transport = None
        self.sftp = None
        self._cwd = "/"

    def connect(self):
        try:
            import paramiko  # type: ignore
        except ImportError:
            raise RuntimeError(
                "SFTP support requires 'paramiko':  pip install paramiko")
        self.transport = paramiko.Transport((self.host, self.port))
        self.transport.connect(
            username=self.user,
            password=self.password if not self.keyfile else None,
            pkey=paramiko.RSAKey.from_private_key_file(self.keyfile)
                 if self.keyfile else None)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        try: self._cwd = self.sftp.normalize(".")
        except Exception: self._cwd = "/"

    def disconnect(self):
        try:
            if self.sftp: self.sftp.close()
        except Exception: pass
        try:
            if self.transport: self.transport.close()
        except Exception: pass
        self.sftp = None; self.transport = None

    def cwd(self, path):
        if not path.startswith("/"):
            path = str(PurePosixPath(self._cwd) / path)
        self.sftp.chdir(path); self._cwd = self.sftp.normalize(".")

    def pwd(self): return self._cwd

    def list_dir(self, path=None):
        p = path or self._cwd
        out = []
        for attr in self.sftp.listdir_attr(p):
            if attr.filename in (".", ".."): continue
            is_dir = bool(attr.st_mode and (attr.st_mode & 0o040000))
            mtime = (datetime.fromtimestamp(attr.st_mtime)
                     if attr.st_mtime else None)
            out.append(RemoteEntry(
                name=attr.filename,
                size=attr.st_size or 0,
                mtime=mtime, is_dir=is_dir))
        return out

    def download(self, remote_name, local_path, progress=None, size=None,
                 mtime=None):
        path = remote_name if remote_name.startswith("/") \
               else str(PurePosixPath(self._cwd) / remote_name)
        def cb(done, total):
            if progress: progress(done, total)
        self.sftp.get(path, str(local_path), callback=cb)
        # Preserve the server's modification date on the local file.
        ts = None
        if mtime is not None:
            try: ts = mtime.timestamp()
            except Exception: ts = None
        if ts is None:
            try: ts = self.sftp.stat(path).st_mtime
            except Exception: ts = None
        if ts is not None:
            try: os.utime(str(local_path), (ts, ts))
            except Exception: pass

    def upload(self, local_path, remote_name, progress=None):
        path = remote_name if remote_name.startswith("/") \
               else str(PurePosixPath(self._cwd) / remote_name)
        def cb(done, total):
            if progress: progress(done, total)
        self.sftp.put(str(local_path), path, callback=cb)

    def delete(self, name):
        self.sftp.remove(name if name.startswith("/")
                         else str(PurePosixPath(self._cwd) / name))

    def mkdir(self, name):
        self.sftp.mkdir(name if name.startswith("/")
                        else str(PurePosixPath(self._cwd) / name))

    def rmdir(self, name):
        self.sftp.rmdir(name if name.startswith("/")
                        else str(PurePosixPath(self._cwd) / name))

    def rename(self, old, new):
        old_p = old if old.startswith("/") else str(PurePosixPath(self._cwd) / old)
        new_p = new if new.startswith("/") else str(PurePosixPath(self._cwd) / new)
        self.sftp.rename(old_p, new_p)


# ============================================================
# Factory
# ============================================================
def make_backend(protocol, **kwargs):
    protocol = protocol.lower()
    # keyfile is only meaningful for SFTP - strip it for FTP/FTPS backends
    if protocol != "sftp":
        kwargs.pop("keyfile", None)
    if protocol == "ftp":
        kwargs.setdefault("port", 21); kwargs.pop("use_tls", None)
        return FtpBackendStd(use_tls=False, **kwargs)
    if protocol in ("ftps", "ftps-explicit"):
        kwargs.setdefault("port", 21); kwargs.pop("use_tls", None)
        return FtpBackendStd(use_tls=True, **kwargs)
    if protocol in ("ftps-implicit", "ftps-ssl"):
        kwargs.setdefault("port", 990); kwargs.pop("use_tls", None)
        return FtpBackendImplicitTls(**kwargs)
    if protocol == "sftp":
        kwargs.setdefault("port", 22); kwargs.pop("use_tls", None)
        kwargs.pop("passive", None)
        return FtpBackendSftp(**kwargs)
    raise ValueError(f"Unknown protocol: {protocol}")
