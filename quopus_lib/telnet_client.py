"""Telnet / Raw TCP terminal client for Quopus Commander.

Designed for BBS hopping and Unix-box admin. Renders ANSI escape
sequences and PETSCII into a fixed-width pixel-perfect grid using
the bundled Topaz / C64 Pro Mono fonts.

Architecture
============

There are three threads of concern:

1. **Network thread** (`_NetworkWorker`, a QThread): owns the
   TCP socket, handles the telnet IAC negotiation, and forwards
   incoming bytes to the UI via a Qt signal. Outgoing bytes are
   pushed in via a queue from the UI thread. The reason for a
   separate thread is that the Qt event loop must keep redrawing
   the terminal grid while bytes arrive; doing this in the UI
   thread leads to freezes on slow connections.

2. **Terminal emulator** (`_TerminalScreen`): a software VT100/ANSI
   state machine over a fixed character grid. Lives on the UI
   thread, fed by `_on_data_received(bytes)`. Holds the cursor,
   attributes (fg/bg/bold/inverse), and the cell buffer. ANSI
   parser handles CSI sequences, SGR (Select Graphic Rendition),
   cursor positioning, clear-screen/line. Not a complete VT220
   emulator - just enough for typical BBS / shell use.

3. **Widget** (`_TerminalWidget`, a QWidget): paints the cell
   buffer using the configured pixel font, draws the cursor,
   captures keystrokes and forwards them to the network.

PETSCII mode skips the ANSI parser and just maps each incoming
byte through `petscii_byte_to_unicode()` with C64-style color
control codes interpreted inline.

Session persistence
===================

Saved sessions live in `<quopus>/config/telnet_sessions.json` as a
list of dicts. Same JSON format as the other quopus modules use
(versioned via `_meta.format_version`). The session manager is a
sub-dialog that loads/saves/edits the list.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSize, QRect, QPoint,
)
from PyQt6.QtGui import (
    QFont, QFontMetrics, QPainter, QColor, QPen,
    QKeyEvent, QFontDatabase, QPalette,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QComboBox, QSpinBox, QCheckBox, QWidget,
    QFileDialog, QMessageBox, QListWidget, QListWidgetItem,
    QSplitter, QPlainTextEdit, QFormLayout, QGroupBox,
    QSizePolicy, QApplication,
)
from .config import scaled_font_px


# ===========================================================
# Color palettes
# ===========================================================

# Standard 8 ANSI colors + bright variants - close to xterm
# but with slightly more saturation so they look good on the
# Workbench grey backdrop.
ANSI_PALETTE = [
    QColor(0x00, 0x00, 0x00),  # 0  black
    QColor(0xCC, 0x00, 0x00),  # 1  red
    QColor(0x4E, 0x9A, 0x06),  # 2  green
    QColor(0xC4, 0xA0, 0x00),  # 3  yellow
    QColor(0x34, 0x65, 0xA4),  # 4  blue
    QColor(0x75, 0x50, 0x7B),  # 5  magenta
    QColor(0x06, 0x98, 0x9A),  # 6  cyan
    QColor(0xD3, 0xD7, 0xCF),  # 7  white
    # Bright variants (used when SGR bold is active)
    QColor(0x55, 0x57, 0x53),  # 8  bright black (= grey)
    QColor(0xEF, 0x29, 0x29),  # 9  bright red
    QColor(0x8A, 0xE2, 0x34),  # 10 bright green
    QColor(0xFC, 0xE9, 0x4F),  # 11 bright yellow
    QColor(0x72, 0x9F, 0xCF),  # 12 bright blue
    QColor(0xAD, 0x7F, 0xA8),  # 13 bright magenta
    QColor(0x34, 0xE2, 0xE2),  # 14 bright cyan
    QColor(0xEE, 0xEE, 0xEC),  # 15 bright white
]

# C64-style palette indexed by PETSCII color control codes 0x90..0x9C.
# Maps to the standard 16 VIC-II colors. Used in PETSCII terminal mode.
C64_PALETTE = {
    0x90: QColor(0x00, 0x00, 0x00),  # black
    0x05: QColor(0xFF, 0xFF, 0xFF),  # white
    0x1C: QColor(0x88, 0x00, 0x00),  # red
    0x9F: QColor(0xAA, 0xFF, 0xEE),  # cyan
    0x9C: QColor(0xCC, 0x44, 0xCC),  # purple
    0x1E: QColor(0x00, 0xCC, 0x55),  # green
    0x1F: QColor(0x00, 0x00, 0xAA),  # blue
    0x9E: QColor(0xEE, 0xEE, 0x77),  # yellow
    0x81: QColor(0xDD, 0x88, 0x55),  # orange
    0x95: QColor(0x66, 0x44, 0x00),  # brown
    0x96: QColor(0xFF, 0x77, 0x77),  # light red
    0x97: QColor(0x33, 0x33, 0x33),  # dark grey
    0x98: QColor(0x77, 0x77, 0x77),  # medium grey
    0x99: QColor(0xAA, 0xFF, 0x66),  # light green
    0x9A: QColor(0x00, 0x88, 0xFF),  # light blue
    0x9B: QColor(0xBB, 0xBB, 0xBB),  # light grey
}

DEFAULT_FG = 7   # ANSI white-ish
DEFAULT_BG = 0   # ANSI black


# ===========================================================
# Telnet protocol bits
# ===========================================================

# IAC = Interpret As Command. Sequences start with 0xFF.
IAC = 0xFF
DONT = 0xFE
DO = 0xFD
WONT = 0xFC
WILL = 0xFB
SB = 0xFA       # Sub-negotiation begin
SE = 0xF0       # Sub-negotiation end
GA = 0xF9       # Go ahead
NOP = 0xF1
ECHO = 1
SUPPRESS_GA = 3
TERMTYPE = 24
NAWS = 31       # Negotiate About Window Size
LINEMODE = 34
NEW_ENVIRON = 39


# ===========================================================
# Session data
# ===========================================================

@dataclass
class TelnetSession:
    """One saved connection profile."""
    name: str = "New session"
    host: str = ""
    port: int = 23
    protocol: str = "telnet"   # 'telnet' | 'raw'
    encoding: str = "cp437"    # 'cp437'|'utf-8'|'latin-1'|'petscii'
    terminal_type: str = "ansi"  # 'ansi' | 'petscii'
    font_family: str = "Topaz New"
    font_size: int = 14
    rows: int = 25
    cols: int = 82
    autologin_user: str = ""
    autologin_password: str = ""
    autologin_user_prompt: str = "login:"
    autologin_pass_prompt: str = "password:"
    autologin_delay_ms: int = 200
    keep_log: bool = False
    log_path: str = ""
    backspace_sends: str = "127"  # '127' (DEL) | '8' (BS)
    local_echo: bool = False
    crlf_mode: str = "cr"   # 'cr' | 'crlf' | 'lf'
    # Macros: dict mapping a Qt key name (e.g. "F1", "F2", "Ctrl+M")
    # to a string snippet that gets sent verbatim. Useful for
    # binding frequent commands like LOAD"*",8,1<CR> or LIST<CR>
    # to a single key. The default is empty.
    macros: dict = field(default_factory=dict)
    # Phonebook metadata - not used by the connection itself but
    # shown in the session manager grid so the user can curate
    # their connection list. `group` lets the phonebook show a
    # category column (e.g. "C64 BBSes", "Unix shells", "Demoparty").
    # `notes` is free-form text. `last_connected` is an ISO
    # timestamp updated each time the connection succeeds.
    group: str = ""
    notes: str = ""
    last_connected: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "TelnetSession":
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


def _sessions_path() -> Path:
    from .config import CONFIG_DIR
    return CONFIG_DIR / "telnet_sessions.json"


def load_sessions() -> list[TelnetSession]:
    """Read the saved-sessions file. Returns [] on first run or
    parse error - never raises so the dialog still opens."""
    p = _sessions_path()
    if not p.is_file():
        return []
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and isinstance(data.get('sessions'), list):
        items = data['sessions']
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [TelnetSession.from_dict(x) for x in items
            if isinstance(x, dict)]


def save_sessions(sessions: list[TelnetSession]):
    """Write the saved-sessions file. Wrapped with `_meta` so we
    can evolve the format later."""
    import datetime
    p = _sessions_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)),
                     exist_ok=True)
        payload = {
            "_meta": {
                "timestamp": datetime.datetime.now().isoformat(),
                "tool": "Quopus Telnet Client",
                "format_version": 1,
            },
            "sessions": [asdict(s) for s in sessions],
        }
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


# ===========================================================
# Network worker - keeps the socket out of the UI thread
# ===========================================================


class _NetworkWorker(QThread):
    """Background socket pump. Talks to the dialog via signals."""
    data_received = pyqtSignal(bytes)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)   # reason string
    log_msg = pyqtSignal(str)        # status messages for the log

    def __init__(self, host, port, protocol='telnet',
                 rows=25, cols=80, term='ansi', parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.protocol = protocol
        self.rows = rows
        self.cols = cols
        self.term = term
        self.sock: Optional[socket.socket] = None
        self._stop = False
        self._out_queue: deque = deque()

    def send(self, data: bytes):
        """Queue bytes to be transmitted. Called from UI thread."""
        if data:
            self._out_queue.append(data)

    def stop(self):
        self._stop = True
        # Calling close() from a different thread is safe and the
        # blocked recv() will raise an error which lets us drop out.
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def run(self):
        try:
            self.log_msg.emit(
                f"connecting to {self.host}:{self.port}...")
            self.sock = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(0.05)   # short poll for tx
            self.connected.emit()
            self.log_msg.emit("connected")
        except (socket.gaierror, socket.timeout,
                ConnectionRefusedError, OSError) as e:
            self.disconnected.emit(f"connect failed: {e}")
            return

        # Telnet IAC state machine. For RAW mode we skip parsing
        # and pass bytes through verbatim.
        while not self._stop:
            # Send queued outbound bytes
            try:
                while self._out_queue:
                    chunk = self._out_queue.popleft()
                    self.sock.sendall(chunk)
            except OSError as e:
                self.disconnected.emit(f"send failed: {e}")
                break
            # Receive
            try:
                data = self.sock.recv(4096)
                if not data:
                    self.disconnected.emit(
                        "remote closed connection")
                    break
                if self.protocol == 'telnet':
                    data = self._strip_telnet_iac(data)
                if data:
                    self.data_received.emit(data)
            except socket.timeout:
                continue
            except OSError as e:
                if not self._stop:
                    self.disconnected.emit(f"recv failed: {e}")
                break

        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _strip_telnet_iac(self, data: bytes) -> bytes:
        """Walk the byte stream and handle IAC subnegotiation.

        Responds to DO TERMTYPE, DO NAWS (window size), and refuses
        everything else with WONT/DONT to keep the negotiation
        short. Returns the filtered byte stream (with IAC sequences
        removed) for the terminal emulator to consume."""
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            # IAC seen - peek ahead
            if i + 1 >= n:
                # Trailing IAC, stash for next chunk - we'd need
                # a buffer to be fully correct but in practice
                # this only matters for protocols that send IAC
                # mid-character which is rare. Drop it for now.
                break
            cmd = data[i + 1]
            if cmd == IAC:
                # Escaped 0xFF literal
                out.append(IAC)
                i += 2
                continue
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                opt = data[i + 2]
                self._handle_iac_3byte(cmd, opt)
                i += 3
                continue
            if cmd == SB:
                # Sub-negotiation: skip until IAC SE
                j = i + 2
                while j + 1 < n:
                    if data[j] == IAC and data[j + 1] == SE:
                        j += 2
                        break
                    j += 1
                self._handle_iac_subneg(data[i + 2:j - 2])
                i = j
                continue
            # Unknown 2-byte command (GA, NOP, etc) - just skip
            i += 2
        return bytes(out)

    def _handle_iac_3byte(self, cmd, opt):
        """Respond to a 3-byte IAC negotiation."""
        if cmd == DO:
            if opt == TERMTYPE:
                # Yes, we have a terminal type
                self._raw_send(bytes([IAC, WILL, TERMTYPE]))
            elif opt == NAWS:
                # Yes, send our window dimensions
                self._raw_send(bytes([IAC, WILL, NAWS]))
                # And immediately follow with the subnegotiation
                self._send_naws(self.cols, self.rows)
            elif opt == SUPPRESS_GA:
                self._raw_send(bytes([IAC, WILL, SUPPRESS_GA]))
            else:
                self._raw_send(bytes([IAC, WONT, opt]))
        elif cmd == WILL:
            if opt in (ECHO, SUPPRESS_GA):
                self._raw_send(bytes([IAC, DO, opt]))
            else:
                self._raw_send(bytes([IAC, DONT, opt]))
        elif cmd == DONT:
            self._raw_send(bytes([IAC, WONT, opt]))
        elif cmd == WONT:
            self._raw_send(bytes([IAC, DONT, opt]))

    def _handle_iac_subneg(self, payload: bytes):
        """Respond to a sub-negotiation block."""
        if not payload:
            return
        if payload[0] == TERMTYPE and payload[1:2] == b"\x01":
            # Server asked "send your terminal type"
            term_name = ("PETSCII" if self.term == 'petscii'
                         else "ANSI").encode('ascii')
            resp = (bytes([IAC, SB, TERMTYPE, 0])
                    + term_name + bytes([IAC, SE]))
            self._raw_send(resp)

    def _raw_send(self, data: bytes):
        try:
            if self.sock:
                self.sock.sendall(data)
        except OSError:
            pass

    def _send_naws(self, cols, rows):
        """Send a NAWS subnegotiation announcing the terminal
        dimensions. Called once during the initial DO-NAWS reply
        and again any time the user resizes the window mid-session
        via the toolbar Cols dropdown."""
        self.cols = cols
        self.rows = rows
        cols = max(1, min(cols, 0xFFFF))
        rows = max(1, min(rows, 0xFFFF))
        payload = struct.pack(">HH", cols, rows)
        # Escape any literal 0xFF bytes in the payload so they
        # don't get interpreted as another IAC by the remote.
        payload = payload.replace(b"\xff", b"\xff\xff")
        self._raw_send(
            bytes([IAC, SB, NAWS]) + payload
            + bytes([IAC, SE]))


# ===========================================================
# SSH worker (optional - needs paramiko)
# ===========================================================

# We import paramiko lazily so the rest of the module works for
# users who don't have it installed. SSH support shows up as a
# protocol choice only when paramiko is importable.
def _have_paramiko():
    try:
        import paramiko    # noqa: F401
        return True
    except ImportError:
        return False


class _SSHWorker(QThread):
    """SSH terminal worker. Uses paramiko's interactive shell
    primitive.

    Same Qt signal interface as _NetworkWorker so the dialog
    doesn't care which protocol it's talking to. Auth tries
    password first, then falls back to the user's ssh-agent /
    ~/.ssh/id_rsa-style keys if paramiko can find them.

    Window resize is supported by re-issuing `resize_pty()` on
    the channel - the dialog calls our resize() method when the
    user changes the rows/cols.
    """
    data_received = pyqtSignal(bytes)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)
    log_msg = pyqtSignal(str)
    auth_needed = pyqtSignal(str)  # prompt for password

    def __init__(self, host, port, username, password,
                 rows=25, cols=80, term='ansi', parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.rows = rows
        self.cols = cols
        self.term = term
        self._stop = False
        self._out_queue: deque = deque()
        self._client = None
        self._channel = None

    def send(self, data: bytes):
        if data:
            self._out_queue.append(data)

    def stop(self):
        self._stop = True
        try:
            if self._channel:
                self._channel.close()
        except Exception:
            pass
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass

    def resize(self, rows, cols):
        """Tell the remote PTY about a new window size."""
        self.rows = rows
        self.cols = cols
        try:
            if self._channel:
                self._channel.resize_pty(
                    width=cols, height=rows)
        except Exception:
            pass

    def run(self):
        try:
            import paramiko
        except ImportError:
            self.disconnected.emit(
                "paramiko library not installed - SSH is "
                "unavailable. Install with: pip install paramiko")
            return
        self.log_msg.emit(
            f"connecting via SSH to {self.host}:{self.port}...")
        client = paramiko.SSHClient()
        # AutoAdd is rude in production but pragmatic for a
        # BBS / playground client. The alternative (RejectPolicy +
        # manual known_hosts) would force the user to deal with
        # host keys before they can connect at all.
        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username or None,
                password=self.password or None,
                # Try ssh-agent keys + ~/.ssh/id_* as fallback
                allow_agent=True,
                look_for_keys=True,
                timeout=15.0,
                banner_timeout=15.0,
                auth_timeout=15.0,
            )
        except (paramiko.AuthenticationException,
                paramiko.SSHException, OSError) as e:
            self.disconnected.emit(f"SSH error: {e}")
            return
        self._client = client
        # Open an interactive shell with a PTY of our terminal size
        try:
            chan = client.invoke_shell(
                term=("xterm-256color" if self.term == 'ansi'
                      else "vt100"),
                width=self.cols, height=self.rows)
            chan.settimeout(0.05)
        except paramiko.SSHException as e:
            self.disconnected.emit(f"shell open failed: {e}")
            return
        self._channel = chan
        self.connected.emit()
        self.log_msg.emit("SSH shell ready")
        # Main pump - same shape as the telnet worker
        while not self._stop:
            try:
                while self._out_queue:
                    chunk = self._out_queue.popleft()
                    chan.sendall(chunk)
            except OSError as e:
                self.disconnected.emit(f"send failed: {e}")
                break
            # Read with a short timeout. paramiko channels raise
            # socket.timeout on no-data, which is the polite way
            # to poll without blocking.
            try:
                data = chan.recv(4096)
                if not data:
                    self.disconnected.emit(
                        "remote closed connection")
                    break
                self.data_received.emit(data)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop:
                    self.disconnected.emit(f"recv error: {e}")
                break
        try:
            chan.close()
            client.close()
        except Exception:
            pass


# ===========================================================
# ZModem transfer handler (lrzsz-style sender + receiver)
# ===========================================================

# ZModem is an 80s file transfer protocol used by BBSes. Spec:
# http://www.gallium.com/~lvirden/zmodem.txt
#
# The big-picture flow for a download (rz on the local side):
#   1. Remote sends "rz" or auto-detects an offered file
#   2. Remote sends ZRQINIT frame: starts with "rz\r" then **\x18B00...
#   3. We respond with ZRINIT (our capabilities)
#   4. Remote sends ZFILE with metadata + ZDATA frames
#   5. We ACK each subpacket with ZACK
#   6. Remote sends ZEOF, we send ZRPOS to acknowledge end-of-file
#   7. Remote sends ZFIN, we reply ZFIN, both close
#
# Upload is the inverse: we send ZRQINIT, remote sends ZRINIT,
# we send ZFILE, then ZDATA chunks, ZEOF, ZFIN.
#
# This is a *minimal* implementation. CRC32 only (no CRC16),
# binary mode only (no escape-control-chars option), single-file
# at a time. That covers what every BBS in the past 30 years
# actually uses.

ZPAD = ord("*")    # 0x2A
ZDLE = 0x18        # cancel-escape prefix
ZBIN = ord("A")    # CRC16 binary header
ZHEX = ord("B")    # hex header (used for handshake)
ZBIN32 = ord("C")  # CRC32 binary header

# Frame types
ZRQINIT = 0
ZRINIT = 1
ZSINIT = 2
ZACK = 3
ZFILE = 4
ZSKIP = 5
ZNAK = 6
ZABORT = 7
ZFIN = 8
ZRPOS = 9
ZDATA = 10
ZEOF = 11
ZFERR = 12
ZCRC = 13
ZCHALLENGE = 14
ZCOMPL = 15
ZCAN = 16
ZFREECNT = 17
ZCOMMAND = 18

# Subpacket terminators
ZCRCE = ord("h")   # last subpacket in frame, no ZACK expected
ZCRCG = ord("i")   # not last, no ZACK
ZCRCQ = ord("j")   # not last, send ZACK
ZCRCW = ord("k")   # last, send ZACK


def _zm_crc32(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    """Incremental CRC32 used by ZModem. Same polynomial as the
    standard CRC32 but different init/xorout."""
    import zlib
    # zlib.crc32 matches what zmodem expects (init=0, ones
    # complement at the end). We compute our own to control
    # the initial state - that lets us chain across subpackets.
    for b in data:
        crc = crc ^ b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = crc >> 1
    return crc & 0xFFFFFFFF


def _zm_escape(data: bytes) -> bytes:
    """Escape special bytes via ZDLE. Used for everything sent
    after the frame header up through the subpacket trailer."""
    out = bytearray()
    for b in data:
        if b == ZDLE:
            out.append(ZDLE)
            out.append(ord("l"))    # ZDLEE = 'l' (0x6C)
        elif b in (0x11, 0x13, 0x91, 0x93):
            # XON / XOFF and their high-bit copies - escape so
            # transparent flow control doesn't eat them
            out.append(ZDLE)
            out.append(b ^ 0x40)
        elif b == 0x0D:
            # Some BBS gateways collapse CR; escape it
            out.append(ZDLE)
            out.append(b ^ 0x40)
        else:
            out.append(b)
    return bytes(out)


def _zm_build_hex_header(frame_type: int,
                          flags: tuple = (0, 0, 0, 0)) -> bytes:
    """Hex (text-readable) header. Used by the initial handshake
    so terminals that don't yet know they're in a transfer have
    a chance to dump them safely."""
    body = bytes([frame_type, *flags])
    crc = 0
    # CRC16-XMODEM
    for b in body:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    hexbody = body + bytes([crc >> 8, crc & 0xFF])
    hex_str = hexbody.hex().upper().encode("ascii")
    return bytes([ZPAD, ZPAD, ZDLE, ZHEX]) + hex_str + b"\r\n\x11"


def _zm_build_bin32_header(frame_type: int,
                            flags: tuple = (0, 0, 0, 0)) -> bytes:
    """Binary header with CRC32. Used after the initial handshake
    for all data-bearing frames."""
    body = bytes([frame_type, *flags])
    crc = _zm_crc32(body) ^ 0xFFFFFFFF
    crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF,
                       (crc >> 16) & 0xFF, (crc >> 24) & 0xFF])
    return (bytes([ZPAD, ZDLE, ZBIN32])
            + _zm_escape(body + crc_bytes))


class ZModemReceiver:
    """Minimal ZModem receiver state machine.

    Drives by calling feed(bytes) with incoming wire data and
    polling send_queue for outgoing responses. Files are written
    to `dest_dir` as they arrive.

    State machine is intentionally permissive - we accept
    whatever the sender does and adapt. Real-world senders vary.
    """

    STATE_IDLE = "idle"
    STATE_WAITING_FILE = "waiting_file"
    STATE_RECEIVING = "receiving"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    def __init__(self, dest_dir):
        self.dest_dir = Path(dest_dir)
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        self.state = self.STATE_IDLE
        self._buffer = bytearray()
        self.send_queue = deque()
        self.current_path: Optional[Path] = None
        self._current_fh = None
        self.received_files: list[Path] = []
        self.bytes_received = 0
        self.expected_size = 0
        self.last_error = ""

    def start(self):
        """Send our capabilities. Called once after the user
        confirms a transfer."""
        # ZRINIT with CANFC32 (can do CRC32) + CANFDX (full-duplex)
        # bits set in the flag word.
        self.send_queue.append(
            _zm_build_hex_header(ZRINIT, (0, 0, 0, 0x23)))

    def feed(self, data: bytes):
        """Push received bytes through the state machine."""
        self._buffer.extend(data)
        # Look for frame starts and subpacket data alternately.
        # In a complete implementation we'd parse frames carefully;
        # for the BBS case we look for the well-known signatures.
        while True:
            # Scan for a frame header
            idx = self._buffer.find(bytes([ZPAD, ZDLE]))
            if idx < 0:
                # No header in sight - if we're receiving data,
                # commit to file
                if (self.state == self.STATE_RECEIVING
                        and self._current_fh):
                    # Drop trailing 4 bytes (potential CRC)
                    if len(self._buffer) > 6:
                        self._write_data(bytes(self._buffer[:-6]))
                        del self._buffer[:-6]
                return
            # Drop everything before the header
            if idx > 0:
                # Pre-header bytes are usually noise / "rz\r" text -
                # but in mid-transfer they're data. Best-effort:
                # only consume as data if we're currently receiving.
                if self.state == self.STATE_RECEIVING:
                    self._write_data(bytes(self._buffer[:idx]))
                del self._buffer[:idx]
            if len(self._buffer) < 4:
                return
            # _buffer starts with ZPAD ZDLE
            header_type = self._buffer[2]
            # We only fully parse hex headers for handshake state;
            # binary headers are recognized but we treat them as
            # signals to advance the state machine.
            if header_type == ZHEX:
                end = self._buffer.find(b"\n")
                if end < 0:
                    return
                hex_str = bytes(self._buffer[4:end]).rstrip(b"\r")
                del self._buffer[:end + 1]
                if len(hex_str) >= 4:
                    try:
                        frame_type = int(
                            hex_str[0:2].decode(), 16)
                        self._on_frame_type(frame_type, hex_str)
                    except ValueError:
                        pass
            elif header_type == ZBIN32:
                if len(self._buffer) < 4 + 5:
                    return
                # Frame type is the byte right after the header
                # marker. With ZDLE escapes that's the first byte
                # whose value is not 0x18.
                ft = self._buffer[3]
                if ft == ZDLE and len(self._buffer) > 4:
                    ft = self._buffer[4] ^ 0x40
                self._on_frame_type(ft, None)
                # We can't easily walk binary frames byte-by-byte
                # in this minimal implementation; just drop the
                # known prefix bytes and let the next iteration
                # find data.
                del self._buffer[:8]
            else:
                # ZBIN (CRC16) - drop 7 bytes header and move on
                del self._buffer[:8]

    def _on_frame_type(self, ft, header):
        if ft == ZRQINIT:
            # Sender wants to start - confirm with ZRINIT
            self.send_queue.append(
                _zm_build_hex_header(ZRINIT, (0, 0, 0, 0x23)))
            self.state = self.STATE_WAITING_FILE
        elif ft == ZFILE:
            # Subpacket follows with filename + size. We accept
            # the next chunk as the filename string (NUL-terminated)
            self.state = self.STATE_WAITING_FILE
            # Best-effort filename extraction: look for first
            # printable run after the header
            self._extract_filename_from_buffer()
        elif ft == ZDATA:
            self.state = self.STATE_RECEIVING
        elif ft == ZEOF:
            # End of file - close it and ACK
            if self._current_fh:
                self._current_fh.close()
                self._current_fh = None
                if self.current_path:
                    self.received_files.append(self.current_path)
            self.send_queue.append(
                _zm_build_bin32_header(ZRINIT, (0, 0, 0, 0x23)))
            self.state = self.STATE_WAITING_FILE
        elif ft == ZFIN:
            # End of session - reply ZFIN
            self.send_queue.append(
                _zm_build_hex_header(ZFIN))
            self.send_queue.append(b"OO")  # "over and out"
            self.state = self.STATE_DONE

    def _extract_filename_from_buffer(self):
        """When ZFILE arrives, the next subpacket has the file
        info. Best-effort scan for it."""
        # Look for a printable filename run
        ascii_start = -1
        for i, b in enumerate(self._buffer[:512]):
            if 0x20 <= b < 0x7F:
                ascii_start = i
                break
        if ascii_start < 0:
            return
        end = self._buffer.find(b"\x00", ascii_start)
        if end < 0:
            return
        name = bytes(
            self._buffer[ascii_start:end]
        ).decode("latin-1", errors="replace")
        # Strip any path components - we save to dest_dir/<basename>
        safe = os.path.basename(name)
        if not safe:
            safe = "received"
        self.current_path = self.dest_dir / safe
        try:
            self._current_fh = open(self.current_path, "wb")
        except OSError as e:
            self.last_error = f"open {safe}: {e}"
            self.state = self.STATE_ERROR
            return
        # After the filename block comes ZDATA + chunks
        del self._buffer[:end + 1]
        self.bytes_received = 0

    def _write_data(self, data: bytes):
        """Write a chunk to the current file, unescaping ZDLE."""
        if not self._current_fh:
            return
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == ZDLE and i + 1 < len(data):
                out.append(data[i + 1] ^ 0x40)
                i += 2
            else:
                out.append(b)
                i += 1
        try:
            self._current_fh.write(out)
            self.bytes_received += len(out)
        except OSError as e:
            self.last_error = f"write: {e}"

    def cancel(self):
        """Send the ZModem CAN sequence and close everything."""
        # 8x CAN then 10x backspace clears the receiver too
        self.send_queue.append(bytes([ZDLE] * 8 + [0x08] * 10))
        if self._current_fh:
            try:
                self._current_fh.close()
            except OSError:
                pass
            self._current_fh = None
        self.state = self.STATE_ERROR
        self.last_error = "cancelled by user"


class ZModemSender:
    """Minimal ZModem sender state machine. Sends one file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.send_queue = deque()
        self.state = "init"
        self.bytes_sent = 0
        self.total_size = self.path.stat().st_size
        self.last_error = ""

    def start(self):
        # Kick off with ZRQINIT
        self.send_queue.append(
            _zm_build_hex_header(ZRQINIT))
        self.state = "waiting_rinit"

    def feed(self, data: bytes):
        """Process incoming response bytes from the receiver.

        For a minimal implementation we look for the well-known
        ZRINIT response and then dump the file in one go. Real
        ZModem does sliding window with ACK-per-subpacket; we
        simplify because BBS use cases tolerate this."""
        if self.state == "waiting_rinit":
            # As soon as we see anything that looks like a ZRINIT
            # we proceed. Permissive matching.
            if b"\x18B01" in data or b"\x18B01" in bytes(data):
                self._send_file()
        elif self.state == "waiting_zfin":
            if b"\x18B" in data:   # any ZFIN-ish
                self.send_queue.append(b"OO")
                self.state = "done"

    def _send_file(self):
        # Build ZFILE header
        self.send_queue.append(
            _zm_build_bin32_header(ZFILE))
        # File info subpacket: name\0size mtime mode files left
        info = (f"{self.path.name}\x00"
                f"{self.total_size} 0 0 0\x00").encode("latin-1")
        # Frame end marker
        crc = _zm_crc32(info + bytes([ZCRCW])) ^ 0xFFFFFFFF
        crc_bytes = bytes([crc & 0xFF, (crc >> 8) & 0xFF,
                           (crc >> 16) & 0xFF, (crc >> 24) & 0xFF])
        self.send_queue.append(
            _zm_escape(info) + bytes([ZDLE, ZCRCW])
            + _zm_escape(crc_bytes))
        # ZDATA + the actual bytes
        self.send_queue.append(
            _zm_build_bin32_header(ZDATA))
        try:
            with open(self.path, "rb") as f:
                while True:
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    # Each subpacket: data + ZCRCG marker + CRC32
                    crc = (
                        _zm_crc32(chunk + bytes([ZCRCG]))
                        ^ 0xFFFFFFFF)
                    crc_bytes = bytes([
                        crc & 0xFF, (crc >> 8) & 0xFF,
                        (crc >> 16) & 0xFF, (crc >> 24) & 0xFF,
                    ])
                    self.send_queue.append(
                        _zm_escape(chunk)
                        + bytes([ZDLE, ZCRCG])
                        + _zm_escape(crc_bytes))
                    self.bytes_sent += len(chunk)
        except OSError as e:
            self.last_error = f"read: {e}"
            self.state = "error"
            return
        # ZEOF
        self.send_queue.append(
            _zm_build_bin32_header(ZEOF))
        # ZFIN
        self.send_queue.append(
            _zm_build_hex_header(ZFIN))
        self.state = "waiting_zfin"

    def cancel(self):
        self.send_queue.append(bytes([ZDLE] * 8 + [0x08] * 10))
        self.state = "cancelled"


def _detect_zmodem_start(buffer: bytes) -> Optional[int]:
    """Return the index where a ZModem handshake begins, or None.

    The unique signature is "**\x18B00" (download offer) or
    "rz\r**\x18B00". Returns the index of the leading `**`."""
    idx = buffer.find(b"**\x18B00")
    if idx >= 0:
        return idx
    return None


# ===========================================================
# Terminal screen - state + cell buffer
# ===========================================================


@dataclass
class _Cell:
    ch: str = " "
    fg: int = DEFAULT_FG
    bg: int = DEFAULT_BG
    bold: bool = False
    inverse: bool = False
    # PETSCII screen mode only. We store BOTH the raw PETSCII byte
    # (`petscii`) and the derived C64 screencode (`screencode`):
    #
    # - `petscii` indexes into the Style C64 Pro Mono font's PUA
    #   layout: E000+petscii = upper/graphics charset, E100+petscii
    #   = lower/upper charset, E200/E300 for the reverse halves.
    #   This is what the font wants if it's installed.
    #
    # - `screencode` indexes into the raw chargen.bin ROM bitmap
    #   tables. CGTerm-style conversion via petscii_to_screencode().
    #   This is what we draw with by hand when the font isn't
    #   available.
    #
    # Both default to -1 (unset, e.g. on empty/cleared cells).
    petscii: int = -1
    screencode: int = -1
    charset: str = "lower"


class _TerminalScreen:
    """Fixed-grid VT100/ANSI emulator.

    Public surface:
      feed(bytes)       -> push bytes from the wire
      cells[r][c]       -> read a cell for rendering
      cursor_row/col    -> current cursor position
      dirty             -> set of (row, col) since last paint
      reset()           -> wipe and reset attributes
    """

    def __init__(self, rows=25, cols=80, encoding='cp437'):
        self.rows = rows
        self.cols = cols
        self.encoding = encoding
        self.cells: list[list[_Cell]] = []
        self.cursor_row = 0
        self.cursor_col = 0
        # Saved cursor (DECSC / DECRC)
        self._saved_cur = (0, 0)
        self.fg = DEFAULT_FG
        self.bg = DEFAULT_BG
        self.bold = False
        self.inverse = False
        # Parser state
        self._parse_state = "ground"
        self._esc_buf = bytearray()
        # Scroll region (1-based, inclusive) - DECSTBM
        self.scroll_top = 0
        self.scroll_bot = rows - 1
        # Decoder for non-ASCII bytes
        self._byte_buffer = bytearray()
        # Dirty tracking - paint only changed cells
        self.dirty: set[tuple[int, int]] = set()
        self.reset()

    def reset(self):
        self.cells = [
            [_Cell() for _ in range(self.cols)]
            for _ in range(self.rows)
        ]
        self.cursor_row = 0
        self.cursor_col = 0
        self.fg = DEFAULT_FG
        self.bg = DEFAULT_BG
        self.bold = False
        self.inverse = False
        self.scroll_top = 0
        self.scroll_bot = self.rows - 1
        self.dirty = {(r, c) for r in range(self.rows)
                      for c in range(self.cols)}

    def resize(self, rows, cols):
        new = [[_Cell() for _ in range(cols)] for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            for c in range(min(cols, self.cols)):
                new[r][c] = self.cells[r][c]
        self.cells = new
        self.rows = rows
        self.cols = cols
        self.scroll_top = 0
        self.scroll_bot = rows - 1
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(self.cursor_col, cols - 1)
        self.dirty = {(r, c) for r in range(rows)
                      for c in range(cols)}

    # ---- input ----------------------------------------------

    def feed(self, data: bytes):
        """Push wire-bytes through the parser."""
        for byte in data:
            self._feed_byte(byte)

    def _feed_byte(self, b: int):
        st = self._parse_state
        if st == "ground":
            if b == 0x1B:   # ESC
                self._parse_state = "esc"
                self._esc_buf.clear()
                return
            if b == 0x0D:    # CR
                # Reset any pending wrap from a previous full-row
                # write before resetting cursor_col. This is part of
                # the xterm/vt220 "xenl" behavior: a CR after the
                # 80th char of a row sets cursor to col 0 of THIS
                # row, not col 0 of the next.
                if self.cursor_col >= self.cols:
                    self.cursor_col = self.cols - 1
                self.cursor_col = 0
                return
            if b == 0x0A:    # LF
                # If we have a pending wrap, the LF *consumes* it
                # (also xenl). The cursor moves to the next line
                # at col 0, not one beyond.
                if self.cursor_col >= self.cols:
                    self.cursor_col = 0
                self._line_feed()
                return
            if b == 0x08:    # BS
                if self.cursor_col > 0:
                    self.cursor_col -= 1
                return
            if b == 0x09:    # TAB
                next_tab = (self.cursor_col // 8 + 1) * 8
                self.cursor_col = min(next_tab, self.cols - 1)
                return
            if b == 0x07:    # BEL
                # We just ignore the bell. Could play a sound but
                # it's annoying in BBS scrollback.
                return
            if b < 0x20:
                # Other C0 controls - drop silently
                return
            # Printable: decode using current encoding
            self._byte_buffer.append(b)
            self._flush_printable()
            return
        if st == "esc":
            if b == ord('['):
                self._parse_state = "csi"
                self._esc_buf.clear()
                return
            if b == ord(']'):
                # OSC - operating system command (window title etc)
                self._parse_state = "osc"
                self._esc_buf.clear()
                return
            if b == ord('('):
                # Select character set G0 - eat one byte
                self._parse_state = "scs"
                return
            if b == ord(')'):
                self._parse_state = "scs"
                return
            if b == ord('='):
                # Application keypad - ignore
                self._parse_state = "ground"
                return
            if b == ord('>'):
                # Normal keypad - ignore
                self._parse_state = "ground"
                return
            if b == ord('7'):
                # DECSC - save cursor
                self._saved_cur = (
                    self.cursor_row, self.cursor_col)
                self._parse_state = "ground"
                return
            if b == ord('8'):
                # DECRC - restore cursor
                self.cursor_row, self.cursor_col = self._saved_cur
                self._parse_state = "ground"
                return
            if b == ord('D'):
                # IND - index (line feed)
                self._line_feed()
                self._parse_state = "ground"
                return
            if b == ord('M'):
                # RI - reverse index
                if self.cursor_row > self.scroll_top:
                    self.cursor_row -= 1
                else:
                    self._scroll_down()
                self._parse_state = "ground"
                return
            if b == ord('c'):
                # RIS - full reset
                self.reset()
                self._parse_state = "ground"
                return
            # Unknown escape - drop
            self._parse_state = "ground"
            return
        if st == "scs":
            # Discard the G0/G1 charset designator byte
            self._parse_state = "ground"
            return
        if st == "csi":
            # CSI accumulates until a final byte 0x40..0x7E
            if 0x30 <= b <= 0x3F:
                self._esc_buf.append(b)
                return
            if 0x20 <= b <= 0x2F:
                self._esc_buf.append(b)
                return
            if 0x40 <= b <= 0x7E:
                self._execute_csi(bytes(self._esc_buf), b)
                self._parse_state = "ground"
                return
            # Invalid - abort
            self._parse_state = "ground"
            return
        if st == "osc":
            # OSC ends at BEL (0x07) or ST (ESC \). We ignore the
            # contents entirely - just collect until terminator.
            if b == 0x07:
                self._parse_state = "ground"
                return
            if b == 0x1B:
                self._parse_state = "osc_esc"
                return
            self._esc_buf.append(b)
            return
        if st == "osc_esc":
            # Expecting backslash to complete ST
            self._parse_state = "ground"
            return

    def _flush_printable(self):
        """Convert accumulated bytes to text using the configured
        encoding and write them at the cursor."""
        try:
            text = self._byte_buffer.decode(self.encoding,
                                              errors="replace")
        except LookupError:
            text = self._byte_buffer.decode("latin-1",
                                              errors="replace")
        self._byte_buffer.clear()
        for ch in text:
            self._put_char(ch)

    def _put_char(self, ch: str):
        if self.cursor_col >= self.cols:
            # Auto-wrap
            self.cursor_col = 0
            self._line_feed()
        r, c = self.cursor_row, self.cursor_col
        if 0 <= r < self.rows and 0 <= c < self.cols:
            cell = self.cells[r][c]
            cell.ch = ch
            cell.fg = self.fg
            cell.bg = self.bg
            cell.bold = self.bold
            cell.inverse = self.inverse
            self.dirty.add((r, c))
        self.cursor_col += 1

    def _line_feed(self):
        if self.cursor_row < self.scroll_bot:
            self.cursor_row += 1
        else:
            self._scroll_up()

    def _scroll_up(self):
        # Shift rows [top+1..bot] up by one, blank the last
        for r in range(self.scroll_top, self.scroll_bot):
            self.cells[r] = self.cells[r + 1]
            for c in range(self.cols):
                self.dirty.add((r, c))
        self.cells[self.scroll_bot] = [_Cell() for _ in range(self.cols)]
        for c in range(self.cols):
            self.dirty.add((self.scroll_bot, c))

    def _scroll_down(self):
        for r in range(self.scroll_bot, self.scroll_top, -1):
            self.cells[r] = self.cells[r - 1]
            for c in range(self.cols):
                self.dirty.add((r, c))
        self.cells[self.scroll_top] = [_Cell() for _ in range(self.cols)]
        for c in range(self.cols):
            self.dirty.add((self.scroll_top, c))

    def _execute_csi(self, params: bytes, final: int):
        """Dispatch a CSI sequence."""
        # Parse semicolon-separated decimal params, treating empty
        # as the function's default.
        is_private = params.startswith(b"?")
        if is_private:
            params = params[1:]
        try:
            text = params.decode("ascii", errors="replace")
        except Exception:
            text = ""
        parts = text.split(";") if text else []
        nums = []
        for p in parts:
            try:
                nums.append(int(p) if p else 0)
            except ValueError:
                nums.append(0)

        def n(i, default=0):
            return nums[i] if i < len(nums) else default

        final_ch = chr(final)
        if final_ch == 'm':
            # SGR
            if not nums:
                nums = [0]
            self._handle_sgr(nums)
        elif final_ch == 'H' or final_ch == 'f':
            # CUP - cursor position. 1-based.
            r = max(1, n(0, 1))
            c = max(1, n(1, 1))
            self.cursor_row = min(r - 1, self.rows - 1)
            self.cursor_col = min(c - 1, self.cols - 1)
        elif final_ch == 'A':
            self.cursor_row = max(0, self.cursor_row - max(1, n(0, 1)))
        elif final_ch == 'B':
            self.cursor_row = min(self.rows - 1,
                                    self.cursor_row + max(1, n(0, 1)))
        elif final_ch == 'C':
            self.cursor_col = min(self.cols - 1,
                                    self.cursor_col + max(1, n(0, 1)))
        elif final_ch == 'D':
            self.cursor_col = max(0, self.cursor_col - max(1, n(0, 1)))
        elif final_ch == 'G':
            self.cursor_col = min(self.cols - 1, max(0, n(0, 1) - 1))
        elif final_ch == 'd':
            self.cursor_row = min(self.rows - 1, max(0, n(0, 1) - 1))
        elif final_ch == 'J':
            # ED - erase in display
            mode = n(0, 0)
            self._erase_display(mode)
        elif final_ch == 'K':
            # EL - erase in line
            mode = n(0, 0)
            self._erase_line(mode)
        elif final_ch == 'L':
            # IL - insert lines
            self._insert_lines(max(1, n(0, 1)))
        elif final_ch == 'M':
            # DL - delete lines
            self._delete_lines(max(1, n(0, 1)))
        elif final_ch == 'P':
            # DCH - delete characters
            self._delete_chars(max(1, n(0, 1)))
        elif final_ch == '@':
            # ICH - insert characters
            self._insert_chars(max(1, n(0, 1)))
        elif final_ch == 's':
            self._saved_cur = (self.cursor_row, self.cursor_col)
        elif final_ch == 'u':
            self.cursor_row, self.cursor_col = self._saved_cur
        elif final_ch == 'r':
            # DECSTBM - set scroll region
            top = max(1, n(0, 1)) - 1
            bot = max(1, n(1, self.rows)) - 1
            if top < bot < self.rows:
                self.scroll_top = top
                self.scroll_bot = bot
                self.cursor_row = 0
                self.cursor_col = 0
        # Other sequences (h, l, n, c, q, x, ...) - silently ignored
        # to avoid breaking the screen. These tend to be terminal
        # mode toggles, status reports, and DEC-specific options
        # that BBS / Linux box use rarely.

    def _handle_sgr(self, nums):
        """Select Graphic Rendition - colors and attributes."""
        i = 0
        while i < len(nums):
            v = nums[i]
            if v == 0:
                self.fg = DEFAULT_FG
                self.bg = DEFAULT_BG
                self.bold = False
                self.inverse = False
            elif v == 1:
                self.bold = True
            elif v == 22:
                self.bold = False
            elif v == 7:
                self.inverse = True
            elif v == 27:
                self.inverse = False
            elif 30 <= v <= 37:
                self.fg = v - 30
            elif v == 39:
                self.fg = DEFAULT_FG
            elif 40 <= v <= 47:
                self.bg = v - 40
            elif v == 49:
                self.bg = DEFAULT_BG
            elif 90 <= v <= 97:
                self.fg = v - 90 + 8
            elif 100 <= v <= 107:
                self.bg = v - 100 + 8
            elif v == 38 and i + 2 < len(nums) and nums[i + 1] == 5:
                # 256-color FG - clamp to 16 by quick mod
                self.fg = nums[i + 2] % 16
                i += 2
            elif v == 48 and i + 2 < len(nums) and nums[i + 1] == 5:
                self.bg = nums[i + 2] % 16
                i += 2
            i += 1

    def _erase_display(self, mode):
        if mode == 0:
            # From cursor to end
            r, c = self.cursor_row, self.cursor_col
            for cc in range(c, self.cols):
                self.cells[r][cc] = _Cell(bg=self.bg)
                self.dirty.add((r, cc))
            for rr in range(r + 1, self.rows):
                self.cells[rr] = [_Cell(bg=self.bg)
                                  for _ in range(self.cols)]
                for cc in range(self.cols):
                    self.dirty.add((rr, cc))
        elif mode == 1:
            # From start to cursor
            r, c = self.cursor_row, self.cursor_col
            for rr in range(r):
                self.cells[rr] = [_Cell(bg=self.bg)
                                  for _ in range(self.cols)]
                for cc in range(self.cols):
                    self.dirty.add((rr, cc))
            for cc in range(c + 1):
                self.cells[r][cc] = _Cell(bg=self.bg)
                self.dirty.add((r, cc))
        else:
            # 2 or 3 - full clear
            self.cells = [
                [_Cell(bg=self.bg) for _ in range(self.cols)]
                for _ in range(self.rows)
            ]
            for r in range(self.rows):
                for c in range(self.cols):
                    self.dirty.add((r, c))

    def _erase_line(self, mode):
        r = self.cursor_row
        if mode == 0:
            for c in range(self.cursor_col, self.cols):
                self.cells[r][c] = _Cell(bg=self.bg)
                self.dirty.add((r, c))
        elif mode == 1:
            for c in range(self.cursor_col + 1):
                self.cells[r][c] = _Cell(bg=self.bg)
                self.dirty.add((r, c))
        else:
            self.cells[r] = [_Cell(bg=self.bg)
                             for _ in range(self.cols)]
            for c in range(self.cols):
                self.dirty.add((r, c))

    def _insert_lines(self, n):
        r = self.cursor_row
        if r < self.scroll_top or r > self.scroll_bot:
            return
        n = min(n, self.scroll_bot - r + 1)
        for _ in range(n):
            for rr in range(self.scroll_bot, r, -1):
                self.cells[rr] = self.cells[rr - 1]
            self.cells[r] = [_Cell(bg=self.bg)
                             for _ in range(self.cols)]
        for rr in range(r, self.scroll_bot + 1):
            for cc in range(self.cols):
                self.dirty.add((rr, cc))

    def _delete_lines(self, n):
        r = self.cursor_row
        if r < self.scroll_top or r > self.scroll_bot:
            return
        n = min(n, self.scroll_bot - r + 1)
        for _ in range(n):
            for rr in range(r, self.scroll_bot):
                self.cells[rr] = self.cells[rr + 1]
            self.cells[self.scroll_bot] = [
                _Cell(bg=self.bg) for _ in range(self.cols)]
        for rr in range(r, self.scroll_bot + 1):
            for cc in range(self.cols):
                self.dirty.add((rr, cc))

    def _delete_chars(self, n):
        r = self.cursor_row
        c = self.cursor_col
        n = min(n, self.cols - c)
        line = self.cells[r]
        for i in range(c, self.cols - n):
            line[i] = line[i + n]
        for i in range(self.cols - n, self.cols):
            line[i] = _Cell(bg=self.bg)
        for cc in range(c, self.cols):
            self.dirty.add((r, cc))

    def _insert_chars(self, n):
        r = self.cursor_row
        c = self.cursor_col
        n = min(n, self.cols - c)
        line = self.cells[r]
        for i in range(self.cols - 1, c + n - 1, -1):
            line[i] = line[i - n]
        for i in range(c, c + n):
            line[i] = _Cell(bg=self.bg)
        for cc in range(c, self.cols):
            self.dirty.add((r, cc))


# ===========================================================
# PETSCII screen - C64-style terminal
# ===========================================================


# Direct PETSCII -> screen position color code mapping. The C64
# uses bytes 0x05/0x1C/0x1E/0x1F + 0x81/0x90-0x9F for color
# control. The terminal maps each to one of the 16 VIC-II colors.
_PETSCII_COLORS = {
    0x05: 1,    # white
    0x90: 0,    # black
    0x1C: 2,    # red
    0x1E: 5,    # green
    0x1F: 6,    # blue
    0x81: 8,    # orange
    0x95: 9,    # brown
    0x96: 10,   # light red
    0x97: 11,   # dark grey
    0x98: 12,   # medium grey
    0x99: 13,   # light green
    0x9A: 14,   # light blue
    0x9B: 15,   # light grey
    0x9C: 4,    # purple
    0x9E: 7,    # yellow
    0x9F: 3,    # cyan
}

# 16-entry C64 VIC-II palette in 0..15 color order
C64_VIC2_PALETTE = [
    QColor(0x00, 0x00, 0x00),  # 0  black
    QColor(0xFF, 0xFF, 0xFF),  # 1  white
    QColor(0x88, 0x39, 0x32),  # 2  red
    QColor(0x67, 0xB6, 0xBD),  # 3  cyan
    QColor(0x8B, 0x3F, 0x96),  # 4  purple
    QColor(0x55, 0xA0, 0x49),  # 5  green
    QColor(0x40, 0x31, 0x8D),  # 6  blue
    QColor(0xBF, 0xCE, 0x72),  # 7  yellow
    QColor(0x8B, 0x54, 0x29),  # 8  orange
    QColor(0x57, 0x42, 0x00),  # 9  brown
    QColor(0xB8, 0x69, 0x62),  # 10 light red
    QColor(0x50, 0x50, 0x50),  # 11 dark grey
    QColor(0x78, 0x78, 0x78),  # 12 medium grey
    QColor(0x94, 0xE0, 0x89),  # 13 light green
    QColor(0x78, 0x69, 0xC4),  # 14 light blue
    QColor(0x9F, 0x9F, 0x9F),  # 15 light grey
]


# ============================================================
# C64 chargen ROM loader for pixel-perfect PETSCII rendering
# ============================================================
#
# A C64 chargen.bin file is 4096 bytes laid out as:
#   - first 2048 bytes (256 chars * 8 bytes) = upper/graphics
#   - second 2048 bytes (256 chars * 8 bytes) = lower/upper
# Each char is 8x8 1-bit pixels - bit 7 = leftmost pixel.
#
# We load the ROM once at module import time and cache the
# expanded per-pixel bool tables. With the ROM cached we can
# render any PETSCII screen by indexing screencode -> 64 bools.
#
# If no chargen ROM is found (user didn't drop the ROMs into
# roms/), we silently fall back to the Unicode-glyph renderer.
# The .png output then depends on whatever Qt font is set.

_CHARGEN_CACHE = {'rom_bytes': None, 'upper': None, 'lower': None}


def _load_chargen_rom():
    """Find a C64 chargen ROM. Returns the 4096-byte blob or None.

    Search order:
        1. <bundle>/roms/chargen*.bin (preferred - shipped with
           the build)
        2. <bundle>/quopus_lib/c64_chargen.bin (legacy location
           used by other tools)
        3. ~/.vice/C64/chargen, etc - the same paths the SID
           player's rom_finder.py checks
    """
    try:
        from .config import BUNDLE_DIR
    except ImportError:
        return None
    candidates = []
    # 1. roms/ directory
    roms_dir = BUNDLE_DIR / "roms"
    if roms_dir.is_dir():
        for f in roms_dir.iterdir():
            if (f.is_file() and "chargen" in f.name.lower()
                    and f.stat().st_size == 4096):
                candidates.append(f)
    # 2. Legacy in-quopus_lib chargen
    legacy = BUNDLE_DIR / "quopus_lib" / "c64_chargen.bin"
    if legacy.is_file() and legacy.stat().st_size == 4096:
        candidates.append(legacy)
    # 3. VICE / sidplayfp install paths
    for vice_path in [
        Path.home() / ".vice" / "C64" / "chargen",
        Path("/usr/share/vice/C64/chargen"),
        Path("/usr/lib/vice/C64/chargen"),
        Path("/usr/local/share/vice/C64/chargen"),
        Path("C:/Program Files/WinVICE/C64/chargen"),
        Path("C:/Program Files (x86)/WinVICE/C64/chargen"),
    ]:
        if vice_path.is_file() and vice_path.stat().st_size == 4096:
            candidates.append(vice_path)
    for c in candidates:
        try:
            return c.read_bytes()
        except OSError:
            continue
    return None


def _get_chargen():
    """Return (upper_table, lower_table) where each is a list of
    256 entries, each entry is a list of 8 bytes (one per row).

    Cached on first call. Returns (None, None) if no ROM."""
    if _CHARGEN_CACHE['rom_bytes'] is None:
        rom = _load_chargen_rom()
        if rom is None or len(rom) != 4096:
            return None, None
        _CHARGEN_CACHE['rom_bytes'] = rom
        # First half = upper/graphics, second half = lower/upper.
        # Each char = 8 bytes. We store as list of bytearrays so
        # the renderer can index directly: chargen[code][row] gives
        # a byte where bit 7 = leftmost pixel.
        upper = [rom[i * 8:i * 8 + 8] for i in range(256)]
        lower = [rom[2048 + i * 8:2048 + i * 8 + 8]
                 for i in range(256)]
        _CHARGEN_CACHE['upper'] = upper
        _CHARGEN_CACHE['lower'] = lower
    return _CHARGEN_CACHE['upper'], _CHARGEN_CACHE['lower']


class _PetsciiScreen(_TerminalScreen):
    """Terminal that interprets a stream of PETSCII bytes from a
    C64 BBS instead of ANSI escapes.

    Same cell-buffer / dirty-tracking interface as the base class
    so the widget paints both the same way. The differences:

    - Color is driven by inline PETSCII control codes (0x05/0x1C/
      0x9X) rather than CSI SGR sequences. We map them through
      _PETSCII_COLORS to a 16-entry palette indexed into
      C64_VIC2_PALETTE (which the widget knows about).
    - Cursor controls: $11 down, $1D right, $91 up, $9D left,
      $13 home, $93 clear-home, $14 delete/insert.
    - Reverse video toggles on $12 (RVS ON) and $92 (RVS OFF).
    - Character translation goes through petscii_byte_to_unicode()
      so glyphs render approximately right with any font - or
      perfectly with C64 Pro Mono / PETSCII Charset 64.
    """

    def __init__(self, rows=25, cols=40, charset='lower'):
        # Default to 40 cols which is what a C64 actually has.
        # Some BBSes negotiate 80 via term type if the user has
        # an 80col cartridge - those will set cols=80 explicitly.
        #
        # Default charset is 'lower' (lower/upper mixed mode) -
        # which is what every modern C64 BBS expects. The original
        # default on the C64 itself is 'upper' (upper/graphics),
        # but BBSes universally switch to lower via PETSCII $0E
        # right after CONNECT. Defaulting to 'lower' means we get
        # the right rendering even if the BBS skips the switch.
        super().__init__(rows=rows, cols=cols, encoding='petscii')
        self.charset = charset    # 'upper' or 'lower'

    def feed(self, data: bytes):
        """Process a stream of PETSCII bytes.

        Each printable byte is converted to a C64 screencode (via
        the CGTerm-exact petscii_to_screencode() table) and stored
        in the target cell along with the active charset. The
        widget paints from the chargen ROM bitmap, giving pixel-
        accurate rendering regardless of which font Qt would
        otherwise use.

        Control bytes (color codes, RVS, charset switch, cursor
        movement, screen clear) are interpreted inline as on a
        real C64 - no escape sequences."""
        from .petscii_tables import (
            petscii_byte_to_unicode, petscii_to_screencode,
        )
        for b in data:
            # Color control codes
            if b in _PETSCII_COLORS:
                self.fg = _PETSCII_COLORS[b]
                continue
            # Reverse on/off
            if b == 0x12:
                self.inverse = True
                continue
            if b == 0x92:
                self.inverse = False
                continue
            # Charset switch
            if b == 0x0E:
                self.charset = 'lower'
                continue
            if b == 0x8E:
                self.charset = 'upper'
                continue
            # Cursor / screen control
            if b == 0x0D:
                self.cursor_col = 0
                if self.cursor_row < self.rows - 1:
                    self.cursor_row += 1
                else:
                    self._scroll_up()
                continue
            if b == 0x11:
                # Down
                if self.cursor_row < self.rows - 1:
                    self.cursor_row += 1
                else:
                    self._scroll_up()
                continue
            if b == 0x91:
                # Up
                if self.cursor_row > 0:
                    self.cursor_row -= 1
                continue
            if b == 0x1D:
                # Right
                if self.cursor_col < self.cols - 1:
                    self.cursor_col += 1
                else:
                    self.cursor_col = 0
                    if self.cursor_row < self.rows - 1:
                        self.cursor_row += 1
                continue
            if b == 0x9D:
                # Left
                if self.cursor_col > 0:
                    self.cursor_col -= 1
                else:
                    if self.cursor_row > 0:
                        self.cursor_row -= 1
                        self.cursor_col = self.cols - 1
                continue
            if b == 0x13:
                # Home
                self.cursor_row = 0
                self.cursor_col = 0
                continue
            if b == 0x93:
                # Clear-home
                self.cursor_row = 0
                self.cursor_col = 0
                for r in range(self.rows):
                    self.cells[r] = [_Cell(bg=self.bg)
                                     for _ in range(self.cols)]
                    for c in range(self.cols):
                        self.dirty.add((r, c))
                continue
            if b == 0x14 or b == 0x94:
                # Delete (0x14) / Insert (0x94)
                if b == 0x14 and self.cursor_col > 0:
                    self.cursor_col -= 1
                    self.cells[self.cursor_row][self.cursor_col] = (
                        _Cell(bg=self.bg))
                    self.dirty.add(
                        (self.cursor_row, self.cursor_col))
                continue
            if b == 0x07 or b == 0x00:
                # Bell / NUL - drop
                continue
            # Strict printable range filter, matching CGTerm
            # kernal.c logic (line 204):
            #   printable = (b >= 0x20 && b <= 0x7F) || b >= 0xA0
            # Bytes 0x80..0x9F that survived the control-code
            # checks above are unhandled controls - dropping them
            # avoids garbage glyphs like the spurious 'X' marks at
            # the left column from byte 0x18 (CAN), 0x1A (SUB)
            # etc that the BBS may leak.
            if not ((0x20 <= b <= 0x7F) or b >= 0xA0):
                continue
            # Convert to screencode + glyph.
            # We store the raw PETSCII byte (for the C64 Pro Mono
            # PUA lookup), the screencode (for the ROM bitmap
            # fallback), and the unicode fallback. Each renderer
            # path picks what it needs.
            try:
                ch = petscii_byte_to_unicode(b, self.charset)
            except Exception:
                ch = chr(b) if 0x20 <= b < 0x7F else "?"
            try:
                sc = petscii_to_screencode(b)
            except Exception:
                sc = -1
            self._put_petscii_char(ch, sc, b)

    def _put_petscii_char(self, ch, screencode, petscii_byte):
        """Drop a char into the active cell honoring auto-wrap.
        Records the unicode fallback, the screencode (for ROM-bitmap
        path) AND the raw PETSCII byte (for C64-Pro-Mono PUA
        path) so the paint code can pick the best renderer at
        runtime."""
        if self.cursor_col >= self.cols:
            self.cursor_col = 0
            if self.cursor_row < self.rows - 1:
                self.cursor_row += 1
            else:
                self._scroll_up()
        r, c = self.cursor_row, self.cursor_col
        if 0 <= r < self.rows and 0 <= c < self.cols:
            cell = self.cells[r][c]
            cell.ch = ch
            cell.fg = self.fg
            cell.bg = self.bg
            cell.bold = False
            cell.inverse = self.inverse
            cell.petscii = petscii_byte
            cell.screencode = screencode
            cell.charset = self.charset
            self.dirty.add((r, c))
        self.cursor_col += 1


class _TerminalWidget(QWidget):
    """Renders the terminal cell grid and sends keystrokes."""
    key_pressed = pyqtSignal(bytes)

    def __init__(self, screen: _TerminalScreen,
                 font_family="Courier New", font_size=14,
                 parent=None):
        super().__init__(parent)
        self.screen = screen
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.set_font(font_family, font_size)
        # Backspace mode - what byte to send for the BS key.
        # Default 0x7F (DEL) matches Unix expectations; some
        # ancient BBSes want 0x08 (BS).
        self.backspace_byte = 0x7F
        self.local_echo = False
        self.crlf_mode = "cr"  # send CR / CRLF / LF for Enter
        # Macros: dict {"F1": "command\r", ...} - checked on every
        # key press BEFORE default handling. Bound by the dialog.
        self.macros: dict = {}

    def set_font(self, family, size):
        f = QFont(family, size)
        f.setFixedPitch(True)
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(f)
        fm = QFontMetrics(f)
        # PETSCII uses 8x8 pixel glyphs that should butt up against
        # each other with zero gap - any padding produces a visible
        # grid pattern between cells. ANSI uses proportional or
        # variable-pitch fonts where a 1px safety margin avoids
        # last-column clipping on platforms with quirky font
        # metrics. We pick the right strategy per screen type.
        is_petscii = isinstance(self.screen, _PetsciiScreen)
        if is_petscii:
            # PETSCII glyphs come from the C64 chargen ROM as 8x8
            # 1-bit bitmaps. We want the rendered cell to be an
            # integer multiple of 8 in both dimensions so each
            # source pixel maps to N>=1 destination pixels with
            # no aliasing - any non-integer scale produces uneven
            # row heights and visible interpolation seams.
            #
            # Strategy: take the font size as an approximate
            # target cell height (a size=14 request means "I want
            # roughly 14px tall chars"), round to the nearest
            # multiple of 8, and use that.
            target_h = max(fm.ascent() + fm.descent(),
                           fm.horizontalAdvance("M"))
            zoom = max(1, round(target_h / 8))
            self.cell_w = 8 * zoom
            self.cell_h = 8 * zoom
        else:
            # ANSI: be generous on width to prevent last-column
            # clipping. Pick widest of several wide chars + 1px
            # safety margin.
            widths = [fm.horizontalAdvance(ch)
                      for ch in ("M", "W", "@", "_")]
            widths.append(fm.averageCharWidth())
            self.cell_w = max(w for w in widths if w > 0) + 1
            # tightBoundingRect can return 0 for some pixel fonts -
            # fall back to lineSpacing which is always sane.
            self.cell_h = max(fm.lineSpacing(),
                              fm.ascent() + fm.descent())
        self.ascent = fm.ascent()
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.cell_w * self.screen.cols,
                     self.cell_h * self.screen.rows)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, ev):
        p = QPainter(self)
        scr = self.screen
        # Choose the palette based on which screen subclass we're
        # rendering. PETSCII screens use C64 VIC-II colors; ANSI
        # screens use the xterm-ish 16-color set.
        is_petscii = isinstance(scr, _PetsciiScreen)
        if is_petscii:
            palette = C64_VIC2_PALETTE
            bg_default = palette[0]    # black
        else:
            palette = ANSI_PALETTE
            bg_default = palette[DEFAULT_BG]
        # Background fill (full repaint - we redraw every cell)
        p.fillRect(self.rect(), bg_default)
        p.setFont(self.font())

        # PETSCII mode renderer selection. Triple fallback,
        # in PREFERENCE order (best result first):
        #
        #   1. C64 chargen ROM (best): we have the 4 KB ROM
        #      cached, so we can paint 8x8 1-bit bitmaps scaled
        #      to the cell size by hand. Pixel-perfect, no
        #      vertical-padding artifacts because we control
        #      exactly which pixels get drawn. This is the
        #      preferred path when chargen.bin ships with Quopus.
        #
        #   2. C64 Pro Mono font: looks great at the "designed"
        #      sizes but has ~1px of vertical padding in each
        #      glyph that shows up as a thin black stripe between
        #      consecutive rows of reverse-video text. Used as
        #      fallback when no ROM is on disk.
        #
        #   3. Unicode glyphs (final fallback): generic monospace
        #      font + approximate Unicode characters from the
        #      PETSCII tables. Works but boxes and shifted
        #      characters look wrong.
        chargen_upper = chargen_lower = None
        use_c64_font = False
        if is_petscii:
            chargen_upper, chargen_lower = _get_chargen()
            # Only try the font path if ROM isn't available -
            # ROM rendering has no inter-row gap artifact.
            if chargen_upper is None:
                fam = self.font().family()
                if fam in ("C64 Pro Mono", "C64 Pro"):
                    use_c64_font = True
        use_rom = is_petscii and chargen_upper is not None

        for r in range(scr.rows):
            for c in range(scr.cols):
                cell = scr.cells[r][c]
                fg_idx = cell.fg
                bg_idx = cell.bg
                if cell.bold and fg_idx < 8:
                    fg_idx += 8   # use bright variant (ANSI only)
                if cell.inverse:
                    fg_idx, bg_idx = bg_idx, fg_idx
                # Clamp into palette range - PETSCII screens can
                # request indices 0..15 too
                fg = palette[fg_idx % len(palette)]
                bg = palette[bg_idx % len(palette)]
                x = c * self.cell_w
                y = r * self.cell_h
                if bg != bg_default:
                    p.fillRect(x, y, self.cell_w, self.cell_h, bg)

                # Render the glyph itself.
                if use_c64_font and cell.petscii >= 0:
                    # Style C64 Pro Mono path. The font's PUA
                    # layout is documented at
                    #   https://style64.org/c64-truetype
                    # and maps PETSCII (not screencode!) bytes:
                    #
                    #   0xE000 + petscii -> upper/graphics (unshifted)
                    #   0xE100 + petscii -> lower/upper (shifted)
                    #   0xE200 + petscii -> upper REVERSE
                    #   0xE300 + petscii -> lower REVERSE
                    #
                    # The font has dedicated RVS glyphs (E200/E300
                    # halves) so we let it do the inversion
                    # instead of swapping fg/bg in software. That
                    # means we need to UNDO the fg/bg swap we did
                    # at the top of the cell loop for ANSI mode -
                    # the RVS glyph is already pre-inverted.
                    if cell.inverse:
                        # Undo the top-swap; restore fg=foreground,
                        # bg=background. We already painted the
                        # swapped bg above so do not repaint.
                        true_fg = palette[cell.fg % len(palette)]
                        true_bg = palette[cell.bg % len(palette)]
                        # Top of loop drew bg as fg_color (swapped),
                        # which is what we WANTED for ANSI but not
                        # here. Repaint the cell with the proper
                        # bg so the RVS glyph composites cleanly
                        # against it.
                        if true_bg != bg_default:
                            p.fillRect(x, y, self.cell_w,
                                          self.cell_h, true_bg)
                        else:
                            # Need to clear what we painted before
                            p.fillRect(x, y, self.cell_w,
                                          self.cell_h, bg_default)
                        fg = true_fg
                    base = 0xE100 if cell.charset == 'lower' else 0xE000
                    if cell.inverse:
                        # Add 0x200 to get to the reverse halves
                        base += 0x200
                    code = base + (cell.petscii & 0xFF)
                    p.setPen(fg)
                    p.drawText(x, y + self.ascent, chr(code))
                elif use_rom and cell.screencode >= 0:
                    # Pixel-perfect chargen ROM rendering. The ROM
                    # has 8x8 1-bit bitmaps; we scale them up to
                    # the cell size so any zoom level looks crisp.
                    sc = cell.screencode & 0x7F
                    table = (chargen_lower
                             if cell.charset == 'lower'
                             else chargen_upper)
                    rom_char = table[sc]
                    # 1-bit -> pixel scale factor. cell_w/cell_h
                    # may not be exact multiples of 8; we round
                    # down to nearest int per axis so the cell
                    # fills cleanly with no gaps.
                    px = self.cell_w / 8
                    py = self.cell_h / 8
                    for row in range(8):
                        byte = rom_char[row]
                        for col in range(8):
                            if byte & (0x80 >> col):
                                # Pixel set - draw fg
                                p.fillRect(
                                    int(x + col * px),
                                    int(y + row * py),
                                    max(1, int(px + 0.5)),
                                    max(1, int(py + 0.5)),
                                    fg)
                elif cell.ch and cell.ch != " ":
                    p.setPen(fg)
                    p.drawText(x, y + self.ascent, cell.ch)

        # Cursor (block, semi-transparent)
        if self.hasFocus():
            cx = scr.cursor_col * self.cell_w
            cy = scr.cursor_row * self.cell_h
            if is_petscii:
                cur_color = QColor(palette[1])   # white
            else:
                cur_color = QColor(palette[DEFAULT_FG])
            cur_color.setAlpha(120)
            p.fillRect(cx, cy, self.cell_w, self.cell_h, cur_color)
        scr.dirty.clear()
        p.end()

    # ---- keyboard ------------------------------------------

    def keyPressEvent(self, ev: QKeyEvent):
        # Translate Qt key codes into the byte sequences a real
        # terminal would generate.
        key = ev.key()
        mods = ev.modifiers()
        text = ev.text()

        # Macro check first - if this key chord has a binding,
        # send the snippet instead of the normal key sequence.
        if self.macros:
            key_name = self._key_event_name(ev)
            if key_name in self.macros:
                snippet = self.macros[key_name]
                # Expand simple escapes: \r \n \t \e (ESC)
                snippet = (snippet
                           .replace("\\r", "\r")
                           .replace("\\n", "\n")
                           .replace("\\t", "\t")
                           .replace("\\e", "\x1b"))
                self._send(snippet.encode("utf-8", errors="replace"))
                return

        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            if self.crlf_mode == "crlf":
                self._send(b"\r\n")
            elif self.crlf_mode == "lf":
                self._send(b"\n")
            else:
                self._send(b"\r")
            return
        if key == Qt.Key.Key_Backspace:
            self._send(bytes([self.backspace_byte]))
            return
        if key == Qt.Key.Key_Delete:
            self._send(b"\x1b[3~")
            return
        if key == Qt.Key.Key_Tab:
            self._send(b"\t")
            return
        if key == Qt.Key.Key_Escape:
            self._send(b"\x1b")
            return
        if key == Qt.Key.Key_Up:
            self._send(b"\x1b[A")
            return
        if key == Qt.Key.Key_Down:
            self._send(b"\x1b[B")
            return
        if key == Qt.Key.Key_Right:
            self._send(b"\x1b[C")
            return
        if key == Qt.Key.Key_Left:
            self._send(b"\x1b[D")
            return
        if key == Qt.Key.Key_Home:
            self._send(b"\x1b[H")
            return
        if key == Qt.Key.Key_End:
            self._send(b"\x1b[F")
            return
        if key == Qt.Key.Key_PageUp:
            self._send(b"\x1b[5~")
            return
        if key == Qt.Key.Key_PageDown:
            self._send(b"\x1b[6~")
            return
        # F1..F12
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
            fn = key - Qt.Key.Key_F1 + 1
            f_codes = [
                b"\x1bOP", b"\x1bOQ", b"\x1bOR", b"\x1bOS",
                b"\x1b[15~", b"\x1b[17~", b"\x1b[18~", b"\x1b[19~",
                b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~",
            ]
            self._send(f_codes[fn - 1])
            return
        # Ctrl+A..Z -> 0x01..0x1A
        if (mods & Qt.KeyboardModifier.ControlModifier
                and Qt.Key.Key_A <= key <= Qt.Key.Key_Z):
            self._send(bytes([key - Qt.Key.Key_A + 1]))
            return
        # Regular text
        if text:
            # Strip the line terminator if the OS dropped one in;
            # we handle Enter explicitly above.
            text = text.replace("\r", "").replace("\n", "")
            if text:
                self._send(text.encode("utf-8", errors="replace"))

    def _send(self, data: bytes):
        if self.local_echo and data and data[0] >= 0x20:
            # Echo printable bytes locally so user sees what
            # they're typing on servers that don't echo back.
            self.screen.feed(data)
            self.update()
        self.key_pressed.emit(data)

    @staticmethod
    def _key_event_name(ev: QKeyEvent) -> str:
        """Build a stable key-binding name from a QKeyEvent.

        Format: "[Ctrl+][Shift+][Alt+]<KeyName>"
        Examples: "F1", "F2", "Ctrl+M", "Ctrl+Shift+L"

        Used to look up macro bindings. Modifier order is fixed
        (Ctrl, Shift, Alt) so two keystrokes that produce the
        same chord map to the same string."""
        key = ev.key()
        mods = ev.modifiers()
        # Identify the base key name. For F1..F12 the QKeySequence
        # text is "F1", "F2", etc. For letters and digits we use
        # the uppercase character. For anything else we fall back
        # to the Qt enum name.
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F35:
            n = key - Qt.Key.Key_F1 + 1
            base = f"F{n}"
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            base = chr(key - Qt.Key.Key_A + ord('A'))
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            base = chr(key - Qt.Key.Key_0 + ord('0'))
        else:
            # No useful base name for arrows / esc / etc -
            # macros there don't really make sense
            return ""
        prefix = ""
        if mods & Qt.KeyboardModifier.ControlModifier:
            prefix += "Ctrl+"
        if mods & Qt.KeyboardModifier.ShiftModifier:
            prefix += "Shift+"
        if mods & Qt.KeyboardModifier.AltModifier:
            prefix += "Alt+"
        return prefix + base


# ===========================================================
# Main dialog
# ===========================================================


class TelnetClientDialog(QDialog):
    """The full telnet/raw-tcp terminal window.

    Layout:
      * Toolbar with Quick Connect (host/port) + session combo
      * Terminal widget (cell grid)
      * Status bar with connection state + bytes counters

    Menus / dialogs:
      * Session manager - save/load/delete profiles
      * Settings - font, encoding, terminal type, autologin,
                   backspace, CRLF mode, logging
    """

    def __init__(self, parent=None, *,
                 session: Optional[TelnetSession] = None):
        super().__init__(parent)
        self.setWindowTitle("Quopus Telnet")
        self.resize(900, 650)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "telnet_client")
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.session = session or TelnetSession()
        self.worker: Optional[_NetworkWorker] = None
        self._log_fh = None
        self._tx = 0
        self._rx = 0
        self._autologin_step = 0
        self._autologin_buffer = b""
        self._build_ui()

    # ---- UI build -------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # ----- Toolbar -----
        tb = QHBoxLayout()
        tb.setSpacing(4)
        tb.addWidget(QLabel("Host:"))
        self.ed_host = QLineEdit(self.session.host)
        self.ed_host.setPlaceholderText("host.example.com")
        self.ed_host.setMaximumWidth(260)
        tb.addWidget(self.ed_host)
        tb.addWidget(QLabel("Port:"))
        # Editable combo so the user can either pick a well-known
        # BBS / shell port from the dropdown or type any number.
        # The values are the most common defaults: 23 telnet, 22
        # ssh, 6400/6464/64128 typical BBS forwards, 2323 alt
        # telnet, 992 telnets, 513 rlogin.
        self.sp_port = QComboBox()
        self.sp_port.setEditable(True)
        self.sp_port.setInsertPolicy(
            QComboBox.InsertPolicy.NoInsert)
        for p in (23, 22, 2323, 992, 513, 64128, 6464, 6400,
                  1541, 8023, 6800):
            self.sp_port.addItem(str(p), p)
        self.sp_port.setMaximumWidth(110)
        self.sp_port.setToolTip(
            "Pick a well-known port from the dropdown or\n"
            "type any value 1..65535.")
        # Initialise from session
        self.sp_port.setEditText(str(self.session.port))
        tb.addWidget(self.sp_port)
        tb.addWidget(QLabel("Proto:"))
        self.cmb_proto = QComboBox()
        self.cmb_proto.addItems(["telnet", "raw"])
        if _have_paramiko():
            self.cmb_proto.addItem("ssh")
        else:
            self.cmb_proto.setToolTip(
                "SSH not available - install 'paramiko' "
                "(pip install paramiko) and restart Quopus.")
        self.cmb_proto.setCurrentText(self.session.protocol)
        # When the protocol changes, auto-suggest its default port
        # if the current port still matches the previous protocol's
        # default. This makes the common case (telnet -> ssh) one
        # click instead of two.
        self.cmb_proto.currentTextChanged.connect(
            self._on_protocol_changed)
        tb.addWidget(self.cmb_proto)
        # Cols dropdown - some BBSes mis-count their own line widths
        # and send 81 chars when they claim to send 80, which wraps
        # mid-word at column 80. Letting the user bump to 82 or 84
        # quickly is far less painful than digging into Settings...
        tb.addSpacing(8)
        tb.addWidget(QLabel("Cols:"))
        self.cmb_cols = QComboBox()
        self.cmb_cols.setEditable(True)
        self.cmb_cols.setInsertPolicy(
            QComboBox.InsertPolicy.NoInsert)
        for c in (40, 82, 80, 84, 100, 120, 132):
            self.cmb_cols.addItem(str(c), c)
        self.cmb_cols.setEditText(str(self.session.cols))
        self.cmb_cols.setMaximumWidth(80)
        self.cmb_cols.setToolTip(
            "Terminal width in characters.\n"
            "  40  - PETSCII / C64 default\n"
            "  80  - ANSI default\n"
            "  82  - Some BBSes that wrap one column too eagerly\n"
            "  132 - Wide terminals / spreadsheets\n"
            "Change applies on next Connect.")
        self.cmb_cols.currentTextChanged.connect(
            self._on_cols_changed)
        tb.addWidget(self.cmb_cols)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setStyleSheet(
            "QPushButton { background: #2e8b57; color: white; "
            "font-weight: bold; }")
        self.btn_connect.clicked.connect(self._on_connect)
        tb.addWidget(self.btn_connect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setStyleSheet(
            "QPushButton { background: #b22222; color: white; }")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        tb.addWidget(self.btn_disconnect)
        tb.addSpacing(12)
        self.btn_sessions = QPushButton("Phonebook...")
        self.btn_sessions.setToolTip(
            "Open the phonebook - browse, edit and pick saved\n"
            "connections. Double-click an entry to connect\n"
            "immediately, or use Load && Close to just populate\n"
            "the toolbar fields without connecting.")
        self.btn_sessions.clicked.connect(self._on_sessions)
        tb.addWidget(self.btn_sessions)
        self.btn_settings = QPushButton("Settings...")
        self.btn_settings.setToolTip(
            "Font, encoding, autologin, CR/LF mode, logging, "
            "backspace key behavior.")
        self.btn_settings.clicked.connect(self._on_settings)
        tb.addWidget(self.btn_settings)
        tb.addSpacing(12)
        # ZModem upload button - manually kick a sender. Receiving
        # is auto-detected from the data stream so no Download
        # button is needed.
        self.btn_send = QPushButton("Send file...")
        self.btn_send.setToolTip(
            "Start a ZModem upload to the remote (BBS / shell).\n"
            "Most BBSes need you to type 'rz' or hit a file-upload\n"
            "menu option BEFORE pressing this button.")
        self.btn_send.clicked.connect(self._on_send_file)
        self.btn_send.setEnabled(False)
        tb.addWidget(self.btn_send)
        tb.addStretch(1)
        outer.addLayout(tb)

        # ----- Terminal -----
        self.screen = self._make_screen()
        # Pick the best font for the active terminal type. PETSCII
        # sessions auto-upgrade to C64 Pro Mono if available.
        font_family = self._resolve_font_family(
            self.session.font_family, self.session.terminal_type)
        self.term = _TerminalWidget(
            self.screen,
            font_family=font_family,
            font_size=self.session.font_size,
            parent=self)
        self.term.backspace_byte = (
            int(self.session.backspace_sends or "127"))
        self.term.local_echo = self.session.local_echo
        self.term.crlf_mode = self.session.crlf_mode
        self.term.macros = dict(self.session.macros or {})
        self.term.key_pressed.connect(self._on_keystroke)
        # Wrap in a horizontal layout so it doesn't stretch full
        # window width - the grid has a fixed pixel size driven
        # by the cell metrics, and we want it centered.
        term_row = QHBoxLayout()
        term_row.setContentsMargins(0, 0, 0, 0)
        term_row.addStretch(1)
        term_row.addWidget(self.term)
        term_row.addStretch(1)
        outer.addLayout(term_row, 1)

        # ----- Status bar -----
        sb = QHBoxLayout()
        sb.setSpacing(12)
        self.lbl_state = QLabel("disconnected")
        self.lbl_state.setStyleSheet(
            "QLabel { padding: 2px 6px; "
            "background: #333; color: #ccc; }")
        sb.addWidget(self.lbl_state)
        self.lbl_counters = QLabel("rx: 0   tx: 0")
        sb.addWidget(self.lbl_counters)
        sb.addStretch(1)
        outer.addLayout(sb)

        # Repaint timer - terminal redraws on a steady 30 Hz
        # tick rather than after every byte chunk, smoothing out
        # bursts of incoming data on fast connections.
        self._paint_timer = QTimer(self)
        self._paint_timer.setInterval(33)
        self._paint_timer.timeout.connect(self._maybe_repaint)
        self._paint_timer.start()

        # First show: enforce that the dialog is wide enough for
        # the terminal grid. We have to defer this until after the
        # constructor returns because Qt computes the actual
        # widget metrics lazily and a self.resize() here would
        # operate on stale sizeHint values. QTimer.singleShot(0)
        # runs immediately after the constructor when the dialog
        # is about to be shown.
        QTimer.singleShot(0, self._ensure_dialog_fits_terminal)

    # ---- session handling -----------------------------------

    def _gather_session_from_ui(self) -> TelnetSession:
        """Build a Session from the current UI fields."""
        s = TelnetSession()
        # Start from the loaded session so we preserve any settings
        # the user can't reach from the toolbar (autologin etc)
        for k, v in asdict(self.session).items():
            setattr(s, k, v)
        s.host = self.ed_host.text().strip()
        # Editable combo: read the current text, fall back to the
        # previous value if it's not a clean integer.
        try:
            s.port = int(self.sp_port.currentText().strip())
        except (TypeError, ValueError):
            s.port = self.session.port
        s.port = max(1, min(65535, s.port))
        s.protocol = self.cmb_proto.currentText()
        # Pull cols from the toolbar combo too - we treat the
        # toolbar as authoritative for "what this connection
        # should look like" over the saved profile.
        try:
            s.cols = int(self.cmb_cols.currentText().strip())
            s.cols = max(20, min(300, s.cols))
        except (TypeError, ValueError):
            s.cols = self.session.cols
        return s

    def _on_protocol_changed(self, proto):
        """Auto-suggest the default port when the protocol changes,
        unless the user has already typed something non-default."""
        defaults = {"telnet": 23, "raw": 23, "ssh": 22}
        target = defaults.get(proto, 23)
        # Only switch if the current port is one of the standard
        # defaults - if the user typed e.g. 6464 we keep it.
        try:
            cur = int(self.sp_port.currentText().strip())
        except (TypeError, ValueError):
            cur = 23
        if cur in (22, 23):
            self.sp_port.setEditText(str(target))

    def _on_cols_changed(self, text):
        """User picked a new col count from the toolbar combo.

        Live-apply: update session.cols, rebuild the terminal
        screen at the new width preserving as much content as
        possible, and renegotiate window size with the BBS via
        NAWS if we're connected. For BBSes that wrap one column
        too aggressively, bumping from 80 to 82 fixes the look
        without a reconnect."""
        try:
            new_cols = int(text.strip())
        except (TypeError, ValueError):
            return
        if not (20 <= new_cols <= 300):
            return
        if new_cols == self.session.cols:
            return
        self.session.cols = new_cols
        # Rebuild the screen at the new width. _TerminalScreen
        # has its own resize() that preserves cells - call it
        # rather than tossing the buffer.
        if hasattr(self.screen, 'resize'):
            self.screen.resize(self.screen.rows, new_cols)
        else:
            # PETSCII screen doesn't have an explicit resize -
            # rebuild from scratch.
            self.screen = self._make_screen()
            self.term.screen = self.screen
        # Tell the BBS about the new size if we have a live
        # worker. NAWS is a real-time mid-session re-negotiation.
        if (self.worker is not None
                and hasattr(self.worker, '_send_naws')):
            try:
                self.worker._send_naws(
                    new_cols, self.session.rows)
            except Exception:
                pass
        self._ensure_dialog_fits_terminal()
        self.term.update()

    def _make_screen(self):
        """Build the right screen type for the current session.
        PETSCII terminal type swaps the parser for one that
        understands C64 inline color codes.

        For PETSCII we auto-clamp cols to 40 if the saved session
        still has the ANSI default of 80. A real C64 has a 40-col
        screen and BBSes assume that width when laying out menus
        and box drawing - 80-col PETSCII makes the right margin
        wrap mid-glyph and looks broken. Users who actually run
        80-col PETSCII (rare - needs an 80-col cart) can set cols
        explicitly in Settings and we respect that.
        """
        s = self.session
        if s.terminal_type == 'petscii':
            # ANSI default is 82 cols (most BBSes wrap one column
            # too eagerly at 80, so we err high). PETSCII is always
            # 40 unless the user explicitly set something else, so
            # auto-clamp both 80 and 82 down to 40 here.
            cols = 40 if s.cols in (80, 82) else s.cols
            return _PetsciiScreen(rows=s.rows, cols=cols)
        return _TerminalScreen(
            rows=s.rows, cols=s.cols, encoding=s.encoding)

    def _resolve_font_family(self, requested, terminal_type):
        """Pick the right font family for the active session.

        If PETSCII mode is selected and the user's chosen font
        isn't a C64 family, silently upgrade to 'C64 Pro Mono' if
        it's installed - that's what makes the screencode-PUA
        path light up. We don't override an explicit choice for
        another C64 variant the user picked.
        """
        if terminal_type != 'petscii':
            return requested or "Courier New"
        # PETSCII: prefer C64 Pro Mono if available
        c64_fonts = ("C64 Pro Mono", "C64 Pro", "C64 Elite Mono",
                      "PetMe64", "PetMe2Y")
        if requested in c64_fonts:
            return requested
        try:
            from .palette import has_c64_pro_mono
            if has_c64_pro_mono():
                return "C64 Pro Mono"
        except Exception:
            pass
        return requested or "Courier New"

    def _ensure_dialog_fits_terminal(self):
        """Make sure the dialog is wide/tall enough to show the
        full terminal grid. PyQt does NOT auto-resize a non-modal
        dialog when an inner widget's sizeHint changes, so a
        large terminal (80 cols at font-size 14 ~= 880 pixels)
        ends up clipped on the right side. This method:

          1. Forces the Qt layout system to recompute the term's
             sizeHint (cheap if nothing changed)
          2. Reads the current required size from term.sizeHint()
          3. Adds a generous overhead for toolbar, status, margins,
             scrollbar gutter, window frame border
          4. Resizes the dialog UP if it's currently smaller. We
             never SHRINK the dialog here - if the user has it
             cranked to a custom big size for any reason, that
             stays.

        We err generously on the size estimate. Better a window
        with 30px of empty space on the right than one where the
        last column of the terminal is clipped and the BBS thinks
        it has a wider window than the user can actually see.
        """
        if not hasattr(self, 'term') or self.term is None:
            return
        # Force a layout recompute - term.sizeHint() is cached
        # until updateGeometry() invalidates it.
        self.term.updateGeometry()
        if self.layout() is not None:
            self.layout().activate()
        term_size = self.term.sizeHint()

        # Generous overhead to account for the window decoration
        # (title bar + close button) on top, the toolbar row, the
        # status row at the bottom, and the layout's content
        # margins. The platform-dependent parts (window chrome)
        # vary between Windows/Linux/macOS, so we add a fat margin.
        # Toolbar height: input fields ~32px + 8px padding = ~40px
        # Status row: label + counters ~28px
        # Outer layout margins: 6px top + 6px bottom = 12px
        # Window decoration: ~30px on Windows, varies elsewhere
        # Total vertical overhead: ~110px, round up to 130 for safety
        v_overhead = 130
        # Horizontal overhead is the tricky one because Windows
        # adds a left+right resize border (~8px each) that doesn't
        # show up in self.layout().contentsMargins(). Topaz New
        # measurement variance between Linux headless test (which
        # this code runs on) and Windows native rendering can add
        # another 5-10px the test misses. Pad generously - excess
        # right-side empty space is invisible, missing right-side
        # text is not.
        h_overhead = 80

        needed_w = term_size.width() + h_overhead
        needed_h = term_size.height() + v_overhead
        cur = self.size()
        new_w = max(cur.width(), needed_w)
        new_h = max(cur.height(), needed_h)
        if new_w != cur.width() or new_h != cur.height():
            self.resize(new_w, new_h)

    def apply_session(self, s: TelnetSession):
        """Reflect a new session in the UI without connecting."""
        self.session = s
        self.ed_host.setText(s.host)
        self.sp_port.setEditText(str(s.port))
        # The combo may not have ssh if paramiko isn't installed -
        # findText returns -1 then and we silently fall back.
        idx = self.cmb_proto.findText(s.protocol)
        if idx >= 0:
            self.cmb_proto.setCurrentIndex(idx)
        # Update cols combo without triggering its change handler
        # (would otherwise call _ensure_dialog_fits_terminal too
        # early, before the new screen is built).
        self.cmb_cols.blockSignals(True)
        self.cmb_cols.setEditText(str(s.cols))
        self.cmb_cols.blockSignals(False)
        self.screen = self._make_screen()
        self.term.screen = self.screen
        # Auto-pick C64 Pro Mono for PETSCII sessions if it's
        # installed - this enables the pixel-perfect PUA glyph
        # path in the renderer.
        font_family = self._resolve_font_family(
            s.font_family, s.terminal_type)
        self.term.set_font(font_family, s.font_size)
        self.term.backspace_byte = int(s.backspace_sends or "127")
        self.term.local_echo = s.local_echo
        self.term.crlf_mode = s.crlf_mode
        self.term.macros = dict(s.macros or {})
        self.term.update()
        # Grow the dialog if the new session needs more space (80
        # cols + ANSI at a bigger font easily exceeds the default
        # 900px width).
        self._ensure_dialog_fits_terminal()

    # ---- connection -----------------------------------------

    def _on_connect(self):
        s = self._gather_session_from_ui()
        if not s.host:
            QMessageBox.information(self, "Connect",
                "Enter a host first.")
            return
        if s.protocol == 'ssh' and not _have_paramiko():
            QMessageBox.warning(self, "SSH unavailable",
                "The 'paramiko' library is not installed.\n\n"
                "Install with:\n"
                "    pip install paramiko\n\n"
                "...and restart Quopus to enable SSH.")
            return
        self.session = s
        # Fresh screen if terminal type changed
        self.screen = self._make_screen()
        self.term.screen = self.screen
        self.term.update()
        # Tear down any old worker
        self._teardown_worker()
        self._tx = 0
        self._rx = 0
        self._autologin_step = 0
        self._autologin_buffer = b""
        # ZModem detection state - we sniff the data stream for
        # the well-known download offer signature ("**\x18B00")
        # so the user doesn't have to manually start the receiver.
        self._zm_sniff_buffer = b""
        self._zm_receiver: Optional[ZModemReceiver] = None
        self._zm_sender: Optional[ZModemSender] = None
        self._open_log_if_needed()
        # Build the worker
        if s.protocol == 'ssh':
            self.worker = _SSHWorker(
                host=s.host, port=s.port,
                username=s.autologin_user,
                password=s.autologin_password,
                rows=s.rows, cols=s.cols, term=s.terminal_type,
                parent=self)
        else:
            self.worker = _NetworkWorker(
                host=s.host, port=s.port, protocol=s.protocol,
                rows=s.rows, cols=s.cols, term=s.terminal_type,
                parent=self)
        self.worker.data_received.connect(self._on_data_received)
        self.worker.connected.connect(self._on_worker_connected)
        self.worker.disconnected.connect(self._on_worker_disconnected)
        self.worker.log_msg.connect(self._set_state)
        self.worker.start()
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.term.setFocus()
        # Start the trial-session timer if this is an unregistered
        # build. Trial users get 5 minutes of telnet per session,
        # then we auto-disconnect with a "buy pro" message. Pro
        # users have unlimited time.
        self._start_trial_timer_if_needed()

    def _start_trial_timer_if_needed(self):
        """Start the trial-mode session timer that auto-disconnects
        after 5 minutes. The timer is created lazily and reused
        across connections. Pro users (FEATURE_TELNET) get no
        timer at all."""
        try:
            from . import license
            if license.has_feature(license.FEATURE_TELNET):
                return
        except Exception:
            # License lookup failed - err on the permissive side
            # (no timer = no surprise disconnects for paid users
            # whose license read transiently failed)
            return
        # Lazy-create the timer the first time we connect
        if not hasattr(self, '_trial_timer'):
            self._trial_timer = QTimer(self)
            self._trial_timer.setSingleShot(True)
            self._trial_timer.timeout.connect(
                self._on_trial_timeout)
        self._trial_timer.start(5 * 60 * 1000)  # 5 minutes
        # Also start a short warning timer that fires 30 seconds
        # before disconnect so the user can save what they're
        # doing in the BBS.
        if not hasattr(self, '_trial_warn_timer'):
            self._trial_warn_timer = QTimer(self)
            self._trial_warn_timer.setSingleShot(True)
            self._trial_warn_timer.timeout.connect(
                self._on_trial_warning)
        self._trial_warn_timer.start(
            (5 * 60 - 30) * 1000)  # 4:30

    def _on_trial_warning(self):
        """Show a non-modal warning 30 seconds before trial cut."""
        self._set_state(
            "Trial: 30 seconds left - register to remove the limit")

    def _on_trial_timeout(self):
        """5-minute trial session expired. Disconnect with a
        user-facing message."""
        self._on_disconnect()
        QMessageBox.information(
            self, "Trial Session Ended",
            "Your 5-minute trial telnet session has ended.\n\n"
            "Trial users are limited to 5 minutes per session.\n"
            "Register Quopus to remove this limit and enjoy\n"
            "unlimited telnet sessions.\n\n"
            "See BUYING.md or click 'Enter License File...' on\n"
            "the next nag screen to register.")

    def _on_disconnect(self):
        self._teardown_worker()
        self._set_state("disconnected")
        # Stop the trial timer if it was running - re-arms next
        # connect.
        for attr in ('_trial_timer', '_trial_warn_timer'):
            t = getattr(self, attr, None)
            if t is not None:
                t.stop()
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_send.setEnabled(False)
        self._close_log()

    def _teardown_worker(self):
        if self.worker is None:
            return
        try:
            self.worker.stop()
            self.worker.wait(2000)
        except Exception:
            pass
        self.worker = None

    def _on_worker_connected(self):
        self._set_state(
            f"connected to {self.session.host}:{self.session.port}")
        # Update the matching phonebook entry's last_connected
        # timestamp so the user can see when each entry was last
        # actually reached. We match by name+host+port - the
        # toolbar-only case (no name) just won't match anything.
        try:
            import datetime
            sessions = load_sessions()
            changed = False
            for ph in sessions:
                if (ph.name == self.session.name
                        and ph.host == self.session.host
                        and ph.port == self.session.port):
                    ph.last_connected = (
                        datetime.datetime.now().isoformat())
                    changed = True
                    break
            if changed:
                save_sessions(sessions)
        except Exception:
            # Phonebook update is best-effort - don't let a write
            # error prevent the connection from being usable.
            pass
        # SSH already does its own auth - skip autologin for ssh.
        # Telnet/raw can use the prompt-detect mechanism.
        if (self.session.protocol != 'ssh'
                and self.session.autologin_user):
            self._autologin_step = 1   # expecting user prompt

    def _on_worker_disconnected(self, reason):
        self._set_state(f"disconnected: {reason}")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_send.setEnabled(False)
        self._close_log()

    def _on_data_received(self, data: bytes):
        self._rx += len(data)
        self._update_counters()
        # If a ZModem transfer is active, every byte goes to the
        # protocol handler rather than the terminal screen - the
        # terminal would corrupt the binary data and the screen
        # would fill with garbage.
        if self._zm_receiver is not None:
            self._handle_zm_recv(data)
            return
        if self._zm_sender is not None:
            self._handle_zm_send(data)
            return
        # Sniff for an incoming ZModem offer in the stream.
        # We keep a small rolling buffer so the marker isn't
        # missed when it straddles a recv() boundary.
        self._zm_sniff_buffer = (
            self._zm_sniff_buffer + data)[-32:]
        idx = _detect_zmodem_start(self._zm_sniff_buffer)
        if idx is not None:
            self._start_zm_receive()
            return
        # Normal terminal data
        self.screen.feed(data)
        # Log raw incoming bytes if logging is on
        if self._log_fh:
            try:
                self._log_fh.write(data)
                self._log_fh.flush()
            except OSError:
                pass
        # Check for autologin prompts (telnet/raw only)
        if self._autologin_step:
            self._check_autologin(data)

    # ---- ZModem ---------------------------------------------

    def _start_zm_receive(self):
        """Auto-detected an incoming ZModem offer. Ask the user
        where to drop the files, then activate the receiver."""
        dest = QFileDialog.getExistingDirectory(
            self, "ZModem: choose download folder",
            os.path.expanduser("~"))
        if not dest:
            # User declined - tell the sender to cancel
            if self.worker is not None:
                self.worker.send(
                    bytes([ZDLE] * 8 + [0x08] * 10))
            self._set_state("zmodem cancelled")
            return
        self._zm_receiver = ZModemReceiver(dest)
        self._zm_receiver.start()
        self._set_state(f"zmodem: receiving to {dest}")
        self._drain_zm_queue()

    def _handle_zm_recv(self, data: bytes):
        self._zm_receiver.feed(data)
        self._drain_zm_queue()
        if self._zm_receiver.state in (
                ZModemReceiver.STATE_DONE,
                ZModemReceiver.STATE_ERROR):
            files = self._zm_receiver.received_files
            err = self._zm_receiver.last_error
            self._zm_receiver = None
            if files:
                names = ", ".join(p.name for p in files)
                self._set_state(
                    f"zmodem: received {len(files)} file(s): {names}")
                QMessageBox.information(self, "ZModem",
                    f"Received {len(files)} file(s):\n"
                    + "\n".join(str(p) for p in files))
            elif err:
                self._set_state(f"zmodem error: {err}")
            else:
                self._set_state("zmodem: done")

    def _handle_zm_send(self, data: bytes):
        self._zm_sender.feed(data)
        self._drain_zm_queue()
        if self._zm_sender.state in ("done", "cancelled", "error"):
            if self._zm_sender.last_error:
                self._set_state(
                    f"zmodem send error: {self._zm_sender.last_error}")
            else:
                self._set_state(
                    f"zmodem: sent {self._zm_sender.path.name}")
            self._zm_sender = None

    def _drain_zm_queue(self):
        """Push queued ZModem bytes through the worker. Called
        whenever the sender/receiver might have generated new
        outgoing frames."""
        handler = self._zm_receiver or self._zm_sender
        if handler is None or self.worker is None:
            return
        while handler.send_queue:
            chunk = handler.send_queue.popleft()
            self.worker.send(chunk)

    def _on_send_file(self):
        """User clicked 'Send file...' - pick a file and start a
        ZModem upload. Assumes the remote already typed 'rz' or
        is otherwise waiting for an offer."""
        if self.worker is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Send file via ZModem",
            os.path.expanduser("~"),
            "All files (*)")
        if not path:
            return
        try:
            self._zm_sender = ZModemSender(Path(path))
            self._zm_sender.start()
        except OSError as e:
            QMessageBox.warning(self, "ZModem", f"Can't open:\n{e}")
            return
        self._set_state(f"zmodem: sending {os.path.basename(path)}")
        self._drain_zm_queue()

    def _on_keystroke(self, data: bytes):
        if self.worker is None:
            return
        self._tx += len(data)
        self._update_counters()
        self.worker.send(data)

    def _check_autologin(self, data: bytes):
        """Scan the rolling input window for the prompts we know
        about and respond with the configured user/password."""
        self._autologin_buffer = (
            self._autologin_buffer + data)[-256:]
        # ASCII-lowercase for matching - prompts use various cases
        text = self._autologin_buffer.decode(
            "latin-1", errors="replace").lower()
        u_prompt = (self.session.autologin_user_prompt or "").lower()
        p_prompt = (self.session.autologin_pass_prompt or "").lower()
        if self._autologin_step == 1 and u_prompt in text:
            QTimer.singleShot(
                max(50, self.session.autologin_delay_ms),
                lambda: self._send_autologin(
                    self.session.autologin_user + "\r"))
            self._autologin_step = 2
            self._autologin_buffer = b""
        elif self._autologin_step == 2 and p_prompt in text:
            QTimer.singleShot(
                max(50, self.session.autologin_delay_ms),
                lambda: self._send_autologin(
                    self.session.autologin_password + "\r"))
            self._autologin_step = 0
            self._autologin_buffer = b""

    def _send_autologin(self, s: str):
        if self.worker is not None:
            self.worker.send(s.encode("utf-8", errors="replace"))

    # ---- logging --------------------------------------------

    def _open_log_if_needed(self):
        self._close_log()
        if not self.session.keep_log:
            return
        path = self.session.log_path.strip()
        if not path:
            import datetime
            from .config import SCRIPT_DIR
            log_dir = SCRIPT_DIR / "telnet_logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log_dir = Path(os.path.expanduser("~"))
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = (self.session.name or "session").replace(
                " ", "_").lower()
            path = str(log_dir / f"{slug}_{ts}.log")
        try:
            self._log_fh = open(path, "ab")
        except OSError as e:
            QMessageBox.warning(self, "Logging",
                f"Couldn't open log file:\n{path}\n{e}")
            self._log_fh = None

    def _close_log(self):
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    # ---- ui helpers ----------------------------------------

    def _set_state(self, text):
        self.lbl_state.setText(text)

    def _update_counters(self):
        self.lbl_counters.setText(
            f"rx: {self._rx}   tx: {self._tx}")

    def _maybe_repaint(self):
        if self.screen.dirty:
            self.term.update()

    # ---- sub-dialogs ---------------------------------------

    def _on_sessions(self):
        dlg = _SessionManagerDialog(self.session, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.picked_session
            if picked is not None:
                self.apply_session(picked)
                # "Connect now" button / double-click sets this flag.
                # The toolbar already shows the right host/port now,
                # so connect button is enabled - press it.
                if dlg.connect_immediately:
                    self._on_connect()

    def _on_settings(self):
        dlg = _SettingsDialog(self.session, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.session = dlg.result_session
            self.apply_session(self.session)

    # ---- cleanup -------------------------------------------

    def closeEvent(self, ev):
        self._teardown_worker()
        self._close_log()
        super().closeEvent(ev)


# ===========================================================
# Session manager sub-dialog
# ===========================================================


class _SessionManagerDialog(QDialog):
    """Phonebook of saved sessions.

    Shows every saved session as a row in a sortable table with
    columns: Name | Group | Protocol | Host | Port | Last Connected.
    Buttons let the user add, edit, duplicate, delete, save the
    current toolbar connection, or pick one and load it. Double-
    click a row connects immediately (load + close + connect).
    """

    def __init__(self, current_session: TelnetSession, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Telnet Phonebook")
        self.resize(820, 480)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "telnet_phonebook")
        self.current = current_session
        self.picked_session: Optional[TelnetSession] = None
        self.connect_immediately = False
        self.sessions = load_sessions()
        self._build_ui()

    def _build_ui(self):
        from PyQt6.QtWidgets import (
            QTreeWidget, QTreeWidgetItem, QHeaderView,
            QInputDialog,
        )
        lay = QVBoxLayout(self)

        # Filter row - quick search across all visible fields
        filt_row = QHBoxLayout()
        filt_row.addWidget(QLabel("Filter:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText(
            "type to filter by name / group / host...")
        self.ed_filter.textChanged.connect(self._apply_filter)
        filt_row.addWidget(self.ed_filter, 1)
        lay.addLayout(filt_row)

        # Table - QTreeWidget gives us free sortable columns
        self.tbl = QTreeWidget()
        self.tbl.setHeaderLabels([
            "Name", "Group", "Proto", "Host", "Port",
            "Last connected",
        ])
        from quopus_lib.window_state import install_table_state
        install_table_state(self.tbl, "telnet_phonebook:tbl")
        self.tbl.setRootIsDecorated(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSortingEnabled(True)
        self.tbl.setSelectionMode(
            QTreeWidget.SelectionMode.SingleSelection)
        # Column widths
        self.tbl.setColumnWidth(0, 180)
        self.tbl.setColumnWidth(1, 120)
        self.tbl.setColumnWidth(2, 60)
        self.tbl.setColumnWidth(3, 200)
        self.tbl.setColumnWidth(4, 60)
        self.tbl.setColumnWidth(5, 140)
        # Double-click = connect
        self.tbl.itemDoubleClicked.connect(self._on_connect_now)
        # Selection drives the buttons
        self.tbl.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.tbl, 1)
        self._fill_table()

        # Notes preview - shows the notes field of the selected
        # session. Read-only.
        notes_row = QHBoxLayout()
        notes_row.addWidget(QLabel("Notes:"))
        self.lbl_notes = QLabel("(select a row)")
        self.lbl_notes.setStyleSheet(
            "QLabel { padding: 4px 6px; background: #f5f5f5; "
            "border: 1px solid #ddd; }")
        self.lbl_notes.setWordWrap(True)
        self.lbl_notes.setMinimumHeight(36)
        self.lbl_notes.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum)
        notes_row.addWidget(self.lbl_notes, 1)
        lay.addLayout(notes_row)

        # Button row
        row = QHBoxLayout()
        self.btn_new = QPushButton("New...")
        self.btn_new.setToolTip(
            "Create a blank phonebook entry and open the editor.")
        self.btn_new.clicked.connect(self._on_new)
        row.addWidget(self.btn_new)
        self.btn_save_cur = QPushButton("Save current...")
        self.btn_save_cur.setToolTip(
            "Capture the toolbar's current host/port/protocol\n"
            "etc into a new phonebook entry.")
        self.btn_save_cur.clicked.connect(self._on_save_current)
        row.addWidget(self.btn_save_cur)
        self.btn_edit = QPushButton("Edit...")
        self.btn_edit.clicked.connect(self._on_edit)
        row.addWidget(self.btn_edit)
        self.btn_dup = QPushButton("Duplicate")
        self.btn_dup.clicked.connect(self._on_duplicate)
        row.addWidget(self.btn_dup)
        self.btn_del = QPushButton("Delete")
        self.btn_del.clicked.connect(self._on_delete)
        row.addWidget(self.btn_del)
        row.addStretch(1)
        self.btn_load = QPushButton("Load && Close")
        self.btn_load.setToolTip(
            "Load the selected entry into the toolbar without\n"
            "actually connecting. Useful for tweaking before\n"
            "you commit.")
        self.btn_load.clicked.connect(self._on_load)
        row.addWidget(self.btn_load)
        self.btn_connect = QPushButton("Connect now")
        self.btn_connect.setStyleSheet(
            "QPushButton { font-weight: bold; "
            "background: #2e8b57; color: white; }")
        self.btn_connect.setToolTip(
            "Load the selected entry and immediately connect.\n"
            "(Same as double-clicking the row.)")
        self.btn_connect.clicked.connect(self._on_connect_now)
        row.addWidget(self.btn_connect)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self._update_buttons()

    # ---- table population ----------------------------------

    def _fill_table(self):
        self.tbl.setSortingEnabled(False)
        self.tbl.clear()
        for s in self.sessions:
            self._add_row(s)
        self.tbl.setSortingEnabled(True)
        # Default sort by Name
        self.tbl.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def _add_row(self, s: TelnetSession):
        from PyQt6.QtWidgets import QTreeWidgetItem
        last_short = (s.last_connected[:19].replace("T", " ")
                      if s.last_connected else "")
        item = QTreeWidgetItem([
            s.name, s.group, s.protocol, s.host,
            str(s.port), last_short,
        ])
        # Stable numeric sort for Port column via UserRole + 1
        item.setData(4, Qt.ItemDataRole.UserRole + 1, s.port)
        item.setData(0, Qt.ItemDataRole.UserRole, s)
        self.tbl.addTopLevelItem(item)

    def _apply_filter(self, text):
        """Hide rows that don't match the filter substring (case-
        insensitive search across name/group/host)."""
        needle = text.strip().lower()
        for i in range(self.tbl.topLevelItemCount()):
            item = self.tbl.topLevelItem(i)
            if not needle:
                item.setHidden(False)
                continue
            hay = " ".join([
                item.text(0), item.text(1), item.text(2),
                item.text(3), item.text(4),
            ]).lower()
            item.setHidden(needle not in hay)

    def _selected(self) -> Optional[TelnetSession]:
        sel = self.tbl.selectedItems()
        if not sel:
            return None
        return sel[0].data(0, Qt.ItemDataRole.UserRole)

    def _update_buttons(self):
        has = self._selected() is not None
        for b in (self.btn_edit, self.btn_dup, self.btn_del,
                   self.btn_load, self.btn_connect):
            b.setEnabled(has)
        s = self._selected()
        if s and s.notes:
            self.lbl_notes.setText(s.notes)
        elif s:
            self.lbl_notes.setText("(no notes)")
        else:
            self.lbl_notes.setText("(select a row)")

    # ---- actions -------------------------------------------

    def _on_new(self):
        # Trial users limited to 3 phonebook entries
        if not self._check_phonebook_limit():
            return
        s = TelnetSession()
        s.name = "New entry"
        dlg = _SettingsDialog(s, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.sessions.append(dlg.result_session)
        save_sessions(self.sessions)
        self._fill_table()

    def _check_phonebook_limit(self) -> bool:
        """Enforce the trial limit on phonebook size. Returns
        True if the user is allowed to add another entry,
        otherwise shows a "buy pro" dialog and returns False."""
        try:
            from . import license
            if license.has_feature(
                    license.FEATURE_PHONEBOOK_UNLIMITED):
                return True
            if len(self.sessions) < 3:
                return True
            QMessageBox.information(
                self, "Trial Limit",
                "The trial version is limited to 3 phonebook\n"
                "entries. Delete one to add a new one, or buy a\n"
                "license to unlock unlimited entries.\n\n"
                "Use 'Enter License File...' from the nag screen\n"
                "on next startup to register.")
            return False
        except Exception:
            # If anything goes wrong with the license check,
            # err on the permissive side - we never want license
            # bugs to deny features the user paid for.
            return True

    def _on_save_current(self):
        if not self._check_phonebook_limit():
            return
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Save phonebook entry", "Entry name:",
            text=self.current.name or self.current.host or "session")
        if not ok or not name.strip():
            return
        new = TelnetSession()
        for k, v in asdict(self.current).items():
            setattr(new, k, v)
        new.name = name.strip()
        self.sessions.append(new)
        save_sessions(self.sessions)
        self._fill_table()

    def _on_edit(self):
        s = self._selected()
        if s is None:
            return
        dlg = _SettingsDialog(s, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx = self.sessions.index(s)
            self.sessions[idx] = dlg.result_session
            save_sessions(self.sessions)
            self._fill_table()

    def _on_duplicate(self):
        s = self._selected()
        if s is None:
            return
        new = TelnetSession()
        for k, v in asdict(s).items():
            setattr(new, k, v)
        new.name = f"{s.name} (copy)"
        new.last_connected = ""
        self.sessions.append(new)
        save_sessions(self.sessions)
        self._fill_table()

    def _on_delete(self):
        s = self._selected()
        if s is None:
            return
        reply = QMessageBox.question(
            self, "Delete entry",
            f"Delete phonebook entry '{s.name}'?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.sessions.remove(s)
        save_sessions(self.sessions)
        self._fill_table()

    def _on_load(self):
        s = self._selected()
        if s is None:
            return
        self.picked_session = s
        self.connect_immediately = False
        self.accept()

    def _on_connect_now(self):
        s = self._selected()
        if s is None:
            return
        self.picked_session = s
        self.connect_immediately = True
        self.accept()


# ===========================================================
# Settings dialog
# ===========================================================


class _SettingsDialog(QDialog):
    """Edit every Session field that isn't on the toolbar."""

    def __init__(self, session: TelnetSession, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Telnet Settings")
        # Two-column layout is much shorter than the old stacked
        # one - 720x600 fits on small laptop screens (1366x768)
        # without scrolling. Vertical resize allowed so the user
        # can grow the macros area if they have lots of bindings.
        self.resize(720, 600)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "telnet_settings")
        # Start from a copy so Cancel actually cancels
        self.result_session = TelnetSession()
        for k, v in asdict(session).items():
            setattr(self.result_session, k, v)
        self._build_ui(session)

    def _build_ui(self, s: TelnetSession):
        # Top-level: vertical split between "two columns of settings"
        # and "macros + buttons" so macros always have full width.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        # Two columns side by side - reduces vertical demand from
        # 6 stacked groupboxes to 3+3.
        cols = QHBoxLayout()
        cols.setSpacing(8)
        col_left = QVBoxLayout()
        col_left.setSpacing(6)
        col_right = QVBoxLayout()
        col_right.setSpacing(6)
        cols.addLayout(col_left, 1)
        cols.addLayout(col_right, 1)
        outer.addLayout(cols, 1)

        # --- Identity --- (left column, top)
        g_id = QGroupBox("Identity")
        f_id = QFormLayout(g_id)
        f_id.setContentsMargins(8, 8, 8, 8)
        f_id.setVerticalSpacing(4)
        self.ed_name = QLineEdit(s.name)
        f_id.addRow("Session name:", self.ed_name)
        self.ed_group = QLineEdit(s.group)
        self.ed_group.setPlaceholderText(
            "optional - e.g. 'C64 BBSes', 'Work shells'")
        f_id.addRow("Group:", self.ed_group)
        self.ed_host = QLineEdit(s.host)
        f_id.addRow("Host:", self.ed_host)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(s.port)
        f_id.addRow("Port:", self.sp_port)
        self.cmb_proto = QComboBox()
        self.cmb_proto.addItems(["telnet", "raw"])
        if _have_paramiko():
            self.cmb_proto.addItem("ssh")
        self.cmb_proto.setCurrentText(s.protocol)
        f_id.addRow("Protocol:", self.cmb_proto)
        self.ed_notes = QPlainTextEdit(s.notes)
        # Smaller notes box - 2 lines visible, scrollable for more
        self.ed_notes.setMaximumHeight(48)
        self.ed_notes.setPlaceholderText(
            "Free-form notes shown in the phonebook preview")
        f_id.addRow("Notes:", self.ed_notes)
        col_left.addWidget(g_id)

        # --- Terminal --- (left column, middle)
        g_term = QGroupBox("Terminal")
        f_term = QFormLayout(g_term)
        f_term.setContentsMargins(8, 8, 8, 8)
        f_term.setVerticalSpacing(4)
        self.cmb_term = QComboBox()
        self.cmb_term.addItems(["ansi", "petscii"])
        self.cmb_term.setCurrentText(s.terminal_type)
        f_term.addRow("Terminal type:", self.cmb_term)
        self.cmb_enc = QComboBox()
        self.cmb_enc.addItems([
            "cp437", "utf-8", "latin-1", "ascii",
            "cp1252", "petscii",
        ])
        self.cmb_enc.setCurrentText(s.encoding)
        f_term.addRow("Encoding:", self.cmb_enc)
        # Font picker - bundled pixel fonts first, then monospace
        # families from the system.
        self.cmb_font = QComboBox()
        preferred = ["Topaz New", "Topaz", "C64 Pro Mono",
                       "Courier New", "Consolas", "Monaco",
                       "DejaVu Sans Mono"]
        families = set(QFontDatabase.families())
        for fam in preferred:
            if fam in families:
                self.cmb_font.addItem(fam)
        for fam in sorted(families):
            if fam not in preferred:
                f = QFont(fam)
                if f.fixedPitch():
                    self.cmb_font.addItem(fam)
        if s.font_family:
            idx = self.cmb_font.findText(s.font_family)
            if idx >= 0:
                self.cmb_font.setCurrentIndex(idx)
            else:
                self.cmb_font.insertItem(0, s.font_family)
                self.cmb_font.setCurrentIndex(0)
        f_term.addRow("Font:", self.cmb_font)
        # Pack font size + rows + cols into one row to save height
        size_row = QHBoxLayout()
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.setSpacing(4)
        self.sp_fsize = QSpinBox()
        self.sp_fsize.setRange(6, 48)
        self.sp_fsize.setValue(s.font_size)
        self.sp_fsize.setMaximumWidth(60)
        size_row.addWidget(QLabel("Size:"))
        size_row.addWidget(self.sp_fsize)
        size_row.addSpacing(8)
        self.sp_rows = QSpinBox()
        self.sp_rows.setRange(10, 200)
        self.sp_rows.setValue(s.rows)
        self.sp_rows.setMaximumWidth(60)
        size_row.addWidget(QLabel("Rows:"))
        size_row.addWidget(self.sp_rows)
        size_row.addSpacing(8)
        self.sp_cols = QSpinBox()
        self.sp_cols.setRange(20, 300)
        self.sp_cols.setValue(s.cols)
        self.sp_cols.setMaximumWidth(60)
        size_row.addWidget(QLabel("Cols:"))
        size_row.addWidget(self.sp_cols)
        size_row.addStretch(1)
        size_w = QWidget()
        size_w.setLayout(size_row)
        f_term.addRow("Geometry:", size_w)
        col_left.addWidget(g_term)
        col_left.addStretch(1)

        # --- Behavior --- (right column, top)
        g_beh = QGroupBox("Behavior")
        f_beh = QFormLayout(g_beh)
        f_beh.setContentsMargins(8, 8, 8, 8)
        f_beh.setVerticalSpacing(4)
        # Quick-preset row: a handful of common scenarios that
        # require setting several of the fields below in lockstep.
        # The user can still tweak individual values after picking
        # a preset - the preset just gets the baseline right so
        # the user doesn't have to look up the encoding/CR/echo
        # triplet that goes with each scenario.
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.setSpacing(4)
        preset_row.addWidget(QLabel("Quick preset:"))
        # Each entry is (label, tooltip, field-dict). Field-dict
        # maps widget attr name to the value to write. Booleans go
        # to setChecked; QComboBox values are dispatched via
        # data-vs-text (we use setCurrentIndex if itemData matches,
        # else setCurrentText).
        self._preset_specs = [
            ("U64 Remote", (
                "Settings for controlling an Ultimate 64/II+\n"
                "menu via telnet, matching the 1541u_remote PDF:\n"
                "  Terminal: xterm-like ANSI, no local echo,\n"
                "  no local line editing, no implicit CR,\n"
                "  Backspace = BS (Ctrl-H), Enter = CR only."),
                {
                    "cmb_proto": ("data", "telnet"),
                    "cmb_term": ("text", "ansi"),
                    "cmb_enc": ("text", "latin-1"),
                    "cmb_bs": ("data", "8"),
                    "cmb_crlf": ("data", "cr"),
                    "chk_echo": False,
                }),
            ("PETSCII BBS", (
                "Classic C64 BBS over telnet using PETSCII:\n"
                "  Encoding PETSCII, Topaz/C64 font, CR only,\n"
                "  Backspace = DEL (most PETSCII BBSes expect\n"
                "  this), no local echo."),
                {
                    "cmb_proto": ("data", "telnet"),
                    "cmb_term": ("text", "petscii"),
                    "cmb_enc": ("text", "petscii"),
                    "cmb_bs": ("data", "127"),
                    "cmb_crlf": ("data", "cr"),
                    "chk_echo": False,
                }),
            ("ANSI BBS", (
                "Standard ANSI BBS (cp437, CRLF, no local echo).\n"
                "Works for typical PC/Amiga BBSes that send\n"
                "colour codes and box-drawing glyphs."),
                {
                    "cmb_proto": ("data", "telnet"),
                    "cmb_term": ("text", "ansi"),
                    "cmb_enc": ("text", "cp437"),
                    "cmb_bs": ("data", "8"),
                    "cmb_crlf": ("data", "crlf"),
                    "chk_echo": False,
                }),
            ("Unix shell", (
                "Generic Unix telnet/SSH-style endpoint with\n"
                "UTF-8 encoding, CR-only line endings, and DEL\n"
                "for backspace (modern bash/zsh default)."),
                {
                    "cmb_proto": ("data", "telnet"),
                    "cmb_term": ("text", "ansi"),
                    "cmb_enc": ("text", "utf-8"),
                    "cmb_bs": ("data", "127"),
                    "cmb_crlf": ("data", "cr"),
                    "chk_echo": False,
                }),
            ("Raw 8-bit", (
                "Raw TCP, no telnet negotiation, no encoding\n"
                "translation. Good for talking to custom\n"
                "binary protocols where bytes must pass through\n"
                "untouched."),
                {
                    "cmb_proto": ("data", "raw"),
                    "cmb_term": ("text", "ansi"),
                    "cmb_enc": ("text", "latin-1"),
                    "cmb_bs": ("data", "8"),
                    "cmb_crlf": ("data", "cr"),
                    "chk_echo": True,
                }),
        ]
        for label, tooltip, fields in self._preset_specs:
            btn = QPushButton(label)
            btn.setToolTip(tooltip)
            # Bind via closure - default-arg trick to capture
            # `fields` by value rather than by late binding.
            btn.clicked.connect(
                lambda _=False, f=fields:
                    self._apply_preset(f))
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        preset_widget = QWidget()
        preset_widget.setLayout(preset_row)
        f_beh.addRow("", preset_widget)

        self.cmb_bs = QComboBox()
        self.cmb_bs.addItem("DEL (0x7F)", "127")
        self.cmb_bs.addItem("BS (0x08)", "8")
        if s.backspace_sends == "8":
            self.cmb_bs.setCurrentIndex(1)
        f_beh.addRow("Backspace sends:", self.cmb_bs)
        self.cmb_crlf = QComboBox()
        self.cmb_crlf.addItem("CR (0x0D)", "cr")
        self.cmb_crlf.addItem("CRLF (0x0D 0x0A)", "crlf")
        self.cmb_crlf.addItem("LF (0x0A)", "lf")
        for i in range(3):
            if self.cmb_crlf.itemData(i) == s.crlf_mode:
                self.cmb_crlf.setCurrentIndex(i)
                break
        f_beh.addRow("Enter sends:", self.cmb_crlf)
        self.chk_echo = QCheckBox("Local echo")
        self.chk_echo.setToolTip(
            "Echo typed characters locally (useful when the\n"
            "remote doesn't echo back).")
        self.chk_echo.setChecked(s.local_echo)
        f_beh.addRow("", self.chk_echo)
        col_right.addWidget(g_beh)

        # --- Auto-login --- (right column, middle)
        g_log = QGroupBox("Auto-login (optional)")
        f_log = QFormLayout(g_log)
        f_log.setContentsMargins(8, 8, 8, 8)
        f_log.setVerticalSpacing(4)
        self.ed_user = QLineEdit(s.autologin_user)
        self.ed_user.setPlaceholderText("(blank = no autologin)")
        f_log.addRow("Username:", self.ed_user)
        self.ed_pass = QLineEdit(s.autologin_password)
        self.ed_pass.setEchoMode(QLineEdit.EchoMode.Password)
        f_log.addRow("Password:", self.ed_pass)
        self.ed_uprompt = QLineEdit(s.autologin_user_prompt)
        self.ed_uprompt.setPlaceholderText("e.g. login:")
        f_log.addRow("User prompt:", self.ed_uprompt)
        self.ed_pprompt = QLineEdit(s.autologin_pass_prompt)
        self.ed_pprompt.setPlaceholderText("e.g. password:")
        f_log.addRow("Pass prompt:", self.ed_pprompt)
        self.sp_delay = QSpinBox()
        self.sp_delay.setRange(0, 5000)
        self.sp_delay.setSuffix(" ms")
        self.sp_delay.setValue(s.autologin_delay_ms)
        f_log.addRow("Reply delay:", self.sp_delay)
        col_right.addWidget(g_log)

        # --- Logging --- (right column, bottom)
        g_lg = QGroupBox("Session logging")
        f_lg = QFormLayout(g_lg)
        f_lg.setContentsMargins(8, 8, 8, 8)
        f_lg.setVerticalSpacing(4)
        self.chk_log = QCheckBox(
            "Record received bytes to log file")
        self.chk_log.setChecked(s.keep_log)
        f_lg.addRow("", self.chk_log)
        log_row = QHBoxLayout()
        log_row.setContentsMargins(0, 0, 0, 0)
        self.ed_logpath = QLineEdit(s.log_path)
        self.ed_logpath.setPlaceholderText(
            "(blank = auto-name in <quopus>/telnet_logs/)")
        log_row.addWidget(self.ed_logpath, 1)
        btn_browse = QPushButton("Browse...")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self._browse_log)
        log_row.addWidget(btn_browse)
        log_widget = QWidget()
        log_widget.setLayout(log_row)
        f_lg.addRow("Log path:", log_widget)
        col_right.addWidget(g_lg)
        col_right.addStretch(1)

        # --- Macros --- (full width, below the two columns)
        # Simple text editor where each line is "KEY = snippet".
        # KEY can be F1..F12, or Ctrl/Shift/Alt + a letter or digit
        # (e.g. "Ctrl+L"). Snippet supports \r, \n, \t, \e escapes.
        # No defaults are seeded - the user explicitly fills this
        # in if they want bindings.
        g_mac = QGroupBox("Macros (key -> command snippet)")
        f_mac = QVBoxLayout(g_mac)
        f_mac.setContentsMargins(8, 8, 8, 8)
        f_mac.setSpacing(4)
        f_mac.addWidget(QLabel(
            "One per line: <code>KEY = snippet</code>. Use "
            "<code>\\r</code> for Enter, <code>\\e</code> for ESC. "
            "Keys: <code>F1</code>..<code>F12</code>, "
            "<code>Ctrl+A</code>..<code>Ctrl+Z</code>, "
            "<code>Ctrl+Shift+letter</code> etc."))
        self.ed_macros = QPlainTextEdit()
        # Smaller default - 3 lines visible, grows on demand
        self.ed_macros.setMaximumHeight(80)
        self.ed_macros.setStyleSheet(
            "QPlainTextEdit { font-family: 'Consolas', monospace; "
            f"font-size: {scaled_font_px(11)}px; }}")
        self.ed_macros.setPlaceholderText(
            "(empty - add bindings here, e.g. F1 = ls\\r)")
        lines = []
        for k, v in sorted((s.macros or {}).items()):
            lines.append(f"{k} = {v}")
        self.ed_macros.setPlainText("\n".join(lines))
        f_mac.addWidget(self.ed_macros)
        outer.addWidget(g_mac)

        # --- Buttons --- (always at the bottom)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(
            "QPushButton { font-weight: bold; }")
        btn_ok.clicked.connect(self._on_ok)
        row.addWidget(btn_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        outer.addLayout(row)

    def _apply_preset(self, fields: dict) -> None:
        """Apply a quick-preset's field map to the dialog widgets.

        Each entry in `fields` is keyed by the widget's attribute
        name on the dialog (e.g. "cmb_enc") and valued either:

          - bool          for QCheckBox.setChecked
          - ("data", x)   for QComboBox - matches itemData(i) == x
          - ("text", x)   for QComboBox - matches text-only
          - other         set as currentText for combos / setText
                          for QLineEdit, best-effort fallback

        Bool/Tuple form means the user doesn't have to remember
        which combos use data-vs-text-matching. Easier to maintain
        the preset list as data declarations than as imperative
        sequences of widget setter calls.
        """
        from PyQt6.QtWidgets import (
            QComboBox, QCheckBox, QLineEdit, QSpinBox,
        )
        for attr, value in fields.items():
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
                continue
            if isinstance(widget, QComboBox):
                if (isinstance(value, tuple)
                        and len(value) == 2):
                    kind, target = value
                    if kind == "data":
                        for i in range(widget.count()):
                            if widget.itemData(i) == target:
                                widget.setCurrentIndex(i)
                                break
                        else:
                            # Couldn't match by data - fall back
                            # to text.
                            widget.setCurrentText(str(target))
                    elif kind == "text":
                        widget.setCurrentText(str(target))
                else:
                    widget.setCurrentText(str(value))
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))
                continue
            if isinstance(widget, QSpinBox):
                try:
                    widget.setValue(int(value))
                except (TypeError, ValueError):
                    pass

    def _browse_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose log file",
            self.ed_logpath.text() or "",
            "Log files (*.log *.txt);;All files (*)")
        if path:
            self.ed_logpath.setText(path)

    def _on_ok(self):
        s = self.result_session
        s.name = self.ed_name.text().strip() or "session"
        s.group = self.ed_group.text().strip()
        s.notes = self.ed_notes.toPlainText().rstrip()
        s.host = self.ed_host.text().strip()
        s.port = self.sp_port.value()
        s.protocol = self.cmb_proto.currentText()
        s.terminal_type = self.cmb_term.currentText()
        s.encoding = self.cmb_enc.currentText()
        s.font_family = self.cmb_font.currentText()
        s.font_size = self.sp_fsize.value()
        s.rows = self.sp_rows.value()
        s.cols = self.sp_cols.value()
        s.backspace_sends = self.cmb_bs.currentData()
        s.crlf_mode = self.cmb_crlf.currentData()
        s.local_echo = self.chk_echo.isChecked()
        s.autologin_user = self.ed_user.text()
        s.autologin_password = self.ed_pass.text()
        s.autologin_user_prompt = self.ed_uprompt.text()
        s.autologin_pass_prompt = self.ed_pprompt.text()
        s.autologin_delay_ms = self.sp_delay.value()
        s.keep_log = self.chk_log.isChecked()
        s.log_path = self.ed_logpath.text().strip()
        # Parse macros text: each line "KEY = snippet"
        macros = {}
        for line in self.ed_macros.toPlainText().splitlines():
            if "=" not in line:
                continue
            key, sep, snippet = line.partition("=")
            key = key.strip()
            snippet = snippet.lstrip(" \t")  # keep trailing spaces
            if key and snippet:
                macros[key] = snippet
        s.macros = macros
        self.accept()
