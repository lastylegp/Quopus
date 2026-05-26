"""
Quopus Drive Client Backend
===========================

Network filesystem backend that talks to a `quopus_drive_server`
running on another machine over a TLS connection. Mimics the
LocalFs / RemoteFs interface so a Quopus lister can mount one
of the server's drives the same way it mounts an FTP site.

== Auth flow ==

The server challenges with a random nonce. The client signs
nonce + timestamp + its own MAC address using HMAC-SHA256
with a pre-shared per-client secret. The server checks the
HMAC AND verifies the MAC is on its allowlist - both must
match, so a stolen secret on a different machine doesn't help
the attacker.

The server's TLS certificate is pinned by SHA-256 fingerprint
to defeat any MitM. The fingerprint comes out of the server's
`setup` wizard and the user pastes it into the Quopus
connection profile.

== Public surface ==

  QDriveConnection            connect once, holds the socket
  QDriveFs                    LocalFs-style facade per drive
  QDriveBookmark              dict-serialisable connection info
  connect_with_bookmark()     convenience for action handlers

This module has zero Qt dependencies - it's pure networking
+ pathlib. The UI hooks (connect dialog, bookmark manager)
live in qdrive_browser.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional


PROTOCOL_VERSION = 1
DEFAULT_PORT = 2000             # Quopus Drive Server default port
CONNECT_TIMEOUT = 10.0          # seconds
COMMAND_TIMEOUT = 60.0          # seconds, per command roundtrip
MAX_COMMAND_SIZE = 64 * 1024
READ_CHUNK_SIZE = 4 * 1024 * 1024


# =============================================================
# MAC enumeration (client side)
# =============================================================

# Same heuristics as the server - duplicated rather than
# imported to keep the client backend self-contained and not
# require the server script on the client machine.
import re
_VIRTUAL_NAME_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bvirtual\b", r"\bvmware\b", r"\bvbox\b",
        r"\bvirtualbox\b", r"\bhyper-?v\b", r"\bvethernet\b",
        r"\bvmnet\b", r"\bdocker\b", r"\bbridge\b",
        r"\bveth\d+\b", r"\btun\d+\b", r"\btap\d+\b",
        r"\btailscale\b", r"\bwireguard\b", r"\bwg\d+\b",
        r"\bzerotier\b", r"\bzt\w+\b", r"\bnpcap\b",
        r"\bpppoe\b", r"\bloopback\b", r"\bppp\d+\b", r"^lo$",
    ]
]
_VIRTUAL_OUI_PREFIXES = {
    "000C29", "001C14", "005056",      # VMware
    "080027", "0A0027",                 # VirtualBox
    "00155D", "0003FF",                 # Hyper-V
    "525400",                            # QEMU/KVM
    "020000",                            # locally administered
}


def _is_virtual(name: str, mac: str) -> bool:
    if not mac or mac == "00:00:00:00:00:00":
        return True
    for pat in _VIRTUAL_NAME_PATTERNS:
        if pat.search(name):
            return True
    oui = mac.replace(":", "").replace("-", "").upper()[:6]
    if oui in _VIRTUAL_OUI_PREFIXES:
        return True
    return False


def list_local_macs() -> list[tuple[str, str]]:
    """List the client machine's physical LAN MACs (iface, mac).
    The Quopus connect dialog uses this so the user can pick
    which MAC to advertise. We try multiple MACs at connect time
    until one is accepted by the server."""
    out: list[tuple[str, str]] = []
    if sys.platform == "win32":
        import subprocess
        try:
            r = subprocess.run(
                ["getmac", "/v", "/nh", "/fo", "csv"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = [p.strip().strip('"')
                          for p in line.split('","')]
                parts = [p.replace('"', '').strip()
                          for p in parts if p]
                if len(parts) < 3:
                    continue
                name = f"{parts[0]} ({parts[1]})"
                mac = parts[2].replace("-", ":").lower()
                if mac == "n/a" or len(mac) != 17:
                    continue
                if _is_virtual(name, mac):
                    continue
                out.append((name, mac))
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "darwin":
        import subprocess
        try:
            r = subprocess.run(
                ["ifconfig"], capture_output=True,
                text=True, timeout=5)
            cur = None
            for line in r.stdout.splitlines():
                if not line.startswith("\t"):
                    cur = line.split(":")[0]
                elif "ether " in line and cur:
                    mac = line.strip().split()[1].lower()
                    if not _is_virtual(cur, mac):
                        out.append((cur, mac))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        net_dir = "/sys/class/net"
        if os.path.isdir(net_dir):
            for iface in sorted(os.listdir(net_dir)):
                addr = os.path.join(net_dir, iface, "address")
                if not os.path.isfile(addr):
                    continue
                try:
                    with open(addr) as f:
                        mac = f.read().strip().lower()
                except OSError:
                    continue
                if not _is_virtual(iface, mac):
                    out.append((iface, mac))
    if not out:
        # Last resort: uuid.getnode (not always reliable but
        # always present)
        node = uuid.getnode()
        mac = ":".join(f"{(node >> i) & 0xff:02x}"
                       for i in range(40, -1, -8))
        out.append(("uuid.getnode()", mac))
    return out


# =============================================================
# Bookmark / connection profile
# =============================================================

@dataclass
class QDriveBookmark:
    """Serializable connection profile. Stored in Quopus's
    config alongside FTP / Rclone bookmarks."""
    name: str = ""              # user label for this bookmark
    host: str = ""              # hostname or IP of the server
    port: int = DEFAULT_PORT
    client_name: str = ""       # client identity on the server
    secret: str = ""            # hex string, 64 chars
    cert_fingerprint: str = ""  # SHA-256, colon-separated hex
    # Optional: pre-selected MAC. If empty we try all
    # client-side physical MACs until one is accepted.
    forced_mac: str = ""
    # Optional: which drive to enter first. Empty = show drive
    # list on connect.
    initial_drive: str = ""
    # Optional: server-side path to cd into right after mount.
    # Set by the right-click "Save current dir as default for
    # this bookmark" entry, so qdrive_site connects land back
    # in the directory the user was last in.
    initial_path: str = ""

    def to_dict(self) -> dict:
        return {
            "name":             self.name,
            "host":             self.host,
            "port":             self.port,
            "client_name":      self.client_name,
            "secret":           self.secret,
            "cert_fingerprint": self.cert_fingerprint,
            "forced_mac":       self.forced_mac,
            "initial_drive":    self.initial_drive,
            "initial_path":     self.initial_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QDriveBookmark":
        return cls(
            name=d.get("name", ""),
            host=d.get("host", ""),
            port=int(d.get("port", DEFAULT_PORT)),
            client_name=d.get("client_name", ""),
            secret=d.get("secret", ""),
            cert_fingerprint=d.get("cert_fingerprint", ""),
            forced_mac=d.get("forced_mac", ""),
            initial_drive=d.get("initial_drive", ""),
            initial_path=d.get("initial_path", ""),
        )


# =============================================================
# Wire helpers - mirrored from the server
# =============================================================

def _send_json(sock, obj: dict) -> None:
    payload = json.dumps(obj).encode("utf-8")
    if len(payload) > MAX_COMMAND_SIZE:
        raise ValueError("command too large")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed connection")
        buf.extend(chunk)
    return bytes(buf)


def _recv_json(sock) -> dict:
    raw_len = _recv_exact(sock, 4)
    n = struct.unpack(">I", raw_len)[0]
    if n > MAX_COMMAND_SIZE:
        raise ValueError(f"server response too large: {n}")
    return json.loads(
        _recv_exact(sock, n).decode("utf-8"))


def _recv_blob(sock, max_size: int = 256 * 1024 * 1024) -> bytes:
    raw_len = _recv_exact(sock, 8)
    n = struct.unpack(">Q", raw_len)[0]
    if n > max_size:
        raise ValueError(f"blob too large: {n}")
    return _recv_exact(sock, n)


def _send_blob(sock, data: bytes) -> None:
    sock.sendall(struct.pack(">Q", len(data)) + data)


# =============================================================
# Connection
# =============================================================

class QDriveError(Exception):
    """Connection/protocol/server-reported error."""


class QDriveConnection:
    """Long-lived connection to a Quopus Drive server. Thread-
    safe: a single lock serializes commands so multiple lister
    operations can share one socket.

    Holds a list of remote drives (from the server's handshake
    reply) and the certificate fingerprint that was actually
    verified."""

    def __init__(self, bookmark: QDriveBookmark):
        self.bookmark = bookmark
        self._sock: Optional[ssl.SSLSocket] = None
        self._lock = threading.RLock()
        self.drives: list[dict] = []        # [{"name":..., "readonly":bool}]
        self.server_name: str = ""
        self.protocol_version: int = 0

    # ---- low-level cert pinning -------------------------
    def _pin_cert(self, der_cert: bytes) -> None:
        """Verify the peer cert matches the pinned fingerprint.
        Raises QDriveError on mismatch."""
        expected = self.bookmark.cert_fingerprint.strip().lower()
        if not expected:
            raise QDriveError(
                "No cert fingerprint configured - refusing to "
                "trust the server. Paste the fingerprint from "
                "the server's setup output into the bookmark.")
        actual = hashlib.sha256(der_cert).hexdigest()
        actual_fmt = ":".join(actual[i:i+2]
                                for i in range(0, len(actual), 2))
        if expected.replace(":", "") != actual:
            raise QDriveError(
                f"Server cert fingerprint mismatch!\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual_fmt}\n\n"
                f"Either the server was reinstalled and got a "
                f"new cert (update the fingerprint in the "
                f"bookmark), or someone is impersonating it "
                f"(DON'T connect).")

    # ---- connect + auth ---------------------------------
    def connect(self) -> None:
        """Establish TLS, do the handshake, populate drives.
        Raises QDriveError on any failure."""
        if not self.bookmark.host:
            raise QDriveError("No host configured")
        if not self.bookmark.client_name or not self.bookmark.secret:
            raise QDriveError("client_name + secret required")

        # We DON'T let Python verify the cert chain - it's self-
        # signed by intent. Instead we extract the DER cert
        # after handshake and check the SHA-256 ourselves.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        raw = socket.create_connection(
            (self.bookmark.host, self.bookmark.port),
            timeout=CONNECT_TIMEOUT)
        try:
            sock = ctx.wrap_socket(
                raw, server_hostname=self.bookmark.host)
        except (ssl.SSLError, OSError) as e:
            raw.close()
            raise QDriveError(f"TLS handshake failed: {e}")

        # Verify pin BEFORE sending any credentials
        der = sock.getpeercert(binary_form=True)
        if der is None:
            sock.close()
            raise QDriveError("Server didn't present a certificate")
        try:
            self._pin_cert(der)
        except QDriveError:
            sock.close()
            raise

        sock.settimeout(COMMAND_TIMEOUT)
        self._sock = sock

        # Read challenge
        try:
            challenge = _recv_json(sock)
        except (ConnectionError, OSError, ValueError) as e:
            self._sock = None
            sock.close()
            raise QDriveError(f"Bad challenge from server: {e}")
        if challenge.get("msg") != "challenge":
            self._sock = None
            sock.close()
            raise QDriveError(f"Unexpected server message: "
                              f"{challenge.get('msg')!r}")

        nonce = bytes.fromhex(challenge["nonce"])
        ts = int(challenge["ts"])

        # Pick a MAC to advertise. Either the user-forced one
        # from the bookmark, or we try the first physical one
        # we find. If the server rejects it we report a clear
        # error - the user can edit the bookmark to pin a
        # different MAC.
        if self.bookmark.forced_mac:
            macs_to_try = [self.bookmark.forced_mac.lower()]
        else:
            macs_to_try = [m for _name, m in list_local_macs()]
            if not macs_to_try:
                raise QDriveError(
                    "Could not detect any physical MAC address "
                    "on this machine. Set forced_mac in the "
                    "bookmark.")

        secret = bytes.fromhex(self.bookmark.secret)
        last_err = None
        for mac in macs_to_try:
            msg = nonce + struct.pack(">Q", ts) + mac.encode()
            sig = hmac.new(secret, msg,
                            hashlib.sha256).hexdigest()
            try:
                _send_json(sock, {
                    "msg":     "auth",
                    "client":  self.bookmark.client_name,
                    "mac":     mac,
                    "hmac":    sig,
                })
                reply = _recv_json(sock)
            except (OSError, ValueError) as e:
                self._sock = None
                sock.close()
                raise QDriveError(f"Auth network error: {e}")

            if reply.get("msg") == "ok":
                self.drives = reply.get("drives", [])
                self.server_name = reply.get("server", "")
                self.protocol_version = int(
                    reply.get("version", 0))
                if (self.protocol_version
                        and self.protocol_version != PROTOCOL_VERSION):
                    # Tolerate but warn - both sides should be
                    # backwards-compatible across minor changes,
                    # we just log this for support.
                    print(f"  [qdrive] server speaks proto v"
                          f"{self.protocol_version}, "
                          f"we are v{PROTOCOL_VERSION}")
                return
            elif reply.get("msg") == "deny":
                last_err = reply.get("reason", "denied")
                # If denial reason is "mac not allowed" we can
                # try the next MAC; otherwise it's a permanent
                # error, stop trying.
                if "mac" not in last_err.lower():
                    break
                # Server closes the connection after deny, so
                # we have to reconnect for the next MAC. For
                # simplicity we just fail out and let the user
                # set forced_mac explicitly.
                self._sock = None
                try: sock.close()
                except OSError: pass
                raise QDriveError(
                    f"Server rejected this connection.\n"
                    f"Reason given: {last_err}\n\n"
                    f"Possible causes:\n"
                    f"  - This client's MAC ({mac}) is not in "
                    f"the server's allowlist for client name "
                    f"{self.bookmark.client_name!r}\n"
                    f"  - The shared secret is wrong\n"
                    f"  - The client name is unknown to the "
                    f"server\n\n"
                    f"(For security the server doesn't "
                    f"distinguish between these cases in its "
                    f"reply.)")

        self._sock = None
        try: sock.close()
        except OSError: pass
        raise QDriveError(
            f"Authentication failed: {last_err or 'unknown'}\n\n"
            f"Check client name, shared secret, and that the "
            f"server has this client's MAC on its allowlist.")

    def close(self):
        with self._lock:
            if self._sock is None:
                return
            try:
                _send_json(self._sock, {"cmd": "bye"})
                _recv_json(self._sock)
            except (OSError, ValueError):
                pass
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ---- command roundtrip ------------------------------
    def _call(self, **cmd) -> dict:
        with self._lock:
            if self._sock is None:
                raise QDriveError("not connected")
            try:
                _send_json(self._sock, cmd)
                reply = _recv_json(self._sock)
            except (OSError, ValueError, ConnectionError) as e:
                raise QDriveError(f"network error: {e}")
            if reply.get("msg") == "err":
                raise QDriveError(reply.get("reason", "server error"))
            if reply.get("msg") != "ok" and reply.get("msg") != "ready":
                raise QDriveError(
                    f"unexpected reply: {reply.get('msg')!r}")
            return reply

    def list_dir(self, drive: str, path: str) -> list[dict]:
        return self._call(cmd="list", drive=drive,
                            path=path).get("entries", [])

    def stat(self, drive: str, path: str) -> dict:
        return self._call(cmd="stat", drive=drive, path=path)

    def read_file(self, drive: str, path: str,
                    progress=None) -> bytes:
        """Stream a file in READ_CHUNK_SIZE chunks. progress is
        called as progress(bytes_so_far, total) where total
        comes from a preliminary stat()."""
        info = self.stat(drive, path)
        if info.get("is_dir"):
            raise QDriveError(f"is a directory: {path}")
        total = int(info.get("size", 0))
        chunks = bytearray()
        offset = 0
        while offset < total:
            want = min(READ_CHUNK_SIZE, total - offset)
            reply = self._call(cmd="read", drive=drive,
                                 path=path, offset=offset,
                                 length=want)
            length = int(reply.get("length", 0))
            if length <= 0:
                break
            with self._lock:
                data = _recv_blob(self._sock)
            chunks.extend(data)
            offset += len(data)
            if progress is not None:
                try:
                    progress(offset, total)
                except Exception:
                    pass
            if length < want:
                # End of file
                break
        return bytes(chunks)

    def write_file(self, drive: str, path: str,
                     data: bytes, append: bool = False) -> int:
        with self._lock:
            if self._sock is None:
                raise QDriveError("not connected")
            try:
                _send_json(self._sock, {
                    "cmd":    "write",
                    "drive":  drive,
                    "path":   path,
                    "append": append,
                })
                ready = _recv_json(self._sock)
                if ready.get("msg") != "ready":
                    raise QDriveError(
                        f"server not ready: "
                        f"{ready.get('reason', '?')}")
                _send_blob(self._sock, data)
                reply = _recv_json(self._sock)
            except (OSError, ValueError) as e:
                raise QDriveError(f"network error: {e}")
            if reply.get("msg") != "ok":
                raise QDriveError(reply.get("reason",
                                              "write failed"))
            return int(reply.get("written", len(data)))

    def mkdir(self, drive: str, path: str,
                parents: bool = False) -> None:
        self._call(cmd="mkdir", drive=drive, path=path,
                    parents=parents, exist_ok=False)

    def delete(self, drive: str, path: str,
                 recursive: bool = False) -> None:
        self._call(cmd="delete", drive=drive, path=path,
                    recursive=recursive)

    def rename(self, drive: str, src: str, dst: str) -> None:
        self._call(cmd="rename", drive=drive,
                    **{"from": src, "to": dst})


# =============================================================
# LocalFs-style facade
# =============================================================

@dataclass
class QDriveFsEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: Optional[float]
    source_dir: Optional[str] = None


class QDriveFs:
    """Drop-in replacement for LocalFs / RemoteFs that routes
    every call through a QDriveConnection. One instance per
    drive, but multiple instances can share a single
    QDriveConnection so all listers on the same remote machine
    use one socket.

    `kind` is set to 'remote' (not 'qdrive') so the existing
    Quopus lister code that special-cases remote filesystems
    (download-to-temp-for-viewing, disconnect dialog, REMOTE
    title prefix, etc.) treats QDrive mounts the same as FTP
    mounts. The actual transport is hidden behind the API."""

    kind = 'remote'

    def __init__(self, conn: QDriveConnection, drive: str,
                  start_path: str = "/"):
        self._conn = conn
        self._drive = drive
        # Server-side paths are always POSIX-style. We keep an
        # internal PurePosixPath and join with "/" everywhere.
        self._cwd = PurePosixPath(start_path or "/")
        # Find the readonly flag for this drive so write attempts
        # can fail with a nice error before going on the wire.
        self._readonly = False
        for d in conn.drives:
            if d.get("name") == drive:
                self._readonly = bool(d.get("readonly"))
                break

    # ---- info -------------------------------------------
    @property
    def drive(self) -> str:
        return self._drive

    @property
    def readonly(self) -> bool:
        return self._readonly

    @property
    def label(self) -> str:
        """Short human label shown in disconnect prompts and
        the [REMOTE] title bar prefix - matches the convention
        used by the FTP RemoteFs facade."""
        host = self._conn.bookmark.host or "?"
        return f"qdrive://{host}/{self._drive}"

    def pwd(self) -> str:
        return str(self._cwd)

    @property
    def current_path(self):
        """Returns a path-like object that 'looks enough like'
        pathlib.Path for the lister code that treats it as one.
        We expose `.name`, `.parent`, `.parts` via PurePosixPath
        which is exactly what the listers consume."""
        return self._cwd

    def display_path(self) -> str:
        return f"qdrive://{self._conn.bookmark.host}" \
               f"/{self._drive}{self._cwd}"

    def cd(self, path) -> None:
        p = PurePosixPath(str(path))
        if not p.is_absolute():
            p = self._cwd / p
        # Normalize ".." manually because PurePosixPath doesn't
        # resolve those. We can't ask the server to resolve
        # paths because that would require a roundtrip per cd -
        # better to do it locally.
        parts = []
        for part in p.parts:
            if part == "..":
                if len(parts) > 1:           # keep leading "/"
                    parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        # Reconstruct absolute path
        target = PurePosixPath("/" + "/".join(
            x for x in parts if x != "/"))
        # Validate by listing - if the server doesn't accept
        # the path, raise NotADirectoryError so the lister
        # falls back to whatever it does for bad dirs.
        try:
            self._conn.list_dir(self._drive, str(target))
        except QDriveError as e:
            raise NotADirectoryError(str(e))
        self._cwd = target

    def list(self) -> list:
        try:
            raw = self._conn.list_dir(self._drive, str(self._cwd))
        except QDriveError:
            return []
        out = []
        for e in raw:
            full = self._cwd / e["name"]
            out.append(QDriveFsEntry(
                name=e["name"],
                path=str(full),
                is_dir=bool(e.get("is_dir")),
                size=int(e.get("size", 0)),
                mtime=e.get("mtime"),
            ))
        return out

    # ---- mutations --------------------------------------
    def _check_rw(self):
        if self._readonly:
            raise PermissionError(
                f"drive {self._drive!r} is mounted read-only")

    def make_dir(self, name: str) -> None:
        self._check_rw()
        target = self._cwd / name
        self._conn.mkdir(self._drive, str(target),
                           parents=False)

    def delete(self, path) -> None:
        self._check_rw()
        # Lister passes the full posix path from the entry, but
        # also sometimes just a name. Be permissive.
        p = PurePosixPath(str(path))
        if not p.is_absolute():
            p = self._cwd / p
        # Try as file first, then recursive on dir
        try:
            self._conn.delete(self._drive, str(p),
                                recursive=False)
        except QDriveError:
            # Try recursive (it's a non-empty dir)
            self._conn.delete(self._drive, str(p),
                                recursive=True)

    def rename(self, old, new) -> None:
        self._check_rw()
        op = PurePosixPath(str(old))
        np = PurePosixPath(str(new))
        if not op.is_absolute():
            op = self._cwd / op
        if not np.is_absolute():
            np = self._cwd / np
        self._conn.rename(self._drive, str(op), str(np))

    # ---- io ---------------------------------------------
    def open_read(self, path, progress=None) -> bytes:
        p = PurePosixPath(str(path))
        if not p.is_absolute():
            p = self._cwd / p
        return self._conn.read_file(self._drive, str(p),
                                       progress=progress)

    def write_bytes(self, name: str, data: bytes,
                     target_dir=None) -> None:
        self._check_rw()
        d = (PurePosixPath(str(target_dir))
              if target_dir else self._cwd)
        target = d / name
        self._conn.write_file(self._drive, str(target), data)

    def download_to(self, remote_name, local_path,
                      progress=None, size=None) -> None:
        from pathlib import Path
        p = PurePosixPath(str(remote_name))
        if not p.is_absolute():
            p = self._cwd / p
        data = self._conn.read_file(self._drive, str(p),
                                       progress=progress)
        Path(local_path).write_bytes(data)

    def upload_from(self, local_path, remote_name=None,
                      progress=None) -> None:
        self._check_rw()
        from pathlib import Path
        data = Path(local_path).read_bytes()
        name = remote_name or Path(local_path).name
        if progress is not None:
            try: progress(0, len(data))
            except Exception: pass
        self.write_bytes(name, data)
        if progress is not None:
            try: progress(len(data), len(data))
            except Exception: pass

    def close(self) -> None:
        """Close the underlying TLS connection. Called by the
        lister's disconnect_remote() path when the user
        unmounts the drive.

        If you want to keep the connection alive (e.g. you
        explicitly share one connection between multiple
        QDriveFs instances on different listers), call
        QDriveConnection.close() yourself when you're really
        done and ignore this hook."""
        try:
            self._conn.close()
        except Exception:
            pass


# =============================================================
# Bookmark store
# =============================================================

def _bookmark_store_path():
    """Where bookmarks live - inside Quopus's config dir."""
    try:
        from .config import CONFIG_DIR
        return CONFIG_DIR / "qdrive_bookmarks.json"
    except ImportError:
        # Running standalone for tests
        return PurePosixPath("/tmp/qdrive_bookmarks.json")


def load_bookmarks() -> list[QDriveBookmark]:
    p = _bookmark_store_path()
    if not hasattr(p, "exists") or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [QDriveBookmark.from_dict(d) for d in data]


def save_bookmarks(bookmarks: list[QDriveBookmark]) -> None:
    p = _bookmark_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([b.to_dict() for b in bookmarks], indent=2),
        encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# =============================================================
# Convenience entry point
# =============================================================

def connect_with_bookmark(bookmark: QDriveBookmark
                           ) -> QDriveConnection:
    """Create a QDriveConnection from a bookmark, connect, and
    return it. Caller is responsible for calling .close() when
    done."""
    conn = QDriveConnection(bookmark)
    conn.connect()
    return conn
