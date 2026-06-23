# date_time: 2026-06-23 10:19
"""Ultimate 64 VIC video streamer for Quopus.

Python port of DusteDdk/u64view (https://github.com/DusteDdk/u64view).

The Ultimate 64 firmware can stream the VIC video output and the
SID audio over UDP. This module:

  * Listens on UDP port 11000 for video packets and decodes them
    into a 384x272 frame buffer using the standard C64 palette.
  * Listens on UDP port 11001 for audio packets (48 kHz S16LSB
    stereo, 192 samples per packet) and queues them for playback.
  * Talks to the Ultimate 64 via its telnet interface on port 23
    to send the F5+arrow+enter key sequences that start/stop the
    stream from the menu.

Everything happens in worker QThreads so Quopus stays responsive
while the streamer window is open. The streamer is non-modal -
multiple instances can theoretically run side by side, though in
practice the Ultimate64 only emits one stream at a time.

Audio playback uses PyQt6.QtMultimedia.QAudioSink. If that import
fails (some Linux/Windows installs are missing the multimedia
plugin), audio is silently disabled and only video plays.
"""
import struct
import socket
import time
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QByteArray, QIODevice, QBuffer,
    QRegularExpression, QRect, QSize, QPoint,
)
from PyQt6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFontDatabase,
    QRegularExpressionValidator,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QInputDialog, QMessageBox, QComboBox, QFrame, QSizePolicy,
    QPlainTextEdit, QFileDialog, QRadioButton, QButtonGroup,
    QTableWidget, QTableWidgetItem, QHeaderView, QStyledItemDelegate,
    QLineEdit, QAbstractItemView, QStackedWidget, QCheckBox, QMenu,
    QWidget, QApplication, QLayout,
)

# Optional audio support. Some PyQt6 builds don't have multimedia
# installed by default - we degrade gracefully to silent video.
try:
    from PyQt6.QtMultimedia import (
        QAudioFormat, QAudioSink, QMediaDevices,
    )
    AUDIO_AVAILABLE = True
except Exception:
    AUDIO_AVAILABLE = False

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS, button_qss,
    get_mono_font,
)


# ---------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------

PORT_VIDEO = 11000        # UDP, Ultimate64 -> us
PORT_AUDIO = 11001        # UDP, Ultimate64 -> us
PORT_TELNET = 23          # TCP, us -> Ultimate64 (menu control)
PORT_HTTP = 80            # TCP, us -> Ultimate64 (REST API for run/mount)

# 384 visible pixels x 272 visible lines (PAL, including borders)
FRAME_W = 384
FRAME_H = 272

# Video packet header: 12 bytes followed by up to 768 payload bytes.
# Packed little-endian (network captures confirm LE on the wire).
#   uint16 seq
#   uint16 frame
#   uint16 line          (top bit = vsync/end-of-frame flag)
#   uint16 pixelsInLine
#   uint8  linesInPacket
#   uint8  bpp
#   uint16 encoding
VIDEO_HEADER_FMT = "<HHHHBBH"
VIDEO_HEADER_SIZE = struct.calcsize(VIDEO_HEADER_FMT)
assert VIDEO_HEADER_SIZE == 12

# Audio packet header is just a uint16 seq, then 192 stereo samples
# in S16LE format (left/right interleaved). Total payload = 192 * 2
# channels * 2 bytes = 768 bytes. UDP packet size = 770 bytes incl
# the 2-byte header. (Spec at
# https://1541u-documentation.readthedocs.io/en/latest/data_streams.html )
AUDIO_HEADER_FMT = "<H"
AUDIO_HEADER_SIZE = struct.calcsize(AUDIO_HEADER_FMT)
AUDIO_SAMPLES_PER_PACKET = 192        # stereo frames per packet
AUDIO_PAYLOAD_SIZE = AUDIO_SAMPLES_PER_PACKET * 2 * 2   # 768 bytes

# Standard C64 palette (matches sred/sgreen/sblue in u64view main.c).
# Order: black, white, red, cyan, purple, green, blue, yellow,
#        orange, brown, pink, dark-grey, grey, light-green,
#        light-blue, light-grey.
C64_PALETTE = [
    (0x00, 0x00, 0x00),    # 0  black
    (0xff, 0xff, 0xff),    # 1  white
    (0x68, 0x37, 0x2b),    # 2  red
    (0x70, 0xa4, 0xb2),    # 3  cyan
    (0x6f, 0x3d, 0x86),    # 4  purple
    (0x58, 0x8d, 0x43),    # 5  green
    (0x35, 0x28, 0x79),    # 6  blue
    (0xb8, 0xc7, 0x6f),    # 7  yellow
    (0x6f, 0x4f, 0x25),    # 8  orange
    (0x43, 0x39, 0x00),    # 9  brown
    (0x9a, 0x67, 0x59),    # 10 pink
    (0x44, 0x44, 0x44),    # 11 dark-grey
    (0x6c, 0x6c, 0x6c),    # 12 grey
    (0x9a, 0xd2, 0x84),    # 13 light-green
    (0x6c, 0x5e, 0xb5),    # 14 light-blue
    (0x95, 0x95, 0x95),    # 15 light-grey
]


def _build_pixmap_lut():
    """Precompute a 256-entry table that converts a packed byte
    (two 4-bit indices into the C64 palette) into 8 bytes of RGBA
    output (2 pixels x 4 bytes each).

    Pixel ordering in the U64 stream: LOW nibble is the LEFT pixel,
    HIGH nibble is the RIGHT pixel. Verified against the slow-path
    code in u64view's main.c which renders the low nibble at x*2
    (the even/left column) and the high nibble at x*2+1.

    By doing this lookup once per packet byte instead of two
    palette indexes per byte, we save a LOT of CPU - the C
    original calls this 'fast' mode.
    """
    lut = [None] * 256
    for b in range(256):
        ph = (b & 0xf0) >> 4    # right pixel
        pl = b & 0x0f           # left pixel
        rl, gl, bl = C64_PALETTE[pl]
        rh, gh, bh = C64_PALETTE[ph]
        # Qt's RGBA8888 = R, G, B, A in memory order on a little-
        # endian host. Two pixels = 8 bytes total.
        # Left pixel first (low addresses), then right pixel.
        lut[b] = bytes([
            rl, gl, bl, 0xff,
            rh, gh, bh, 0xff,
        ])
    return lut


PIXMAP_LUT = _build_pixmap_lut()


# ---------------------------------------------------------------------
# Telnet control - sends F5+arrows+enter sequences to the U64 menu
# ---------------------------------------------------------------------

# Same byte sequences as u64view's startStream/stopStream/reset/
# powerOff functions. They navigate the U64 boot menu via
# F5 -> N down arrows -> Enter.
TELNET_F5         = bytes([0x1b, 0x5b, 0x31, 0x35, 0x7e])
TELNET_DOWN       = bytes([0x1b, 0x5b, 0x42])
TELNET_ENTER      = bytes([0x0d, 0x00])

# Build the actual sequences. These match u64view 1.0.0 firmware
# layout - newer firmwares may need different counts; if Mario's
# U64 doesn't react to these, we'll need to adjust the down-counts.
SEQ_START_STREAM = (TELNET_F5
    + TELNET_DOWN * 8
    + TELNET_ENTER * 3)
SEQ_STOP_STREAM  = (TELNET_F5
    + TELNET_DOWN * 8
    + TELNET_ENTER * 2)
SEQ_RESET = (TELNET_F5
    + TELNET_DOWN
    + TELNET_ENTER * 2)
SEQ_POWEROFF = (TELNET_F5
    + TELNET_DOWN
    + TELNET_ENTER
    + TELNET_DOWN * 2
    + TELNET_ENTER)


def send_telnet_sequence(host: str, data: bytes,
                            port: int = PORT_TELNET,
                            timeout: float = 3.0):
    """Send a control sequence to the Ultimate64's telnet menu.

    The C original sends ONE BYTE AT A TIME with 1ms delays and
    drains incoming data between bytes, because the U64 telnet
    server is sensitive to timing. We replicate that behaviour
    here - it's slow (~50ms per sequence) but it's run-once-per-
    button-click so it doesn't matter.

    Returns (True, "") on success or (False, error_message).
    """
    try:
        sock = socket.create_connection((host, port),
                                          timeout=timeout)
    except Exception as e:
        return False, f"Could not connect to {host}:{port}: {e}"
    try:
        # Non-blocking so the drain reads return EWOULDBLOCK quickly
        # instead of stalling the loop.
        sock.setblocking(False)
        time.sleep(0.01)
        for byte_val in data:
            try:
                sock.sendall(bytes([byte_val]))
            except (BlockingIOError, InterruptedError):
                # Retry once, slow down a bit
                time.sleep(0.005)
                try:
                    sock.sendall(bytes([byte_val]))
                except Exception as e:
                    return False, f"Send failed mid-sequence: {e}"
            # Drain incoming reply bytes (echo, prompt updates) so
            # the U64's send buffer doesn't stall.
            time.sleep(0.001)
            try:
                while True:
                    data = sock.recv(1024)
                    if not data:
                        break
            except (BlockingIOError, InterruptedError):
                pass    # nothing waiting, that's fine
            except Exception:
                pass    # ignore drain errors, not fatal
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return True, ""


# ---------------------------------------------------------------------
# Ultimate64 HTTP REST API (firmware >= 3.11)
# ---------------------------------------------------------------------
# These run a PRG / mount a D64 / etc. by hitting the U64's HTTP
# server (default TCP 80). Used by the drag-and-drop autostart
# feature - drop a .prg onto the streamer window, it gets uploaded
# and run on the U64.
#
# The API endpoint structure is documented at
#   https://1541u-documentation.readthedocs.io/en/latest/api/api_calls.html
#
# All routes return a JSON body with at least an "errors" array,
# which we forward to the caller as part of the (ok, message)
# tuple result.

import urllib.request
import urllib.parse
import json as _json
from .config import scaled_font_px, scaled_px


def _u64_http_post(host: str, path: str, body: bytes,
                     password: str = "",
                     content_type: str = "application/octet-stream",
                     port: int = 80, timeout: float = 30.0):
    """POST a binary payload to the Ultimate's HTTP API.

    `path` is the part after the host - e.g. "/v1/runners:run_prg"
    or "/v1/drives/a:mount?mode=readonly". `body` is the file
    bytes (PRG/CRT/D64/SID etc.), uploaded as the request body.

    Returns (True, "") on success, (False, error_message) on
    failure. HTTP-level errors and JSON-level errors (entries in
    the 'errors' array of the response) are both flattened to the
    error message.
    """
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    if password:
        req.add_header("X-Password", password)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            try:
                data = _json.loads(txt)
            except Exception:
                data = {"errors": []}
            errs = data.get("errors") or []
            if errs:
                return False, "U64 API errors: " + "; ".join(errs)
            return True, ""
    except urllib.error.HTTPError as e:
        # Read the body for a more detailed error message if any.
        try:
            tail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            tail = ""
        return False, f"HTTP {e.code} from U64{(': '+tail) if tail else ''}"
    except urllib.error.URLError as e:
        return False, f"Cannot reach U64 at {host}: {e.reason}"
    except Exception as e:
        return False, f"U64 API call failed: {e}"


def _u64_http_put(host: str, path: str, password: str = "",
                    port: int = 80, timeout: float = 10.0):
    """PUT request without a body - used for menu_button, reset,
    drives:remove etc. Same return convention as _u64_http_post."""
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, method="PUT")
    if password:
        req.add_header("X-Password", password)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from U64"
    except urllib.error.URLError as e:
        return False, f"Cannot reach U64 at {host}: {e.reason}"
    except Exception as e:
        return False, f"U64 API call failed: {e}"


def u64_run_prg(host: str, prg_bytes: bytes, password: str = "",
                  port: int = 80):
    """Reset the U64 and run the given PRG via DMA load. The PRG
    starts running automatically. Used for direct .prg drops."""
    return _u64_http_post(host, "/v1/runners:run_prg", prg_bytes,
                            password=password, port=port)


def u64_run_crt(host: str, crt_bytes: bytes, password: str = "",
                  port: int = 80):
    """Reset the U64 with the given .crt cartridge active."""
    return _u64_http_post(host, "/v1/runners:run_crt", crt_bytes,
                            password=password, port=port)


def u64_play_sid(host: str, sid_bytes: bytes, songnr: int = 0,
                   password: str = "", port: int = 80):
    """Play a .sid file. Songnr 0 = default song."""
    path = "/v1/runners:sidplay"
    if songnr > 0:
        path += f"?songnr={songnr}"
    return _u64_http_post(host, path, sid_bytes,
                            password=password, port=port)


def u64_play_mod(host: str, mod_bytes: bytes, password: str = "",
                   port: int = 80):
    """Play an Amiga .mod file."""
    return _u64_http_post(host, "/v1/runners:modplay", mod_bytes,
                            password=password, port=port)


def u64_mount_disk(host: str, disk_bytes: bytes, drive: str = "a",
                     mode: str = "readonly", disk_type: str = "",
                     password: str = "", port: int = 80):
    """Mount a disk image (D64/D71/D81/G64) on the specified drive.
    `disk_type` defaults to "" which lets the U64 sniff it from the
    Content-Disposition. `mode` is readwrite | readonly | unlinked."""
    path = f"/v1/drives/{drive}:mount?mode={mode}"
    if disk_type:
        path += f"&type={disk_type}"
    return _u64_http_post(host, path, disk_bytes,
                            password=password, port=port)


def u64_reset(host: str, password: str = "", port: int = 80):
    """Soft-reset the C64."""
    return _u64_http_put(host, "/v1/machine:reset",
                           password=password, port=port)


def u64_reboot(host: str, password: str = "", port: int = 80):
    """Reboot the Ultimate device (re-initializes cartridge config
    and sends a reset). Per the REST API spec, this is heavier than
    a plain reset - it restarts the whole Ultimate firmware."""
    return _u64_http_put(host, "/v1/machine:reboot",
                           password=password, port=port)


def u64_poweroff(host: str, password: str = "", port: int = 80):
    """Power off the U64 (Ultimate-64 only command - not available
    on Ultimate-II+). Per the spec: "it is likely that you won't
    receive a valid response" - that's expected behavior, not an
    error. We swallow connection-closed errors here."""
    ok, resp = _u64_http_put(host, "/v1/machine:poweroff",
                                password=password, port=port)
    # poweroff cuts the connection mid-response - normalize to True
    # if we got far enough to send the request.
    if not ok and resp and any(
        s in resp.lower()
        for s in ("connection", "reset by peer", "closed", "timeout")
    ):
        return True, "powered off (connection closed as expected)"
    return ok, resp


def u64_pause(host: str, password: str = "", port: int = 80):
    """Pause the C64 by pulling the DMA line low - freezes the CPU
    at a safe moment. Timers continue running. Resume with u64_resume."""
    return _u64_http_put(host, "/v1/machine:pause",
                           password=password, port=port)


def u64_resume(host: str, password: str = "", port: int = 80):
    """Resume the C64 from a paused state - releases the DMA line
    and the CPU continues where it left off."""
    return _u64_http_put(host, "/v1/machine:resume",
                           password=password, port=port)


def u64_menu_button(host: str, password: str = "", port: int = 80):
    """Simulate pressing the Menu button (Ultimate cart) / Multi
    Button (U64). Enters or exits the Ultimate menu system based on
    current state."""
    return _u64_http_put(host, "/v1/machine:menu_button",
                           password=password, port=port)


# -----------------------------------------------------------------
# Device info routes (GET)
# -----------------------------------------------------------------

def u64_info(host: str, password: str = "", port: int = 80,
              timeout: float = 5.0):
    """GET /v1/info - returns device info as dict with keys:
    product, firmware_version, fpga_version, core_version (U64
    only), hostname, unique_id. Returns (True, dict) or
    (False, error_str)."""
    import http.client, json
    try:
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        conn.request("GET", "/v1/info", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        return True, d
    except Exception as e:
        return False, str(e)


def u64_version(host: str, password: str = "", port: int = 80,
                  timeout: float = 5.0):
    """GET /v1/version - returns REST API version string.
    Returns (True, str) or (False, error_str)."""
    import http.client, json
    try:
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        conn.request("GET", "/v1/version", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        return True, d.get("version", "unknown")
    except Exception as e:
        return False, str(e)


# -----------------------------------------------------------------
# Drive routes (mount, reset, on/off, mode, remove)
# -----------------------------------------------------------------

def u64_get_drives(host: str, password: str = "", port: int = 80,
                     timeout: float = 5.0):
    """GET /v1/drives - returns full drive info (a/b/softiec) as a
    dict. Returns (True, dict) or (False, error_str). Useful for
    showing what's currently mounted in the U64 streamer UI."""
    import http.client, json
    try:
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        conn.request("GET", "/v1/drives", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        return True, d
    except Exception as e:
        return False, str(e)


def u64_drive_reset(host: str, drive: str = "a",
                      password: str = "", port: int = 80):
    """Reset the specified drive ('a' or 'b')."""
    return _u64_http_put(host, f"/v1/drives/{drive}:reset",
                           password=password, port=port)


def u64_drive_remove(host: str, drive: str = "a",
                       password: str = "", port: int = 80):
    """Remove the mounted disk from the specified drive."""
    return _u64_http_put(host, f"/v1/drives/{drive}:remove",
                           password=password, port=port)


def u64_drive_on(host: str, drive: str = "a",
                   password: str = "", port: int = 80):
    """Turn on the specified drive (or reset it if already on)."""
    return _u64_http_put(host, f"/v1/drives/{drive}:on",
                           password=password, port=port)


def u64_drive_off(host: str, drive: str = "a",
                    password: str = "", port: int = 80):
    """Turn off the specified drive - removes it from the serial bus."""
    return _u64_http_put(host, f"/v1/drives/{drive}:off",
                           password=password, port=port)


def u64_drive_set_mode(host: str, drive: str = "a", mode: str = "1541",
                          password: str = "", port: int = 80):
    """Change drive mode. Valid values: '1541', '1571', '1581'.
    Also reloads the drive's default ROM."""
    path = f"/v1/drives/{drive}:set_mode?mode={mode}"
    return _u64_http_put(host, path, password=password, port=port)


# -----------------------------------------------------------------
# Configuration routes (GET/PUT/POST configs)
# -----------------------------------------------------------------

def u64_get_config_categories(host: str, password: str = "",
                                  port: int = 80, timeout: float = 5.0):
    """GET /v1/configs - returns list of all config category names.
    Returns (True, list_of_strings) or (False, error)."""
    import http.client, json
    try:
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        conn.request("GET", "/v1/configs", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        return True, d.get("categories", [])
    except Exception as e:
        return False, str(e)


def u64_get_config_category(host: str, category: str,
                                password: str = "", port: int = 80,
                                timeout: float = 5.0):
    """GET /v1/configs/<category> - returns all items in one category
    as a dict. The category is URL-escaped (spaces -> %20)."""
    import http.client, json, urllib.parse
    try:
        cat = urllib.parse.quote(category, safe='')
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        conn.request("GET", f"/v1/configs/{cat}", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        # The response has the category name as a top-level key
        # plus 'errors'. Extract the category dict.
        if category in d:
            return True, d[category]
        # Try first non-errors key
        for k, v in d.items():
            if k != "errors":
                return True, v
        return True, {}
    except Exception as e:
        return False, str(e)


def u64_get_config_definitions(host: str, category: str,
                                  password: str = "", port: int = 80,
                                  timeout: float = 8.0):
    """GET /v1/configs/<category>/* - returns the full definition
    schema for every item in the category.

    Each item is reported as a dict with fields like:
        {"current": 8, "min": 8, "max": 11, "format": "%d",
         "default": 8}
    For enum-style items the schema also includes a "values" list
    (or "options"), e.g.
        {"current": "PAL", "values": ["PAL", "NTSC", "DREAN"],
         "default": "PAL"}

    This is what we need to render real combo boxes / spin boxes
    in the Config Editor instead of treating every value as a free
    LineEdit. Falls back to an empty dict on any error - the caller
    should then use u64_get_config_category() for the bare values.
    """
    import http.client, json, urllib.parse
    try:
        cat = urllib.parse.quote(category, safe='')
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {"X-Password": password} if password else {}
        # The wildcard "*" means "every item". Without it we'd
        # need one GET per item which thrashes the device.
        conn.request("GET", f"/v1/configs/{cat}/*", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        d = json.loads(body)
        # Response shape:
        #   {"<category>": {"<item>": {"current":..., ...}, ...},
        #    "errors": [...]}
        # We strip the wrapping and return just the per-item map.
        if category in d:
            return True, d[category]
        for k, v in d.items():
            if k != "errors" and isinstance(v, dict):
                return True, v
        return True, {}
    except Exception as e:
        return False, str(e)


def u64_set_config(host: str, category: str, item: str, value,
                     password: str = "", port: int = 80):
    """PUT /v1/configs/<category>/<item>?value=<value> - set one
    config item. Value gets stringified and URL-escaped."""
    import urllib.parse
    cat = urllib.parse.quote(category, safe='')
    itm = urllib.parse.quote(item, safe='')
    val = urllib.parse.quote(str(value), safe='')
    path = f"/v1/configs/{cat}/{itm}?value={val}"
    return _u64_http_put(host, path, password=password, port=port)


def u64_set_configs_bulk(host: str, configs: dict,
                            password: str = "", port: int = 80,
                            timeout: float = 30.0):
    """POST /v1/configs with a JSON body containing multiple
    category/item/value mappings. `configs` is a dict like:
        {"Drive A Settings": {"Drive Bus ID": 9, "Drive": "Enabled"}}
    Returns (True, response_dict) or (False, error)."""
    import http.client, json
    try:
        body = json.dumps(configs).encode("utf-8")
        conn = http.client.HTTPConnection(host, port,
                                            timeout=timeout)
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        if password:
            headers["X-Password"] = password
        conn.request("POST", "/v1/configs", body, headers)
        resp = conn.getresponse()
        ok = (resp.status == 200)
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if ok:
            return True, json.loads(resp_body) if resp_body else {}
        return False, f"HTTP {resp.status}: {resp_body}"
    except Exception as e:
        return False, str(e)


def u64_config_save_to_flash(host: str, password: str = "",
                                  port: int = 80):
    """PUT /v1/configs:save_to_flash - write current config to NVM."""
    return _u64_http_put(host, "/v1/configs:save_to_flash",
                           password=password, port=port)


def u64_config_load_from_flash(host: str, password: str = "",
                                    port: int = 80):
    """PUT /v1/configs:load_from_flash - restore config from NVM."""
    return _u64_http_put(host, "/v1/configs:load_from_flash",
                           password=password, port=port)


def u64_config_reset_to_default(host: str, password: str = "",
                                     port: int = 80):
    """PUT /v1/configs:reset_to_default - factory reset current
    config (does NOT touch NVM)."""
    return _u64_http_put(host, "/v1/configs:reset_to_default",
                           password=password, port=port)


def u64_backup_all_configs(host: str, password: str = "",
                                port: int = 80, timeout: float = 30.0):
    """Fetch all config categories from the device and return them
    as a nested dict suitable for save-to-file or for restore via
    u64_set_configs_bulk(). Returns (True, big_dict) on success."""
    ok, cats = u64_get_config_categories(host, password, port,
                                            timeout=timeout)
    if not ok:
        return False, cats
    backup = {}
    for category in cats:
        ok, items = u64_get_config_category(host, category,
                                                password=password,
                                                port=port,
                                                timeout=timeout)
        if not ok:
            return False, f"Failed on category '{category}': {items}"
        backup[category] = items
    return True, backup


# -----------------------------------------------------------------
# Network device discovery (UDP broadcast)
# -----------------------------------------------------------------

def u64_discover(timeout: float = 2.0, port: int = 64):
    """Discover Ultimate devices on the LAN via UDP broadcast on the
    Ultimate Ident service port (default 64). Returns a list of
    (ip, hostname, product, firmware) tuples.

    The Ultimate firmware listens on UDP port 64 for an "Ultimate
    Ident" broadcast. When it receives any packet on this port it
    replies with a JSON string containing product / firmware /
    hostname / unique_id.

    Note: this requires "Ultimate Ident" service to be enabled in
    the U64's network settings.
    """
    import socket, json
    found = []
    seen_ips = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        # Send broadcast probe
        try:
            sock.sendto(b"\x00", ("255.255.255.255", port))
        except Exception:
            pass
        # Listen for replies for `timeout` seconds total
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
                ip = addr[0]
                if ip in seen_ips:
                    continue
                seen_ips.add(ip)
                # Try to parse as JSON
                try:
                    info = json.loads(data.decode("utf-8",
                                                      errors="replace"))
                    found.append((
                        ip,
                        info.get("hostname", "?"),
                        info.get("product", "?"),
                        info.get("firmware_version",
                                  info.get("firmware", "?")),
                    ))
                except Exception:
                    # Non-JSON reply, just note the IP
                    found.append((ip, "?", "?", "?"))
            except socket.timeout:
                continue
            except Exception:
                break
        sock.close()
    except Exception:
        pass
    # Fall back: try a /v1/info GET on common candidate IPs in the
    # local /24 if nothing replied to broadcast.
    if not found:
        local_ip = _local_ip_for_host("255.255.255.255", 80)
        if local_ip and local_ip != "0.0.0.0":
            base = ".".join(local_ip.split(".")[:3]) + "."
            # Try a small probe set instead of /24 sweep
            for last in (64, 100, 178, 200):
                ip = base + str(last)
                ok, info = u64_info(ip, timeout=0.5)
                if ok:
                    found.append((
                        ip,
                        info.get("hostname", "?"),
                        info.get("product", "?"),
                        info.get("firmware_version", "?"),
                    ))
    return found


def u64_writemem(host: str, address: int, data: bytes,
                   password: str = "", port: int = 80):
    """Write up to 128 bytes to C64 memory via DMA. `address` is
    the destination, `data` is what to write (must be <=128 bytes
    per spec). Used by u64_type_text() to poke the keyboard buffer.
    """
    if len(data) > 128:
        return False, "writemem max is 128 bytes per call"
    path = f"/v1/machine:writemem?address={address:04X}"
    return _u64_http_post(host, path, data,
                            password=password, port=port,
                            content_type="application/octet-stream")


def u64_readmem(host: str, address: int, length: int = 256,
                  password: str = "", port: int = 80,
                  timeout: float = 30.0):
    """Read `length` bytes of C64 memory starting at `address` via
    the U64's DMA cartridge bus. Returns (True, bytes) on success
    or (False, error_message).

    Per the U64 REST API spec, /v1/machine:readmem returns the raw
    bytes as a binary attachment (not JSON). The `length` parameter
    defaults to 256 if not given. No documented per-call hard cap,
    but very large reads can take noticeable time on the U64 since
    it has to halt the CPU via DMA for the duration. We split big
    requests into 4 KB chunks so we don't hold the bus too long in
    one go and so the timeout per chunk stays reasonable.
    """
    if length <= 0:
        return False, "length must be >= 1"
    if address < 0 or address > 0xFFFF:
        return False, "address must be in $0000..$FFFF"
    if address + length > 0x10000:
        return False, "read would wrap past $FFFF"

    CHUNK = 4096
    out = bytearray()
    remaining = length
    cur = address
    while remaining > 0:
        n = min(CHUNK, remaining)
        url = (f"http://{host}:{port}/v1/machine:readmem"
                 f"?address={cur:04X}&length={n}")
        req = urllib.request.Request(url, method="GET")
        if password:
            req.add_header("X-Password", password)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code} from U64 readmem"
        except urllib.error.URLError as e:
            return False, f"Cannot reach U64 at {host}: {e.reason}"
        except Exception as e:
            return False, f"U64 readmem failed: {e}"
        if len(body) != n:
            # The U64 should always return exactly `length` bytes.
            # If not, we got something funny - report and bail.
            return False, (f"readmem returned {len(body)} bytes, "
                              f"expected {n} (at ${cur:04X})")
        out.extend(body)
        cur += n
        remaining -= n
    return True, bytes(out)


# C64 memory addresses for the keyboard buffer:
#   $0277-$0280 (10 bytes) - the actual buffer (PETSCII chars to be processed)
#   $00C6 (NDX) - number of characters currently in the buffer
# The KERNAL's IRQ pulls from this buffer ~60 times/sec, so to inject
# typing we just write PETSCII chars to $0277+ and set $00C6 to the
# count. Trick used by Gideon's own socket interface and copied by
# every U64 control tool since.
_KEYBUF_ADDR = 0x0277
_KEYBUF_NDX  = 0x00C6
_KEYBUF_SIZE = 10


def _ascii_char_to_petscii(ch: str) -> int:
    """Convert one ASCII char to a PETSCII byte for keyboard
    injection. Returns -1 if no sensible mapping.

    The C64 reads keyboard codes via the KERNAL IRQ which converts
    them via the keyboard matrix table - the value we poke into the
    keyboard buffer at $0277 is the PETSCII code that would have
    been produced if the user had typed that key.

    For the unshifted ("uppercase + graphics") mode that the C64
    powers up in:
      - 'A'-'Z' (typed unshifted) -> 0x41..0x5A (PETSCII uppercase)
      - 'a'-'z' (typed unshifted in lowercase mode) -> 0x41..0x5A
        as well, since the screen-character mapping handles the
        case switch in hardware. We map both ranges to 0x41..0x5A
        because that's what the BASIC tokenizer expects to see in
        commands like LOAD, RUN, LIST regardless of which mode the
        screen is currently in.
      - digits, space, basic punctuation: passthrough
      - newline -> 0x0D (RETURN)
      - backspace/del -> 0x14 (DEL/INSERT)

    Note: an earlier version of this function mapped 'A'-'Z' to
    0xC1..0xDA (shifted PETSCII uppercase) which only worked in
    the lowercase/shifted screen mode. After a RESET the C64 is
    in unshifted mode, so 0xC1..0xDA would render as graphics
    characters and break LOAD/RUN/LIST commands. The current
    mapping works in both modes.
    """
    if ch == '\n' or ch == '\r':
        return 0x0D
    if ch == '\b' or ch == '\x7f':
        return 0x14
    if ch == '\t':
        return 0x09
    code = ord(ch)
    if 0x20 <= code <= 0x3F:
        # Space, digits, basic punctuation - passthrough
        return code
    if 0x41 <= code <= 0x5A:    # 'A'-'Z' ASCII
        # PETSCII uppercase is at 0x41-0x5A. Direct passthrough
        # works in both screen modes.
        return code
    if 0x61 <= code <= 0x7A:    # 'a'-'z' ASCII
        # Map lowercase to uppercase PETSCII (0x41-0x5A) for
        # command typing - BASIC keywords are case-insensitive
        # at the tokenizer level but the buffer must contain
        # uppercase to match the LOAD/RUN/LIST tokens.
        return code - 0x20
    if code in (0x40, 0x5B, 0x5D, 0x5E, 0x5F, 0x60):
        # @, [, ], ^, _, `
        return code
    return -1


def u64_type_text(host: str, text: str, password: str = "",
                    port: int = 80, chunk_delay: float = 0.18):
    """Inject `text` into the C64's keyboard buffer.

    Workflow per chunk of up to 10 chars:
      1. Convert ASCII -> PETSCII bytes
      2. writemem $0277 with the bytes
      3. writemem $00C6 with the count
      4. Sleep ~180ms - just long enough for the KERNAL IRQ to
         consume those keys (it pulls ~60/sec, 10 chars = 167ms).

    Anything that doesn't have a sensible PETSCII mapping is
    silently dropped. Special keys (RETURN, F-keys) can be
    embedded as the literal PETSCII codes by passing bytes
    instead of str (use u64_type_petscii_bytes for that).

    Returns (True, "") on success, (False, error) if any chunk
    fails. Successful chunks before the failure are kept on the
    C64 - we don't roll back.
    """
    pet = []
    for ch in text:
        b = _ascii_char_to_petscii(ch)
        if b >= 0:
            pet.append(b)
    return u64_type_petscii_bytes(
        host, bytes(pet), password=password, port=port,
        chunk_delay=chunk_delay)


def u64_type_petscii_bytes(host: str, pet: bytes,
                              password: str = "", port: int = 80,
                              chunk_delay: float = 0.18):
    """Inject already-PETSCII bytes into the C64's keyboard buffer.
    Used directly by direct-keypress forwarding which has already
    done its own ASCII->PETSCII mapping.
    """
    if not pet:
        return True, ""
    for i in range(0, len(pet), _KEYBUF_SIZE):
        chunk = pet[i:i + _KEYBUF_SIZE]
        ok, msg = u64_writemem(host, _KEYBUF_ADDR, chunk,
                                  password=password, port=port)
        if not ok:
            return False, f"keybuf write failed: {msg}"
        ok, msg = u64_writemem(host, _KEYBUF_NDX,
                                  bytes([len(chunk)]),
                                  password=password, port=port)
        if not ok:
            return False, f"keybuf NDX write failed: {msg}"
        # Pause for the KERNAL to drain the buffer. Chunks shorter
        # than 10 still pause the same amount - tiny overhead, but
        # keeps logic simple.
        if i + _KEYBUF_SIZE < len(pet):
            time.sleep(chunk_delay)
    return True, ""


# Matrix-Code for Space bar (row 7, col 4 -> 7*8+4 = 60 = $3C).
# Zero-page locations that mirror the most-recently-pressed key:
#   $00C5 (LSTX)  - last key scanned, updated by KERNAL IRQ
#   $00CB (SFDX)  - current key in matrix-scan register
# These are what most "press any key" / "press space to continue"
# routines poll, since reading the raw matrix at $DC00/$DC01 is
# more work and only games doing direct hardware scanning need it.
_C64_MATRIX_SPACE = 0x3C
_C64_LSTX = 0x00C5
_C64_SFDX = 0x00CB
_CIA1_PORTA = 0xDC00
_CIA1_PORTB = 0xDC01


def u64_press_space_burst(host: str, password: str = "", port: int = 80):
    """Try every reasonable trick to make the C64 think SPACE was
    pressed. Used by the SPC button when the user is in an intro or
    game that scans the keyboard matrix directly (rather than going
    through the KERNAL keyboard buffer).

    We do four things in quick succession:

    1. KERNAL buffer poke at $0277 + $00C6 (the normal path; works
       for BASIC and any KERNAL-using software).
    2. Matrix-code poke at $00C5 and $00CB ($3C = SPACE). Many demos
       and intros poll $C5 directly for "press any key" detection
       because it's simpler than scanning the matrix themselves.
    3. CIA1 Port B poke: bit 4 cleared = fire pressed on joystick
       port 1 (same hardware line as SPACE in the matrix). Some
       intros accept fire-on-stick-1 as equivalent to SPACE.
    4. CIA1 Port A poke: same trick for joystick port 2.

    Steps 3 and 4 are best-effort - the CIA continuously rewrites
    its port registers based on the actual hardware pins, so our
    poked value is only valid for the few CPU cycles before the
    next CIA refresh. But during those cycles the running code
    might read it, so it's worth trying.

    Fire-and-forget: we return success if the KERNAL-buffer poke
    went through; the other attempts are bonus shots and don't
    fail the whole call.
    """
    # 1. KERNAL buffer
    ok, msg = u64_writemem(
        host, _KEYBUF_ADDR, bytes([0x20]),    # PETSCII space
        password=password, port=port)
    if not ok:
        return False, f"keybuf write failed: {msg}"
    ok, msg = u64_writemem(
        host, _KEYBUF_NDX, bytes([1]),
        password=password, port=port)
    if not ok:
        return False, f"keybuf NDX write failed: {msg}"

    # 2. LSTX / SFDX zero-page mirror. Fehler ignorieren - das ist
    #    eine Zusatzchance, keine Kernfunktion.
    try:
        u64_writemem(host, _C64_LSTX, bytes([_C64_MATRIX_SPACE]),
                       password=password, port=port)
        u64_writemem(host, _C64_SFDX, bytes([_C64_MATRIX_SPACE]),
                       password=password, port=port)
    except Exception:
        pass

    # 3. + 4. CIA Port-Bytes. Bit 4 = Fire (active LOW).
    #    Port A ($DC00) = Joystick 2, Port B ($DC01) = Joystick 1.
    #    Wert $EF = 1110 1111 = nur Bit 4 (Fire) low.
    try:
        u64_writemem(host, _CIA1_PORTB, bytes([0xEF]),
                       password=password, port=port)
        u64_writemem(host, _CIA1_PORTA, bytes([0xEF]),
                       password=password, port=port)
    except Exception:
        pass

    return True, ""


def _local_ip_for_host(host: str, hint_port: int = 80) -> str:
    """Determine which of our local IPs the OS would use to reach
    `host`. Used to tell the U64 where to send its UDP stream
    packets when we don't want to hard-code our IP.

    Trick: we open an unconnected UDP socket and ask the kernel to
    'connect' it to the target. That doesn't actually send anything
    (UDP) but it triggers route selection and getsockname() then
    gives us the local IP for that route. Falls back to '127.0.0.1'
    if anything goes wrong - which means the U64 would try to send
    packets to itself, but at least we don't crash.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, hint_port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def u64_stream_start(host: str, stream_name: str,
                       target_ip: str = "",
                       target_port: int = 0,
                       password: str = "",
                       port: int = 80):
    """Tell the U64 to start emitting one of its data streams.

    `stream_name` is "video", "audio", or "debug".
    `target_ip` is the address where the U64 should send the UDP
    packets. If empty, we auto-detect the local IP that reaches
    the U64 - that's the right answer in 99% of setups.
    `target_port` overrides the default port (11000/11001/11002
    for v/a/d). 0 means use the default.

    Per the docs at
      https://1541u-documentation.readthedocs.io/en/latest/api/api_calls.html
    this is the modern alternative to the telnet-menu navigation
    that older tools used. Doesn't require sending F5+arrows+enter
    bytes that depend on the U64's menu layout.

    The 'ip' argument is REQUIRED by the U64 firmware - it has no
    way to know where to send packets otherwise.
    """
    ip = target_ip or _local_ip_for_host(host, port)
    if target_port:
        ip_arg = f"{ip}:{target_port}"
    else:
        ip_arg = ip
    path = (f"/v1/streams/{stream_name}:start"
              f"?ip={urllib.parse.quote(ip_arg)}")
    return _u64_http_put(host, path, password=password, port=port)


def u64_stream_stop(host: str, stream_name: str, password: str = "",
                      port: int = 80):
    """Stop one of the U64's data streams. stream_name as for
    u64_stream_start."""
    path = f"/v1/streams/{stream_name}:stop"
    return _u64_http_put(host, path, password=password, port=port)


# ---------------------------------------------------------------------
# Memory-view helpers
# ---------------------------------------------------------------------
# Fuer die ASM-Anzeige im MemoryViewDialog nutzen wir das schon
# vorhandene `c64_disasm` Modul (volle 6502 + illegal opcodes +
# 65C02 Unterstuetzung). Der Import ist lazy in _render(), damit
# beim Modul-Import keine Probleme entstehen falls c64_disasm
# selbst irgendwann mal von u64_streamer-Symbolen abhinge.

def format_hexdump(data: bytes, start_addr: int, bytes_per_row: int = 16):
    """Klassischer Hex-Dump: ADDR  HEX...  |ASCII|"""
    out = []
    for off in range(0, len(data), bytes_per_row):
        chunk = data[off:off + bytes_per_row]
        addr = (start_addr + off) & 0xFFFF
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        hex_part = hex_part.ljust(bytes_per_row * 3 - 1)
        ascii_part = "".join(
            chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        out.append(f"{addr:04X}  {hex_part}  |{ascii_part}|")
    return "\n".join(out)


def format_disasm_lines(lines):
    """Formatiere c64_disasm._DisasmLine-Objekte als plain text.
    Spaltenbreiten passen zu c64_disasm.build_render_data:
    ADDR(4)  BYTES(9) MNEMONIC(6) OPERAND[ ; COMMENT]
    """
    out = []
    for ln in lines:
        bs = " ".join(f"{b:02X}" for b in ln.bytes)
        line = f"{ln.pc:04X}  {bs:<9} {ln.mnemonic:<6} {ln.operand}"
        if ln.comment:
            line += f" ; {ln.comment}"
        out.append(line)
    return "\n".join(out)


def _parse_c64_address(s: str) -> int:
    """Parse '$C000', '0xC000', 'C000', '.49152', '%1010' -> int.
    Hex ist Default (C64-Konvention).
    """
    s = s.strip()
    if not s:
        raise ValueError("empty address")
    if s.startswith("$"):
        value = int(s[1:], 16)
    elif s.startswith(("0x", "0X")):
        value = int(s[2:], 16)
    elif s.startswith("."):
        value = int(s[1:], 10)
    elif s.startswith("%"):
        value = int(s[1:], 2)
    else:
        try:
            value = int(s, 16)
        except ValueError:
            raise ValueError(f"Invalid address: {s!r}")
    if value < 0 or value > 0xFFFF:
        raise ValueError(
            f"Address ${value:X} is outside C64 range $0000..$FFFF")
    return value

# ---------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------


class _VideoWorker(QThread):
    """UDP receiver + decoder for the VIC stream.

    Listens on the configured video port (default 11000), parses
    each packet, expands the 4-bit-packed payload bytes to RGBA via
    PIXMAP_LUT, writes them into the appropriate scanline of the
    shared frame buffer.

    On each vsync flag (high bit of the line field) emits
    `frame_ready` so the GUI thread can repaint. Also emits
    `stats(packets, bytes)` once per second for the status bar.
    """
    frame_ready = pyqtSignal()
    stats = pyqtSignal(int, int)        # (packets/sec, bytes/sec)
    error = pyqtSignal(str)

    def __init__(self, framebuf: bytearray, port: int = PORT_VIDEO,
                   parent=None):
        super().__init__(parent)
        # framebuf is the SHARED RGBA byte buffer (FRAME_W * FRAME_H * 4
        # bytes). We write into it from this thread; the GUI reads it
        # at frame_ready time. We deliberately don't lock - tearing
        # at frame boundaries is acceptable for a video preview.
        self._fb = framebuf
        self._port = port
        self._stop = False
        self._sock = None

    def stop(self):
        """Tell the worker to exit. Closes the socket so the recv
        call returns immediately."""
        self._stop = True
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            self._sock = socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 256 KB receive buffer - the U64 sends a full PAL frame
            # in ~272 packets, all in one ~20ms burst. A small kernel
            # buffer drops packets and tears the picture.
            try:
                self._sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            except Exception:
                pass
            self._sock.bind(("0.0.0.0", self._port))
            self._sock.settimeout(0.5)   # so we can poll _stop
        except Exception as e:
            self.error.emit(f"Cannot open UDP {self._port}: {e}")
            return

        last_stats_t = time.monotonic()
        pkt_count = 0
        byte_count = 0

        while not self._stop:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                # Socket closed while we were waiting - normal exit.
                break
            except Exception as e:
                self.error.emit(f"recv error: {e}")
                break
            if len(data) < VIDEO_HEADER_SIZE:
                continue
            pkt_count += 1
            byte_count += len(data)

            # Parse header
            (seq, frame, line_raw, pixels_in_line,
                lines_in_packet, bpp, encoding) = struct.unpack_from(
                    VIDEO_HEADER_FMT, data, 0)
            # Top bit of line = vsync (end of frame).
            vsync = (line_raw & 0x8000) != 0
            y = line_raw & 0x7fff

            # Defensive bounds checks - garbage packets shouldn't
            # crash us.
            if y >= FRAME_H or pixels_in_line == 0:
                continue
            if lines_in_packet == 0:
                continue
            half_pix = pixels_in_line // 2     # bytes per row of payload
            payload_off = VIDEO_HEADER_SIZE
            # Hot path: for each row in this packet, expand the
            # 4-bit-packed payload bytes into RGBA via PIXMAP_LUT
            # and slice the result into the framebuffer.
            #
            # The previous code did `for x in range(half_pix)` with
            # one-byte-at-a-time indexing in pure Python, which
            # ate 9 million iterations/sec at 50 fps and made the
            # whole stream choppy. The fix here: use a generator-
            # expression + bytes.join to push the inner loop into
            # C code. ~10x faster on the same data.
            fb = self._fb
            lut = PIXMAP_LUT
            for l in range(lines_in_packet):
                row_y = y + l
                if row_y >= FRAME_H:
                    break
                row_src_off = payload_off + l * half_pix
                row_end = row_src_off + half_pix
                if row_end > len(data):
                    row_end = len(data)
                # 1 byte -> 8 bytes via LUT, then concat all rows'
                # output once. bytes.join with an 8-byte-per-input
                # generator runs entirely in C.
                row_bytes = b"".join(
                    lut[b] for b in data[row_src_off:row_end])
                dst_off = row_y * FRAME_W * 4
                # Trim if the U64 sent fewer pixels than the frame
                # width (defensive - shouldn't happen for full-row
                # packets but ymmv with NTSC mode).
                end = dst_off + len(row_bytes)
                if end > len(fb):
                    end = len(fb)
                fb[dst_off:end] = row_bytes[:end - dst_off]
            if vsync:
                self.frame_ready.emit()

            now = time.monotonic()
            if now - last_stats_t >= 1.0:
                self.stats.emit(pkt_count, byte_count)
                pkt_count = 0
                byte_count = 0
                last_stats_t = now

        try:
            self._sock.close()
        except Exception:
            pass


# =====================================================================
# Video recorder
# =====================================================================
class _VideoRecorder(QThread):
    """Background video-recording worker.

    Receives raw RGBA frames (FRAME_W * FRAME_H * 4 bytes each)
    via push_frame() from the GUI thread, encodes them in a
    background thread so the UI stays responsive. Two output
    modes:

      "mp4":     pipe raw frames into an ffmpeg subprocess that
                 muxes H.264 at 50 fps. Single self-contained
                 output file. Requires `ffmpeg` on PATH.
      "png_seq": dump each frame as a numbered PNG into an
                 output directory. No external deps. Larger on
                 disk but trivial to post-process.

    Frame timing: the U64 emits ~50 frames/sec PAL (60 NTSC).
    We tag each pushed frame with its arrival time but don't
    resample - ffmpeg is told the input is constant 50 fps,
    which is close enough for the casual demoscene capture use
    case Mario wants this for. For exact-timing work the PNG
    sequence path keeps per-frame timestamps in a sidecar JSON
    so the user can post-process with any frame rate they like.

    Signals:
      stats(frames_written: int, seconds_elapsed: float)
      error(msg: str)            non-fatal: recording continues
      stopped(final_path: str)   emitted from run() after the
                                 encoder has flushed and exited
    """
    stats   = pyqtSignal(int, float)
    error   = pyqtSignal(str)
    stopped = pyqtSignal(str)

    # Hardcoded frame rate for the MP4 path. 50 Hz matches PAL VIC
    # output; NTSC users get a ~17% time-stretch which is harmless
    # for visual capture. If anyone complains we can autodetect from
    # the U64 config.
    FPS = 50

    # Audio format constants - the Ultimate emits 48 kHz / stereo /
    # S16LE on the UDP audio stream, same as the live playback path.
    # We capture it 1:1 (no resampling) into a WAV sidecar that
    # ffmpeg muxes into the final MP4 on stop.
    AUDIO_RATE     = 48000
    AUDIO_CHANNELS = 2
    AUDIO_BITS     = 16

    def __init__(self, output_path: str, mode: str = "mp4",
                  parent=None, record_audio: bool = True):
        super().__init__(parent)
        self._output_path = output_path     # file (mp4) or dir (png_seq)
        self._mode = mode                   # "mp4" or "png_seq"
        self._record_audio = bool(record_audio)
        # Bounded queue so a stuck encoder can't blow out memory.
        # 60 frames @ 384x272x4 = 24 MB worst case, ~1.2s of video.
        # If the encoder falls behind we drop oldest frames so the
        # captured video stays close to real-time.
        import queue as _queue
        self._q = _queue.Queue(maxsize=60)
        self._stop = False
        self._frames_written = 0
        self._t_start = None
        self._proc = None                   # ffmpeg subprocess
        # Audio sidecar state. We write incoming PCM directly to a
        # temp WAV file from push_audio() (called from the GUI
        # thread); on stop, the WAV header gets finalised with the
        # correct byte counts and ffmpeg muxes it into the MP4. WAV
        # is the right container because the chunks arrive in
        # order without metadata - just raw S16LE stereo at 48 kHz.
        self._wav_fp = None                 # file handle for temp WAV
        self._wav_path = None               # path of temp WAV
        self._wav_bytes_written = 0         # excludes header
        self._wav_lock = None               # threading.Lock for writes
        # We initialise the lock lazily in start() because creating a
        # threading object before the QThread fires is fine, but the
        # WAV file itself must be opened on the QThread side so the
        # GUI thread doesn't block on disk-create latency.

    def push_audio(self, pcm_chunk: bytes):
        """Called from the GUI thread when an audio packet arrives
        from _AudioWorker.audio_chunk. Appends the raw S16LE stereo
        samples to the temp WAV sidecar.

        Direct write is OK here: WAV is just a header + concatenated
        PCM frames, no encoding involved. We hold a lock so a
        background thread writing the WAV header on stop() doesn't
        race with this method.

        Quietly drops audio if we're not in MP4 mode, audio
        recording is disabled, the file isn't open yet (start-up
        race), or the recorder has been told to stop. WAV file
        problems (disk full etc.) are logged once and then silently
        ignored - the video should still produce a usable file.
        """
        if (self._stop or not self._record_audio
                or self._mode != "mp4"
                or self._wav_fp is None):
            return
        try:
            with self._wav_lock:
                self._wav_fp.write(pcm_chunk)
                self._wav_bytes_written += len(pcm_chunk)
        except Exception:
            # Disable further audio writes after the first failure to
            # avoid spamming. We don't reset _wav_path - finalize_wav
            # will still close out and fix the header on whatever
            # bytes did make it to disk, so muxing can use it.
            try:
                self._wav_fp.close()
            except Exception:
                pass
            self._wav_fp = None

    def push_frame(self, frame_bytes: bytes):
        """Called from the GUI thread. Hands a frame copy off to
        the encoder queue. Non-blocking: if the queue is full
        (encoder stuck) we drop the OLDEST frame to keep the
        recording in sync with real-time rather than the GUI
        stalling. The dropped count surfaces via stats so the
        user knows when they should switch to a faster mode or
        smaller window."""
        if self._stop:
            return
        # Copy the bytearray slice so we own a frozen snapshot;
        # the framebuf gets overwritten by the next packet stream
        # within ~20 ms and we'd otherwise encode torn frames.
        snap = bytes(frame_bytes)
        try:
            self._q.put_nowait(snap)
        except Exception:
            # Queue full. Drop the OLDEST queued frame and retry.
            # We use put_nowait again rather than blocking so the
            # caller (GUI thread) never sees a stall.
            try:
                self._q.get_nowait()
            except Exception:
                pass
            try:
                self._q.put_nowait(snap)
            except Exception:
                pass

    def stop(self):
        """Tell the worker to drain its queue and exit. The thread
        will emit stopped(final_path) when done so callers can
        update UI / show a 'saved to ...' status."""
        self._stop = True
        # Put a sentinel so a blocked get() returns immediately.
        try:
            self._q.put_nowait(None)
        except Exception:
            pass

    def run(self):
        import time as _time
        self._t_start = _time.monotonic()
        if self._mode == "mp4":
            ok = self._run_mp4()
        else:
            ok = self._run_png_seq()
        # Final stats tick so the UI sees the last numbers even if
        # the timer-based emit missed the tail.
        try:
            elapsed = _time.monotonic() - self._t_start
            self.stats.emit(self._frames_written, elapsed)
        except Exception:
            pass
        self.stopped.emit(self._output_path if ok else "")

    # ----- mp4 path ----------------------------------------------------
    def _run_mp4(self) -> bool:
        """Pipe raw RGBA frames to a libx264 ffmpeg process. Returns
        True if the encoder finished cleanly."""
        import subprocess, shutil, os, time as _time
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.error.emit(
                "ffmpeg not found on PATH - cannot encode MP4. "
                "Falling back to PNG sequence in the same folder.")
            # Switch the mode and the output_path on the fly. The
            # user gets a directory named like the original file
            # without the .mp4 extension.
            self._mode = "png_seq"
            if self._output_path.lower().endswith(".mp4"):
                self._output_path = self._output_path[:-4]
            return self._run_png_seq()

        # Open the WAV audio sidecar BEFORE we start the video
        # encoder, so push_audio() calls from the GUI thread land
        # in a real file from the first packet. If WAV-open fails
        # we keep going video-only (logged in _open_wav).
        if self._record_audio:
            if not self._open_wav():
                # Disable further audio writes for this recording
                self._record_audio = False

        # Encode video to a temp file (we'll mux audio in afterwards).
        # If audio recording is off, write directly to the final
        # path - no mux needed.
        if self._record_audio:
            video_only_path = self._output_path + ".video.mp4"
        else:
            video_only_path = self._output_path

        # Build the ffmpeg command. -re isn't needed; we feed at
        # real-time rate from the GUI hook. -an = no audio (we add
        # audio in the mux step from the WAV sidecar).
        cmd = [
            ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-y",                              # overwrite output
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-s", f"{FRAME_W}x{FRAME_H}",
            "-r", str(self.FPS),
            "-i", "-",                         # stdin
            "-an",                             # no audio in pass 1
            "-c:v", "libx264",
            "-preset", "veryfast",             # encode > realtime
            "-pix_fmt", "yuv420p",             # ubiquitous, plays
                                                # in any browser/VLC
            "-movflags", "+faststart",         # web-streamable
            video_only_path,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
        except Exception as e:
            self.error.emit(f"Cannot launch ffmpeg: {e}")
            self._finalize_wav()
            return False

        encode_ok = True
        last_stats_t = _time.monotonic()
        try:
            while not (self._stop and self._q.empty()):
                try:
                    frame = self._q.get(timeout=0.5)
                except Exception:
                    continue
                if frame is None:           # sentinel
                    break
                try:
                    self._proc.stdin.write(frame)
                except (BrokenPipeError, OSError) as e:
                    # ffmpeg died mid-stream. Drain its stderr so
                    # the user gets a useful diagnostic.
                    try:
                        err = self._proc.stderr.read().decode(
                            "utf-8", errors="replace")
                    except Exception:
                        err = str(e)
                    self.error.emit(
                        f"ffmpeg encoder died: {err[:400]}")
                    encode_ok = False
                    break
                self._frames_written += 1
                now = _time.monotonic()
                if now - last_stats_t >= 1.0:
                    self.stats.emit(
                        self._frames_written, now - self._t_start)
                    last_stats_t = now
        finally:
            # Cleanly close stdin so ffmpeg flushes the trailer.
            try:
                if self._proc and self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                if self._proc:
                    self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

        # Finalize the audio sidecar regardless of encode outcome -
        # the user can still recover audio if video died.
        self._finalize_wav()

        # If audio was requested AND the video encode succeeded,
        # mux the two together. On any mux failure, leave the
        # video-only file in place with the WAV next to it so
        # nothing is lost.
        if encode_ok and self._record_audio:
            if self._mux_audio_into_mp4(video_only_path):
                # Mux succeeded: cleanup WAV sidecar.
                self._cleanup_wav()
            else:
                # Mux failed: keep the WAV. Rename video_only_path
                # to the final path so the user at least gets a
                # video file at the expected location.
                try:
                    if os.path.exists(self._output_path):
                        os.remove(self._output_path)
                    os.rename(video_only_path, self._output_path)
                except Exception:
                    # Couldn't rename - leave the .video.mp4 file
                    # in place. _output_path will be reported back
                    # as the result so the user knows what to look
                    # for.
                    self._output_path = video_only_path
        return encode_ok

    # ----- png sequence path ------------------------------------------
    def _run_png_seq(self) -> bool:
        """Dump each frame as a numbered PNG into output_path/.
        Writes a sidecar `frames.json` with per-frame wall-clock
        timestamps so post-processors can reconstruct exact timing
        (the U64's 50 Hz isn't actually constant - VICE pause,
        snapshot loads etc. introduce jitter)."""
        import os, json, time as _time
        try:
            os.makedirs(self._output_path, exist_ok=True)
        except Exception as e:
            self.error.emit(
                f"Cannot create output dir {self._output_path}: {e}")
            return False
        timestamps = []
        last_stats_t = _time.monotonic()
        try:
            while not (self._stop and self._q.empty()):
                try:
                    frame = self._q.get(timeout=0.5)
                except Exception:
                    continue
                if frame is None:
                    break
                idx = self._frames_written
                fpath = os.path.join(
                    self._output_path, f"frame_{idx:06d}.png")
                if not self._write_png(fpath, frame):
                    # Disk full / permission error - bail rather than
                    # spamming errors.
                    self.error.emit(
                        f"PNG write failed at frame {idx} "
                        f"({fpath}); stopping capture.")
                    break
                timestamps.append(_time.monotonic() - self._t_start)
                self._frames_written += 1
                now = _time.monotonic()
                if now - last_stats_t >= 1.0:
                    self.stats.emit(
                        self._frames_written, now - self._t_start)
                    last_stats_t = now
        finally:
            # Write the timing sidecar regardless of how we exited
            # so partial captures are still reconstructable.
            try:
                meta = {
                    "frame_count": self._frames_written,
                    "width": FRAME_W,
                    "height": FRAME_H,
                    "nominal_fps": self.FPS,
                    "timestamps_seconds": timestamps,
                }
                with open(os.path.join(
                        self._output_path, "frames.json"), "w") as f:
                    json.dump(meta, f, indent=2)
            except Exception:
                pass
        return True

    def _write_png(self, path: str, rgba: bytes) -> bool:
        """Encode a 384x272 RGBA buffer as PNG. Uses QImage so we
        don't pull in PIL just for this. Returns True on success."""
        try:
            img = QImage(rgba, FRAME_W, FRAME_H,
                          FRAME_W * 4, QImage.Format.Format_RGBA8888)
            return bool(img.save(path, "PNG"))
        except Exception:
            return False

    # ----- audio sidecar helpers -------------------------------------
    def _open_wav(self) -> bool:
        """Create the temp WAV sidecar next to the final MP4 path.
        We write a placeholder header now (with sizes = 0) and patch
        the real byte counts in on finalize_wav() after the recording
        ends. Returns True on success."""
        import os, struct, threading
        if self._wav_lock is None:
            self._wav_lock = threading.Lock()
        # Place the WAV next to the MP4 with a `.audio.wav` suffix so
        # if the mux step fails the user can still recover the audio
        # manually. Removed by _cleanup_wav after a successful mux.
        wav_path = self._output_path
        if wav_path.lower().endswith(".mp4"):
            wav_path = wav_path[:-4] + ".audio.wav"
        else:
            wav_path = wav_path + ".audio.wav"
        try:
            fp = open(wav_path, "wb")
        except Exception as e:
            self.error.emit(
                f"Cannot create audio sidecar {wav_path}: {e}")
            return False
        # 44-byte canonical WAV/PCM header. Sizes filled in later.
        byte_rate   = (self.AUDIO_RATE * self.AUDIO_CHANNELS
                        * self.AUDIO_BITS // 8)
        block_align = self.AUDIO_CHANNELS * self.AUDIO_BITS // 8
        header = b""
        header += b"RIFF"                          # chunk id
        header += struct.pack("<I", 0)             # chunk size (patched)
        header += b"WAVE"
        header += b"fmt "
        header += struct.pack("<I", 16)            # PCM subchunk size
        header += struct.pack("<H", 1)             # AudioFormat=PCM
        header += struct.pack("<H", self.AUDIO_CHANNELS)
        header += struct.pack("<I", self.AUDIO_RATE)
        header += struct.pack("<I", byte_rate)
        header += struct.pack("<H", block_align)
        header += struct.pack("<H", self.AUDIO_BITS)
        header += b"data"
        header += struct.pack("<I", 0)             # data size (patched)
        try:
            fp.write(header)
        except Exception as e:
            self.error.emit(f"WAV header write failed: {e}")
            try: fp.close()
            except Exception: pass
            return False
        self._wav_fp = fp
        self._wav_path = wav_path
        self._wav_bytes_written = 0
        return True

    def _finalize_wav(self):
        """Patch the WAV header with the actual byte counts and
        close the file. Safe to call multiple times; no-op after
        the first call."""
        import struct
        if self._wav_fp is None and self._wav_path is None:
            return
        try:
            if self._wav_fp is not None:
                # Hold the lock so a stray push_audio call from the
                # GUI thread can't write between the seek and the
                # patch.
                with self._wav_lock:
                    fp = self._wav_fp
                    self._wav_fp = None
                    data_sz = self._wav_bytes_written
                    riff_sz = 36 + data_sz
                    try:
                        fp.seek(4)
                        fp.write(struct.pack("<I", riff_sz))
                        fp.seek(40)
                        fp.write(struct.pack("<I", data_sz))
                    except Exception:
                        pass
                    try:
                        fp.close()
                    except Exception:
                        pass
        except Exception:
            pass

    def _cleanup_wav(self):
        """Remove the temp WAV sidecar after a successful mux."""
        import os
        if self._wav_path and os.path.exists(self._wav_path):
            try:
                os.remove(self._wav_path)
            except Exception:
                # Leave it - it's harmless next to the mp4 and the
                # user can clean up manually if they care.
                pass
        self._wav_path = None

    def _mux_audio_into_mp4(self, video_only_path: str) -> bool:
        """Second-pass ffmpeg invocation that merges the video-only
        MP4 with the WAV sidecar into the final output. Uses
        `-c:v copy` so we don't re-encode H.264 - the video stream
        is byte-identical to what came out of the first pass. Audio
        is transcoded to AAC at 192 kbps (sane default for 48 kHz
        stereo, plays everywhere).

        `-shortest` makes sure the output ends with the shorter of
        the two streams, so an interrupted recording where audio
        and video lengths diverge by a few hundred ms doesn't add a
        trailing silent block.

        Returns True if the mux succeeded and the final file is in
        place at self._output_path; False if anything failed (in
        which case the video-only file is left at video_only_path
        and the WAV stays around for manual recovery).
        """
        import subprocess, shutil, os
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False
        if not self._wav_path or not os.path.exists(self._wav_path):
            return False
        if self._wav_bytes_written == 0:
            # Audio stream was enabled but nothing arrived (audio
            # worker not running, U64 didn't have audio enabled).
            # Nothing to mux.
            return False
        # Write to a temp output, then rename - so an interrupted
        # mux can't leave the user with a 0-byte final.mp4.
        tmp_out = self._output_path + ".muxing.mp4"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_only_path,
            "-i", self._wav_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            tmp_out,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as e:
            self.error.emit(f"ffmpeg mux failed to launch: {e}")
            return False
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace")
            self.error.emit(
                f"ffmpeg mux returned {r.returncode}: "
                f"{err[:400]}")
            try: os.remove(tmp_out)
            except Exception: pass
            return False
        # Swap tmp_out into place
        try:
            try:
                os.remove(self._output_path)
            except Exception:
                pass
            try:
                os.remove(video_only_path)
            except Exception:
                pass
            os.rename(tmp_out, self._output_path)
        except Exception as e:
            self.error.emit(f"final rename failed: {e}")
            return False
        return True


class _AudioWorker(QThread):
    """UDP receiver for the audio stream. Pushes incoming samples
    into a shared bytes deque; the audio device pulls from that
    deque on demand. Lives only when AUDIO_AVAILABLE.

    The Ultimate64 emits 192 stereo samples (= 768 bytes of S16LE)
    per packet, every 4ms (192 / 48000 = 4ms). We forward those
    bytes verbatim - no resampling, no decoding."""
    audio_chunk = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, port: int = PORT_AUDIO, parent=None):
        super().__init__(parent)
        self._port = port
        self._stop = False
        self._sock = None

    def stop(self):
        self._stop = True
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            self._sock = socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            except Exception:
                pass
            self._sock.bind(("0.0.0.0", self._port))
            self._sock.settimeout(0.5)
        except Exception as e:
            self.error.emit(f"Cannot open UDP {self._port}: {e}")
            return

        while not self._stop:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                self.error.emit(f"audio recv error: {e}")
                break
            # Expected size: 2 (seq) + 768 (samples) = 770 bytes.
            # Anything weird, just skip.
            if len(data) < AUDIO_HEADER_SIZE + AUDIO_PAYLOAD_SIZE:
                continue
            samples = data[AUDIO_HEADER_SIZE:
                              AUDIO_HEADER_SIZE + AUDIO_PAYLOAD_SIZE]
            self.audio_chunk.emit(bytes(samples))

        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Config dialog
# ---------------------------------------------------------------------


class _DnDUploadWorker(QThread):
    """Background HTTP-upload thread for drag-and-drop autostart.

    The Ultimate's REST API can take 2-30 seconds to respond on
    larger files (PRG load via DMA, D64 mount, etc.) so we MUST
    NOT do it on the GUI thread - the streamer would freeze every
    time you drop a file.

    `kind` is one of: "run_prg", "run_crt", "sidplay", "modplay",
    or "mount_disk". For "mount_disk", `mount_and_run=True` will
    follow the mount with a machine:reset so the C64 boots from
    the freshly-mounted disk; mount_and_run=False is the manual-
    disk-swap path used when the user holds Ctrl while dropping.
    """
    done = pyqtSignal(bool, str)        # (success, message)

    def __init__(self, host, port, password, kind, file_bytes,
                   file_ext="", mount_and_run=True, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._password = password
        self._kind = kind
        self._bytes = file_bytes
        self._ext = file_ext
        self._mount_and_run = mount_and_run

    def run(self):
        try:
            kind = self._kind
            if kind == "run_prg":
                ok, msg = u64_run_prg(
                    self._host, self._bytes,
                    password=self._password, port=self._port)
                self.done.emit(ok, "PRG running" if ok else msg)
                return
            if kind == "run_crt":
                ok, msg = u64_run_crt(
                    self._host, self._bytes,
                    password=self._password, port=self._port)
                self.done.emit(ok, "Cartridge started" if ok else msg)
                return
            if kind == "sidplay":
                ok, msg = u64_play_sid(
                    self._host, self._bytes,
                    password=self._password, port=self._port)
                self.done.emit(ok, "SID playing" if ok else msg)
                return
            if kind == "modplay":
                ok, msg = u64_play_mod(
                    self._host, self._bytes,
                    password=self._password, port=self._port)
                self.done.emit(ok, "MOD playing" if ok else msg)
                return
            if kind == "mount_disk":
                # Determine the mount type from extension. The U64
                # can usually sniff this from Content-Disposition
                # but we pass it explicitly to be safe.
                disk_type = self._ext.lstrip(".") if self._ext else ""
                ok, msg = u64_mount_disk(
                    self._host, self._bytes,
                    drive="a", mode="readonly",
                    disk_type=disk_type,
                    password=self._password, port=self._port)
                if not ok:
                    self.done.emit(False, msg)
                    return
                if self._mount_and_run:
                    # Reset the C64 so it sees the new disk.
                    # IMPORTANT: this is a soft reset that will
                    # boot to BASIC; the user still has to type
                    # LOAD"*",8,1 / RUN unless the disk has an
                    # autoboot setup. We can't do better via the
                    # REST API alone - the official TSB streamer
                    # likely uses keyboard-emulation to type the
                    # autoload commands. We just reset and let
                    # the user run it from BASIC.
                    rok, rmsg = u64_reset(
                        self._host,
                        password=self._password, port=self._port)
                    if not rok:
                        self.done.emit(True,
                            "Disk mounted, reset failed: " + rmsg)
                        return
                    self.done.emit(True,
                        "Disk mounted + C64 reset (LOAD\"*\",8,1 to run)")
                else:
                    self.done.emit(True, "Disk mounted (no reset)")
                return
            self.done.emit(False, f"Unknown drop kind: {kind}")
        except Exception as e:
            self.done.emit(False, f"Upload thread error: {e}")


class _PersistentKeyboardWorker(QThread):
    """Long-lived worker that drains a key queue and posts each key
    to the U64 over a REUSED HTTP connection.

    Compared to spinning up a fresh _TypeWorker per keystroke (the
    old behaviour):
      - One TCP/HTTP connection is opened on first key and kept
        alive for subsequent keys. No 3-way handshake per key.
      - All keys flow through one queue.Queue, so bursts are
        handled in order even if they arrive faster than the
        round-trip time.
      - We do NOT block the GUI - this thread runs forever; the
        UI just puts bytes into the queue and returns.

    Lifetime: created when 'Capture keys' is first turned on,
    stopped via stop() when the streamer closes or capture is
    turned off. While running we hold one socket open to the U64,
    re-connecting only if the connection drops.
    """

    error = pyqtSignal(str)

    # Sentinel value used to ask the run loop to exit cleanly.
    _STOP = object()

    def __init__(self, host: str, port: int, password: str,
                 parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._password = password
        # Queue from GUI thread -> worker thread. Unbounded; key
        # capture can't produce data faster than the user types so
        # we won't realistically OOM here.
        import queue
        self._queue = queue.Queue()
        self._stop_requested = False

    def enqueue(self, pet_byte: int) -> None:
        """Add one PETSCII byte to the send queue. Returns
        immediately - the worker thread picks it up. Safe to
        call from the GUI thread."""
        self._queue.put(int(pet_byte) & 0xff)

    def stop(self) -> None:
        """Ask the worker to exit. The thread will finish its
        current key (if any) and then return from run()."""
        self._stop_requested = True
        self._queue.put(self._STOP)

    def run(self) -> None:
        """Worker loop. Hold a single HTTP connection open and
        replay queued keys through it as fast as the C64's
        keyboard buffer can drain."""
        import http.client
        import socket
        conn = None
        # Address of the keyboard buffer + index byte on the C64.
        # Same constants u64_type_petscii_bytes uses.
        keybuf_addr = 0x0277
        ndx_addr = 0x00C6
        # PETSCII control: keep the headers tiny so the per-key
        # overhead stays in the high tens of bytes range.
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": "1",
        }
        if self._password:
            headers["X-Password"] = self._password

        def _open() -> "http.client.HTTPConnection":
            """Open a fresh keep-alive connection."""
            c = http.client.HTTPConnection(
                self._host, self._port, timeout=5.0)
            # Disable the small extra delay urlopen() adds for
            # short writes; we want the key to leave as fast as
            # possible.
            try:
                c.connect()
                c.sock.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            return c

        def _post(path: str, body: bytes) -> bool:
            """Single keep-alive POST. Returns True on 200 OK,
            False on any failure (in which case the caller will
            re-open the connection and retry once)."""
            nonlocal conn
            try:
                conn.request("POST", path, body=body,
                              headers=headers)
                resp = conn.getresponse()
                resp.read()  # drain body so the connection is
                # reusable for the next request
                return 200 <= resp.status < 300
            except (http.client.HTTPException,
                    ConnectionError, OSError):
                return False

        while not self._stop_requested:
            try:
                item = self._queue.get()
            except Exception:
                break
            if item is self._STOP:
                break
            try:
                pet = int(item) & 0xff
            except (TypeError, ValueError):
                continue
            # Drain a brief burst: if multiple keys are queued
            # already, batch them into one buffer-write. This
            # halves the per-key overhead during typing bursts
            # without losing keys to the small _KEYBUF_SIZE cap.
            burst = [pet]
            while (len(burst) < 8
                    and not self._queue.empty()):
                try:
                    nxt = self._queue.get_nowait()
                except Exception:
                    break
                if nxt is self._STOP:
                    # Honour stop even if it arrived mid-burst -
                    # don't drop the pending bytes though, flush
                    # the burst first then bail.
                    self._stop_requested = True
                    break
                try:
                    burst.append(int(nxt) & 0xff)
                except (TypeError, ValueError):
                    continue
            # Open / reopen the connection lazily so failed sends
            # only impact the failing key, not the whole session.
            if conn is None:
                try:
                    conn = _open()
                except Exception as e:
                    self.error.emit(
                        f"keyboard connect failed: {e}")
                    continue
            buf_path = (
                f"/v1/machine:writemem?address="
                f"{keybuf_addr:04X}")
            ndx_path = (
                f"/v1/machine:writemem?address="
                f"{ndx_addr:04X}")
            # Buffer + length writes. Two POSTs but on the SAME
            # connection - that's the win vs spawning a new
            # urllib.request per keystroke.
            buf_body = bytes(burst)
            ndx_body = bytes([len(burst)])
            # Content-Length header has to be updated per call
            # because burst lengths vary; we copy headers since
            # http.client mutates them.
            buf_headers = dict(headers)
            buf_headers["Content-Length"] = str(len(buf_body))
            ndx_headers = dict(headers)
            ndx_headers["Content-Length"] = str(len(ndx_body))
            ok = False
            for attempt in range(2):
                try:
                    conn.request("POST", buf_path,
                                  body=buf_body,
                                  headers=buf_headers)
                    r1 = conn.getresponse()
                    r1.read()
                    if not 200 <= r1.status < 300:
                        raise OSError(
                            f"keybuf write HTTP {r1.status}")
                    conn.request("POST", ndx_path,
                                  body=ndx_body,
                                  headers=ndx_headers)
                    r2 = conn.getresponse()
                    r2.read()
                    if not 200 <= r2.status < 300:
                        raise OSError(
                            f"keybuf NDX HTTP {r2.status}")
                    ok = True
                    break
                except (http.client.HTTPException,
                        ConnectionError, OSError) as e:
                    # Connection died - close and re-open once.
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                    if attempt == 0:
                        try:
                            conn = _open()
                            continue
                        except Exception:
                            pass
                    # Both attempts failed - surface the error and
                    # move on. The user will see "key FAILED" in
                    # the status line and can retry.
                    self.error.emit(
                        f"key send failed: {e}")
            # We let the U64's keyboard scan timing handle pacing
            # naturally - no explicit sleep. The buffer at $0277
            # is 10 bytes; if we go faster than that the KERNAL
            # just drops the overflow, same as a real human-typed
            # buffer overflow.
        # Clean shutdown
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class _TypeWorker(QThread):
    """Background HTTP-write thread for keyboard injection.

    Either takes a `text` (str, ASCII -> PETSCII conversion done
    inside u64_type_text) or `pet_bytes` (already-translated
    PETSCII bytes, used by direct key capture).

    Each chunk does 2 HTTP POSTs (one for the buffer, one for NDX)
    plus a sleep, so total time = ceil(N/10) * (2 RTT + 180ms).
    Typical 20-char line: ~400ms. Doing this on the GUI thread
    would be a freeze - hence the worker.
    """
    done = pyqtSignal(bool, str)

    def __init__(self, host, port, password, text=None,
                   pet_bytes=None, chunk_delay=0.18, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._password = password
        self._text = text
        self._pet_bytes = pet_bytes
        self._chunk_delay = chunk_delay

    def run(self):
        try:
            if self._pet_bytes is not None:
                ok, msg = u64_type_petscii_bytes(
                    self._host, self._pet_bytes,
                    password=self._password, port=self._port,
                    chunk_delay=self._chunk_delay)
            else:
                ok, msg = u64_type_text(
                    self._host, self._text or "",
                    password=self._password, port=self._port,
                    chunk_delay=self._chunk_delay)
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"type thread error: {e}")


class _SpaceBurstWorker(QThread):
    """Background-Worker fuer den SPC-Button-Burst.

    Macht in einem Rutsch bis zu 6 writemem-Calls (keybuf, matrix
    mirror, CIA1 ports). Im Worker damit die GUI nicht zappelt
    waehrend der Roundtrips - typische Gesamtdauer ~50-150ms je
    nach Latenz.
    """
    done = pyqtSignal(bool, str)

    def __init__(self, host, port, password, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._password = password

    def run(self):
        try:
            ok, msg = u64_press_space_burst(
                self._host, password=self._password, port=self._port)
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"space burst thread error: {e}")


class _U64MemoryBackend:
    """Backend-Wrapper fuer den U64 REST-API-Memory-Zugriff.

    Verkapselt host/port/password damit MemoryViewDialog und die
    Worker nichts ueber den konkreten Transport wissen muessen -
    derselbe Dialog kann genauso einen VICE-TCP-Backend bekommen.
    """
    def __init__(self, host, port, password):
        self._host = host
        self._port = port
        self._password = password

    def read(self, address, length):
        return u64_readmem(
            self._host, address, length,
            password=self._password, port=self._port)

    def write(self, address, data):
        return u64_writemem(
            self._host, address, data,
            password=self._password, port=self._port)

    @property
    def label(self):
        return f"U64@{self._host}"


class _ViceMemoryBackend:
    """Backend fuer VICE binary monitor.

    VICE muss mit -binarymonitor gestartet sein (Default-Port 6502).
    Im Gegensatz zum U64 brauchen wir kein Passwort - der binary
    monitor authentisiert nicht.
    """
    def __init__(self, host, port):
        self._host = host
        self._port = port

    def read(self, address, length):
        from . import vice_monitor
        return vice_monitor.vice_readmem(
            self._host, self._port, address, length)

    def write(self, address, data):
        from . import vice_monitor
        return vice_monitor.vice_writemem(
            self._host, self._port, address, bytes(data))

    @property
    def label(self):
        return f"VICE@{self._host}:{self._port}"


class _ReadMemWorker(QThread):
    """Background-Thread fuer den readmem-Call.

    Wir laufen im Worker damit GUI/Video-Stream nicht freezen
    waehrend der Backend-Roundtrip laeuft (kann je nach Backend und
    Read-Groesse mehrere hundert ms dauern).
    """
    done = pyqtSignal(bool, object, int)   # (ok, data_or_errmsg, address)

    def __init__(self, backend, address, length, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._address = address
        self._length = length

    def run(self):
        try:
            ok, result = self._backend.read(self._address, self._length)
            self.done.emit(ok, result, self._address)
        except Exception as e:
            self.done.emit(False, f"readmem thread error: {e}",
                              self._address)


class _WriteMemWorker(QThread):
    """Background-Thread fuer einen Poke.

    Schreibt 1..N Bytes an `address`. Bei U64-Backend gilt das 128-
    Byte-Limit; bei VICE praktisch unlimitiert (bis 64K). Single-
    Byte ist der typische Fall (Hex-Zellen-Edit); Mnemonic-Edits
    schreiben 1-3 Bytes atomar.
    """
    # ok, errmsg, address, value_bytes
    done = pyqtSignal(bool, str, int, bytes)

    def __init__(self, backend, address, data, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._address = address & 0xFFFF
        if isinstance(data, int):
            data = bytes([data & 0xFF])
        self._data = bytes(data)

    def run(self):
        try:
            ok, msg = self._backend.write(self._address, self._data)
            self.done.emit(ok, msg, self._address, self._data)
        except Exception as e:
            self.done.emit(False, f"writemem thread error: {e}",
                              self._address, self._data)


class _HexByteDelegate(QStyledItemDelegate):
    """Custom-Editor fuer Hex-Bytes in QTableWidget-Zellen.

    Erzwingt 1-2 hex-Ziffern als Eingabe (validiert per Regex). Bei
    Single-Char-Input (z.B. nur 'A' eingetippt) wird das spaeter im
    Commit-Pfad als '0A' interpretiert.
    """
    def createEditor(self, parent, option, index):
        ed = QLineEdit(parent)
        ed.setMaxLength(2)
        # Akzeptiert 1-2 Hex-Ziffern. Wir nutzen QRegularExpression,
        # weil das die moderne Qt6-API ist; QRegExp ist deprecated.
        rx = QRegularExpression(r"[0-9A-Fa-f]{1,2}")
        ed.setValidator(QRegularExpressionValidator(rx, ed))
        # Monospace damit die Eingabe optisch zur Tabelle passt
        mono = get_mono_font(11)
        ed.setFont(mono)
        # Selektiere bestehenden Text wenn Editor sichtbar wird,
        # damit Tippen ihn direkt ersetzt (klassisches "in cell
        # editing" Verhalten).
        ed.selectAll()
        return ed

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.ItemDataRole.EditRole) or "")
        editor.selectAll()


class ReferencesDialog(QDialog):
    """Zeigt das Ergebnis einer 'Find references'-Analyse.

    Strategie: bei Aufgehen wird einmal das KOMPLETTE RAM
    $0000-$FFFF vom Backend gelesen und in `_full_dump` gecached.
    Dann wird linear disassembliert und nach Zugriffen auf die
    target-Adresse gefiltert. Das ist wichtig, weil der User im
    MemoryView meist nur einen Ausschnitt geladen hat (z.B.
    $C000..$CFFF), aber der Code der auf die Adresse zugreift
    typischerweise irgendwo anders liegt ($0801, $8000, ...).
    Ohne Vollscan wuerde die Liste leer bleiben.

    UI:
      - 'Re-analyze'  arbeitet auf dem Cache (schnell, kein I/O)
      - 'Re-read RAM' frisch ueber Backend + dann re-analysieren
        (fuer Live-Aenderungen waehrend das Game laeuft)
      - Reads-Tabelle + Writes-Tabelle nebeneinander

    Non-modal: kann offen bleiben waehrend der User weitere
    MemoryView-Aktionen macht.

    Caveats:
      - lineare Disasm interpretiert Daten als Code -> false positives
      - indexed modes werden als 'fuzzy' markiert mit dem noetigen
        Index-Wert (siehe c64_disasm.find_references)
    """

    def __init__(self, parent, backend, target_addr,
                   show_illegal=False):
        # parent=None damit der Dialog selbststaendig lebt - sonst
        # geht er beim Minimieren des MemoryViewDialog mit. Der
        # `parent` Argument bleibt nur als Ankerpunkt fuer den
        # Halte-Liste-Workaround (siehe MemoryViewDialog._open_references_for).
        super().__init__(None)
        self._dopus_parent = parent
        self._backend = backend
        self._target = target_addr & 0xFFFF
        self._show_illegal = show_illegal
        # _full_dump bleibt None bis der erste Read komplett ist.
        # Bis dahin zeigen die Tabellen 'reading...' und der
        # Re-Analyze-Button ist disabled.
        self._full_dump = None
        self._read_worker = None

        self.setWindowTitle(
            f"References to ${self._target:04X} "
            f"(full RAM scan)")
        self.resize(900, 520)

        # Eigenstaendiges Fenster (siehe Kommentar in MemoryViewDialog).
        # ReferencesDialog ist meistens parallel zum MemoryView offen
        # - User soll beide unabhaengig minimieren/verschieben koennen.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header mit Caveat-Hinweis. Lineare Disasm ueber einen Dump
        # der teils Code, teils Daten ist, ist per Definition fuzzy.
        # Der User soll wissen dass die Liste rauschen kann.
        info = QLabel(
            f"Scanning the full $0000-$FFFF address space for every "
            f"6502 instruction that loads or stores at "
            f"<b>${self._target:04X}</b>. Linear disassembly may "
            f"reinterpret data bytes as opcodes - treat unfamiliar "
            f"matches as potential false positives. <i>Fuzzy</i> "
            f"matches indicate indexed addressing where the index "
            f"value would have to land on the target.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: #888; font-size: {scaled_font_px(11)}px;")
        layout.addWidget(info)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_reanalyze = QPushButton("Re-analyze")
        self.btn_reanalyze.setStyleSheet(button_qss("blue"))
        self.btn_reanalyze.setMinimumWidth(scaled_px(100))
        self.btn_reanalyze.setToolTip(
            "Re-run the static analysis on the cached RAM dump. "
            "Cheap; no I/O.")
        self.btn_reanalyze.clicked.connect(self._populate)
        self.btn_reanalyze.setEnabled(False)
        bar.addWidget(self.btn_reanalyze)

        self.btn_reread = QPushButton("Re-read RAM")
        self.btn_reread.setStyleSheet(button_qss("green"))
        self.btn_reread.setMinimumWidth(scaled_px(100))
        self.btn_reread.setToolTip(
            "Read the full $0000-$FFFF RAM again from the backend "
            "and re-analyze. Use when the running program has "
            "changed memory since the last scan.")
        self.btn_reread.clicked.connect(self._start_read)
        bar.addWidget(self.btn_reread)

        self.lbl_summary = QLabel("  reading full RAM...  ")
        self.lbl_summary.setStyleSheet(INFOBAR_QSS)
        bar.addWidget(self.lbl_summary, 1)

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setMinimumWidth(scaled_px(70))
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        # Zwei Tabellen nebeneinander: Reads links, Writes rechts
        tables_row = QHBoxLayout()
        tables_row.setSpacing(8)

        mono = get_mono_font(11)

        # Reads-Tabelle
        reads_box = QVBoxLayout()
        reads_box.setSpacing(2)
        reads_box.addWidget(QLabel("<b>Reads</b> (LDA/CMP/BIT/RMW/JMP ind)"))
        self.tbl_reads = QTableWidget()
        self.tbl_reads.setFont(mono)
        self.tbl_reads.setColumnCount(5)
        self.tbl_reads.setHorizontalHeaderLabels(
            ["PC", "Bytes", "Mnemonic", "Operand", "Note"])
        self.tbl_reads.verticalHeader().setVisible(False)
        self.tbl_reads.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_reads.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_reads.setColumnWidth(0, 50)
        self.tbl_reads.setColumnWidth(1, 80)
        self.tbl_reads.setColumnWidth(2, 60)
        self.tbl_reads.setColumnWidth(3, 90)
        hh = self.tbl_reads.horizontalHeader()
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tbl_reads.verticalHeader().setDefaultSectionSize(20)
        reads_box.addWidget(self.tbl_reads, 1)
        reads_w = QWidget(); reads_w.setLayout(reads_box)
        tables_row.addWidget(reads_w, 1)

        # Writes-Tabelle
        writes_box = QVBoxLayout()
        writes_box.setSpacing(2)
        writes_box.addWidget(QLabel("<b>Writes</b> (STA/STX/STY/STZ/RMW)"))
        self.tbl_writes = QTableWidget()
        self.tbl_writes.setFont(mono)
        self.tbl_writes.setColumnCount(5)
        self.tbl_writes.setHorizontalHeaderLabels(
            ["PC", "Bytes", "Mnemonic", "Operand", "Note"])
        self.tbl_writes.verticalHeader().setVisible(False)
        self.tbl_writes.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_writes.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_writes.setColumnWidth(0, 50)
        self.tbl_writes.setColumnWidth(1, 80)
        self.tbl_writes.setColumnWidth(2, 60)
        self.tbl_writes.setColumnWidth(3, 90)
        hh2 = self.tbl_writes.horizontalHeader()
        hh2.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tbl_writes.verticalHeader().setDefaultSectionSize(20)
        writes_box.addWidget(self.tbl_writes, 1)
        writes_w = QWidget(); writes_w.setLayout(writes_box)
        tables_row.addWidget(writes_w, 1)

        layout.addLayout(tables_row, 1)

        # Initial: vollen RAM-Dump anstossen. Disable Re-read waehrend
        # noch gelesen wird damit der User nicht doppelt feuert.
        self._start_read()

    def _start_read(self):
        """Vollen RAM-Dump $0000-$FFFF im Worker lesen. UI bleibt
        responsive; bei Erfolg wird automatisch analysiert."""
        if self._read_worker is not None and self._read_worker.isRunning():
            return
        self.btn_reread.setEnabled(False)
        self.btn_reanalyze.setEnabled(False)
        self.lbl_summary.setText("  reading full $0000-$FFFF RAM...  ")
        # _ReadMemWorker erwartet ein backend-Objekt. Wir liefern es
        # vom Parent durch.
        self._read_worker = _ReadMemWorker(
            backend=self._backend,
            address=0x0000, length=0x10000, parent=self)
        self._read_worker.done.connect(self._on_read_done)
        self._read_worker.start()

    def _on_read_done(self, ok, result, address):
        self._read_worker = None
        self.btn_reread.setEnabled(True)
        if not ok:
            self.lbl_summary.setText(f"  FAILED: {result}  ")
            QMessageBox.warning(self, "Find references",
                                  f"RAM read failed: {result}")
            return
        self._full_dump = bytes(result)
        self.btn_reanalyze.setEnabled(True)
        self._populate()

    def _populate(self):
        """Disassembly + Filter + beide Tabellen befuellen."""
        if self._full_dump is None:
            # Nothing to analyse yet
            return
        from . import c64_disasm
        refs = c64_disasm.find_references(
            self._full_dump, 0x0000, self._target,
            show_illegal=self._show_illegal)

        # RMW erscheint in BEIDEN Listen - lokal split. indjmp gilt
        # als read (Vector wird geladen).
        reads = [r for r in refs
                   if r.access in ("read", "rmw", "indjmp")]
        writes = [r for r in refs
                    if r.access in ("write", "rmw")]

        self._fill_table(self.tbl_reads, reads)
        self._fill_table(self.tbl_writes, writes)

        # Summary: exact vs fuzzy counts
        ex_r = sum(1 for r in reads if r.exact)
        fz_r = len(reads) - ex_r
        ex_w = sum(1 for r in writes if r.exact)
        fz_w = len(writes) - ex_w
        self.lbl_summary.setText(
            f"  ${self._target:04X}: "
            f"reads {len(reads)} ({ex_r} exact, {fz_r} fuzzy), "
            f"writes {len(writes)} ({ex_w} exact, {fz_w} fuzzy)  ")

    def _fill_table(self, tbl, refs):
        tbl.setRowCount(len(refs))
        for row, r in enumerate(refs):
            it_pc = QTableWidgetItem(f"{r.pc:04X}")
            it_pc.setFlags(Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 0, it_pc)

            bs = " ".join(f"{b:02X}" for b in r.bytes)
            it_bytes = QTableWidgetItem(bs)
            it_bytes.setFlags(Qt.ItemFlag.ItemIsEnabled
                               | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 1, it_bytes)

            it_mn = QTableWidgetItem(r.mnemonic)
            it_mn.setFlags(Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
            if not r.exact:
                it_mn.setForeground(QColor(Qt.GlobalColor.darkGray))
            tbl.setItem(row, 2, it_mn)

            it_op = QTableWidgetItem(r.operand_str)
            it_op.setFlags(Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
            if not r.exact:
                it_op.setForeground(QColor(Qt.GlobalColor.darkGray))
            tbl.setItem(row, 3, it_op)

            note = r.note if r.exact else f"fuzzy: {r.note}"
            it_note = QTableWidgetItem(note)
            it_note.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
            if not r.exact:
                it_note.setForeground(QColor(Qt.GlobalColor.darkGray))
            tbl.setItem(row, 4, it_note)


class CodePatternDialog(QDialog):
    """Zeigt Code-Pattern-Treffer (Cheat-Engine-Stil) und tracked sie
    optional live.

    Zwei Modi:
      mode="value_loads"   - sucht Stellen wo `value` als immediate
                              geladen/verglichen wird (LDA #$VV etc).
                              Eine Tabelle, gefuellt aus find_value_loads.
      mode="counter_ops"   - sucht Counter-Manipulation auf `target_addr`
                              (INC/DEC/LDA+STA/LDA+CMP+BEQ).
                              Zwei Tabellen (Modifications + Compares),
                              gefuellt aus find_counter_ops.

    Bei Aufgehen wird $0000-$FFFF gelesen und gecached - so kann die
    Pattern-Suche auch Stellen finden die NICHT im aktuellen
    MemoryView-Range liegen.

    Live-Tracking-Checkbox: bei Aktivierung wird alle 100ms nur der
    Speicher-Bereich gelesen der die gefundenen Pattern-Bytes enthaelt
    (min-max-Range). Bei aktuellem Read werden die Werte in der Tabelle
    aktualisiert - bei selbst-modifizierendem Code sieht man so welche
    Patterns "zerstoert" werden (z.B. ein NOP-Patch zur Laufzeit).

    Non-modal, parent=None damit es separat vom Quopus minimiert wird.
    """

    def __init__(self, parent, backend, mode, value=None,
                   target_addr=None):
        super().__init__(None)
        self._dopus_parent = parent
        self._backend = backend
        self._mode = mode               # "value_loads" | "counter_ops"
        self._value = value             # bei value_loads gesetzt
        self._target_addr = target_addr # bei counter_ops gesetzt
        self._full_dump = None
        self._read_worker = None
        self._hits = []                 # alle Treffer (CodePatternHit-Liste)
        self._mods = []                 # bei counter_ops: hits['mods']
        self._compares = []             # bei counter_ops: hits['compares']

        # Live-Tracking
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._on_live_tick)
        self._live_in_flight = False
        # Range fuer Live-Reads: min/max PC + Pattern-Laenge.
        # Wird in _populate gesetzt sobald wir hits haben.
        self._live_range = None

        if mode == "value_loads":
            self.setWindowTitle(
                f"Find code loads of ${value:02X}")
        else:
            self.setWindowTitle(
                f"Find counter ops on ${target_addr:04X}")
        self.resize(900, 520)

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header
        if mode == "value_loads":
            info_txt = (
                f"Showing every 6502 instruction that loads or "
                f"compares value <b>${value:02X}</b> as an immediate "
                f"(LDA/LDX/LDY/CMP/CPX/CPY/ADC/SBC/AND/ORA/EOR #$"
                f"{value:02X}). Scans the full $0000-$FFFF address "
                f"space. False positives possible at byte sequences "
                f"that happen to match.")
        else:
            info_txt = (
                f"Showing every 6502 code pattern that modifies or "
                f"compares <b>${target_addr:04X}</b>. Modifications "
                f"are INC/DEC and immediate stores; comparisons are "
                f"LDA + CMP + conditional branch sequences. "
                f"Full $0000-$FFFF scan, may include false positives.")
        info = QLabel(info_txt)
        info.setWordWrap(True)
        info.setStyleSheet(f"color: #888; font-size: {scaled_font_px(11)}px;")
        layout.addWidget(info)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_reanalyze = QPushButton("Re-analyze")
        self.btn_reanalyze.setStyleSheet(button_qss("blue"))
        self.btn_reanalyze.setMinimumWidth(scaled_px(100))
        self.btn_reanalyze.setToolTip(
            "Re-run the pattern search on the cached RAM dump.")
        self.btn_reanalyze.clicked.connect(self._populate)
        self.btn_reanalyze.setEnabled(False)
        bar.addWidget(self.btn_reanalyze)

        self.btn_reread = QPushButton("Re-read RAM")
        self.btn_reread.setStyleSheet(button_qss("green"))
        self.btn_reread.setMinimumWidth(scaled_px(100))
        self.btn_reread.setToolTip(
            "Read the full $0000-$FFFF RAM again and re-analyze.")
        self.btn_reread.clicked.connect(self._start_read)
        bar.addWidget(self.btn_reread)

        self.chk_live = QCheckBox("Live (100ms)")
        self.chk_live.setToolTip(
            "Continuously poll the pattern bytes to detect self-"
            "modifying code or runtime patching.")
        self.chk_live.setEnabled(False)
        self.chk_live.toggled.connect(self._on_live_toggled)
        bar.addWidget(self.chk_live)

        self.lbl_summary = QLabel("  reading full RAM...  ")
        self.lbl_summary.setStyleSheet(INFOBAR_QSS)
        bar.addWidget(self.lbl_summary, 1)

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setMinimumWidth(scaled_px(70))
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        mono = get_mono_font(11)

        if mode == "value_loads":
            # Eine Tabelle
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(QLabel(
                f"<b>Immediate loads/compares of ${value:02X}</b>"))
            self.tbl_main = self._make_table(mono)
            box.addWidget(self.tbl_main, 1)
            wrap = QWidget(); wrap.setLayout(box)
            layout.addWidget(wrap, 1)
            # In counter_ops-Mode haben wir 2 Tabellen - hier nur eine.
            # tbl_compares = None damit das Live-Update beide handlen
            # kann.
            self.tbl_compares = None
        else:
            # Counter-Ops: zwei Tabellen untereinander
            mods_box = QVBoxLayout()
            mods_box.setSpacing(2)
            mods_box.addWidget(QLabel(
                "<b>Modifications</b> (INC/DEC/LDA+STA)"))
            self.tbl_main = self._make_table(mono)
            mods_box.addWidget(self.tbl_main, 1)
            mods_w = QWidget(); mods_w.setLayout(mods_box)
            layout.addWidget(mods_w, 1)

            cmp_box = QVBoxLayout()
            cmp_box.setSpacing(2)
            cmp_box.addWidget(QLabel(
                "<b>Comparisons</b> (LDA+CMP+BEQ/BNE/BPL/BMI)"))
            self.tbl_compares = self._make_table(mono)
            cmp_box.addWidget(self.tbl_compares, 1)
            cmp_w = QWidget(); cmp_w.setLayout(cmp_box)
            layout.addWidget(cmp_w, 1)

        self._start_read()

    def _make_table(self, mono):
        """Hilfs-Konstruktor: leere Pattern-Tabelle."""
        t = QTableWidget()
        t.setFont(mono)
        t.setColumnCount(4)
        t.setHorizontalHeaderLabels(
            ["PC", "Bytes", "Description", "Note"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        t.setColumnWidth(0, 50)
        t.setColumnWidth(1, 130)
        t.setColumnWidth(2, 280)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        t.verticalHeader().setDefaultSectionSize(20)
        return t

    def _start_read(self):
        """Vollen RAM-Dump $0000-$FFFF im Worker lesen."""
        if (self._read_worker is not None
                and self._read_worker.isRunning()):
            return
        self.btn_reread.setEnabled(False)
        self.btn_reanalyze.setEnabled(False)
        # Live nicht stoppen, falls aktiv - der naechste Tick laeuft
        # nach dem Read normal weiter.
        self.lbl_summary.setText("  reading full $0000-$FFFF RAM...  ")
        self._read_worker = _ReadMemWorker(
            backend=self._backend,
            address=0x0000, length=0x10000, parent=self)
        self._read_worker.done.connect(self._on_read_done)
        self._read_worker.start()

    def _on_read_done(self, ok, result, address):
        self._read_worker = None
        self.btn_reread.setEnabled(True)
        if not ok:
            self.lbl_summary.setText(f"  FAILED: {result}  ")
            QMessageBox.warning(self, "Find code pattern",
                                  f"RAM read failed: {result}")
            return
        self._full_dump = bytes(result)
        self.btn_reanalyze.setEnabled(True)
        self._populate()

    def _populate(self):
        """Pattern-Suche + Tabellen befuellen + Live-Range bestimmen."""
        if self._full_dump is None:
            return
        from . import c64_disasm

        if self._mode == "value_loads":
            self._hits = c64_disasm.find_value_loads(
                self._full_dump, 0x0000, self._value)
            self._fill_table(self.tbl_main, self._hits)
            self.lbl_summary.setText(
                f"  ${self._value:02X}: {len(self._hits)} immediate "
                f"loads/compares  ")
            all_hits = self._hits
        else:
            result = c64_disasm.find_counter_ops(
                self._full_dump, 0x0000, self._target_addr)
            self._mods = result["mods"]
            self._compares = result["compares"]
            self._fill_table(self.tbl_main, self._mods)
            self._fill_table(self.tbl_compares, self._compares)
            self.lbl_summary.setText(
                f"  ${self._target_addr:04X}: "
                f"{len(self._mods)} modifications, "
                f"{len(self._compares)} comparisons  ")
            all_hits = self._mods + self._compares

        # Live-Range bestimmen: kleinster PC bis groesster PC + Pattern-
        # Laenge. Wenn keine Treffer: Live disabled.
        if all_hits:
            min_pc = min(h.pc for h in all_hits)
            max_pc = max(h.pc + len(h.bytes) - 1 for h in all_hits)
            self._live_range = (min_pc, max_pc - min_pc + 1)
            self.chk_live.setEnabled(True)
        else:
            self._live_range = None
            self.chk_live.setEnabled(False)
            if self.chk_live.isChecked():
                self.chk_live.setChecked(False)

    def _fill_table(self, tbl, hits):
        tbl.setRowCount(len(hits))
        for row, h in enumerate(hits):
            it_pc = QTableWidgetItem(f"{h.pc:04X}")
            it_pc.setFlags(Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 0, it_pc)

            bs = " ".join(f"{b:02X}" for b in h.bytes)
            it_bytes = QTableWidgetItem(bs)
            it_bytes.setFlags(Qt.ItemFlag.ItemIsEnabled
                               | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 1, it_bytes)

            it_desc = QTableWidgetItem(h.description)
            it_desc.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 2, it_desc)

            it_note = QTableWidgetItem(h.note)
            it_note.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
            tbl.setItem(row, 3, it_note)

    def _on_live_toggled(self, checked):
        if checked:
            if self._live_range is None:
                self.chk_live.setChecked(False)
                return
            self._live_in_flight = False
            self._live_timer.start()
            self.lbl_summary.setText(
                f"  Live: polling pattern bytes every 100ms  ")
        else:
            self._live_timer.stop()
            # Status zurueck auf statisches Ergebnis
            self._populate_summary_static()

    def _populate_summary_static(self):
        """Status-Label auf statischen 'Treffer-Count' zuruecksetzen."""
        if self._mode == "value_loads":
            self.lbl_summary.setText(
                f"  ${self._value:02X}: {len(self._hits)} immediate "
                f"loads/compares  ")
        else:
            self.lbl_summary.setText(
                f"  ${self._target_addr:04X}: "
                f"{len(self._mods)} modifications, "
                f"{len(self._compares)} comparisons  ")

    def _on_live_tick(self):
        """Timer-Tick: Live-Range lesen, Tabellen-Bytes aktualisieren,
        farblich hervorheben was sich geaendert hat (selbst-modi-
        fizierender Code)."""
        if self._live_in_flight:
            return
        if self._live_range is None:
            self._live_timer.stop()
            return
        self._live_in_flight = True
        try:
            start, length = self._live_range
            ok, result = self._backend.read(start, length)
            if not ok:
                self.lbl_summary.setText(
                    f"  Live FAILED: {result}  ")
                return
            chunk = bytes(result)
            # full_dump patchen damit Re-Analyze konsistent ist
            if self._full_dump is not None:
                fd = bytearray(self._full_dump)
                fd[start:start + length] = chunk
                self._full_dump = bytes(fd)

            # Tabellen-Bytes-Spalte updaten + Farbe wenn aenderung
            self._update_table_live(self.tbl_main,
                                       self._mods if self._mode == "counter_ops"
                                       else self._hits,
                                       start, chunk)
            if self.tbl_compares is not None:
                self._update_table_live(self.tbl_compares,
                                           self._compares, start, chunk)
        finally:
            self._live_in_flight = False

    def _update_table_live(self, tbl, hits, range_start, chunk):
        """Vergleicht die alten hit.bytes mit dem frisch gelesenen
        chunk und faerbt die Bytes-Zelle gelb wenn sich was geaendert
        hat. Aktualisiert auch hit.bytes damit der naechste Tick die
        Aenderung als Basis hat."""
        changed_any = False
        for row, h in enumerate(hits):
            offset = (h.pc - range_start) & 0xFFFF
            if offset + len(h.bytes) > len(chunk):
                continue
            new_bytes = list(chunk[offset:offset + len(h.bytes)])
            if new_bytes != list(h.bytes):
                changed_any = True
                # Hex-Anzeige updaten
                bs = " ".join(f"{b:02X}" for b in new_bytes)
                it = tbl.item(row, 1)
                if it is not None:
                    it.setText(bs)
                    it.setForeground(QColor(Qt.GlobalColor.red))
                h.bytes = new_bytes
        # Status: zeige live-Aktivitaet (nicht den statischen Count)
        if changed_any:
            self.lbl_summary.setText(
                f"  Live: PATTERN BYTES CHANGED - self-modifying "
                f"code detected!  ")

    def closeEvent(self, ev):
        try:
            self._live_timer.stop()
        except Exception:
            pass
        super().closeEvent(ev)


class MemoryViewDialog(QDialog):
    """Zeigt aus dem U64 ausgelesenes Memory als 6502 Disassembly
    oder als klassischen Hex-Dump - mit Live-Edit.

    Bedienung:
      - Refresh: liest erneut von der gleichen Adresse
      - ASM / HEX: schaltet zwischen den beiden Darstellungen
      - Doppelklick auf Hex-Zelle: editiert ein einzelnes Byte
        (1-2 Hex-Ziffern). Mit Enter wird der neue Wert sofort via
        DMA in den C64-Speicher gepokt (u64_writemem).
      - Save...: schreibt rohe Bytes (.bin) oder Text-Dump auf Platte
      - Close: macht das Fenster zu

    HEX-View: 18-spaltige Tabelle - ADDR + 16 Byte-Zellen + ASCII.
    Alle 16 Byte-Zellen sind editierbar; ADDR und ASCII sind read-
    only. ASCII-Spalte wird live mitgeupdatet wenn ein Byte poked
    wurde.

    ASM-View: 5-spaltige Tabelle - ADDR + 3 Hex-Byte-Zellen +
    Mnemonic+Operand. Die Hex-Spalten sind editierbar (manche
    Instructions belegen nur 1-2 davon - leere bleiben gesperrt).
    Nach einem Poke wird ab der Edit-Adresse neu disassembliert,
    weil eine Byte-Aenderung die Instruction-Laenge umwerfen kann.

    Dialog ist non-modal (show()), damit Mario nebenher weiter mit
    dem Stream interagieren kann.
    """

    # Spalten-Indizes fuer HEX-View
    _HX_COL_ADDR = 0
    _HX_COL_FIRST_BYTE = 1
    _HX_COL_LAST_BYTE = 16   # inklusive (1..16)
    _HX_COL_ASCII = 17

    # Spalten-Indizes fuer ASM-View
    _AS_COL_ADDR = 0
    _AS_COL_B0 = 1
    _AS_COL_B1 = 2
    _AS_COL_B2 = 3
    _AS_COL_MN = 4

    # UserRole-Key fuer "Adresse die hinter dieser Zelle steckt"
    _ROLE_ADDR = Qt.ItemDataRole.UserRole + 1
    # UserRole-Key fuer "Original-Hex-String vor dem Edit" (Rollback)
    _ROLE_ORIGINAL = Qt.ItemDataRole.UserRole + 2
    # UserRole-Key fuer "wie viele Bytes belegt die Instruction"
    # (nur in der ASM-Mnemonic-Spalte gesetzt)
    _ROLE_LEN = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent, backend, address, length):
        """Create the dialog.

        `backend` is an object with `read(addr, length)` and
        `write(addr, data)` methods (see _U64MemoryBackend and
        _ViceMemoryBackend). This is what lets the same dialog drive
        either an Ultimate 64 or a VICE emulator.

        Wir uebergeben parent=None an QDialog, damit das Fenster
        wirklich unabhaengig vom Quopus-Mainwindow lebt - andernfalls
        minimiert/schliesst Windows den Dialog mit dem Parent
        mit, egal welche Window-Flags wir setzen. Den `parent`-
        Argument benutzen wir nur fuer Config-Lookups (siehe
        `_show_illegal`).
        """
        super().__init__(None)
        # Parent-Ref nur fuer Config-Zugriffe (keine Window-Beziehung)
        self._dopus_parent = parent
        self._backend = backend
        self._address = address & 0xFFFF
        self._length = length
        self._data = bytearray()         # mutable - wird vom poke updated
        # Snapshot vom letzten Render-Zustand. Wird vor jedem
        # Backend-Read aktualisiert; das Rendering vergleicht byte-fuer-
        # byte mit dem aktuellen _data um geaenderte Bytes farbig zu
        # markieren (rot). Leer = "noch nie gerendert", dann werden
        # alle Bytes als unveraendert behandelt (kein rot beim ersten
        # Read).
        self._prev_data = bytearray()
        self._read_worker = None
        self._write_worker = None
        self._view_mode = "asm"          # "asm" | "hex"
        self._suppress_item_changed = False  # Re-Entrancy-Schutz
        # Set von Hex-Zeilen-Indizes die NICHT komprimiert dargestellt
        # werden sollen (User hat sie per Doppelklick aufgeklappt).
        # Wird bei jedem fresh read (RefreshLeft) geleert.
        self._hex_expanded_rows = set()

        # --- Cheat-Engine / ICU64-Style Memory-Search state ---
        # _start_values: erster komplett-dump (referenz zum Vergleichen
        #   wenn der User "Reset" drueckt).
        # _last_values:  dump vom *letzten* Filter-Klick (oder Start).
        #   Vergleichsbasis fuer Changed/Increased/Decreased - jeder
        #   Klick verwendet das vorige Snapshot, nicht den Start.
        # _candidates:   set von Adressen die nach allen Filter-Schritten
        #   noch uebrig sind. None = noch nicht gestartet, dann zeigt der
        #   Dialog wie bisher alle Zeilen.
        # _last_filter:  Name des letzten Filters ("changed" | "inc" |
        #   "dec" | None) - der Live-Timer wendet diesen erneut an.
        # _live_timer:   QTimer der bei aktivem Live-Mode alle 100ms
        #   den letzten Filter neu anwendet.
        self._start_values = None
        self._last_values = None
        self._candidates = None
        self._last_filter = None
        # Fuer den "= Value" Filter: das zuletzt eingegebene Target-
        # Byte, damit Live-Mode den Value-Filter erneut anwenden kann.
        self._last_value_target = None
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._on_live_tick)
        # Wir verhindern parallele live-reads: wenn ein read noch
        # laeuft, ueberspringen wir den naechsten timer-tick statt
        # eine zweite Anfrage aufzustapeln.
        self._live_in_flight = False

        # show_illegal-Praeferenz aus der Quopus-Config lesen
        self._show_illegal = False
        try:
            w = parent
            while w is not None and not hasattr(w, 'config'):
                w = w.parent()
            if w and hasattr(w, 'config'):
                self._show_illegal = bool(
                    w.config.get('c64_show_illegal', False))
        except Exception:
            pass

        backend_label = getattr(self._backend, 'label', 'Memory')
        self.setWindowTitle(
            f"{backend_label}: ${self._address:04X}..${(self._address + length - 1) & 0xFFFF:04X}"
            f" ({length} bytes)")
        self.resize(820, 560)

        # Eigenstaendiges Top-Level-Fenster: damit das Memory-Tool
        # zugaenglich bleibt wenn Quopus minimiert wird. Mit dem Window-
        # Flag bekommt der Dialog einen eigenen Taskleisten-Eintrag
        # und unterliegt nicht mehr dem Parent-Lifecycle (auf-/zu mit
        # Quopus). Min/Max-Buttons in die Titelleiste damit man das
        # Fenster auch separat verschieben/skalieren kann.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setStyleSheet(button_qss("green"))
        self.btn_refresh.setMinimumWidth(scaled_px(90))
        self.btn_refresh.setToolTip(
            "Re-read the same memory range from the U64")
        self.btn_refresh.clicked.connect(self._start_read)
        bar.addWidget(self.btn_refresh)

        self.rb_asm = QRadioButton("ASM")
        self.rb_asm.setChecked(True)
        self.rb_asm.setToolTip("6502 disassembly view")
        self.rb_hex = QRadioButton("HEX")
        self.rb_hex.setToolTip("Classic hex dump view")
        self._rb_group = QButtonGroup(self)
        self._rb_group.addButton(self.rb_asm)
        self._rb_group.addButton(self.rb_hex)
        self.rb_asm.toggled.connect(self._on_view_changed)
        bar.addWidget(self.rb_asm)
        bar.addWidget(self.rb_hex)

        bar.addSpacing(12)
        self.lbl_status = QLabel("  ready  ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        bar.addWidget(self.lbl_status, 1)

        self.btn_save = QPushButton("Save...")
        self.btn_save.setStyleSheet(button_qss("blue"))
        self.btn_save.setMinimumWidth(scaled_px(80))
        self.btn_save.setToolTip(
            "Save raw bytes (.bin) or current text view")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save.setEnabled(False)
        bar.addWidget(self.btn_save)

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setMinimumWidth(scaled_px(70))
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)

        layout.addLayout(bar)

        # --- Memory-Search / Cheat-Engine bar ---
        # ICU64-style: Start snapshot, dann Filter-Klicks reduzieren die
        # Kandidatenliste. Jeder Filter vergleicht den AKTUELLEN Read mit
        # dem LETZTEN Snapshot (nicht dem Start) - so kann man eine sich
        # aendernde Adresse (z.B. Lifecount in einem Game) in wenigen
        # Schritten eingrenzen: Spieler verliert ein Leben -> "Decreased"
        # klicken, Spieler bekommt eines dazu -> "Increased", etc.
        search_bar = QHBoxLayout()
        search_bar.setSpacing(6)
        self.btn_search_start = QPushButton("Start")
        self.btn_search_start.setStyleSheet(button_qss("blue"))
        self.btn_search_start.setMinimumWidth(scaled_px(70))
        self.btn_search_start.setToolTip(
            "Take an initial snapshot of the current memory range. "
            "All addresses become candidates; the Changed/Increased/"
            "Decreased buttons then filter them down.")
        self.btn_search_start.clicked.connect(self._on_search_start)
        search_bar.addWidget(self.btn_search_start)

        self.btn_search_changed = QPushButton("Changed")
        self.btn_search_changed.setStyleSheet(button_qss("orange"))
        self.btn_search_changed.setMinimumWidth(scaled_px(80))
        self.btn_search_changed.setToolTip(
            "Keep only addresses whose value changed since the last "
            "snapshot (Start or last filter click).")
        self.btn_search_changed.clicked.connect(
            lambda: self._apply_filter("changed"))
        self.btn_search_changed.setEnabled(False)
        search_bar.addWidget(self.btn_search_changed)

        self.btn_search_inc = QPushButton("Increased")
        self.btn_search_inc.setStyleSheet(button_qss("orange"))
        self.btn_search_inc.setMinimumWidth(scaled_px(85))
        self.btn_search_inc.setToolTip(
            "Keep only addresses whose value is now strictly greater "
            "than at the last snapshot.")
        self.btn_search_inc.clicked.connect(
            lambda: self._apply_filter("inc"))
        self.btn_search_inc.setEnabled(False)
        search_bar.addWidget(self.btn_search_inc)

        self.btn_search_dec = QPushButton("Decreased")
        self.btn_search_dec.setStyleSheet(button_qss("orange"))
        self.btn_search_dec.setMinimumWidth(scaled_px(90))
        self.btn_search_dec.setToolTip(
            "Keep only addresses whose value is now strictly smaller "
            "than at the last snapshot.")
        self.btn_search_dec.clicked.connect(
            lambda: self._apply_filter("dec"))
        self.btn_search_dec.setEnabled(False)
        search_bar.addWidget(self.btn_search_dec)

        self.btn_search_unchanged = QPushButton("Unchanged")
        self.btn_search_unchanged.setStyleSheet(button_qss("orange"))
        self.btn_search_unchanged.setMinimumWidth(scaled_px(95))
        self.btn_search_unchanged.setToolTip(
            "Keep only addresses whose value is exactly equal to the "
            "last snapshot (e.g. constants, idle counters).")
        self.btn_search_unchanged.clicked.connect(
            lambda: self._apply_filter("unchanged"))
        self.btn_search_unchanged.setEnabled(False)
        search_bar.addWidget(self.btn_search_unchanged)

        # Exact-Value Filter: User gibt einen Byte-Wert ein und der
        # Filter behaelt nur Adressen die *exakt* diesen Wert haben.
        # Pre-fill mit der ersten Suche moeglich: wenn der User noch
        # keine Start-Snapshot gemacht hat, macht der Klick implizit
        # erst Start (alle Adressen als Kandidaten) und filtert dann.
        # So findet man z.B. einen Lifecount sofort: "3" eingeben,
        # klick -> alle bytes die 3 sind sind Kandidaten. Spieler
        # verliert ein Leben -> "Value: 2" filter -> nur noch der
        # echte Lifecount-Counter bleibt uebrig.
        self.btn_search_value = QPushButton("= Value")
        self.btn_search_value.setStyleSheet(button_qss("orange"))
        self.btn_search_value.setMinimumWidth(scaled_px(70))
        self.btn_search_value.setToolTip(
            "Keep only addresses whose value matches the entered byte. "
            "Format: decimal (3, 100, 255) or hex ($03, 0x03). "
            "Auto-starts a snapshot if none active yet.")
        self.btn_search_value.clicked.connect(self._on_search_value)
        search_bar.addWidget(self.btn_search_value)

        self.ed_value = QLineEdit()
        self.ed_value.setMinimumWidth(scaled_px(60))
        self.ed_value.setPlaceholderText("3 / $03")
        self.ed_value.setToolTip(
            "Byte value to search for. Decimal (0-255) or hex with "
            "$ or 0x prefix.")
        # Enter im LineEdit triggert den Button - bequemer
        self.ed_value.returnPressed.connect(self._on_search_value)
        search_bar.addWidget(self.ed_value)

        # Pattern-Suche im CODE auf den eingegebenen Value: findet
        # alle Stellen wo dieser Byte als immediate geladen oder
        # verglichen wird (LDA #$VV, LDX #$VV, LDY #$VV, CMP/CPX/CPY
        # #$VV, ADC/SBC/AND/ORA/EOR #$VV). Anders als der Value-
        # Filter (der Daten-Adressen mit diesem Wert sucht) zielt das
        # auf Code: "wo wird in dieser ROM die '3' fuer 3 Leben geladen?"
        self.btn_find_loads = QPushButton("Find loads")
        self.btn_find_loads.setStyleSheet(button_qss("blue"))
        self.btn_find_loads.setMinimumWidth(scaled_px(80))
        self.btn_find_loads.setToolTip(
            "Find all code locations where this byte value is loaded "
            "or compared as immediate (LDA/LDX/LDY/CMP/CPX/CPY/ADC/SBC "
            "#$VV). Scans the full $0000-$FFFF address space.")
        self.btn_find_loads.clicked.connect(self._on_find_value_loads)
        search_bar.addWidget(self.btn_find_loads)

        self.chk_live = QCheckBox("Live (100ms)")
        self.chk_live.setToolTip(
            "Continuously re-apply the last filter every 100ms. "
            "Useful to watch a value pulse / flicker / count.")
        self.chk_live.setEnabled(False)
        self.chk_live.toggled.connect(self._on_live_toggled)
        search_bar.addWidget(self.chk_live)

        self.btn_search_reset = QPushButton("Reset")
        self.btn_search_reset.setStyleSheet(button_qss("red"))
        self.btn_search_reset.setMinimumWidth(scaled_px(60))
        self.btn_search_reset.setToolTip(
            "Clear the search: show all addresses again.")
        self.btn_search_reset.clicked.connect(self._on_search_reset)
        self.btn_search_reset.setEnabled(False)
        search_bar.addWidget(self.btn_search_reset)

        self.lbl_cand = QLabel("  no search active  ")
        self.lbl_cand.setStyleSheet(INFOBAR_QSS)
        search_bar.addWidget(self.lbl_cand, 1)

        layout.addLayout(search_bar)

        # Hinweis zur Edit-Funktion (kleiner Hint, damit klar ist
        # dass die Tabellen edit-bar sind und Aenderungen LIVE poken)
        hint = QLabel(
            "Double-click a hex cell to edit (1-2 hex digits, "
            "Enter to poke).")
        hint.setStyleSheet(f"color: #888; font-size: {scaled_font_px(11)}px;")
        layout.addWidget(hint)

        # Beide Views als QTableWidget; via QStackedWidget gewechselt
        self._stack = QStackedWidget()

        mono = get_mono_font(11)
        self._delegate = _HexByteDelegate(self)

        # --- HEX-Table ---
        self.tbl_hex = QTableWidget()
        self.tbl_hex.setFont(mono)
        self.tbl_hex.setColumnCount(18)
        self.tbl_hex.setHorizontalHeaderLabels(
            ["Addr"] + [f"{i:X}" for i in range(16)] + ["ASCII"])
        # Edit nur in Byte-Spalten zulassen. Wir setzen ItemFlags
        # per Cell unten (ADDR und ASCII haben kein ItemIsEditable).
        self.tbl_hex.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.tbl_hex.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_hex.verticalHeader().setVisible(False)
        # Delegate fuer alle Byte-Spalten
        for c in range(self._HX_COL_FIRST_BYTE,
                          self._HX_COL_LAST_BYTE + 1):
            self.tbl_hex.setItemDelegateForColumn(c, self._delegate)
        # Spaltenbreiten: ADDR enger, Bytes alle gleich, ASCII breit
        hh = self.tbl_hex.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tbl_hex.setColumnWidth(self._HX_COL_ADDR, 60)
        for c in range(self._HX_COL_FIRST_BYTE,
                          self._HX_COL_LAST_BYTE + 1):
            self.tbl_hex.setColumnWidth(c, 30)
        hh.setSectionResizeMode(
            self._HX_COL_ASCII, QHeaderView.ResizeMode.Stretch)
        # Globaler Hook fuer "Zelle wurde editiert"
        self.tbl_hex.itemChanged.connect(self._on_hex_cell_changed)
        # Doppelklick auf eine komprimierte Zeile = aufklappen.
        # Wir koennen das nicht ueber ItemFlags.ItemIsEditable
        # signalisieren weil Doppelklick auf die x16-Zelle dort
        # gar nichts macht (NoItemFlags). cellDoubleClicked feuert
        # auf JEDE Zelle - wir filtern dann.
        self.tbl_hex.cellDoubleClicked.connect(
            self._on_hex_cell_double_clicked)
        # Kompakte Zeilen damit mehr auf den Schirm passt
        self.tbl_hex.verticalHeader().setDefaultSectionSize(20)
        # Rechtsklick-Menu (z.B. "Find references" auf die geklickte
        # Adresse). Auf der Tabelle selbst hat Qt schon ein
        # DefaultContextMenu; wir uebernehmen mit Custom damit wir
        # ueberhaupt was hinzufuegen koennen.
        self.tbl_hex.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_hex.customContextMenuRequested.connect(
            self._on_hex_context_menu)
        self._stack.addWidget(self.tbl_hex)

        # --- ASM-Table ---
        self.tbl_asm = QTableWidget()
        self.tbl_asm.setFont(mono)
        self.tbl_asm.setColumnCount(5)
        self.tbl_asm.setHorizontalHeaderLabels(
            ["Addr", "B0", "B1", "B2", "Mnemonic"])
        self.tbl_asm.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.tbl_asm.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_asm.verticalHeader().setVisible(False)
        for c in (self._AS_COL_B0, self._AS_COL_B1, self._AS_COL_B2):
            self.tbl_asm.setItemDelegateForColumn(c, self._delegate)
        hh = self.tbl_asm.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tbl_asm.setColumnWidth(self._AS_COL_ADDR, 60)
        self.tbl_asm.setColumnWidth(self._AS_COL_B0, 36)
        self.tbl_asm.setColumnWidth(self._AS_COL_B1, 36)
        self.tbl_asm.setColumnWidth(self._AS_COL_B2, 36)
        hh.setSectionResizeMode(
            self._AS_COL_MN, QHeaderView.ResizeMode.Stretch)
        self.tbl_asm.itemChanged.connect(self._on_asm_cell_changed)
        self.tbl_asm.verticalHeader().setDefaultSectionSize(20)
        # Rechtsklick-Menu auch in der ASM-Sicht
        self.tbl_asm.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_asm.customContextMenuRequested.connect(
            self._on_asm_context_menu)
        self._stack.addWidget(self.tbl_asm)

        # Sichtbarer Default = ASM
        self._stack.setCurrentWidget(self.tbl_asm)

        layout.addWidget(self._stack, 1)

        # Sofort den ersten Read starten
        self._start_read()

    # ------------------------------------------------------------------
    # Read (HTTP GET via Worker)
    # ------------------------------------------------------------------

    def _start_read(self):
        if self._read_worker is not None:
            return
        self.btn_refresh.setEnabled(False)
        self.btn_save.setEnabled(False)
        # Frischer Read = alle Run-Length-Expansions zuruecksetzen.
        # Sonst zeigen Zeilen die jetzt komprimiert dargestellt werden
        # koennten weiter ihre 16 Einzelzellen.
        self._hex_expanded_rows.clear()
        self.lbl_status.setText(
            f"  reading ${self._address:04X} +{self._length}...  ")
        self._read_worker = _ReadMemWorker(
            backend=self._backend,
            address=self._address, length=self._length,
            parent=self)
        self._read_worker.done.connect(self._on_read_done)
        self._read_worker.start()

    def _on_read_done(self, ok, result, address):
        self._read_worker = None
        self.btn_refresh.setEnabled(True)
        if not ok:
            self.lbl_status.setText(f"  FAILED: {result}  ")
            QMessageBox.warning(self, "Read memory", str(result))
            return
        # Snapshot der alten Bytes BEVOR wir _data ueberschreiben -
        # das Rendering vergleicht damit und markiert geaenderte Bytes
        # rot. Erster Read: _data ist noch leer, also wird _prev_data
        # auch leer und alles als unveraendert behandelt (kein rot).
        self._prev_data = bytes(self._data)
        self._data = bytearray(result)
        self.btn_save.setEnabled(True)
        end_addr = (address + len(self._data) - 1) & 0xFFFF
        self.lbl_status.setText(
            f"  got ${len(self._data):04X} bytes from "
            f"${address:04X} to ${end_addr:04X}  ")
        self._render()

    # ------------------------------------------------------------------
    # Cheat-Engine / ICU64-Style memory search
    # ------------------------------------------------------------------
    # Model:
    #   _start_values   {addr: byte}  -  first snapshot (for Reset)
    #   _last_values    {addr: byte}  -  snapshot to compare against
    #   _candidates     set(addr)     -  surviving addresses
    #   _last_filter    "changed"|"inc"|"dec"|"unchanged"|None
    #
    # Each filter click does a fresh read, compares each candidate
    # address with _last_values, removes addresses that no longer match
    # the filter predicate, then sets _last_values = (current read).
    # That way the user can chain filters: each click narrows further.
    #
    # Live mode re-applies _last_filter every 100ms via QTimer.

    def _on_search_start(self):
        """Initial snapshot: aktueller _data wird als 'last' und
        'start' gespeichert, alle Adressen werden Kandidaten."""
        if not self._data:
            # Noch kein Read passiert - dann erst lesen, dann starten.
            self.lbl_status.setText(
                "  no data yet - press Refresh first  ")
            return
        base = self._address
        # Snapshot als dict {addr: byte} ablegen (NICHT bytearray-Index,
        # damit candidates per Adresse statt offset zugegriffen werden).
        snap = {(base + i) & 0xFFFF: self._data[i]
                  for i in range(len(self._data))}
        self._start_values = snap
        self._last_values = dict(snap)
        self._candidates = set(snap.keys())
        self._last_filter = None
        # Filter-Buttons aktivieren
        for b in (self.btn_search_changed, self.btn_search_inc,
                    self.btn_search_dec, self.btn_search_unchanged,
                    self.btn_search_reset):
            b.setEnabled(True)
        self.chk_live.setEnabled(True)
        self.lbl_cand.setText(
            f"  {len(self._candidates)} candidates "
            f"(initial snapshot)  ")
        # Render anstossen - alle Zeilen sichtbar, das ist der
        # Initial-Zustand. _render() respektiert _candidates schon.
        self._render()

    def _on_search_reset(self):
        """Suchzustand komplett verwerfen. Alle Zeilen werden wieder
        sichtbar. Wenn Live laeuft, blockieren wir Reset - der User
        soll Live bewusst ausschalten muessen, sonst geht durch einen
        falschen Klick die ganze muehsam reduzierte Kandidatenliste
        verloren."""
        if self.chk_live.isChecked():
            self.lbl_status.setText(
                "  Reset blocked while Live is active - "
                "uncheck Live first  ")
            return
        self._start_values = None
        self._last_values = None
        self._candidates = None
        self._last_filter = None
        self._last_value_target = None
        for b in (self.btn_search_changed, self.btn_search_inc,
                    self.btn_search_dec, self.btn_search_unchanged,
                    self.btn_search_reset):
            b.setEnabled(False)
        self.chk_live.setEnabled(False)
        self.lbl_cand.setText("  no search active  ")
        self._render()

    def _apply_filter(self, kind, target_value=None):
        """Filter anwenden. `kind` in
        {"changed","inc","dec","unchanged","value"}.

        Bei kind="value" muss target_value (0..255) gesetzt sein. Der
        Filter behaelt nur Adressen die diesen Byte-Wert haben.

        Wir machen synchron einen fresh Read (Backend.read), weil
        - der User klickt einen Button und erwartet sofort Ergebnis
        - die Range ist meistens klein (1 KB - 4 KB) bei aktiver Suche
        - im Live-Modus ist Latenz wichtiger als Concurrency

        Optimierung: Sobald Kandidaten reduziert sind, lesen wir nur
        noch den Bereich min(candidates)..max(candidates) statt des
        kompletten Dump-Ranges. Bei einer aktiven Suche mit z.B. 50
        Adressen in $0800..$2000 sind das 6 KB statt 63 KB - im Live-
        Mode mit 100ms-Tick macht das einen massiven Unterschied.

        Wir lesen den GESAMTEN min..max-Block (zusammenhaengend) statt
        50 einzelne Bytes weil ein groesserer zusammenhaengender Read
        ueber Backend.read() viel schneller ist als 50 Roundtrips,
        und das Backend chunked intern eh.

        Wenn der Read fehlschlaegt: Status, keine Aenderung am State.
        """
        if self._candidates is None or self._last_values is None:
            return

        # Smart range: nur Kandidaten-Bereich lesen wenn die Liste
        # bereits eingegrenzt ist. Initial (oder bei voller Liste) den
        # ganzen Range, damit auch _data fuer den HEX/ASM-Render
        # frisch ist.
        full_count = self._length
        cand_count = len(self._candidates)
        # Heuristik: wenn die Kandidaten weniger als die Haelfte des
        # Ranges sind, lohnt der schmalere Read. Sonst der ganze Range
        # damit _data (Display-Buffer) komplett aktuell bleibt.
        if cand_count > 0 and cand_count < full_count // 2:
            read_start = min(self._candidates)
            read_end = max(self._candidates)
            read_len = read_end - read_start + 1
            partial = True
        else:
            read_start = self._address
            read_len = self._length
            partial = False

        ok, result = self._backend.read(read_start, read_len)
        if not ok:
            self.lbl_status.setText(
                f"  FAILED: {result}  ")
            return
        chunk = bytearray(result)

        # new_values: dict {addr -> byte} fuer die gelesenen Bytes.
        # Bei full-Read deckt das den ganzen Range ab; bei partial
        # deckt es den min..max-Bereich ab - mehr brauchen wir auch
        # nicht, weil alle Kandidaten in dem Bereich liegen.
        new_values = {(read_start + i) & 0xFFFF: chunk[i]
                        for i in range(len(chunk))}

        # _data (Display-Buffer) updaten: bei partial nur das Stueck
        # patchen, der Rest bleibt von vorher. Beim Re-Render sieht der
        # User dann nur die Kandidaten-Zeilen frisch - was OK ist, weil
        # ausgeblendete (Nicht-Kandidaten-) Zeilen eh nicht sichtbar
        # sind im laufenden Filter.
        # Vor dem Update Snapshot fuers Diff-Rendering (rote Bytes).
        self._prev_data = bytes(self._data)
        if partial:
            offset = (read_start - self._address) & 0xFFFF
            # Defensiv: Offset koennte negativ werden wenn die min-
            # Kandidaten-Adresse VOR self._address liegt. Sollte
            # normal nicht passieren (Kandidaten sind ja Bytes aus
            # dem Original-Range), aber checken schadet nicht.
            if 0 <= offset and offset + len(chunk) <= len(self._data):
                self._data[offset:offset + len(chunk)] = chunk
        else:
            self._data = chunk

        # Wertvergleich per Kandidat. Predicate liefert True wenn der
        # Kandidat im Set bleibt. Vergleichsbasis ist _last_values
        # (ausser bei "value" - das ist ein absoluter Vergleich, kein
        # relativer; vorigen Wert braucht's nicht).
        if kind == "changed":
            keep = lambda a: new_values[a] != self._last_values[a]
        elif kind == "inc":
            keep = lambda a: new_values[a] > self._last_values[a]
        elif kind == "dec":
            keep = lambda a: new_values[a] < self._last_values[a]
        elif kind == "unchanged":
            keep = lambda a: new_values[a] == self._last_values[a]
        elif kind == "value":
            if target_value is None:
                return
            tv = target_value & 0xFF
            keep = lambda a: new_values[a] == tv
            # Den Value selber merken damit Live-Mode ihn wiederholen kann
            self._last_value_target = tv
        else:
            return

        self._candidates = {a for a in self._candidates if keep(a)}
        # _last_values updaten - bei partial nur die gelesenen
        # Adressen, der Rest bleibt stale (was OK ist, da wir nur
        # ueber Kandidaten iterieren und die sind alle in new_values).
        if partial:
            self._last_values.update(new_values)
        else:
            self._last_values = new_values
        self._last_filter = kind

        if kind == "value":
            kind_label = f"= ${target_value & 0xFF:02X}"
        else:
            kind_label = {
                "changed":   "changed",
                "inc":       "increased",
                "dec":       "decreased",
                "unchanged": "unchanged",
            }[kind]
        scope = (f"partial ${read_start:04X}..${read_start+read_len-1:04X}"
                   if partial else "full range")
        self.lbl_cand.setText(
            f"  {len(self._candidates)} candidates "
            f"after {kind_label}  ({scope})  ")
        self._render()

    def _on_search_value(self):
        """Handler fuer = Value Button und LineEdit-Enter.

        Parst die User-Eingabe (dezimal oder hex), startet ggf. eine
        neue Suche wenn noch keine aktiv ist, und filtert auf den
        eingegebenen Byte-Wert."""
        txt = self.ed_value.text().strip()
        if not txt:
            self.lbl_status.setText(
                "  enter a byte value first (e.g. 3 or $03)  ")
            return
        try:
            if txt.startswith('$'):
                val = int(txt[1:], 16)
            elif txt.startswith('0x') or txt.startswith('0X'):
                val = int(txt[2:], 16)
            else:
                val = int(txt)
        except ValueError:
            self.lbl_status.setText(
                f"  invalid value: {txt!r} - use decimal or $hex  ")
            return
        if not (0 <= val <= 255):
            self.lbl_status.setText(
                f"  value out of range: {val} (must be 0..255)  ")
            return
        # Wenn noch kein Snapshot aktiv: implizit starten damit der
        # User nicht erst Start druecken muss. Macht den haeufigen
        # Workflow "Wert eingeben + Klick" zu einem Schritt.
        if self._candidates is None:
            if not self._data:
                self.lbl_status.setText(
                    "  no data yet - press Refresh first  ")
                return
            self._on_search_start()
        self._apply_filter("value", target_value=val)

    def _on_live_toggled(self, checked):
        """Live-Mode an/aus.

        WICHTIG: Live filtert NICHT - es zeigt nur kontinuierlich die
        aktuellen Werte der vorhandenen Kandidaten. Die Kandidatenliste
        bleibt unveraendert bis der User selber per Rechtsklick einen
        Kandidaten loescht oder einen neuen Filter klickt.

        Vorbedingung: mindestens ein Snapshot (Start oder erster
        Filter) muss aktiv sein, sonst gibt's keine Kandidaten zum
        Refreshen.
        """
        if checked:
            if self._candidates is None or not self._candidates:
                self.chk_live.setChecked(False)
                self.lbl_status.setText(
                    "  Live: no candidates to track - press Start "
                    "or use a filter first  ")
                return
            self._live_in_flight = False
            self._live_timer.start()
            self.lbl_status.setText(
                f"  Live: refreshing {len(self._candidates)} "
                f"candidate values every 100ms (right-click to "
                f"remove)  ")
        else:
            self._live_timer.stop()

    def _on_live_tick(self):
        """Timer-Tick: nur die Kandidaten-Bytes neu lesen und das
        Display refreshen. KEIN Filter, keine Kandidaten-Reduktion -
        die Liste bleibt stabil bis der User aktiv eingreift.

        Falls noch ein Read laeuft: skippen statt parallel feuern.
        """
        if self._live_in_flight:
            return
        if self._candidates is None or not self._candidates:
            self._live_timer.stop()
            return
        self._live_in_flight = True
        try:
            self._live_refresh_only()
        finally:
            self._live_in_flight = False

    def _live_refresh_only(self):
        """Liest nur den Bereich der die aktuellen Kandidaten abdeckt
        und aktualisiert _data + _last_values. KEIN Filter-Predikat,
        keine Aenderung der Kandidatenliste.

        Range-Berechnung wie in _apply_filter: min(cand)..max(cand)
        damit Live im stark eingegrenzten Zustand winzige Reads macht
        (1 Byte alle 100ms wenn nur ein Kandidat uebrig ist).

        Wenn der User gerade eine Zelle editiert (Doppelklick + tippt),
        skippen wir den Tick komplett. Sonst wuerde der Editor alle
        100ms beim _render() weggerissen werden und der User koennte
        nichts editieren waehrend Live laeuft.
        """
        if not self._candidates:
            return
        for tbl in (self.tbl_hex, self.tbl_asm):
            if tbl.state() == QAbstractItemView.State.EditingState:
                return
        full_count = self._length
        cand_count = len(self._candidates)
        if cand_count > 0 and cand_count < full_count // 2:
            read_start = min(self._candidates)
            read_end = max(self._candidates)
            read_len = read_end - read_start + 1
            partial = True
        else:
            read_start = self._address
            read_len = self._length
            partial = False

        ok, result = self._backend.read(read_start, read_len)
        if not ok:
            self.lbl_status.setText(f"  Live FAILED: {result}  ")
            return
        chunk = bytearray(result)

        # _data Display-Buffer patchen. Vor dem Update Snapshot der
        # alten Bytes fuers Diff-Rendering - so werden bei jedem Tick
        # die seit dem letzten Tick geaenderten Werte rot dargestellt.
        self._prev_data = bytes(self._data)
        if partial:
            offset = (read_start - self._address) & 0xFFFF
            if 0 <= offset and offset + len(chunk) <= len(self._data):
                self._data[offset:offset + len(chunk)] = chunk
        else:
            self._data = chunk

        # _last_values updaten - aber als "letzter live-Stand" nur
        # fuer die Anzeige, NICHT als Vergleichsbasis fuer einen
        # impliziten Filter. Wenn der User danach manuell auf
        # "Changed" klickt, ist das der neue Basis-Snapshot.
        new_values = {(read_start + i) & 0xFFFF: chunk[i]
                        for i in range(len(chunk))}
        if partial:
            self._last_values.update(new_values)
        else:
            self._last_values = new_values

        # Render: die Zeilen sind noch dieselben (Kandidatenliste
        # unveraendert), nur die Werte werden frisch dargestellt.
        self._render()

    def _on_hex_context_menu(self, pos):
        """Rechtsklick in der HEX-Tabelle: wir bestimmen die Adresse
        der angeklickten Zelle und bieten 'Find references' an.

        pos ist relative zur viewport-Koordinate, wir uebersetzen das
        in row/col mit indexAt().
        """
        idx = self.tbl_hex.indexAt(pos)
        if not idx.isValid():
            return
        row, col = idx.row(), idx.column()
        # Nur auf Byte-Spalten reagieren - Addr und ASCII sind hilflos
        if not (self._HX_COL_FIRST_BYTE <= col <= self._HX_COL_LAST_BYTE):
            return
        # Cell-Adresse rekonstruieren: row*16 + (col - 1)
        # Bei komprimierten Zeilen ist die Adress-Role gesetzt - die
        # nutzen wir wenn vorhanden, sonst rechnen wir.
        item = self.tbl_hex.item(row, col)
        addr = None
        if item is not None:
            d = item.data(self._ROLE_ADDR)
            if d is not None:
                addr = int(d)
        if addr is None:
            byte_idx = col - self._HX_COL_FIRST_BYTE
            addr = (self._address + row * 16 + byte_idx) & 0xFFFF
        self._show_cell_context_menu(self.tbl_hex.viewport(), pos, addr)

    def _on_asm_context_menu(self, pos):
        """Rechtsklick in der ASM-Tabelle: Adress-Cell oder
        Byte-Cell? Wir nehmen jeweils die zugehoerige Adresse der
        Instruktion. B1/B2 = pc+1 / pc+2.
        """
        idx = self.tbl_asm.indexAt(pos)
        if not idx.isValid():
            return
        row, col = idx.row(), idx.column()
        addr_item = self.tbl_asm.item(row, self._AS_COL_ADDR)
        if addr_item is None:
            return
        try:
            pc = int(addr_item.text(), 16)
        except ValueError:
            return
        if col == self._AS_COL_B1:
            target = (pc + 1) & 0xFFFF
        elif col == self._AS_COL_B2:
            target = (pc + 2) & 0xFFFF
        else:
            target = pc
        self._show_cell_context_menu(self.tbl_asm.viewport(), pos, target)

    def _show_cell_context_menu(self, viewport, pos, addr):
        """Gemeinsamer Menu-Builder fuer Hex- und ASM-Cell-Rechtsklick.
        addr = die Adresse fuer die wir Aktionen anbieten."""
        menu = QMenu(self)
        act_refs = menu.addAction(
            f"Find references to ${addr:04X}...")
        act_counter = menu.addAction(
            f"Find counter ops on ${addr:04X}...")
        # Remove-Action nur einblenden wenn eine Suche aktiv ist UND
        # die Adresse aktueller Kandidat ist - sonst hat das Klicken
        # keinen Effekt.
        act_remove = None
        if (self._candidates is not None
                and addr in self._candidates):
            menu.addSeparator()
            act_remove = menu.addAction(
                f"Remove ${addr:04X} from candidates")
        chosen = menu.exec(viewport.mapToGlobal(pos))
        if chosen is act_refs:
            self._open_references_for(addr)
        elif chosen is act_counter:
            self._open_counter_ops_for(addr)
        elif act_remove is not None and chosen is act_remove:
            self._remove_candidate(addr)

    def _remove_candidate(self, addr):
        """Einen einzelnen Kandidaten manuell entfernen.

        Wird vom Rechtsklick-Menu aufgerufen. Sinnvoll besonders im
        Live-Mode wo die Liste sich nicht selber reduziert - der User
        kann so 'erkennbar falsche' Adressen (z.B. konstante ROM-
        Bytes) aussortieren.
        """
        if self._candidates is None:
            return
        if addr not in self._candidates:
            return
        self._candidates.discard(addr)
        self.lbl_cand.setText(
            f"  {len(self._candidates)} candidates "
            f"(manually removed ${addr:04X})  ")
        self._render()

    def _on_find_value_loads(self):
        """Handler fuer Toolbar-Button 'Find loads'.

        Nutzt den Wert aus self.ed_value (wie der Value-Filter), aber
        durchsucht den CODE statt der Daten. Oeffnet einen neuen
        CodePatternDialog mit den Treffern.
        """
        txt = self.ed_value.text().strip()
        if not txt:
            self.lbl_status.setText(
                "  enter a byte value first (e.g. 3 or $03)  ")
            return
        try:
            if txt.startswith('$'):
                val = int(txt[1:], 16)
            elif txt.startswith('0x') or txt.startswith('0X'):
                val = int(txt[2:], 16)
            else:
                val = int(txt)
        except ValueError:
            self.lbl_status.setText(
                f"  invalid value: {txt!r} - use decimal or $hex  ")
            return
        if not (0 <= val <= 255):
            self.lbl_status.setText(
                f"  value out of range: {val} (must be 0..255)  ")
            return
        self._open_code_pattern_dialog(mode="value_loads",
                                          value=val, target_addr=None)

    def _open_counter_ops_for(self, addr):
        """Vom Context-Menu: oeffne Counter-Pattern-Suche fuer addr."""
        self._open_code_pattern_dialog(mode="counter_ops",
                                          value=None, target_addr=addr)

    def _open_code_pattern_dialog(self, mode, value, target_addr):
        """CodePatternDialog instanziieren + an die Halte-Liste haengen.
        Backend + Mode + ggf. Wert/Adresse uebergeben - der Dialog
        liest selber den vollen Dump und macht die Pattern-Suche."""
        if self._backend is None:
            QMessageBox.information(self, "Find code pattern",
                "No memory backend available.")
            return
        dlg = CodePatternDialog(
            self, self._backend, mode=mode,
            value=value, target_addr=target_addr)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not hasattr(self, '_detached_refs'):
            self._detached_refs = []
        self._detached_refs.append(dlg)
        dlg.destroyed.connect(
            lambda _obj=None, d=dlg: (
                self._detached_refs.remove(d)
                if d in self._detached_refs else None))
        dlg.show()

    def _open_references_for(self, addr):
        """ReferencesDialog oeffnen.

        Der Dialog liest selbststaendig $0000-$FFFF vom Backend - so
        funktioniert die Analyse auch wenn der MemoryView nur einen
        kleinen Ausschnitt geladen hat. Non-modal damit der User
        weiter im MemoryView arbeiten kann.

        Wie der MemoryViewDialog selbst ist auch der ReferencesDialog
        parent-los (siehe ReferencesDialog.__init__) damit er nicht
        mitminimiert wird. Wir halten eine Liste detached Dialoge
        damit der GC sie nicht killt; destroyed-Signal entfernt sie
        wieder beim Schliessen.
        """
        if self._backend is None:
            QMessageBox.information(self, "Find references",
                "No memory backend available.")
            return
        show_ill = getattr(self, "_show_illegal", False)
        dlg = ReferencesDialog(
            self, self._backend, addr,
            show_illegal=show_ill)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not hasattr(self, '_detached_refs'):
            self._detached_refs = []
        self._detached_refs.append(dlg)
        dlg.destroyed.connect(
            lambda _obj=None, d=dlg: (
                self._detached_refs.remove(d)
                if d in self._detached_refs else None))
        dlg.show()

    def closeEvent(self, ev):
        """Live-Timer muss stoppen wenn der Dialog geschlossen wird,
        sonst feuert er weiter im Hintergrund auch nachdem das Widget
        weg ist (zwar harmlos, aber Logspam).

        Zusaetzlich: falls noch ein Inline-Editor offen ist, schliessen
        wir den hier auch - sonst meckert Qt nach dem widget-deinit
        mit der bekannten 'commitData ... does not belong to this
        view'-Warnung.
        """
        try:
            self._live_timer.stop()
        except Exception:
            pass
        # Editor sauber abbrechen wie in _render()
        try:
            from PyQt6.QtWidgets import (
                QApplication, QAbstractItemDelegate)
            for tbl in (self.tbl_hex, self.tbl_asm):
                if tbl.state() == QAbstractItemView.State.EditingState:
                    editor = QApplication.focusWidget()
                    if editor is not None:
                        delegate = tbl.itemDelegate()
                        if delegate is not None:
                            delegate.closeEditor.emit(
                                editor,
                                QAbstractItemDelegate.EndEditHint
                                  .RevertModelCache)
        except Exception:
            pass
        super().closeEvent(ev)

    # ------------------------------------------------------------------
    # View switching / Rendering
    # ------------------------------------------------------------------

    def _on_view_changed(self, _checked):
        # toggled feuert fuer beide Buttons - nur einmal rendern
        self._view_mode = "asm" if self.rb_asm.isChecked() else "hex"
        if self._view_mode == "asm":
            self._stack.setCurrentWidget(self.tbl_asm)
        else:
            self._stack.setCurrentWidget(self.tbl_hex)
        if self._data:
            self._render()

    def _render(self):
        # Wenn gerade ein Cell-Editor offen ist und wir die Tabelle
        # jetzt neu aufbauen wuerden, beschwert sich Qt mit
        # "QAbstractItemView::commitData called with an editor that
        # does not belong to this view". Der alte Item ist weg, der
        # Editor zeigt auf einen freigegebenen Speicher.
        #
        # Saubere Loesung: ueber den ItemDelegate's closeEditor-Signal
        # mit RevertModelCache-Hint - Qt wird den Editor schliessen
        # OHNE commit zu versuchen (was sonst auf das alte Item
        # zugreifen wuerde).
        from PyQt6.QtWidgets import (
            QApplication, QAbstractItemDelegate)
        for tbl in (self.tbl_hex, self.tbl_asm):
            if tbl.state() != QAbstractItemView.State.EditingState:
                continue
            # focusWidget IST das Editor-Widget (typisch QLineEdit).
            # Wir feuern das closeEditor-Signal des Delegates mit
            # Revert-Hint - dann macht Qt sauber den Cancel-Pfad und
            # entfernt das Widget aus dem View. Kein commit, keine
            # Warnung.
            editor = QApplication.focusWidget()
            if editor is not None:
                delegate = tbl.itemDelegate()
                if delegate is not None:
                    delegate.closeEditor.emit(
                        editor,
                        QAbstractItemDelegate.EndEditHint.RevertModelCache)

        # Wir setzen die Zell-Inhalte programmatisch - das feuert
        # itemChanged, was wir hier NICHT als User-Edit interpretieren
        # wollen. Daher der suppress-flag.
        self._suppress_item_changed = True
        try:
            if self._view_mode == "asm":
                self._render_asm()
            else:
                self._render_hex()
        finally:
            self._suppress_item_changed = False

    def _render_hex(self):
        data = self._data
        addr = self._address
        rows = (len(data) + 15) // 16
        self.tbl_hex.setRowCount(rows)
        for row in range(rows):
            self._render_hex_row(row, data, addr)
        # ICU64-Stil: Wenn eine Memory-Search laeuft, blende Zeilen
        # aus die keine Kandidaten-Adresse mehr enthalten. So sieht
        # man auf einen Blick wo die ueberlebenden Adressen liegen.
        # Ist _candidates None: Suche nicht aktiv, alles sichtbar.
        if self._candidates is None:
            for row in range(rows):
                self.tbl_hex.setRowHidden(row, False)
        else:
            cand = self._candidates
            for row in range(rows):
                row_start_addr = (addr + row * 16) & 0xFFFF
                # Zeile sichtbar wenn mindestens eine der 16 Adressen
                # noch Kandidat ist.
                visible = any(
                    ((row_start_addr + i) & 0xFFFF) in cand
                    for i in range(16))
                self.tbl_hex.setRowHidden(row, not visible)

    def _render_hex_row(self, row, data, addr):
        """Eine Zeile des HEX-Views aufbauen.

        Wenn alle 16 Bytes der Zeile identisch sind und die Zeile
        nicht explizit aufgeklappt wurde (`_hex_expanded_rows`):
        Run-Length-Komprimierung: Wert in Zelle 0, "x 16" in Zelle 1,
        Rest leer/disabled. Die ASCII-Spalte zeigt dann den Wert
        16 Mal. Doppelklick auf so eine Zeile expandiert sie.
        """
        row_addr = (addr + row * 16) & 0xFFFF
        # Wie viele bytes hat diese Zeile? Letzte Zeile kann <16 sein.
        row_start = row * 16
        row_end = min(row_start + 16, len(data))
        row_bytes = data[row_start:row_end]
        full_row = len(row_bytes) == 16

        # Kompression: nur wenn full row, alle gleich, und nicht
        # explizit expandiert vom User.
        compress = (full_row
                      and row not in self._hex_expanded_rows
                      and all(b == row_bytes[0] for b in row_bytes))

        # Addr-Zelle (read-only). Keine eigene Foreground-Farbe -
        # Qt's Default ist robust gegen helle/dunkle Themes.
        it_addr = QTableWidgetItem(f"{row_addr:04X}")
        it_addr.setFlags(Qt.ItemFlag.ItemIsEnabled
                          | Qt.ItemFlag.ItemIsSelectable)
        self.tbl_hex.setItem(row, self._HX_COL_ADDR, it_addr)

        if compress:
            # Erste Zelle = der Wert (editable, bekommt Adresse der
            # row_addr). Zweite Zelle = " x 16" Marker (read-only,
            # gibt dem User einen Hinweis dass die Zeile komprimiert
            # ist). Zellen 2..15 sind leer und disabled.
            b = row_bytes[0]
            txt = f"{b:02X}"
            ch = chr(b) if 0x20 <= b < 0x7F else "."
            # Wertzelle (editable, schreibt auf row_addr; ein Poke
            # hier ueberschreibt nur das erste Byte - das ist OK,
            # weil nach dem Poke die Run-Length-Annahme eh weg ist
            # und die Zeile beim Re-Render explodiert wird).
            it_val = QTableWidgetItem(txt)
            it_val.setFlags(Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable
                             | Qt.ItemFlag.ItemIsEditable)
            it_val.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_val.setData(self._ROLE_ADDR, row_addr)
            it_val.setData(self._ROLE_ORIGINAL, txt)
            # Diff-Highlight: bei einer komprimierten Zeile sind ALLE
            # 16 Bytes gleich - wenn auch nur eines davon sich seit
            # _prev_data geaendert hat, faerben wir die Wertzelle rot.
            # (Praktisch passiert das wenn die ganze Zeile vorher
            # konstant 0x00 war und jetzt konstant 0xFF ist o.ae.)
            prev_slice = self._prev_data[row_start:row_end]
            if (len(prev_slice) == len(row_bytes)
                    and bytes(prev_slice) != bytes(row_bytes)):
                it_val.setForeground(QColor(Qt.GlobalColor.red))
            self.tbl_hex.setItem(
                row, self._HX_COL_FIRST_BYTE, it_val)
            # Marker-Zelle: "x16"
            it_mark = QTableWidgetItem("\u00d7 16")    # × 16
            it_mark.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
            it_mark.setForeground(QColor(Qt.GlobalColor.darkGray))
            it_mark.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tbl_hex.setItem(
                row, self._HX_COL_FIRST_BYTE + 1, it_mark)
            # Rest leer + disabled (no flags = klick-tot)
            for col_off in range(2, 16):
                col = self._HX_COL_FIRST_BYTE + col_off
                it = QTableWidgetItem("")
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                self.tbl_hex.setItem(row, col, it)
            # ASCII-Zelle: Hinweis "X x 16"
            it_ascii = QTableWidgetItem(ch * 16)
            it_ascii.setFlags(Qt.ItemFlag.ItemIsEnabled
                                | Qt.ItemFlag.ItemIsSelectable)
            it_ascii.setForeground(QColor(Qt.GlobalColor.darkGray))
            self.tbl_hex.setItem(
                row, self._HX_COL_ASCII, it_ascii)
            return

        # Normale (nicht komprimierte) Zeile
        ascii_chars = []
        for col_off in range(16):
            col = self._HX_COL_FIRST_BYTE + col_off
            idx = row * 16 + col_off
            if idx < len(data):
                b = data[idx]
                txt = f"{b:02X}"
                ch = chr(b) if 0x20 <= b < 0x7F else "."
                ascii_chars.append(ch)
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable
                             | Qt.ItemFlag.ItemIsEditable)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cell_addr = (addr + idx) & 0xFFFF
                it.setData(self._ROLE_ADDR, cell_addr)
                it.setData(self._ROLE_ORIGINAL, txt)
                # Diff-Highlight: wenn das Byte seit dem letzten
                # Render anders ist -> rot. _prev_data ist beim ersten
                # Read leer und damit kein Match -> erster Render hat
                # keine Faerbung (gewollt).
                if (idx < len(self._prev_data)
                        and self._prev_data[idx] != b):
                    it.setForeground(QColor(Qt.GlobalColor.red))
            else:
                it = QTableWidgetItem("")
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tbl_hex.setItem(row, col, it)
        # ASCII-Zelle (read-only) - keine eigene Foreground-Farbe,
        # damit der Text auf hellen wie dunklen Themes lesbar bleibt.
        it_ascii = QTableWidgetItem("".join(ascii_chars))
        it_ascii.setFlags(Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
        self.tbl_hex.setItem(row, self._HX_COL_ASCII, it_ascii)


    def _render_asm(self):
        # Lazy import damit u64_streamer keine harte Abhaengigkeit
        # auf c64_disasm hat.
        try:
            from . import c64_disasm
        except ImportError:
            self.tbl_asm.setRowCount(1)
            self.tbl_asm.setItem(
                0, self._AS_COL_MN,
                QTableWidgetItem("(c64_disasm not available)"))
            return
        lines = c64_disasm.disassemble(
            bytes(self._data), self._address,
            show_illegal=self._show_illegal)
        self._asm_lines = lines    # behalten fuer Re-Render nach Poke
        self.tbl_asm.setRowCount(len(lines))
        for row, ln in enumerate(lines):
            # Addr-Zelle - keine eigene Foreground-Farbe, Qt's Default
            # bleibt auf hellen wie dunklen Themes lesbar.
            it_addr = QTableWidgetItem(f"{ln.pc:04X}")
            it_addr.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
            self.tbl_asm.setItem(row, self._AS_COL_ADDR, it_addr)
            # B0, B1, B2 - editierbar wenn das Byte existiert
            for byte_idx, col in enumerate(
                    (self._AS_COL_B0, self._AS_COL_B1, self._AS_COL_B2)):
                if byte_idx < len(ln.bytes):
                    b = ln.bytes[byte_idx]
                    it = QTableWidgetItem(f"{b:02X}")
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled
                                 | Qt.ItemFlag.ItemIsSelectable
                                 | Qt.ItemFlag.ItemIsEditable)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    cell_addr = (ln.pc + byte_idx) & 0xFFFF
                    it.setData(self._ROLE_ADDR, cell_addr)
                    it.setData(self._ROLE_ORIGINAL, f"{b:02X}")
                    # Diff-Highlight wie im Hex-View: Vergleich mit
                    # dem letzten _prev_data Snapshot. Offset rechnen
                    # wir aus PC - base_addr.
                    data_offset = (cell_addr - self._address) & 0xFFFF
                    if (data_offset < len(self._prev_data)
                            and self._prev_data[data_offset] != b):
                        it.setForeground(QColor(Qt.GlobalColor.red))
                else:
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.ItemFlag.NoItemFlags)
                self.tbl_asm.setItem(row, col, it)
            # Mnemonic + Operand. Editierbar AUSSER bei .byte
            # Zeilen (die haben keine Mnemonic-Form, die assembliert
            # werden koennte) und bei illegal opcodes (Assembler
            # kennt die nicht).
            mn_text = f"{ln.mnemonic} {ln.operand}".strip()
            if ln.comment:
                mn_text += f"  ; {ln.comment}"
            it_mn = QTableWidgetItem(mn_text)
            base_flags = (Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable)
            is_byte = ln.mnemonic == ".byte"
            is_illegal = ln.mnemonic.startswith("*")
            if not is_byte and not is_illegal:
                it_mn.setFlags(base_flags
                                | Qt.ItemFlag.ItemIsEditable)
                src = f"{ln.mnemonic} {ln.operand}".strip()
                it_mn.setData(self._ROLE_ADDR, ln.pc & 0xFFFF)
                it_mn.setData(self._ROLE_ORIGINAL, src)
                it_mn.setData(self._ROLE_LEN, len(ln.bytes))
            else:
                it_mn.setFlags(base_flags)
            # Farb-Akzent fuer .byte und illegal opcodes - subtil,
            # damit es auf hellen wie dunklen Themes funktioniert.
            # Normale Mnemonics bleiben Default (=Theme-Foreground).
            if is_byte:
                it_mn.setForeground(QColor(Qt.GlobalColor.darkGray))
            elif is_illegal:
                it_mn.setForeground(QColor(Qt.GlobalColor.darkMagenta))
            self.tbl_asm.setItem(row, self._AS_COL_MN, it_mn)

        # Wie im HEX-View: Zeilen ausblenden die keine Kandidaten-
        # Adresse mehr abdecken. Bei ASM kann eine Zeile 1-3 Bytes
        # decken (ln.bytes), also pruefen wir alle bytes der instr.
        if self._candidates is None:
            for r in range(len(lines)):
                self.tbl_asm.setRowHidden(r, False)
        else:
            cand = self._candidates
            for r, ln in enumerate(lines):
                # Zeile sichtbar wenn min. ein Byte der Instruction
                # noch Kandidat ist.
                visible = any(
                    ((ln.pc + i) & 0xFFFF) in cand
                    for i in range(len(ln.bytes)))
                self.tbl_asm.setRowHidden(r, not visible)

    # ------------------------------------------------------------------
    # Cell-Edit handlers (-> poke)
    # ------------------------------------------------------------------

    def _on_hex_cell_changed(self, item):
        if self._suppress_item_changed:
            return
        # Nur Byte-Spalten lassen wir durch
        col = item.column()
        if not (self._HX_COL_FIRST_BYTE <= col <= self._HX_COL_LAST_BYTE):
            return
        self._handle_cell_edit(item)

    def _on_hex_cell_double_clicked(self, row, col):
        """Bei Doppelklick auf eine komprimierte Zeile (run-length):
        Zeile aufklappen damit der User einzelne Bytes editieren kann.

        Die erste Zelle (Wert-Zelle) ist editable und triggert in dem
        Fall den normalen Edit-Pfad - das DoubleClick reicht da als
        Edit-Start. Bei den x16-Marker- und den disabled-Folge-Zellen
        kommt aber nichts - daher dieser explizite Handler.
        """
        if row in self._hex_expanded_rows:
            return  # ist eh schon expanded
        # Pruefen ob diese Zeile derzeit komprimiert dargestellt ist:
        # Marker = die x16-Zelle (col index FIRST_BYTE+1) hat Text "× 16".
        marker_item = self.tbl_hex.item(
            row, self._HX_COL_FIRST_BYTE + 1)
        if marker_item is None or "16" not in marker_item.text():
            return  # nicht komprimiert
        # Wenn der User explizit auf die Wert-Zelle (col=FIRST_BYTE)
        # doppelgeklickt hat, koennte er einfach editieren wollen -
        # in dem Fall lassen wir Qt den Editor starten. Nur bei Click
        # auf die Marker- oder Leerzellen klappen wir auf.
        if col == self._HX_COL_FIRST_BYTE:
            return
        self._hex_expanded_rows.add(row)
        # Diese eine Zeile neu rendern - reicht
        self._suppress_item_changed = True
        try:
            self._render_hex_row(row, self._data, self._address)
        finally:
            self._suppress_item_changed = False
        self.lbl_status.setText(
            f"  row {row} expanded  ")

    def _on_asm_cell_changed(self, item):
        if self._suppress_item_changed:
            return
        col = item.column()
        if col in (self._AS_COL_B0, self._AS_COL_B1, self._AS_COL_B2):
            self._handle_cell_edit(item)
        elif col == self._AS_COL_MN:
            self._handle_mnemonic_edit(item)

    def _handle_mnemonic_edit(self, item):
        """Edit-Pfad fuer die Mnemonic-Spalte im ASM-View.

        Nimmt den eingegebenen Source-Text, ruft c64_disasm.assemble_line()
        auf um ihn zu Bytes zu uebersetzen, und poked die resultierenden
        Bytes via _WriteMemWorker.

        Behandlung der Laengenaenderung: assemble_line liefert 1..3 Bytes.
        Diese werden EXAKT an der Adresse der Zeile geschrieben - was
        dahinter steht bleibt unberuehrt (auch wenn die alte Instruction
        laenger war). Der Disassembler interpretiert das Layout danach
        neu - das ist die einzige sinnvolle Semantik, weil "kuerzer
        ueberschreiben" sonst muede Padding-NOPs erfordern wuerde was
        der User in 99% der Faelle nicht will.

        Wenn die neue Instruction laenger ist und ueber das Read-Window
        hinausragen wuerde, brechen wir mit Fehler-Status ab.
        """
        new_text = (item.text() or "").strip()
        # Kommentar abschneiden (alles ab ';')
        if ';' in new_text:
            new_text = new_text.split(';', 1)[0].strip()
        original = item.data(self._ROLE_ORIGINAL) or ""
        cell_addr = item.data(self._ROLE_ADDR)
        orig_len = item.data(self._ROLE_LEN) or 1

        # Leereingabe oder unveraendert: Zeile auf Original-Form
        # zuruecksetzen (komplett neu rendern ist easier als Text
        # rekonstruieren mit Comment)
        if not new_text or new_text == original:
            self._render()
            return

        if cell_addr is None:
            self._render()
            return

        # Reassemblieren
        try:
            from . import c64_disasm
        except ImportError:
            self.lbl_status.setText(
                "  cannot assemble: c64_disasm not available  ")
            self._render()
            return
        try:
            new_bytes = c64_disasm.assemble_line(new_text, cell_addr)
        except c64_disasm.AssemblerError as e:
            self.lbl_status.setText(f"  asm error: {e}  ")
            self._render()    # alte Zeile zuruecksetzen
            return
        except Exception as e:
            self.lbl_status.setText(f"  asm crash: {e}  ")
            self._render()
            return

        if not new_bytes:
            self.lbl_status.setText(
                "  empty assembly result - ignored  ")
            self._render()
            return

        # Pruefen ob die neuen Bytes ins Read-Window passen
        end_idx = (cell_addr - self._address) + len(new_bytes)
        if end_idx > len(self._data):
            self.lbl_status.setText(
                f"  asm produces {len(new_bytes)} bytes - "
                f"reaches past read window, aborting  ")
            self._render()
            return

        if self._backend is None:
            QMessageBox.warning(self, "Poke",
                                  "No memory backend configured.")
            self._render()
            return

        if self._write_worker is not None:
            self.lbl_status.setText(
                "  another poke is in flight - try again  ")
            self._render()
            return

        data_bytes = bytes(new_bytes)
        hex_str = " ".join(f"{b:02X}" for b in data_bytes)
        len_hint = (f"(was {orig_len} byte{'s' if orig_len != 1 else ''}, "
                       f"now {len(data_bytes)})")
        self.lbl_status.setText(
            f"  asm ${cell_addr:04X}: {hex_str} {len_hint}  ")

        # Wir uebergeben item=None fuer die Mnemonic-Path, weil:
        # 1. Bei Erfolg rendern wir komplett neu (siehe _on_write_done)
        # 2. Bei Fehler ebenfalls neu rendern (Original kommt zurueck)
        # Das vereinfacht die ROLE_ORIGINAL-Wartung.
        self._write_worker = _WriteMemWorker(
            backend=self._backend,
            address=cell_addr, data=data_bytes, parent=self)
        self._write_worker.done.connect(
            lambda ok, msg, addr, data:
                self._on_mnemonic_write_done(ok, msg, addr, data))
        self._write_worker.start()

    def _on_mnemonic_write_done(self, ok, msg, address, data):
        """Speziell-Handler fuer Mnemonic-Edit-Pokes.

        Unterschied zum normalen _on_write_done: wir rendern IMMER
        komplett neu (sowohl bei Erfolg als auch bei Fehler), weil
        der Edit eine ganze Zeile betrifft und im Erfolgsfall das
        Layout shiften kann.
        """
        self._write_worker = None
        if not ok:
            self.lbl_status.setText(f"  poke FAILED: {msg}  ")
            self._render()
            return
        # Lokales Mirror updaten
        for i, b in enumerate(data):
            idx = (address - self._address + i) & 0xFFFF
            if 0 <= idx < len(self._data):
                self._data[idx] = b
        hex_str = " ".join(f"{b:02X}" for b in data)
        self.lbl_status.setText(
            f"  poked ${address:04X}: {hex_str} OK  ")
        self._render()

    def _handle_cell_edit(self, item):
        """Gemeinsamer Edit-Pfad fuer beide Views.

        Parsed das eingegebene Hex, startet einen Poke-Worker,
        und macht im _on_write_done Aufraeumarbeiten (Re-Render).
        """
        new_text = item.text().strip()
        original = item.data(self._ROLE_ORIGINAL) or ""
        cell_addr = item.data(self._ROLE_ADDR)

        # Leereingabe oder gleicher Wert: einfach zuruecksetzen
        if not new_text or new_text.upper() == original.upper():
            self._suppress_item_changed = True
            item.setText(original)
            self._suppress_item_changed = False
            return

        # Validator hat 1-2 Hex erzwungen, aber sicherheitshalber
        try:
            value = int(new_text, 16) & 0xFF
        except ValueError:
            self._revert_cell(item, original)
            self.lbl_status.setText(f"  bad input: {new_text!r}  ")
            return

        if cell_addr is None:
            # sollte nicht passieren - Cell wurde ohne Adress-Role gebaut
            self._revert_cell(item, original)
            return

        if self._backend is None:
            self._revert_cell(item, original)
            QMessageBox.warning(self, "Poke",
                                  "No memory backend configured.")
            return

        # Wenn schon ein Write laeuft: erst auf den warten lassen.
        # Wir blocken nicht, sondern verwerfen den Edit mit Hinweis -
        # der User kann den naechsten manuell anstossen.
        if self._write_worker is not None:
            self._revert_cell(item, original)
            self.lbl_status.setText(
                "  another poke is in flight - try again  ")
            return

        # Cell-Anzeige direkt auf "2-stelliges Hex" normalisieren
        # (User darf 'a' eingeben, wir zeigen 'A')
        norm = f"{value:02X}"
        if item.text() != norm:
            self._suppress_item_changed = True
            item.setText(norm)
            self._suppress_item_changed = False

        self.lbl_status.setText(
            f"  poking ${cell_addr:04X} <- ${value:02X}...  ")

        self._write_worker = _WriteMemWorker(
            backend=self._backend,
            address=cell_addr, data=bytes([value]), parent=self)
        # Item-Referenz im lambda festhalten, damit wir bei Fehler
        # zuruecksetzen koennen
        self._write_worker.done.connect(
            lambda ok, msg, addr, data, it=item, orig=original:
                self._on_write_done(ok, msg, addr, data, it, orig))
        self._write_worker.start()

    def _on_write_done(self, ok, msg, address, data, item, original):
        self._write_worker = None
        if not ok:
            # Rollback der Zelle + Fehler-Status
            self._revert_cell(item, original)
            self.lbl_status.setText(f"  poke FAILED: {msg}  ")
            return
        # Erfolg: lokales Mirror updaten (potentiell mehrere Bytes
        # bei Mnemonic-Edit)
        for i, b in enumerate(data):
            idx = (address - self._address + i) & 0xFFFF
            if 0 <= idx < len(self._data):
                self._data[idx] = b
        # Bei Single-Byte-Edit (Hex-Zelle) auch das ROLE_ORIGINAL
        # der Cell aktualisieren, damit ein weiterer Edit den
        # neuen Wert als "Original" sieht
        if len(data) == 1 and item is not None:
            self._suppress_item_changed = True
            item.setData(self._ROLE_ORIGINAL, f"{data[0]:02X}")
            self._suppress_item_changed = False
        if len(data) == 1:
            self.lbl_status.setText(
                f"  poked ${address:04X} = ${data[0]:02X} OK  ")
        else:
            hex_str = " ".join(f"{b:02X}" for b in data)
            self.lbl_status.setText(
                f"  poked ${address:04X}: {hex_str} OK  ")

        # Im HEX-View: ASCII-Spalte der Zeile mitupdaten.
        # Im ASM-View: komplett neu disassemblieren, weil die Byte-
        # Aenderung die Instruction-Laenge / das Folge-Layout aendern
        # kann (z.B. 1-Byte BRK ueberlappt jetzt mit 3-Byte JMP).
        if self._view_mode == "hex":
            # Bei Multi-Byte-Edits koennen mehrere Zeilen betroffen
            # sein; einfacher komplett neu rendern.
            if len(data) > 1:
                self._render()
            elif item is not None:
                self._update_hex_ascii_for_row(item.row())
        else:
            self._render()

    def _revert_cell(self, item, original):
        self._suppress_item_changed = True
        try:
            item.setText(original)
        finally:
            self._suppress_item_changed = False

    def _update_hex_ascii_for_row(self, row):
        """Rendere die HEX-Zeile nach einem Single-Byte-Poke neu.

        Frueher wurde nur die ASCII-Spalte upgedated. Mit Run-Length-
        Compression muss aber die ganze Zeile neu gebaut werden, weil
        ein Poke aus einer "alle gleich"-Zeile eine "gemischte"
        machen kann (Auto-Expand) und umgekehrt. Wir machen ein
        komplettes row-rebuild - das ist nur eine Zeile, also billig.

        Wenn das User-Edit die Zeile von komprimiert auf gemischt
        gebracht hat, entfernen wir auch den expanded-Flag falls
        gesetzt - die Zeile expandiert sich jetzt natuerlich.
        """
        addr = self._address
        row_start = row * 16
        row_end = min(row_start + 16, len(self._data))
        row_bytes = self._data[row_start:row_end]
        # Wenn die Zeile nicht mehr uniform ist, brauchen wir den
        # expanded-Flag nicht (sie expandiert sich von alleine).
        if len(row_bytes) == 16 and not all(
                b == row_bytes[0] for b in row_bytes):
            self._hex_expanded_rows.discard(row)
        self._suppress_item_changed = True
        try:
            self._render_hex_row(row, self._data, addr)
        finally:
            self._suppress_item_changed = False

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save(self):
        """Save dump. Wir bieten ASM, Hexdump und Raw-Binary an -
        unabhaengig davon ob gerade im ASM- oder HEX-View. Das
        Default-Format/Suffix ist passend zum aktuellen View, aber
        der User kann im File-Dialog frei waehlen.

        Format-Erkennung: Endung .asm/.bin/.txt entscheidet, ansonsten
        der gewaehlte Filter. .txt = Hexdump (klassisches Verhalten).
        """
        if not self._data:
            return
        addr_str = f"{self._address:04X}"
        # Default-Suffix passt zum aktuellen View
        if self._view_mode == "asm":
            suggested = f"mem_{addr_str}.asm"
        else:
            suggested = f"mem_{addr_str}.txt"
        # Filter: alle drei Formate gleichzeitig anbieten. Reihenfolge
        # so dass der erste Filter zum Default-Suffix passt - der
        # Qt-Dialog markiert den ersten als aktiv.
        if self._view_mode == "asm":
            filt = ("ASM listing (*.asm);;"
                      "Hex dump (*.txt);;"
                      "Raw binary (*.bin);;"
                      "All files (*)")
        else:
            filt = ("Hex dump (*.txt);;"
                      "ASM listing (*.asm);;"
                      "Raw binary (*.bin);;"
                      "All files (*)")
        path, chosen_filt = QFileDialog.getSaveFileName(
            self, "Save memory dump", suggested, filt)
        if not path:
            return

        # Format anhand Extension + chosen filter bestimmen.
        # Extension hat Vorrang, weil der User bewusst .bin tippen
        # koennte selbst wenn der Filter auf ASM steht.
        low = path.lower()
        if low.endswith(".bin"):
            fmt = "bin"
        elif low.endswith(".asm"):
            fmt = "asm"
        elif low.endswith(".txt"):
            fmt = "hex"
        else:
            # Keine eindeutige Endung -> aus dem Filter ableiten
            cf = chosen_filt.lower()
            if "binary" in cf:
                fmt = "bin"
            elif "asm" in cf:
                fmt = "asm"
            else:
                fmt = "hex"

        try:
            if fmt == "bin":
                with open(path, "wb") as f:
                    f.write(bytes(self._data))
            elif fmt == "asm":
                # Disassembly aus self._data regenerieren - nicht aus
                # den Table-Zellen, damit ungecommittete Edits keine
                # Auswirkung haben.
                try:
                    from . import c64_disasm
                except ImportError:
                    QMessageBox.warning(
                        self, "Save failed",
                        "c64_disasm module not available - "
                        "cannot save as ASM.")
                    return
                lines = c64_disasm.disassemble(
                    bytes(self._data), self._address,
                    show_illegal=self._show_illegal)
                text = format_disasm_lines(lines)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.write("\n")
            else:  # hex
                text = format_hexdump(
                    bytes(self._data), self._address)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.write("\n")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.lbl_status.setText(
            f"  saved to {Path(path).name} ({fmt})  ")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, ev):
        for w in (self._read_worker, self._write_worker):
            if w is not None:
                try:
                    w.wait(2000)
                except Exception:
                    pass
        self._read_worker = None
        self._write_worker = None
        super().closeEvent(ev)


class U64ConfigDialog(QDialog):
    """Editable settings for the Ultimate 64 connection.

    All four fields are persisted in the main Quopus config under
    the keys u64_host / u64_video_port / u64_audio_port /
    u64_telnet_port. Defaults match the firmware:
      video  UDP 11000
      audio  UDP 11001
      telnet TCP 23

    A 'Restore defaults' button resets the ports to those values
    in case the user has experimented with custom firmware ports
    and wants to get back to a known state.
    """

    def __init__(self, host: str, video_port: int,
                   audio_port: int, telnet_port: int,
                   http_port: int = PORT_HTTP,
                   password: str = "",
                   video_only: bool = False,
                   always_on_top: bool = False,
                   screenshot_dir: str = "",
                   parent=None):
        super().__init__(parent)
        self.setWindowTitle("U64 Config")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(520, 0)

        from PyQt6.QtWidgets import (
            QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
            QCheckBox, QWidget,
        )

        layout = QVBoxLayout(self)
        title = QLabel(" Ultimate 64 connection ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        # ----- Device slot selector -----
        # We support up to MAX_DEVICES (= 3) U64 devices. The
        # user picks which slot they're editing via this combo;
        # the form fields below show / save into that slot.
        # The currently-active device (the one actions like
        # "Send to U64" use by default) is marked with a star.
        from .u64_devices import (
            MAX_DEVICES, get_devices, get_active_index,
            device_display_name,
        )
        # Read existing devices from parent's config dict so we
        # can populate the slot dropdown. If the parent doesn't
        # have a config (some tests construct the dialog
        # directly with host/port args), fall back to the single
        # device built from the constructor args.
        self._mw_config = None
        mw = parent
        while mw is not None:
            if hasattr(mw, "config") and isinstance(
                    mw.config, dict):
                self._mw_config = mw.config
                break
            mw = getattr(mw, "parent", lambda: None)() \
                if callable(getattr(mw, "parent", None)) \
                else None
        if self._mw_config is not None:
            self._devices = list(get_devices(self._mw_config))
        else:
            self._devices = []
        # If no devices saved yet, seed slot 0 with the values
        # passed to the constructor (so the legacy single-device
        # entry point still produces a working form).
        if not self._devices:
            self._devices = [{
                "name": "",
                "host": host,
                "video_port": int(video_port),
                "audio_port": int(audio_port),
                "telnet_port": int(telnet_port),
                "http_port": int(http_port),
                "password": password,
                "video_only": video_only,
                "always_on_top": always_on_top,
            }]
        # Pad to MAX_DEVICES with empty slot placeholders so the
        # user can always switch to slot 2/3 and start typing.
        while len(self._devices) < MAX_DEVICES:
            self._devices.append({
                "name": "",
                "host": "",
                "video_port": int(video_port),
                "audio_port": int(audio_port),
                "telnet_port": int(telnet_port),
                "http_port": int(http_port),
                "password": "",
                "video_only": False,
                "always_on_top": False,
            })
        self._current_slot = (
            get_active_index(self._mw_config)
            if self._mw_config is not None else 0)
        if (self._current_slot < 0
                or self._current_slot >= MAX_DEVICES):
            self._current_slot = 0

        slot_row = QHBoxLayout()
        slot_row.setContentsMargins(10, 8, 10, 0)
        slot_row.setSpacing(6)
        slot_row.addWidget(QLabel("Device slot:"))
        from PyQt6.QtWidgets import QComboBox as _QCombo
        self.cmb_slot = _QCombo()
        self.cmb_slot.setMinimumWidth(220)
        self._refresh_slot_combo()
        self.cmb_slot.currentIndexChanged.connect(
            self._on_slot_changed)
        slot_row.addWidget(self.cmb_slot, 1)
        # Star button - sets THIS slot as the active default.
        # That's what gets used when actions like "Send to U64"
        # don't ask the user which device to use.
        self.btn_set_active = QPushButton("Make active")
        self.btn_set_active.setToolTip(
            "Mark this slot as the default U64. Actions that\n"
            "don't show a device picker (or where the user\n"
            "doesn't pick one) will use this device. The active\n"
            "slot is marked with a star in the dropdown.")
        self.btn_set_active.setMinimumWidth(scaled_px(110))
        self.btn_set_active.clicked.connect(
            self._on_set_active_slot)
        slot_row.addWidget(self.btn_set_active)
        layout.addLayout(slot_row)
        # Slim help label below the slot row
        hint = QLabel(
            "Up to 3 Ultimate-64 devices. The active slot is\n"
            "used by default for Send-to-U64 / Streamer /\n"
            "Asm64 'Run on U64'. Actions that target a U64 will\n"
            "ask which device to use when more than one is\n"
            "configured.")
        hint.setStyleSheet(
            f"color: #555; font-size: {scaled_font_px(10)}px; "
            "margin-left: 12px;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setContentsMargins(10, 10, 10, 10)

        # Name field - lets the user label devices ("Living
        # room", "Workshop", "Bookshelf") so the picker dialogs
        # show something meaningful rather than just IP addresses.
        # Empty name falls back to "U64 #N - <host>" in display.
        self.ed_name = QLineEdit(
            self._devices[self._current_slot].get("name", ""))
        self.ed_name.setPlaceholderText(
            "Optional friendly name, e.g. 'Living room'")
        form.addRow("Device name:", self.ed_name)

        self.ed_host = QLineEdit(host)
        self.ed_host.setPlaceholderText("e.g. 192.168.1.42 or u64.local")
        # Row: host edit + Discover button to auto-find U64 on LAN.
        host_row = QHBoxLayout()
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(4)
        host_row.addWidget(self.ed_host, 1)
        btn_discover = QPushButton("Discover...")
        btn_discover.setMinimumWidth(scaled_px(90))
        btn_discover.setToolTip(
            "Scan the local network for Ultimate devices.\n"
            "Requires Ultimate Ident service to be enabled\n"
            "on the device (Ultimate menu -> Network Settings).")
        btn_discover.clicked.connect(self._on_discover)
        host_row.addWidget(btn_discover)
        btn_test = QPushButton("Test")
        btn_test.setMinimumWidth(scaled_px(50))
        btn_test.setToolTip(
            "Test the connection by GETting /v1/info from the\n"
            "configured host. Shows the device firmware version\n"
            "and hostname if successful.")
        btn_test.clicked.connect(self._on_test_connection)
        host_row.addWidget(btn_test)
        host_widget = QWidget()
        host_widget.setLayout(host_row)
        form.addRow("Host / IP:", host_widget)

        # Port spinboxes - 1..65535 each. We don't enforce that
        # video and audio differ (you'd just get one bind error if
        # the user collides them) but ports get auto-clamped to
        # valid TCP/UDP range.
        def _make_port_spin(initial):
            sp = QSpinBox()
            sp.setRange(1, 65535)
            sp.setValue(initial)
            return sp

        self.sp_video = _make_port_spin(video_port)
        form.addRow("Video UDP port:", self.sp_video)
        self.sp_audio = _make_port_spin(audio_port)
        form.addRow("Audio UDP port:", self.sp_audio)
        self.sp_telnet = _make_port_spin(telnet_port)
        form.addRow("Telnet TCP port:", self.sp_telnet)
        # HTTP port for the REST API (run_prg, mount D64, etc.)
        # Ultimate firmware listens on TCP 80 by default but can be
        # remapped under the network settings.
        self.sp_http = _make_port_spin(http_port)
        form.addRow("HTTP TCP port:", self.sp_http)
        # Optional network password (firmware 3.12+). Empty = none.
        self.ed_password = QLineEdit(password)
        self.ed_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_password.setPlaceholderText("(only if set on the U64)")
        form.addRow("Network password:", self.ed_password)

        # Stream behaviour toggles. video_only suppresses both the
        # audio receiver thread AND the audio:start REST call - so
        # the U64 isn't asked to send audio packets at all and we
        # don't bind UDP 11001. Useful when running multiple
        # streamers on the same host (only one can have the audio
        # device anyway) or when the user just wants picture.
        self.chk_video_only = QCheckBox(
            "Video only (no audio stream)")
        self.chk_video_only.setChecked(video_only)
        self.chk_video_only.setToolTip(
            "Skip audio entirely - don't bind audio UDP port and "
            "don't request the audio stream from the U64.")
        form.addRow("", self.chk_video_only)

        # Always-on-top: sets Qt.WindowStaysOnTopHint on the
        # streamer window so it floats over other apps.
        self.chk_always_on_top = QCheckBox(
            "Keep streamer window always on top")
        self.chk_always_on_top.setChecked(always_on_top)
        self.chk_always_on_top.setToolTip(
            "Streamer window stays above other windows even when "
            "they get focus. Useful while debugging on the C64 with "
            "Quopus or an editor in the foreground.")
        form.addRow("", self.chk_always_on_top)

        # Screenshot output folder. Empty -> default (<project>/screenshots/).
        self.ed_screenshot_dir = QLineEdit(screenshot_dir)
        self.ed_screenshot_dir.setPlaceholderText(
            "(leave empty for <quopus>/screenshots/)")
        scr_row = QHBoxLayout()
        scr_row.setContentsMargins(0, 0, 0, 0)
        scr_row.setSpacing(4)
        scr_row.addWidget(self.ed_screenshot_dir, 1)
        btn_browse_scr = QPushButton("Browse...")
        btn_browse_scr.setMinimumWidth(scaled_px(90))
        btn_browse_scr.setToolTip(
            "Where the streamer saves PNG screenshots from the\n"
            "Snap button. Empty means use <quopus_project>/screenshots/\n"
            "next to quopus.py. Set to an absolute path to override.")
        btn_browse_scr.clicked.connect(self._on_browse_screenshot_dir)
        scr_row.addWidget(btn_browse_scr)
        scr_widget = QWidget()
        scr_widget.setLayout(scr_row)
        form.addRow("Screenshot folder:", scr_widget)

        layout.addLayout(form)

        # Now that all widgets exist, populate them from the
        # currently-selected slot. This overrides the constructor
        # args' values for any slot where the user has saved
        # something different. For new installs (no devices in
        # config yet) the slot 0 contents match the constructor
        # args, so nothing visible changes.
        self._load_slot_into_form(self._current_slot)

        # Buttons
        bar = QHBoxLayout()
        bar.setSpacing(2)
        bar.setContentsMargins(10, 0, 10, 10)
        btn_defaults = QPushButton("Restore defaults")
        btn_defaults.setStyleSheet(button_qss("orange"))
        btn_defaults.clicked.connect(self._restore_defaults)
        bar.addWidget(btn_defaults)
        bar.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(button_qss("red"))
        btn_cancel.setMinimumWidth(scaled_px(80))
        btn_cancel.clicked.connect(self.reject)
        bar.addWidget(btn_cancel)
        btn_ok = QPushButton("OK")
        btn_ok.setStyleSheet(button_qss("green"))
        btn_ok.setMinimumWidth(scaled_px(80))
        btn_ok.clicked.connect(self.accept)
        bar.addWidget(btn_ok)
        layout.addLayout(bar)

    def _restore_defaults(self):
        """Reset the port fields to the firmware defaults; keep
        whatever IP and password the user typed in - those they
        pretty much always need to set themselves."""
        self.sp_video.setValue(PORT_VIDEO)
        self.sp_audio.setValue(PORT_AUDIO)
        self.sp_telnet.setValue(PORT_TELNET)
        self.sp_http.setValue(PORT_HTTP)

    # ------------------------------------------------------------
    # Multi-device slot management
    # ------------------------------------------------------------

    def _refresh_slot_combo(self):
        """Rebuild the slot dropdown labels. Active slot gets a
        star prefix. Empty slots show '(empty)' so the user
        knows they can move into them."""
        from .u64_devices import (
            MAX_DEVICES, get_active_index, device_display_name,
        )
        active_idx = (
            get_active_index(self._mw_config)
            if self._mw_config is not None else 0)
        self.cmb_slot.blockSignals(True)
        self.cmb_slot.clear()
        for i in range(MAX_DEVICES):
            d = self._devices[i] if i < len(self._devices) else {}
            host = (d.get("host") or "").strip()
            if host:
                label = device_display_name(d, i)
            else:
                label = f"Slot #{i + 1} (empty)"
            if i == active_idx:
                label = "\u2605 " + label
            self.cmb_slot.addItem(label)
        self.cmb_slot.setCurrentIndex(self._current_slot)
        self.cmb_slot.blockSignals(False)

    def _load_slot_into_form(self, slot_idx: int):
        """Populate the form fields with the contents of slot
        `slot_idx`. Called whenever the slot dropdown changes,
        and once at the end of __init__ to set up the initial
        state. Doesn't touch self._current_slot - the caller
        owns that."""
        if slot_idx < 0 or slot_idx >= len(self._devices):
            return
        d = self._devices[slot_idx]
        self.ed_name.setText(d.get("name", "") or "")
        self.ed_host.setText(d.get("host", "") or "")
        self.sp_video.setValue(int(
            d.get("video_port", PORT_VIDEO)))
        self.sp_audio.setValue(int(
            d.get("audio_port", PORT_AUDIO)))
        self.sp_telnet.setValue(int(
            d.get("telnet_port", PORT_TELNET)))
        self.sp_http.setValue(int(
            d.get("http_port", PORT_HTTP)))
        self.ed_password.setText(d.get("password", "") or "")
        self.chk_video_only.setChecked(bool(
            d.get("video_only", False)))
        self.chk_always_on_top.setChecked(bool(
            d.get("always_on_top", False)))
        # Screenshot dir is global (shared across devices) -
        # it's the destination folder for snapshots, no need to
        # split per-device. Leave it untouched.

    def _capture_form_into_slot(self, slot_idx: int):
        """Take the current form field values and stash them in
        slot `slot_idx`. Called before switching to a different
        slot (so we don't lose edits) and at OK-time (so values()
        sees the latest entry)."""
        if slot_idx < 0 or slot_idx >= len(self._devices):
            return
        self._devices[slot_idx] = {
            "name":          self.ed_name.text().strip(),
            "host":          self.ed_host.text().strip(),
            "video_port":    int(self.sp_video.value()),
            "audio_port":    int(self.sp_audio.value()),
            "telnet_port":   int(self.sp_telnet.value()),
            "http_port":     int(self.sp_http.value()),
            "password":      self.ed_password.text(),
            "video_only":    bool(
                self.chk_video_only.isChecked()),
            "always_on_top": bool(
                self.chk_always_on_top.isChecked()),
        }

    def _on_slot_changed(self, new_idx: int):
        """User picked a different slot from the dropdown. Save
        what's currently on screen into the OLD slot, then load
        the new one into the form. Without the capture step the
        user would lose in-progress edits every time they
        toggled the dropdown."""
        self._capture_form_into_slot(self._current_slot)
        self._current_slot = int(new_idx)
        self._load_slot_into_form(self._current_slot)

    def _on_set_active_slot(self):
        """Make the currently-displayed slot the default device.
        Updates the slot dropdown's star marker and (if accepted)
        writes through to config when the dialog closes."""
        self._capture_form_into_slot(self._current_slot)
        # Only meaningful if this slot actually has a host - an
        # empty slot can't be the active device.
        if not (self._devices[self._current_slot].get(
                "host") or "").strip():
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Make active",
                "This slot has no host/IP set. Enter the IP "
                "address first, then 'Make active'.")
            return
        # Update the pending active index. Actual persistence
        # happens in values() / the dialog accept handler.
        self._pending_active = self._current_slot
        self._refresh_slot_combo()

    def values(self):
        """Return the current dialog values as a dict that maps
        cleanly into the config keys we persist.

        Returns two layers:
          - u64_devices / u64_active_device:  the new multi-
            device list with the user's edits applied
          - u64_host / u64_video_port / etc:  legacy keys
            mirrored from the active device for older code that
            still reads them directly

        Save callers that touch config can apply this dict
        wholesale via config.update(values()) and call
        sync_legacy_keys() to be doubly sure.
        """
        # Make sure the slot the user was editing when they hit
        # OK is captured into the device list - they might have
        # changed fields without clicking elsewhere.
        self._capture_form_into_slot(self._current_slot)

        # If a Make-active click happened, use that as the new
        # default. Otherwise, keep whichever active slot already
        # existed (or 0 for fresh installs).
        from .u64_devices import get_active_index
        active = getattr(self, "_pending_active", None)
        if active is None:
            active = (
                get_active_index(self._mw_config)
                if self._mw_config is not None else 0)
        # Clamp to a slot that actually has a host - empty slots
        # shouldn't be the default.
        if not (self._devices[active].get("host") or "").strip():
            # Fall back to the first populated slot
            for i, d in enumerate(self._devices):
                if (d.get("host") or "").strip():
                    active = i
                    break
            else:
                active = 0

        # Mirror the active device into the legacy single-device
        # keys so the rest of the codebase keeps working.
        legacy = self._devices[active]

        return {
            'u64_devices':        list(self._devices),
            'u64_active_device':  int(active),
            'u64_host':           legacy.get('host', ''),
            'u64_video_port':     int(legacy.get(
                'video_port', PORT_VIDEO)),
            'u64_audio_port':     int(legacy.get(
                'audio_port', PORT_AUDIO)),
            'u64_telnet_port':    int(legacy.get(
                'telnet_port', PORT_TELNET)),
            'u64_http_port':      int(legacy.get(
                'http_port', PORT_HTTP)),
            'u64_password':       legacy.get('password', ''),
            'u64_video_only':     bool(legacy.get(
                'video_only', False)),
            'u64_always_on_top':  bool(legacy.get(
                'always_on_top', False)),
            'u64_screenshot_dir': self.ed_screenshot_dir.text().strip(),
        }

    def _on_browse_screenshot_dir(self):
        """Pick a directory for snapshots. We pre-fill the picker
        with whatever's currently in the field; if empty, start at
        the project root so the user sees the existing screenshots/
        subdir if any."""
        from PyQt6.QtWidgets import QFileDialog
        current = self.ed_screenshot_dir.text().strip()
        if not current:
            try:
                from .config import SCRIPT_DIR
                current = str(SCRIPT_DIR)
            except Exception:
                current = ""
        path = QFileDialog.getExistingDirectory(
            self, "Screenshot folder", current)
        if path:
            self.ed_screenshot_dir.setText(path)

    def _on_discover(self):
        """Run u64_discover() and pop a picker dialog showing every
        device that responded. User picks one -> we copy its IP into
        the ed_host field."""
        from PyQt6.QtWidgets import (
            QDialog, QListWidget, QListWidgetItem, QDialogButtonBox,
            QVBoxLayout as QVBL, QLabel as QLBL, QApplication,
        )
        # Show "scanning..." overlay briefly while discover() runs.
        # u64_discover() is synchronous with ~2s timeout, so just
        # disable the calling button - kept simple.
        self.setEnabled(False)
        QApplication.processEvents()
        try:
            found = u64_discover(timeout=2.0)
        finally:
            self.setEnabled(True)

        if not found:
            QMessageBox.information(
                self, "Discover U64",
                "No Ultimate devices found on the network.\n\n"
                "Make sure 'Ultimate Ident' is enabled in:\n"
                "Ultimate menu -> Network Settings\n\n"
                "(Some firmware versions require explicit\n"
                "broadcast support too.)")
            return

        # Picker dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Discovered Ultimate devices")
        dlg.resize(420, 260)
        v = QVBL(dlg)
        v.addWidget(QLBL(f"Found {len(found)} device(s):"))
        lst = QListWidget()
        for ip, host, prod, fw in found:
            item = QListWidgetItem(
                f"{ip}    {host}    {prod} fw {fw}")
            item.setData(0x0100, ip)   # Qt.UserRole
            lst.addItem(item)
        if lst.count() > 0:
            lst.setCurrentRow(0)
        v.addWidget(lst, 1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        # Double-click also accepts
        lst.itemDoubleClicked.connect(lambda _i: dlg.accept())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            item = lst.currentItem()
            if item is not None:
                ip = item.data(0x0100)
                if ip:
                    self.ed_host.setText(str(ip))

    def _on_test_connection(self):
        """GET /v1/info from the current host - show device info on
        success, error message on failure. Quick sanity-check before
        accepting the config."""
        from PyQt6.QtWidgets import QApplication
        host = self.ed_host.text().strip()
        if not host:
            QMessageBox.information(
                self, "Test connection",
                "Enter a host/IP first.")
            return
        port = int(self.sp_http.value())
        password = self.ed_password.text()
        self.setEnabled(False)
        QApplication.processEvents()
        try:
            ok, info = u64_info(host, password=password,
                                  port=port, timeout=3.0)
        finally:
            self.setEnabled(True)
        if not ok:
            QMessageBox.warning(self, "Test connection",
                f"Failed to reach {host}:{port}\n\n{info}")
            return
        # Show product/firmware/hostname
        product = info.get('product', '?')
        firmware = info.get('firmware_version', '?')
        fpga = info.get('fpga_version', '?')
        hostname = info.get('hostname', '?')
        unique_id = info.get('unique_id', '?')
        core = info.get('core_version', '')
        core_line = f"\n  Core version:      {core}" if core else ""
        QMessageBox.information(self, "Test connection - OK",
            f"Connected to {host}:{port}\n\n"
            f"  Product:           {product}\n"
            f"  Hostname:          {hostname}\n"
            f"  Firmware version:  {firmware}\n"
            f"  FPGA version:      {fpga}{core_line}\n"
            f"  Unique ID:         {unique_id}")


# ---------------------------------------------------------------------
# Drive Mount dialog
# ---------------------------------------------------------------------


class U64MountDialog(QDialog):
    """Mount a disk image on a specific U64 drive with chosen mode.

    Lets the user:
    - Pick a local D64/D71/D81/G64/G71/G81 file
    - Choose target drive (A or B)
    - Choose mount mode (readonly / readwrite / unlinked)
    - Optional: reset machine after mount so the disk is detected
    """

    def __init__(self, host, http_port, password, parent=None,
                   initial_path=""):
        super().__init__(parent)
        self.setWindowTitle("Mount disk image on U64")
        self.resize(520, 0)
        self._host = host
        self._http_port = http_port
        self._password = password

        from PyQt6.QtWidgets import (
            QFormLayout, QLineEdit, QComboBox, QPushButton,
            QHBoxLayout, QDialogButtonBox, QCheckBox, QLabel,
            QWidget,
        )

        layout = QVBoxLayout(self)
        title = QLabel(" Mount disk image ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        form = QFormLayout()
        form.setContentsMargins(10, 10, 10, 10)

        # File picker row
        self.ed_file = QLineEdit(initial_path)
        self.ed_file.setPlaceholderText(
            "Path to D64/D71/D81/G64/G71/G81 file")
        btn_browse = QPushButton("Browse...")
        btn_browse.setMinimumWidth(scaled_px(80))
        btn_browse.clicked.connect(self._on_browse)
        file_row = QHBoxLayout()
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(4)
        file_row.addWidget(self.ed_file, 1)
        file_row.addWidget(btn_browse)
        file_widget = QWidget()
        file_widget.setLayout(file_row)
        form.addRow("Disk image:", file_widget)

        # Drive selector
        self.cmb_drive = QComboBox()
        self.cmb_drive.addItem("Drive A", "a")
        self.cmb_drive.addItem("Drive B", "b")
        form.addRow("Target drive:", self.cmb_drive)

        # Mode selector
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Read-only (write protected)",
                                "readonly")
        self.cmb_mode.addItem("Read-write (writes persist to file)",
                                "readwrite")
        self.cmb_mode.addItem(
            "Unlinked (writable in RAM, file unchanged)",
            "unlinked")
        form.addRow("Mode:", self.cmb_mode)

        # Reset after mount
        self.chk_reset = QCheckBox(
            "Reset C64 after mount (so it detects the new disk)")
        self.chk_reset.setChecked(True)
        form.addRow("", self.chk_reset)

        layout.addLayout(form)

        # Status line for showing what's currently mounted on the
        # selected drive (read on demand from /v1/drives).
        self.lbl_current = QLabel(
            "  current: (click 'Show current' to query)  ")
        self.lbl_current.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_current)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(10, 0, 10, 10)
        btn_show = QPushButton("Show current")
        btn_show.setMinimumWidth(scaled_px(110))
        btn_show.setToolTip(
            "Query the U64 for the disk currently mounted on the\n"
            "selected drive (GET /v1/drives).")
        btn_show.clicked.connect(self._on_show_current)
        btn_row.addWidget(btn_show)
        btn_unmount = QPushButton("Unmount drive")
        btn_unmount.setMinimumWidth(scaled_px(110))
        btn_unmount.setStyleSheet(button_qss("orange"))
        btn_unmount.setToolTip(
            "Remove the currently mounted disk from the selected\n"
            "drive (PUT /v1/drives/<drive>:remove).")
        btn_unmount.clicked.connect(self._on_unmount)
        btn_row.addWidget(btn_unmount)
        btn_drive_reset = QPushButton("Reset drive")
        btn_drive_reset.setMinimumWidth(scaled_px(90))
        btn_drive_reset.setStyleSheet(button_qss("blue"))
        btn_drive_reset.clicked.connect(self._on_drive_reset)
        btn_row.addWidget(btn_drive_reset)
        btn_row.addStretch(1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Mount")
        bb.accepted.connect(self._on_mount)
        bb.rejected.connect(self.reject)
        btn_row.addWidget(bb)
        layout.addLayout(btn_row)

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick a disk image",
            "", "Disk images (*.d64 *.d71 *.d81 *.g64 *.g71 *.g81);;"
                "All files (*)")
        if path:
            self.ed_file.setText(path)

    def _on_show_current(self):
        if not self._host:
            return
        ok, data = u64_get_drives(self._host, password=self._password,
                                     port=self._http_port)
        if not ok:
            self.lbl_current.setText(f"  error: {data}  ")
            return
        # Find the selected drive in the response
        target = self.cmb_drive.currentData()
        drives = data.get("drives", [])
        for d in drives:
            if target in d:
                info = d[target]
                enabled = info.get("enabled", False)
                drive_type = info.get("type", "?")
                bus_id = info.get("bus_id", "?")
                image_file = info.get("image_file", "")
                msg = (f"  Drive {target.upper()}: "
                       f"{'on' if enabled else 'OFF'}, "
                       f"type={drive_type}, bus={bus_id}, "
                       f"mounted: {image_file or '(none)'}  ")
                self.lbl_current.setText(msg)
                return
        self.lbl_current.setText(
            f"  Drive {target.upper()}: not present  ")

    def _on_unmount(self):
        if not self._host:
            return
        target = self.cmb_drive.currentData()
        ok, msg = u64_drive_remove(self._host, drive=target,
                                       password=self._password,
                                       port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Unmount",
                f"Failed:\n{msg}")
        else:
            self.lbl_current.setText(
                f"  Drive {target.upper()}: unmounted  ")

    def _on_drive_reset(self):
        if not self._host:
            return
        target = self.cmb_drive.currentData()
        ok, msg = u64_drive_reset(self._host, drive=target,
                                       password=self._password,
                                       port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Reset drive",
                f"Failed:\n{msg}")

    def _on_mount(self):
        import os
        path = self.ed_file.text().strip()
        if not path:
            QMessageBox.information(self, "Mount",
                "Pick a disk image first.")
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Mount",
                f"File not found:\n{path}")
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Mount",
                f"Couldn't read file:\n{e}")
            return
        target = self.cmb_drive.currentData()
        mode = self.cmb_mode.currentData()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        # Disable buttons during the POST
        self.setEnabled(False)
        QApplication.processEvents()
        try:
            ok, msg = u64_mount_disk(
                self._host, data,
                drive=target, mode=mode,
                disk_type=ext,
                password=self._password, port=self._http_port)
        finally:
            self.setEnabled(True)
        if not ok:
            QMessageBox.warning(self, "Mount", f"Failed:\n{msg}")
            return
        if self.chk_reset.isChecked():
            u64_reset(self._host, password=self._password,
                        port=self._http_port)
        self.accept()


# ---------------------------------------------------------------------
# Drive Status dialog (read-only view of /v1/drives)
# ---------------------------------------------------------------------


class U64DriveStatusDialog(QDialog):
    """Display the U64's drive status from GET /v1/drives.
    Auto-refreshes every 2 seconds while open."""

    def __init__(self, host, http_port, password, parent=None):
        super().__init__(parent)
        self.setWindowTitle("U64 Drive Status")
        self.resize(620, 400)
        self._host = host
        self._http_port = http_port
        self._password = password

        from PyQt6.QtWidgets import (
            QTreeWidget, QTreeWidgetItem, QPushButton,
            QHBoxLayout, QDialogButtonBox, QLabel,
        )

        layout = QVBoxLayout(self)
        title = QLabel(" Drive Status ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Drive", "Setting", "Value"])
        self.tree.setColumnWidth(0, 100)
        self.tree.setColumnWidth(1, 200)
        layout.addWidget(self.tree, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 0, 10, 10)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setMinimumWidth(scaled_px(90))
        self.btn_refresh.clicked.connect(self._refresh)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.button(
            QDialogButtonBox.StandardButton.Close
        ).clicked.connect(self.close)
        bar.addWidget(bb)
        layout.addLayout(bar)

        # Initial fetch
        self._refresh()
        # Auto-refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)

    def closeEvent(self, ev):
        try:
            self._timer.stop()
        except Exception:
            pass
        super().closeEvent(ev)

    def _refresh(self):
        from PyQt6.QtWidgets import QTreeWidgetItem
        if not self._host:
            return
        ok, data = u64_get_drives(self._host, password=self._password,
                                     port=self._http_port)
        if not ok:
            self.tree.clear()
            QTreeWidgetItem(self.tree, ["error", "", str(data)])
            return
        self.tree.clear()
        drives = data.get("drives", [])
        for d in drives:
            for drive_name, info in d.items():
                top = QTreeWidgetItem(
                    self.tree, [drive_name.upper(), "", ""])
                for key, value in info.items():
                    if isinstance(value, (dict, list)):
                        import json
                        value = json.dumps(value)
                    QTreeWidgetItem(top, ["", str(key), str(value)])
                top.setExpanded(True)


# ---------------------------------------------------------------------
# Config Editor dialog
# ---------------------------------------------------------------------


class U64ConfigEditorDialog(QDialog):
    """Read and edit the U64's Ultimate firmware configuration.

    Layout:
    - Left: category list (Drive A, Drive B, U64 Specific, etc.)
    - Right: form with one row per config item in the selected category
    - Bottom: Save to Flash | Load from Flash | Reset to Default | Close

    Editing semantics:
    - String values get a QLineEdit
    - Boolean-like (Enabled/Disabled, Yes/No) get a QComboBox
    - Numeric (per format %d) get a QSpinBox
    - Edits accumulate in a dirty dict; "Apply changes" pushes via
      bulk POST. No live-write per field to keep things predictable.
    """

    def __init__(self, host, http_port, password, parent=None):
        super().__init__(parent)
        self.setWindowTitle("U64 Configuration Editor")
        self.resize(820, 520)
        self._host = host
        self._http_port = http_port
        self._password = password
        # category -> {item -> new_value}  (only modified entries)
        self._dirty = {}
        # category -> dict of current items as fetched
        self._current = {}
        # category -> {item -> {min,max,format,values,default,current}}
        # Lazily filled on first display of each category via
        # u64_get_config_definitions(). Used by _make_widget_for() to
        # pick proper editor widgets (combo box for enums, spin box
        # with range, etc) instead of a blunt LineEdit.
        self._definitions = {}
        # Currently-displayed category form widgets, for collecting
        # values on category switch.
        self._form_widgets = {}
        self._active_category = None

        from PyQt6.QtWidgets import (
            QListWidget, QListWidgetItem, QPushButton,
            QFormLayout, QLineEdit, QSpinBox, QComboBox,
            QDialogButtonBox, QSplitter, QScrollArea, QWidget,
            QLabel,
        )

        layout = QVBoxLayout(self)
        title = QLabel(" U64 Configuration Editor ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        info = QLabel(
            "Changes don't persist until you click <b>Apply</b>. "
            "<b>Save to Flash</b> makes them survive a reboot.")
        info.setStyleSheet("padding: 4px; color: #444;")
        layout.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Left: categories
        self.lst_cats = QListWidget()
        self.lst_cats.setMinimumWidth(220)
        self.lst_cats.currentItemChanged.connect(
            self._on_category_changed)
        splitter.addWidget(self.lst_cats)
        # Right: form in a scroll area
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self._form_container = QWidget()
        self._form_layout = QFormLayout(self._form_container)
        self.form_scroll.setWidget(self._form_container)
        splitter.addWidget(self.form_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 580])
        layout.addWidget(splitter, 1)

        # Status / dirty count
        self.lbl_status = QLabel("  no changes  ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Buttons
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 0, 10, 10)
        btn_refresh = QPushButton("Refresh all")
        btn_refresh.setMinimumWidth(scaled_px(100))
        btn_refresh.setToolTip(
            "Re-fetch every category from the device, discarding\n"
            "any unsaved edits.")
        btn_refresh.clicked.connect(self._fetch_all)
        bar.addWidget(btn_refresh)
        btn_apply = QPushButton("Apply")
        btn_apply.setMinimumWidth(scaled_px(80))
        btn_apply.setStyleSheet(button_qss("green"))
        btn_apply.setToolTip(
            "Send all pending edits to the device via bulk POST.\n"
            "Settings take effect immediately but are not persisted\n"
            "to flash - use 'Save to Flash' for that.")
        btn_apply.clicked.connect(self._on_apply)
        bar.addWidget(btn_apply)
        bar.addSpacing(20)
        btn_save = QPushButton("Save to Flash")
        btn_save.setMinimumWidth(scaled_px(120))
        btn_save.setStyleSheet(button_qss("orange"))
        btn_save.setToolTip(
            "Write the current configuration to non-volatile memory\n"
            "so it survives a reboot.")
        btn_save.clicked.connect(self._on_save_flash)
        bar.addWidget(btn_save)
        btn_load = QPushButton("Load from Flash")
        btn_load.setMinimumWidth(scaled_px(120))
        btn_load.setToolTip(
            "Restore the configuration to what's saved in flash.")
        btn_load.clicked.connect(self._on_load_flash)
        bar.addWidget(btn_load)
        bar.addSpacing(12)
        # Save/Load to/from JSON file - lets the user keep named
        # snapshots like "stock_ntsc.json", "TheC64_emu.json",
        # "Quantum_demoparty.json", etc. They go through the same
        # bulk-config endpoint as Save to Flash, but with the data
        # stored on the PC as a human-readable JSON dump.
        btn_save_file = QPushButton("Save to File...")
        btn_save_file.setMinimumWidth(scaled_px(120))
        btn_save_file.setStyleSheet(button_qss("blue"))
        btn_save_file.setToolTip(
            "Save the current configuration to a named JSON file\n"
            "on the PC. The default location is\n"
            "<quopus_project>/u64_configs/ but you can pick anywhere.\n"
            "Useful for keeping multiple named profiles.")
        btn_save_file.clicked.connect(self._on_save_to_file)
        bar.addWidget(btn_save_file)
        btn_load_file = QPushButton("Load from File...")
        btn_load_file.setMinimumWidth(scaled_px(130))
        btn_load_file.setStyleSheet(button_qss("blue"))
        btn_load_file.setToolTip(
            "Load a previously-saved JSON configuration from the PC,\n"
            "send it to the device, and optionally persist to flash.")
        btn_load_file.clicked.connect(self._on_load_from_file)
        bar.addWidget(btn_load_file)
        bar.addSpacing(12)
        btn_factory = QPushButton("Factory Reset")
        btn_factory.setMinimumWidth(scaled_px(110))
        btn_factory.setStyleSheet(button_qss("red"))
        btn_factory.setToolTip(
            "Reset current config to factory defaults.\n"
            "Does NOT touch what's saved in flash.")
        btn_factory.clicked.connect(self._on_factory_reset)
        bar.addWidget(btn_factory)
        bar.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(
            QDialogButtonBox.StandardButton.Close
        ).clicked.connect(self.close)
        bar.addWidget(bb)
        layout.addLayout(bar)

        # Initial fetch
        QTimer.singleShot(0, self._fetch_all)

    def _fetch_all(self):
        """Pull all categories + their current values via the
        backup endpoint (cheaper than navigating one at a time)."""
        if not self._host:
            return
        self.lst_cats.clear()
        self.lbl_status.setText("  fetching...  ")
        QApplication.processEvents()
        ok, data = u64_backup_all_configs(
            self._host, password=self._password,
            port=self._http_port, timeout=60.0)
        if not ok:
            self.lbl_status.setText(f"  error: {data}  ")
            return
        self._current = data
        self._dirty = {}
        # Drop cached schemas too - device may have rebooted with
        # different firmware in between.
        self._definitions = {}
        for cat in data.keys():
            self.lst_cats.addItem(cat)
        if self.lst_cats.count() > 0:
            self.lst_cats.setCurrentRow(0)
        self._update_status()

    def _on_category_changed(self, current, previous):
        # Save any in-progress edits from the previous category
        # form into _dirty before switching.
        if self._active_category is not None:
            self._collect_form_edits()
        if current is None:
            return
        self._active_category = current.text()
        self._show_form(self._active_category)

    def _show_form(self, category):
        from PyQt6.QtWidgets import (
            QLineEdit, QSpinBox, QComboBox, QLabel,
        )
        # Clear existing form rows
        while self._form_layout.rowCount() > 0:
            self._form_layout.removeRow(0)
        self._form_widgets = {}
        items = self._current.get(category, {})
        # Lazy-load the definition schema for this category the
        # first time it's shown. The schema contains min/max/values/
        # format per item so we can render proper widgets instead
        # of plain text lines. Cached so we only hit the network
        # once per category per session.
        if category not in self._definitions:
            self.lbl_status.setText(
                f"  fetching schema for {category}...  ")
            QApplication.processEvents()
            ok, defs = u64_get_config_definitions(
                self._host, category,
                password=self._password,
                port=self._http_port)
            self._definitions[category] = defs if ok else {}
            self._update_status()
        defs = self._definitions.get(category, {})
        # Apply any dirty values on top
        dirty_for_cat = self._dirty.get(category, {})
        for key, value in items.items():
            display_value = dirty_for_cat.get(key, value)
            spec = defs.get(key) if isinstance(defs, dict) else None
            widget = self._make_widget_for(
                key, display_value, value, spec)
            self._form_widgets[key] = widget
            self._form_layout.addRow(QLabel(key), widget)

    def _make_widget_for(self, key, value, original, spec=None):
        """Create an appropriate widget for `value`.

        When the device provides a schema (`spec` dict with min/max/
        values/format) we honor it:
          - 'values' list -> QComboBox with exactly those options
          - integer 'min'/'max' -> QSpinBox with that range
          - 'format' "%x" / "%X" -> hex-aware LineEdit
        Otherwise we fall back to value-type heuristics (bool, int,
        common enum strings) and finally to a free LineEdit.
        """
        from PyQt6.QtWidgets import (
            QLineEdit, QSpinBox, QComboBox, QLabel,
        )
        # 1) Schema-driven path - prefer this whenever we have it
        if isinstance(spec, dict) and spec:
            # 1a) Enum: explicit list of allowed values from the
            #     device. Use a combo box that holds exactly those.
            #     Some firmware versions name the field "values",
            #     others use "options" - accept both.
            vals = spec.get("values")
            if vals is None:
                vals = spec.get("options")
            if isinstance(vals, list) and vals:
                cb = QComboBox()
                str_vals = [str(v) for v in vals]
                cb.addItems(str_vals)
                cur = str(value)
                if cur in str_vals:
                    cb.setCurrentText(cur)
                else:
                    # Value not in the declared enum - tolerate by
                    # adding it so we don't silently drop it, but
                    # mark with a leading marker so the user notices.
                    cb.insertItem(0, cur)
                    cb.setCurrentIndex(0)
                return cb
            # 1b) Integer with min/max: spin box with the declared
            #     range. Always honors min/max even if value is
            #     outside (Qt clamps automatically).
            lo = spec.get("min")
            hi = spec.get("max")
            fmt = spec.get("format", "")
            if (isinstance(lo, int) and isinstance(hi, int)
                    and isinstance(value, (int, bool))):
                # Hex format: best edited as a hex LineEdit
                if isinstance(fmt, str) and ("x" in fmt.lower()):
                    le = QLineEdit(format(int(value), 'x'))
                    le.setToolTip(
                        f"Hex value, range {lo:x}..{hi:x}")
                    return le
                sp = QSpinBox()
                sp.setRange(int(lo), int(hi))
                sp.setValue(int(value))
                return sp
        # 2) Value-type heuristics (legacy fallback)
        if isinstance(value, bool):
            cb = QComboBox()
            cb.addItems(["false", "true"])
            cb.setCurrentText("true" if value else "false")
            return cb
        if isinstance(value, int):
            sp = QSpinBox()
            sp.setRange(-2147483648, 2147483647)
            sp.setValue(value)
            return sp
        if isinstance(value, str):
            # Common enum-like strings
            low = value.lower()
            if low in ("enabled", "disabled"):
                cb = QComboBox()
                cb.addItems(["Disabled", "Enabled"])
                cb.setCurrentText(value)
                return cb
            if low in ("yes", "no"):
                cb = QComboBox()
                cb.addItems(["No", "Yes"])
                cb.setCurrentText(value)
                return cb
            le = QLineEdit(value)
            return le
        # Other types (lists, dicts): show as read-only label
        import json
        lbl = QLineEdit(json.dumps(value))
        lbl.setReadOnly(True)
        return lbl

    def _read_widget(self, widget, original):
        """Pull a value out of the editor widget in the same type
        family as the original."""
        from PyQt6.QtWidgets import QLineEdit, QSpinBox, QComboBox
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QComboBox):
            txt = widget.currentText()
            if isinstance(original, bool):
                return txt == "true"
            return txt
        if isinstance(widget, QLineEdit):
            return widget.text()
        return original

    def _collect_form_edits(self):
        """Walk the currently-shown form and diff against _current.
        Anything different goes into _dirty."""
        if not self._active_category:
            return
        cat = self._active_category
        original_items = self._current.get(cat, {})
        for key, widget in self._form_widgets.items():
            original = original_items.get(key)
            current = self._read_widget(widget, original)
            if current != original:
                self._dirty.setdefault(cat, {})[key] = current
            elif (cat in self._dirty
                  and key in self._dirty[cat]
                  and current == original):
                # Was dirty, now matches - clean
                del self._dirty[cat][key]
                if not self._dirty[cat]:
                    del self._dirty[cat]
        self._update_status()

    def _update_status(self):
        n = sum(len(v) for v in self._dirty.values())
        if n == 0:
            self.lbl_status.setText("  no changes  ")
        else:
            self.lbl_status.setText(
                f"  {n} change(s) pending - click Apply to send  ")

    def _on_apply(self):
        self._collect_form_edits()
        if not self._dirty:
            QMessageBox.information(self, "Apply",
                "No changes to send.")
            return
        ok, resp = u64_set_configs_bulk(
            self._host, self._dirty,
            password=self._password, port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Apply",
                f"Failed:\n{resp}")
            return
        # Folde changes into _current and clear _dirty
        for cat, items in self._dirty.items():
            cur = self._current.setdefault(cat, {})
            for k, v in items.items():
                cur[k] = v
        self._dirty = {}
        self._update_status()
        # Re-show current form so spinners/combos reflect saved state
        if self._active_category:
            self._show_form(self._active_category)
        QMessageBox.information(self, "Apply",
            "Configuration applied. Use 'Save to Flash' to make\n"
            "the changes persist across reboots.")

    def _on_save_flash(self):
        ok, err = u64_config_save_to_flash(
            self._host, password=self._password,
            port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Save to Flash",
                f"Failed:\n{err}")
            return
        QMessageBox.information(self, "Save to Flash",
            "Configuration saved to non-volatile memory.")

    def _on_load_flash(self):
        reply = QMessageBox.question(
            self, "Load from Flash",
            "Discard the current configuration and reload from flash?\n\n"
            "Any unsaved Apply'd changes will be lost.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, err = u64_config_load_from_flash(
            self._host, password=self._password,
            port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Load from Flash",
                f"Failed:\n{err}")
            return
        self._fetch_all()

    def _on_factory_reset(self):
        reply = QMessageBox.warning(
            self, "Factory Reset",
            "Reset current configuration to factory defaults?\n\n"
            "This does NOT erase what's saved in flash. To wipe\n"
            "flash too, follow this with 'Save to Flash'.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, err = u64_config_reset_to_default(
            self._host, password=self._password,
            port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Factory Reset",
                f"Failed:\n{err}")
            return
        self._fetch_all()

    def _default_config_save_dir(self):
        """Where named JSON configs go by default. Tries
        <quopus_project>/u64_configs/ first, falls back to ~ if
        the project root isn't writable."""
        import os
        try:
            from .config import SCRIPT_DIR
            if os.access(str(SCRIPT_DIR), os.W_OK):
                target = SCRIPT_DIR / "u64_configs"
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                return str(target)
        except Exception:
            pass
        return os.path.expanduser("~")

    def _on_save_to_file(self):
        """Save the current configuration to a named JSON file.

        Two-step process so the user can name the file:
          1. _fetch_all() makes sure self._current has the latest
             values from the device (in case anything's drifted
             since the dialog opened).
          2. File-save picker with a useful default location +
             default filename.

        The dumped file matches the format of U64BackupDialog so
        the two are interchangeable: either can restore the other.
        """
        import json, datetime, os
        if not self._host:
            QMessageBox.information(self, "Save to File",
                "No device host configured.")
            return
        # Make sure we have everything fresh
        self.lbl_status.setText("  fetching latest config...  ")
        QApplication.processEvents()
        # _current is the in-memory snapshot the dialog has been
        # editing; we save THAT, including any unsaved Apply'd
        # changes. The user expects "Save to File" to dump what's
        # on screen, not re-pull from device.
        if not self._current:
            QMessageBox.warning(self, "Save to File",
                "No configuration loaded yet - wait for the\n"
                "initial fetch to complete.")
            self.lbl_status.setText("  no data  ")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"u64_config_{self._host}_{ts}.json"
        start_dir = self._default_config_save_dir()
        suggested = os.path.join(start_dir, default_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save U64 config as...",
            suggested,
            "JSON files (*.json);;All files (*)")
        if not path:
            self.lbl_status.setText("  cancelled  ")
            return
        # Wrap with metadata so a later restore knows where it
        # came from. Same format as U64BackupDialog.
        wrapped = {
            "_meta": {
                "host": self._host,
                "timestamp": datetime.datetime.now().isoformat(),
                "tool": "Quopus U64 Config Editor",
                "format_version": 1,
            },
            "configs": self._current,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(wrapped, f, indent=2, sort_keys=True)
        except OSError as e:
            QMessageBox.warning(self, "Save to File",
                f"Could not write:\n{path}\n{e}")
            self.lbl_status.setText(f"  save failed: {e}  ")
            return
        # Status + brief popup so user knows it worked. Show just
        # the file name in the status bar to keep it compact.
        fname = os.path.basename(path)
        self.lbl_status.setText(f"  saved {fname}  ")
        QMessageBox.information(self, "Save to File",
            f"Saved current configuration to:\n{path}")

    def _on_load_from_file(self):
        """Load a configuration JSON from disk and apply it.

        Three-step process:
          1. Pick the JSON file
          2. Parse + validate the structure (must have a 'configs'
             dict, ideally the metadata wrapper we write but we
             tolerate raw config dicts too)
          3. Confirm with the user, send via bulk-POST, and OFFER
             to save to flash so the new config persists.
        """
        import json, os
        if not self._host:
            QMessageBox.information(self, "Load from File",
                "No device host configured.")
            return
        start_dir = self._default_config_save_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load U64 config from...",
            start_dir,
            "JSON files (*.json);;All files (*)")
        if not path:
            return
        # Parse + validate
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load from File",
                f"Couldn't read or parse:\n{path}\n{e}")
            return
        # Accept either wrapped {_meta, configs} OR a raw dict.
        if isinstance(payload, dict) and "configs" in payload \
                and isinstance(payload["configs"], dict):
            configs = payload["configs"]
            meta = payload.get("_meta", {})
        elif isinstance(payload, dict):
            configs = payload
            meta = {}
        else:
            QMessageBox.warning(self, "Load from File",
                "JSON file doesn't look like a U64 config dump.")
            return
        if not configs:
            QMessageBox.warning(self, "Load from File",
                "Loaded file has an empty 'configs' section.")
            return
        # Summarize what we're about to do
        n_cats = len(configs)
        n_settings = sum(len(v) for v in configs.values()
                          if isinstance(v, dict))
        meta_host = meta.get("host", "unknown")
        meta_ts = meta.get("timestamp", "unknown")
        msg = (f"Apply this configuration to {self._host}?\n\n"
               f"  File:    {os.path.basename(path)}\n"
               f"  Source:  {meta_host}\n"
               f"  Saved:   {meta_ts}\n"
               f"  Content: {n_cats} categories, "
               f"{n_settings} settings\n\n"
               f"Settings take effect immediately. After applying,\n"
               f"you'll be asked whether to also write them to\n"
               f"flash so they survive the next reboot.")
        reply = QMessageBox.question(
            self, "Load from File", msg,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Send via bulk POST
        self.lbl_status.setText("  applying loaded config...  ")
        QApplication.processEvents()
        ok, resp = u64_set_configs_bulk(
            self._host, configs,
            password=self._password, port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Load from File",
                f"Apply failed:\n{resp}")
            self.lbl_status.setText(f"  apply failed: {resp}  ")
            return
        # Update in-memory state to match what we just sent
        for cat, items in configs.items():
            if isinstance(items, dict):
                cur = self._current.setdefault(cat, {})
                for k, v in items.items():
                    cur[k] = v
        self._dirty = {}
        self._update_status()
        if self._active_category:
            self._show_form(self._active_category)
        # Offer to persist to flash
        flash_reply = QMessageBox.question(
            self, "Save to Flash",
            "Configuration applied successfully.\n\n"
            "Also write it to flash so it persists across reboots?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if flash_reply == QMessageBox.StandardButton.Yes:
            fok, ferr = u64_config_save_to_flash(
                self._host, password=self._password,
                port=self._http_port)
            if not fok:
                QMessageBox.warning(self, "Save to Flash",
                    f"Apply was OK but flash write failed:\n{ferr}")
                self.lbl_status.setText(
                    f"  applied but flash failed: {ferr}  ")
                return
            self.lbl_status.setText(
                f"  loaded {os.path.basename(path)}, flashed  ")
        else:
            self.lbl_status.setText(
                f"  loaded {os.path.basename(path)} (not flashed)  ")


# ---------------------------------------------------------------------
# Backup / Restore dialog (JSON snapshot of all configs)
# ---------------------------------------------------------------------


class U64BackupDialog(QDialog):
    """Save the U64's complete config to a JSON file, or restore it
    from a previously-saved JSON file. Just thin GUI around
    u64_backup_all_configs() and u64_set_configs_bulk()."""

    def __init__(self, host, http_port, password, parent=None):
        super().__init__(parent)
        self.setWindowTitle("U64 Config Backup / Restore")
        self.resize(540, 0)
        self._host = host
        self._http_port = http_port
        self._password = password

        from PyQt6.QtWidgets import (
            QPushButton, QDialogButtonBox, QLabel,
        )

        layout = QVBoxLayout(self)
        title = QLabel(" Configuration Backup / Restore ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        info = QLabel(
            "Save the device's full configuration to a JSON file\n"
            "for later restoration. Restore writes settings back\n"
            "via bulk POST; the device's flash is not touched\n"
            "unless you click 'Save to Flash' afterwards.")
        info.setStyleSheet("padding: 8px; color: #444;")
        layout.addWidget(info)

        self.lbl_status = QLabel("  ready  ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Buttons
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 10, 10, 10)
        btn_backup = QPushButton("Backup to file...")
        btn_backup.setStyleSheet(button_qss("green"))
        btn_backup.setMinimumWidth(scaled_px(140))
        btn_backup.clicked.connect(self._on_backup)
        bar.addWidget(btn_backup)
        btn_restore = QPushButton("Restore from file...")
        btn_restore.setStyleSheet(button_qss("orange"))
        btn_restore.setMinimumWidth(scaled_px(150))
        btn_restore.clicked.connect(self._on_restore)
        bar.addWidget(btn_restore)
        bar.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(
            QDialogButtonBox.StandardButton.Close
        ).clicked.connect(self.close)
        bar.addWidget(bb)
        layout.addLayout(bar)

    def _on_backup(self):
        import json, datetime, os
        if not self._host:
            return
        # Suggest a filename with host + timestamp
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suggested = f"u64_config_{self._host}_{ts}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save U64 config backup",
            suggested,
            "JSON files (*.json);;All files (*)")
        if not path:
            return
        self.lbl_status.setText("  fetching all configs...  ")
        QApplication.processEvents()
        ok, data = u64_backup_all_configs(
            self._host, password=self._password,
            port=self._http_port, timeout=60.0)
        if not ok:
            self.lbl_status.setText(f"  error: {data}  ")
            QMessageBox.warning(self, "Backup",
                f"Failed to fetch configuration:\n{data}")
            return
        # Save with a small wrapper containing metadata
        wrapped = {
            "_meta": {
                "host": self._host,
                "timestamp": datetime.datetime.now().isoformat(),
                "tool": "Quopus U64 Backup",
                "format_version": 1,
            },
            "configs": data,
        }
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(wrapped, f, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "Backup",
                f"Couldn't write file:\n{e}")
            return
        size_kb = os.path.getsize(path) / 1024
        cat_count = len(data)
        item_count = sum(
            len(v) for v in data.values()
            if isinstance(v, dict))
        self.lbl_status.setText(
            f"  saved {cat_count} categories, "
            f"{item_count} items ({size_kb:.1f} KB)  ")

    def _on_restore(self):
        import json
        if not self._host:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick a U64 config backup",
            "", "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                wrapped = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Restore",
                f"Couldn't read backup:\n{e}")
            return
        # Support both wrapped (with _meta) and bare dict formats
        if isinstance(wrapped, dict) and "configs" in wrapped:
            data = wrapped["configs"]
            meta = wrapped.get("_meta", {})
            src_host = meta.get("host", "?")
            ts = meta.get("timestamp", "?")
        elif isinstance(wrapped, dict):
            data = wrapped
            src_host = "?"
            ts = "?"
        else:
            QMessageBox.warning(self, "Restore",
                "File doesn't look like a U64 config backup.")
            return
        cat_count = len(data)
        item_count = sum(
            len(v) for v in data.values()
            if isinstance(v, dict))
        reply = QMessageBox.question(
            self, "Restore configuration",
            f"Apply backup to {self._host}?\n\n"
            f"  Source host:  {src_host}\n"
            f"  Backed up:    {ts}\n"
            f"  Categories:   {cat_count}\n"
            f"  Items:        {item_count}\n\n"
            "Settings will take effect immediately but won't be\n"
            "saved to flash unless you click 'Save to Flash' in\n"
            "the Config Editor afterwards.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.lbl_status.setText("  applying...  ")
        QApplication.processEvents()
        ok, resp = u64_set_configs_bulk(
            self._host, data,
            password=self._password,
            port=self._http_port,
            timeout=60.0)
        if not ok:
            self.lbl_status.setText(f"  error: {resp}  ")
            QMessageBox.warning(self, "Restore",
                f"Failed:\n{resp}")
            return
        self.lbl_status.setText(
            f"  restored {item_count} items  ")


# ---------------------------------------------------------------------
# Flow layout: like QHBoxLayout but wraps to the next line when the
# window gets narrower than the row's content. Used for the toolbars so
# the streamer window can be resized small in width without the buttons
# pinning a huge minimum width (which made the window only resizable in
# height). minimumSize() is the widest single item, NOT the row sum.
# ---------------------------------------------------------------------
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspacing=4, vspacing=2):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def addStretch(self, stretch=0):
        # No stretch concept in a wrapping flow layout - ignore so the
        # existing toolbar build code (which calls addStretch) works.
        return

    def addSpacing(self, size):
        from PyQt6.QtWidgets import QSpacerItem
        self.addItem(QSpacerItem(
            int(size), 0, QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Minimum))

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        if 0 <= i < len(self._items):
            return self._items[i]
        return None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, testonly):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_height = 0
        for item in self._items:
            sz = item.sizeHint()
            w, h = sz.width(), sz.height()
            next_x = x + w + self._hspace
            if next_x - self._hspace > right and line_height > 0:
                x = rect.x() + m.left()
                y = y + line_height + self._vspace
                next_x = x + w + self._hspace
                line_height = 0
            if not testonly:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))
            x = next_x
            line_height = max(line_height, h)
        return y + line_height - rect.y() + m.bottom()


# ---------------------------------------------------------------------
# Aspect-ratio frame: a QFrame that, in a vertical layout, reports a
# height of width*rh/rw. The video label inside fills it exactly, so
# the C64 picture is never letterboxed with black bars - the frame is
# always the picture's shape.
# ---------------------------------------------------------------------
class _AspectFrame(QFrame):
    def __init__(self, rw, rh, parent=None):
        super().__init__(parent)
        self._rw, self._rh = rw, rh
        sp = QSizePolicy(QSizePolicy.Policy.Expanding,
                         QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return round(w * self._rh / self._rw)


# ---------------------------------------------------------------------
# Main streamer dialog
# ---------------------------------------------------------------------


class U64Streamer(QDialog):
    """Top-level streamer window. Non-modal.

    Layout:
      +-- title bar -----------------------------------------+
      | [Config...] [Start] [Stop] [Reset]   stats          |
      +------------------------------------------------------+
      |                                                      |
      |               384 x 272 video frame                  |
      |               (scaled 2x or 3x)                      |
      |                                                      |
      +------------------------------------------------------+

    Telnet control is optional - if no IP is configured, the user
    has to start the stream manually from the U64 menu (F5).
    """

    def __init__(self, default_host: str = "",
                   video_port: int = PORT_VIDEO,
                   audio_port: int = PORT_AUDIO,
                   telnet_port: int = PORT_TELNET,
                   http_port: int = PORT_HTTP,
                   password: str = "",
                   video_only: bool = False,
                   always_on_top: bool = False,
                   parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ultimate 64 Stream Viewer")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        # Default scale = 2 -> 768x544 picture, comfortable on most
        # screens without dwarfing Quopus.
        self._scale = 2
        # Shared RGBA frame buffer; both worker and GUI poke at it.
        self._framebuf = bytearray(FRAME_W * FRAME_H * 4)
        # Pre-fill with a "no signal" pattern so the user sees
        # something even before the first frame arrives.
        self._fill_no_signal()

        self._host = default_host    # IP or hostname for telnet ctrl
        self._video_port = video_port
        self._audio_port = audio_port
        self._telnet_port = telnet_port
        self._http_port = http_port
        self._password = password
        self._video_only = video_only
        self._always_on_top = always_on_top
        self._video_worker = None
        self._audio_worker = None
        # Video recorder (lazy: created when user hits Rec).
        # When non-None, _refresh_video_widget pushes each frame
        # into it for background encoding.
        self._video_recorder = None
        # Drag-drop upload worker (one at a time)
        self._dnd_worker = None
        # Keyboard-injection worker (one at a time)
        self._type_worker = None
        # Space-burst worker (one at a time) - separater Worker weil
        # der SPC-Knopf mehrere zusaetzliche Pokes macht (matrix +
        # CIA) jenseits des normalen Keybuf-Wegs.
        self._space_worker = None
        # QAudioSink + IO device for streaming PCM out to the
        # default audio device. Created lazily on first audio
        # chunk so we don't claim the audio device until needed.
        self._audio_sink = None
        self._audio_io = None
        # Aggregate stats
        self._video_pps = 0
        self._video_bps = 0
        # Repaint throttle: True while the GUI is mid-repaint so
        # subsequent vsync signals can be dropped instead of queued.
        self._repaint_busy = False
        # Cinema mode = hide everything but the video frame. Backed
        # up here so toggle can restore.
        self._cinema_mode = False

        # Drag-and-drop autostart support: PRG/CRT/D64/etc files
        # dropped onto the window are uploaded and run on the U64.
        self.setAcceptDrops(True)

        self._build_ui()
        # Apply always-on-top from initial config. Has to be done
        # before show() ideally but Qt allows runtime change too -
        # we do it here so the flag is set the first time the
        # dialog is shown.
        self._apply_always_on_top()

        # Resize to fit the video at default scale + chrome
        self.resize(FRAME_W * self._scale + 20,
                      FRAME_H * self._scale + 90)

    # ---- UI construction --------------------------------------------
    def _build_ui(self):
        from PyQt6.QtWidgets import QWidget

        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(4, 4, 4, 4)

        # Each chrome "row" (titlebar, toolbar, host, keyboard,
        # f-keys) is wrapped in its own QWidget so cinema mode can
        # hide them all in one go via setVisible(False). The
        # widgets are kept as instance members for the toggle.
        self._chrome_widgets = []

        def _row_widget(layout_obj):
            w = QWidget(self)
            w.setLayout(layout_obj)
            if isinstance(layout_obj, FlowLayout):
                # heightForWidth so a wrapped toolbar gets enough height,
                # but PREFERRED vertical policy (not Minimum) so the
                # window's *minimum* height stays one row - otherwise the
                # min height balloons to the fully-wrapped height at the
                # smallest possible width.
                sp = w.sizePolicy()
                sp.setHeightForWidth(True)
                sp.setVerticalPolicy(QSizePolicy.Policy.Preferred)
                w.setSizePolicy(sp)
            self._chrome_widgets.append(w)
            return w

        # Title bar (just for the WB look)
        title = QLabel(" Ultimate 64 Stream Viewer ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)
        self._chrome_widgets.append(title)

        # Toolbar
        bar = FlowLayout()
        bar.setSpacing(2)
        bar.setContentsMargins(0, 0, 0, 0)

        self.btn_config = QPushButton("Config")
        self.btn_config.setStyleSheet(button_qss("blue"))
        self.btn_config.setMinimumWidth(scaled_px(90))
        self.btn_config.setToolTip(
            "Set the U64's host/IP and the video/audio/telnet ports")
        self.btn_config.clicked.connect(self._on_config)
        bar.addWidget(self.btn_config)

        self.btn_start = QPushButton("Start")
        self.btn_start.setStyleSheet(button_qss("green"))
        self.btn_start.setMinimumWidth(scaled_px(70))
        self.btn_start.setToolTip(
            "Start receiving + tell the U64 to begin streaming")
        self.btn_start.clicked.connect(self._on_start)
        bar.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setStyleSheet(button_qss("red"))
        self.btn_stop.setMinimumWidth(scaled_px(70))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip(
            "Stop the stream and tell the U64 to stop sending")
        self.btn_stop.clicked.connect(self._on_stop)
        bar.addWidget(self.btn_stop)

        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setStyleSheet(button_qss("orange"))
        self.btn_reset.setMinimumWidth(scaled_px(70))
        self.btn_reset.setToolTip("Send reset sequence to the U64")
        self.btn_reset.clicked.connect(self._on_reset)
        bar.addWidget(self.btn_reset)

        # Pause / Resume - freeze the C64 mid-execution via DMA line.
        # Useful for screenshots, memory snapshots, or just stopping
        # a runaway program without losing state.
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setStyleSheet(button_qss("blue"))
        self.btn_pause.setMinimumWidth(scaled_px(60))
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip(
            "Pause / Resume the C64 (DMA freeze).\n"
            "When ON the CPU is stopped at a safe moment;\n"
            "when OFF execution continues where it left off.\n"
            "Timers keep running either way.")
        self.btn_pause.toggled.connect(self._on_pause_toggle)
        bar.addWidget(self.btn_pause)

        # Menu = press the cart's Menu button / U64's Multi Button.
        # Toggles in/out of the Ultimate menu system.
        self.btn_menu = QPushButton("Menu")
        self.btn_menu.setStyleSheet(button_qss("blue"))
        self.btn_menu.setMinimumWidth(scaled_px(56))
        self.btn_menu.setToolTip(
            "Simulate pressing the Ultimate's Menu button.\n"
            "Enters or exits the Ultimate menu depending on\n"
            "current state.")
        self.btn_menu.clicked.connect(self._on_menu_button)
        bar.addWidget(self.btn_menu)

        # Reboot = restart the Ultimate firmware itself (heavier
        # than Reset). Re-init's cartridge config etc.
        self.btn_reboot = QPushButton("Reboot")
        self.btn_reboot.setStyleSheet(button_qss("orange"))
        self.btn_reboot.setMinimumWidth(scaled_px(70))
        self.btn_reboot.setToolTip(
            "Reboot the Ultimate firmware (re-init's cartridge\n"
            "config + reset). Heavier than a plain Reset.")
        self.btn_reboot.clicked.connect(self._on_reboot)
        bar.addWidget(self.btn_reboot)

        # Power off - U64-only. Asks for confirmation since this
        # actually cuts the power and you'll need to walk over to the
        # machine to turn it back on.
        self.btn_poweroff = QPushButton("Off")
        self.btn_poweroff.setStyleSheet(button_qss("red"))
        self.btn_poweroff.setMinimumWidth(scaled_px(50))
        self.btn_poweroff.setToolTip(
            "Power off the Ultimate 64 (Ultimate-II+ doesn't\n"
            "support this). You'll have to physically power it\n"
            "back on. Confirmation required.")
        self.btn_poweroff.clicked.connect(self._on_poweroff)
        bar.addWidget(self.btn_poweroff)

        # Screenshot capture - grabs the current video frame and
        # saves it as PNG to the Pictures/Ultimate64/ folder with a
        # timestamped filename. Works only when video stream is
        # active.
        self.btn_screenshot = QPushButton("Snap")
        self.btn_screenshot.setStyleSheet(button_qss("blue"))
        self.btn_screenshot.setMinimumWidth(scaled_px(56))
        self.btn_screenshot.setToolTip(
            "Capture current video frame as a PNG.\n"
            "Saved to ~/Pictures/Ultimate64/ (or %USERPROFILE%\\Pictures\\Ultimate64\\)\n"
            "with a timestamped filename. Hotkey: S")
        self.btn_screenshot.clicked.connect(self._on_screenshot)
        bar.addWidget(self.btn_screenshot)

        # Video recorder toggle. Click toggles between idle and
        # recording. The actual encode happens in a background
        # _VideoRecorder thread; this button just starts/stops it.
        # Right-click drops a small menu to pick MP4 vs PNG sequence
        # for the next recording (default is MP4 if ffmpeg is on
        # PATH, PNG-sequence otherwise).
        self.btn_record = QPushButton("Rec")
        self.btn_record.setStyleSheet(button_qss("blue"))
        self.btn_record.setMinimumWidth(scaled_px(56))
        self.btn_record.setCheckable(True)
        self.btn_record.setToolTip(
            "Record video to MP4 (libx264) or PNG-sequence.\n"
            "Saved to the configured screenshot folder with a\n"
            "timestamped filename. Right-click for format options.\n"
            "MP4 requires ffmpeg on PATH; falls back to PNG-seq if\n"
            "not found.")
        self.btn_record.clicked.connect(self._on_record_toggle)
        self.btn_record.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_record.customContextMenuRequested.connect(
            self._on_record_context_menu)
        bar.addWidget(self.btn_record)
        # Recording format preference; flipped via the right-click
        # menu. Persisted to config when set. Default = MP4.
        try:
            from .config import load_config as _lc
            self._record_format = _lc().get(
                "u64_record_format", "mp4")
        except Exception:
            self._record_format = "mp4"
        if self._record_format not in ("mp4", "png_seq"):
            self._record_format = "mp4"

        # Scale picker - kept on row 1 next to the streaming controls
        bar.addWidget(QLabel(" Scale: "))
        self.cmb_scale = QComboBox()
        for n in (1, 2, 3, 4):
            self.cmb_scale.addItem(f"{n}x", n)
        self.cmb_scale.setCurrentIndex(1)   # default 2x
        self.cmb_scale.currentIndexChanged.connect(self._on_scale_changed)
        bar.addWidget(self.cmb_scale)

        bar.addStretch()

        # Stats label - filled by _on_stats() - stays on row 1
        self.lbl_stats = QLabel("  not connected  ")
        self.lbl_stats.setStyleSheet(INFOBAR_QSS)
        self.lbl_stats.setMinimumWidth(280)
        bar.addWidget(self.lbl_stats)

        bar.addStretch()

        # Cinema mode toggle - row 1, right side
        self.btn_cinema = QPushButton("Cinema")
        self.btn_cinema.setStyleSheet(button_qss("blue"))
        self.btn_cinema.setMinimumWidth(scaled_px(78))
        self.btn_cinema.setCheckable(True)
        self.btn_cinema.setToolTip(
            "Hide all controls and show only the video.\n"
            "Click again or press 'C' / Esc to bring controls back.")
        self.btn_cinema.toggled.connect(self._on_cinema_toggle)
        bar.addWidget(self.btn_cinema)

        # Always-on-top
        self.btn_ontop = QPushButton("On top")
        self.btn_ontop.setStyleSheet(button_qss("blue"))
        self.btn_ontop.setMinimumWidth(scaled_px(70))
        self.btn_ontop.setCheckable(True)
        self.btn_ontop.setChecked(self._always_on_top)
        self.btn_ontop.setToolTip(
            "Keep the streamer window above other windows.")
        self.btn_ontop.toggled.connect(self._on_ontop_toggle)
        bar.addWidget(self.btn_ontop)

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setMinimumWidth(scaled_px(70))
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)

        # Commit row 1
        layout.addWidget(_row_widget(bar))

        # ---------------- Row 2: U64 management features ----------------
        # Splits the U64 ReST API features (mount/drives/config/backup/
        # BASIC/Assembly64) into their own row so the streamer doesn't
        # require a 1900px-wide window.
        bar2 = FlowLayout()
        bar2.setSpacing(2)
        bar2.setContentsMargins(0, 0, 0, 0)

        # Mount disk - opens U64MountDialog with file picker, drive
        # selector (A/B) and mode (RO/RW/Unlinked).
        self.btn_mount = QPushButton("Mount")
        self.btn_mount.setStyleSheet(button_qss("blue"))
        self.btn_mount.setMinimumWidth(scaled_px(80))
        self.btn_mount.setToolTip(
            "Mount a D64/D71/D81/G64/G71/G81 image on Drive A or B\n"
            "with choice of mode (Read-only / Read-write / Unlinked).")
        self.btn_mount.clicked.connect(self._on_mount_dialog)
        bar2.addWidget(self.btn_mount)

        # Drive status - live read of /v1/drives, auto-refresh.
        self.btn_drives = QPushButton("Drives")
        self.btn_drives.setStyleSheet(button_qss("blue"))
        self.btn_drives.setMinimumWidth(scaled_px(64))
        self.btn_drives.setToolTip(
            "Show live drive status (mounted images, modes, bus IDs).\n"
            "Auto-refreshes every 2 seconds while open.")
        self.btn_drives.clicked.connect(self._on_drives_dialog)
        bar2.addWidget(self.btn_drives)

        # Config editor - full read/write UI for U64 firmware settings
        self.btn_cfg_edit = QPushButton("Cfg Edit")
        self.btn_cfg_edit.setStyleSheet(button_qss("blue"))
        self.btn_cfg_edit.setMinimumWidth(scaled_px(74))
        self.btn_cfg_edit.setToolTip(
            "Edit Ultimate firmware configuration: drives, SID,\n"
            "audio, network, cartridge, modem, etc. Save to flash\n"
            "to persist changes across reboots.")
        self.btn_cfg_edit.clicked.connect(self._on_cfg_editor)
        bar2.addWidget(self.btn_cfg_edit)

        # Backup/Restore - JSON snapshot of all U64 configs
        self.btn_backup = QPushButton("Backup")
        self.btn_backup.setStyleSheet(button_qss("blue"))
        self.btn_backup.setMinimumWidth(scaled_px(72))
        self.btn_backup.setToolTip(
            "Backup the U64's full configuration to a JSON file\n"
            "for later restoration on this or another device.")
        self.btn_backup.clicked.connect(self._on_backup_dialog)
        bar2.addWidget(self.btn_backup)

        # BASIC editor - write programs with petcat-style PETSCII codes,
        # syntax highlighting, validate, and Send & Run via REST API
        self.btn_basic = QPushButton("BASIC")
        self.btn_basic.setStyleSheet(button_qss("green"))
        self.btn_basic.setMinimumWidth(scaled_px(64))
        self.btn_basic.setToolTip(
            "Open a BASIC v2 editor with syntax highlighting and\n"
            "petcat-style PETSCII control codes ({CLR}, {RVS_ON}, ...)\n"
            "Tokenize and Send & Run via REST API.")
        self.btn_basic.clicked.connect(self._on_basic_editor)
        bar2.addWidget(self.btn_basic)

        # Assembly64 browser - search the online aggregator (CSDB,
        # HVSC, c64.org, OneLoad64, Gamebase64, ...) and run/mount
        # files directly on the device.
        self.btn_asm64 = QPushButton("Asm64")
        self.btn_asm64.setStyleSheet(button_qss("green"))
        self.btn_asm64.setMinimumWidth(scaled_px(64))
        self.btn_asm64.setToolTip(
            "Open the Assembly64 browser - search CSDB, HVSC, c64.org,\n"
            "OneLoad64, Gamebase64 and friends via Fredrik Aaberg's\n"
            "API at hackerswithstyle.se. Run / mount / download\n"
            "results straight to the U64.")
        self.btn_asm64.clicked.connect(self._on_asm64_browser)
        bar2.addWidget(self.btn_asm64)

        bar2.addStretch(1)

        # Commit row 2
        layout.addWidget(_row_widget(bar2))

        # IP/host display row
        host_row = QHBoxLayout()
        host_row.setSpacing(2)
        host_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_host = QLabel(self._host_display())
        self.lbl_host.setStyleSheet(INFOBAR_QSS)
        host_row.addWidget(self.lbl_host, 1)
        layout.addWidget(_row_widget(host_row))

        # Keyboard injection row: type a string and have it appear
        # on the C64 via the keyboard buffer at $0277. Plus a
        # capture-keys toggle that forwards every key the streamer
        # window sees while it has focus.
        from PyQt6.QtWidgets import QLineEdit, QCheckBox
        kbd_row = QHBoxLayout()
        kbd_row.setSpacing(2)
        kbd_row.setContentsMargins(0, 2, 0, 2)
        kbd_row.addWidget(QLabel(" Type to U64:"))
        self.ed_type = QLineEdit()
        self.ed_type.setPlaceholderText(
            "type a line, press Enter to send (RETURN included)")
        self.ed_type.setToolTip(
            "<qt>ASCII gets converted to PETSCII and pushed into the "
            "C64's keyboard buffer at $0277. Works with BASIC and "
            "anything else that reads the standard KERNAL input.</qt>")
        self.ed_type.returnPressed.connect(self._on_type_send)
        # Install an event filter so that when 'Capture keys' is
        # on, every keystroke into the Type-line gets forwarded
        # to the C64 immediately AND the Type-line itself does
        # NOT receive the key (would otherwise build up a stale
        # local buffer the user can't see being sent live). The
        # filter is a no-op when capture is off, so the
        # Type-line behaves normally for paste-and-Enter usage.
        self.ed_type.installEventFilter(self)
        kbd_row.addWidget(self.ed_type, 1)
        self.btn_send = QPushButton("Send")
        self.btn_send.setStyleSheet(button_qss("blue"))
        self.btn_send.setMinimumWidth(scaled_px(60))
        self.btn_send.setToolTip("Send the typed text + RETURN to the C64")
        self.btn_send.clicked.connect(self._on_type_send)
        kbd_row.addWidget(self.btn_send)
        self.chk_capture = QCheckBox("Capture keys")
        self.chk_capture.setToolTip(
            "<qt>When checked: every key pressed while this window has "
            "focus gets forwarded to the C64. Modifier keys "
            "(Shift, Ctrl) and most function keys are mapped; others "
            "are sent as raw PETSCII or dropped.</qt>")
        # When the user unchecks Capture, tear down the persistent
        # keyboard worker so we don't leave its TCP connection to
        # the U64 hanging open for the rest of the session. The
        # worker auto-restarts on first key after re-enabling.
        self.chk_capture.toggled.connect(
            self._on_capture_toggled)
        kbd_row.addWidget(self.chk_capture)
        layout.addWidget(_row_widget(kbd_row))

        # F-key + special-key button row. These are clickable
        # equivalents of the C64 function keys so the user doesn't
        # have to fight the OS / window manager over F-key capture.
        # Same effect as Capture-keys + pressing the actual key.
        fkey_row = FlowLayout()
        fkey_row.setSpacing(2)
        fkey_row.setContentsMargins(0, 0, 0, 2)
        fkey_row.addWidget(QLabel(" Keys: "))
        # The C64 PETSCII codes for F1-F8. Order matches the layout
        # of the keys on a real C64 keyboard (F1/F2 share a key,
        # F3/F4 share, etc - shifted = even number).
        fkey_specs = [
            ("F1", 0x85), ("F2", 0x89),
            ("F3", 0x86), ("F4", 0x8A),
            ("F5", 0x87), ("F6", 0x8B),
            ("F7", 0x88), ("F8", 0x8C),
        ]
        for label, pet in fkey_specs:
            btn = QPushButton(label)
            btn.setStyleSheet(button_qss("blue"))
            btn.setMinimumWidth(scaled_px(38))
            btn.setToolTip(f"Send {label} (PETSCII {pet:#04x}) "
                              f"to the C64 keyboard buffer")
            # Default arg trick to capture `pet` per-iteration -
            # otherwise all buttons would send the last value.
            btn.clicked.connect(lambda _checked=False, p=pet:
                                  self._send_petscii_byte(p))
            fkey_row.addWidget(btn)

        # Common control keys that also tend to get eaten by the OS
        # before they reach our keyPressEvent.
        for label, pet, tip in [
            ("R/S", 0x03, "RUN/STOP"),
            ("RET", 0x0D, "RETURN"),
            ("DEL", 0x14, "DEL / INST"),
            ("CLR", 0x93, "CLR/HOME (shifted)"),
            ("HM",  0x13, "HOME"),
            ("\u2191", 0x91, "Cursor up"),
            ("\u2193", 0x11, "Cursor down"),
            ("\u2190", 0x9D, "Cursor left"),
            ("\u2192", 0x1D, "Cursor right"),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(button_qss("blue"))
            btn.setMinimumWidth(scaled_px(34))
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False, p=pet:
                                  self._send_petscii_byte(p))
            fkey_row.addWidget(btn)

        # SPACE bekommt einen eigenen Pfad: zusaetzlich zum KERNAL-
        # Buffer poken wir matrix-code ($00C5/$CB = $3C) und CIA1
        # joystick-fire bits, damit auch Intros/Demos angesprochen
        # werden die nicht den KERNAL-Buffer lesen.
        self.btn_space = QPushButton("SPC")
        self.btn_space.setStyleSheet(button_qss("blue"))
        self.btn_space.setMinimumWidth(scaled_px(34))
        self.btn_space.setToolTip(
            "SPACE - tries keybuf + matrix-code + joystick-fire so "
            "intros / demos that don't use the KERNAL buffer can "
            "still receive it")
        self.btn_space.clicked.connect(self._on_space_burst)
        fkey_row.addWidget(self.btn_space)
        # "Memory" button - liest beliebigen C64-RAM Bereich vom U64
        # via DMA und zeigt ihn als ASM/HEX. Adresse + Laenge werden
        # in einem Dialog abgefragt.
        fkey_row.addSpacing(8)
        self.btn_mem = QPushButton("Memory")
        self.btn_mem.setStyleSheet(button_qss("orange"))
        self.btn_mem.setMinimumWidth(scaled_px(70))
        self.btn_mem.setToolTip(
            "Grab a memory range from the U64 (DMA read) and show "
            "it as 6502 disassembly or hex dump")
        self.btn_mem.clicked.connect(self._on_memory_grab)
        fkey_row.addWidget(self.btn_mem)
        fkey_row.addStretch()
        layout.addWidget(_row_widget(fkey_row))

        # Quick-type buttons: pre-canned BASIC commands so the user
        # doesn't have to fight ASCII -> PETSCII conversion or the
        # type line's focus rules for the four commands that come
        # up most often. Each button injects the literal string
        # followed by RETURN into the C64's keyboard buffer.
        quick_row = FlowLayout()
        quick_row.setSpacing(2)
        quick_row.setContentsMargins(0, 0, 0, 2)
        quick_row.addWidget(QLabel(" Quick: "))
        for label, text in (
            ('LOAD"$",8',    'LOAD"$",8'),
            ('LOAD"*",8,1',  'LOAD"*",8,1'),
            ('LIST',         'LIST'),
            ('RUN',          'RUN'),
        ):
            btn = QPushButton(label)
            btn.setStyleSheet(button_qss("green"))
            btn.setMinimumWidth(86)
            btn.setToolTip(
                f"Inject '{text}<RETURN>' into the C64's keyboard "
                "buffer. Works whenever the BASIC READY. prompt "
                "is up (after RESET or after a program ends).")
            # Capture `text` via default-arg trick so the lambda
            # doesn't close over the loop variable.
            btn.clicked.connect(
                lambda checked=False, t=text: self._quick_type(t))
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            quick_row.addWidget(btn)
        quick_row.addStretch()
        layout.addWidget(_row_widget(quick_row))

        # Video display - simple QLabel that we keep replacing the
        # pixmap of. Centered. Frame around it for the WB look.
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet(f"QFrame {{ background-color: {C.BLACK}; }}")
        flayout = QVBoxLayout(frame)
        flayout.setContentsMargins(0, 0, 0, 0)
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        self.video_label.setMinimumSize(FRAME_W, FRAME_H)
        flayout.addWidget(self.video_label)
        layout.addWidget(frame, 1)

        # Initial repaint
        self._refresh_video_widget()

        # Damit Space/Enter NICHT versehentlich Buttons (Reset, RET-
        # Button etc) ausloesen, sobald der User Tastatur-Input ans
        # C64 schicken will, nehmen wir allen Buttons und der Capture-
        # Checkbox den Keyboard-Focus weg. Sie bleiben per Maus voll
        # bedienbar; Qt's Default-"Space klickt aktiven Button" wuerde
        # sonst auf einem C64-Intro fuer 'press space to start' nie
        # ans C64 ankommen sondern lokal einen Button triggern.
        # ed_type bleibt focusable, sonst kann man dort nicht tippen.
        for w in self.findChildren(QPushButton):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chk_capture.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cmb_scale.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    # ---- helpers ----------------------------------------------------
    def _fill_no_signal(self):
        """Paint a 'no signal' message into the framebuf so the
        user sees something on first open."""
        # All-blue background (the classic C64 boot blue).
        bg = bytes([0x35, 0x28, 0x79, 0xff])
        for i in range(0, len(self._framebuf), 4):
            self._framebuf[i:i+4] = bg

    def _refresh_video_widget(self):
        """Push the current framebuf into the QLabel as a scaled
        QPixmap. Called on every vsync via frame_ready signal AND
        whenever the window is resized.

        The pixmap is scaled to fit the QLabel's *current size*,
        keeping the 384:272 aspect ratio. So the user can grab the
        window edge and the picture follows. The Scale: combobox
        merely sets the initial window size; after that the user
        is free to resize.

        Throttling: if a repaint is still in flight (the worker
        thread has emitted frame_ready faster than the GUI can
        keep up - happens at large window sizes) we just drop the
        new frame instead of letting the signal queue grow. This
        keeps the picture responsive at the cost of a few dropped
        frames.
        """
        if self._repaint_busy:
            return
        self._repaint_busy = True
        try:
            # If a recording is active, hand off a snapshot of the
            # current framebuf BEFORE we scale/touch anything else.
            # The recorder copies the bytes in push_frame so we
            # don't hold a reference to the live buffer.
            if self._video_recorder is not None:
                try:
                    self._video_recorder.push_frame(self._framebuf)
                except Exception as e:
                    # Don't let a recorder hiccup kill the video
                    # display loop - just log via the status bar.
                    print(f"  [u64 rec] push_frame failed: {e}")
            # Wrap bytearray directly - QImage can handle memoryview
            # without an extra copy. The previous bytes(self._framebuf)
            # call was copying ~417KB per frame at 50fps = 20MB/sec
            # of pointless work.
            img = QImage(self._framebuf, FRAME_W, FRAME_H,
                           FRAME_W * 4, QImage.Format.Format_RGBA8888)
            # Scale the picture to fit the QLabel's current size,
            # keeping the 384:272 aspect ratio via KeepAspectRatio.
            # Both DOWN-scaling (small window) and UP-scaling
            # (large window) work - the previous version used
            # max(FRAME_W, avail.width()) which clamped the
            # scale target to a minimum of 384x272, so windows
            # smaller than that got the picture rendered at
            # full size and cropped by the label's bounds (only
            # the top-left quarter visible). Now any window
            # size gets a properly fitted picture.
            avail = self.video_label.size()
            # Lower bound of 1 px so the QImage.scaled() call
            # doesn't crash on a degenerate 0-sized label
            # during initial layout / mid-resize. Anything
            # below ~50 px is unreadable anyway.
            target_w = max(1, avail.width())
            target_h = max(1, avail.height())
            scaled = img.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
            self.video_label.setPixmap(QPixmap.fromImage(scaled))
        finally:
            self._repaint_busy = False

    # ---- UI handlers ------------------------------------------------
    def _host_display(self):
        """Status line showing host + ports. Updated whenever the
        config changes."""
        if self._host:
            return (f"  U64 host: {self._host}    "
                      f"video UDP {self._video_port},  "
                      f"audio UDP {self._audio_port},  "
                      f"telnet TCP {self._telnet_port}  ")
        return (f"  U64 host: (not set - use Config...)    "
                  f"video UDP {self._video_port},  "
                  f"audio UDP {self._audio_port},  "
                  f"telnet TCP {self._telnet_port}  ")

    def _on_cinema_toggle(self, checked: bool):
        """Toggle cinema mode: hide all chrome AND go fullscreen.

        Entering cinema mode:
          1. Save the current window geometry + window state so we
             can restore them when leaving cinema mode
          2. Hide every chrome widget (titlebar, toolbars, host
             info, type field, F-keys row)
          3. Reparent the cinema-toggle button as a floating overlay
             in the top-right corner so the user has a way back
             without keyboard
          4. Call showFullScreen() - takes over the whole monitor

        Leaving cinema mode reverses all four steps.

        Esc also leaves cinema (handled in event() override).
        """
        self._cinema_mode = checked
        # Hide/show every chrome row except the cinema button itself.
        for w in self._chrome_widgets:
            # The bar layout that owns btn_cinema ALSO owns Close,
            # On-top, etc. We want those gone too in cinema mode -
            # the cinema button itself is the one exception.
            w.setVisible(not checked)
        if checked:
            # Save geometry so we can restore it.
            self._pre_cinema_geometry = self.saveGeometry()
            # Reposition the cinema toggle as a floating overlay
            # in the top-right corner. Style it with a slightly
            # transparent background so the picture can show
            # underneath but the button stays clickable.
            self.btn_cinema.setText("Show controls (Esc)")
            self.btn_cinema.setParent(self)
            self.btn_cinema.setMinimumWidth(scaled_px(160))
            # Slightly translucent so the picture peeks through.
            # rgba(48, 80, 168, 0.85) is the blue button at 85%.
            self.btn_cinema.setStyleSheet(
                "QPushButton { background-color: rgba(48, 80, 168, 0.85);"
                " color: white; border: 1px solid black;"
                " padding: 4px; }"
                "QPushButton:hover { background-color: rgba(60, 100, 200, 1.0); }")
            self.btn_cinema.move(self.width() - 170, 6)
            self.btn_cinema.raise_()
            self.btn_cinema.show()
            # Maximize the window so the C64 picture fills the
            # whole work area. Combined with hidden chrome that
            # gives the biggest possible picture without the
            # OS-window-decoration-stripping that fullscreen does.
            self.showMaximized()
        else:
            # Leave maximize and restore previous size.
            self.showNormal()
            geom = getattr(self, '_pre_cinema_geometry', None)
            if geom is not None:
                self.restoreGeometry(geom)
            # Re-parent the button back to the toolbar with normal
            # (non-translucent) styling.
            self.btn_cinema.setText("Cinema")
            self.btn_cinema.setMinimumWidth(scaled_px(78))
            self.btn_cinema.setStyleSheet(button_qss("blue"))
            if len(self._chrome_widgets) >= 2:
                bar_w = self._chrome_widgets[1]
                bar_layout = bar_w.layout()
                if bar_layout is not None:
                    # Original order in the toolbar: ... Cinema,
                    # On-top, Close. So insert at count-2 to land
                    # before the On-top button.
                    self.btn_cinema.setParent(bar_w)
                    bar_layout.insertWidget(
                        bar_layout.count() - 2,
                        self.btn_cinema)
                    self.btn_cinema.show()

    # Explicit window-size table for --minimal=N. Stepped at
    # 192 px width / 136 px height (the half-VIC-II frame) for
    # N=1..6, then two bigger jumps (×8, ×10 of the base step)
    # to give the user useful jumps at the top end up to ~FullHD
    # without spamming a dozen near-identical entries. Width:
    # height ratio stays at the C64's 7:5 (1.41:1) everywhere.
    _MINIMAL_SIZES = {
        1: ( 192,  136),
        2: ( 384,  272),
        3: ( 576,  408),
        4: ( 768,  544),
        5: ( 960,  680),
        6: (1152,  816),
        7: (1536, 1088),
        8: (1920, 1360),
    }

    def enter_minimal_mode(self, scale: int = None) -> None:
        """Enter a minimal display-only mode: every UI chrome
        element AND the OS window decoration is hidden. Only the
        raw video frame is visible. The user can:
          - LEFT-click anywhere in the picture and drag to move
            the window (since there is no titlebar)
          - RIGHT-click anywhere to get a "Close streamer?" Yes/No
            confirmation

        `scale` (1..8) picks the window size from a fixed table:
          1 ->  192 x  136   (half VIC-II, smallest sensible)
          2 ->  384 x  272   (native VIC-II 1:1)
          3 ->  576 x  408
          4 ->  768 x  544   (default - the old "scale 2" pixel size)
          5 ->  960 x  680
          6 -> 1152 x  816
          7 -> 1536 x 1088
          8 -> 1920 x 1360   (~FullHD - widest sensible)
        If None, scale 4 (768x544) is used. Values outside 1..8
        are clamped to the range.

        The window position is persisted across launches in
        u64_streamer_minimal_pos.json under the Quopus config
        dir. Next time the streamer is launched with --minimal
        it lands at the same spot.

        Used by the standalone streamer's --minimal command-line
        flag for BBS-style "kiosk" displays where the user
        should not see any chrome.
        """
        from PyQt6.QtCore import Qt, QTimer, QPoint
        from PyQt6.QtWidgets import QFrame
        # Resolve the requested scale to a (w, h) pair from the
        # explicit lookup table. Values outside 1..8 clamp to
        # the closest valid entry. None -> default 4 (768x544).
        if scale is None:
            scale = 4
        scale = max(1, min(8, int(scale)))
        video_w, video_h = self._MINIMAL_SIZES[scale]
        # The internal self._scale attribute is used by the
        # frame renderer for QPixmap sizing. We can't directly
        # use the lookup-table sizes (they're not all integer
        # multiples of FRAME_W=384), so we pick the closest
        # integer multiplier the renderer can handle. The
        # KeepAspectRatio scaler in _refresh_video_widget will
        # do the final fit-to-label scaling anyway, so this
        # mostly affects render quality, not correctness.
        self._scale = max(1, round(video_w / FRAME_W))
        # Hide every chrome widget
        for w in self._chrome_widgets:
            try:
                w.setVisible(False)
            except Exception:
                pass
        try:
            self.btn_cinema.hide()
        except Exception:
            pass
        self._minimal_mode = True
        # Layout cleanup: no margins, no spacing - the picture
        # fills the window edge-to-edge.
        try:
            self.layout().setContentsMargins(0, 0, 0, 0)
            self.layout().setSpacing(0)
        except Exception:
            pass
        # The video QLabel sits inside a QFrame with StyledPanel
        # shape - that draws a 1-2 px border around the picture.
        # In minimal mode we don't want a border (the whole point
        # is "just the picture"), so we walk up from video_label
        # to find that frame and turn its border off.
        try:
            parent = self.video_label.parent()
            if isinstance(parent, QFrame):
                parent.setFrameShape(QFrame.Shape.NoFrame)
                # Inner layout margins on the frame too
                inner_layout = parent.layout()
                if inner_layout is not None:
                    inner_layout.setContentsMargins(0, 0, 0, 0)
                    inner_layout.setSpacing(0)
        except Exception:
            pass
        # Drop the label's minimum-size constraint so resize()
        # to exactly video_w x video_h doesn't get rejected by
        # the layout system. The constraint was there to keep
        # the chrome usable when the window is dragged tiny;
        # in minimal mode there is no chrome to protect.
        try:
            self.video_label.setMinimumSize(0, 0)
        except Exception:
            pass
        # Persistent drag state - set by mousePressEvent when
        # the user starts dragging the picture.
        self._minimal_drag_offset = None

        # --- Frameless window with draggable picture ----------
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint,
                            True)

        # Install our eventFilter on the video QLabel so its
        # mouse events get forwarded to the dialog-level
        # mousePressEvent / mouseMoveEvent / mouseReleaseEvent /
        # contextMenuEvent overrides. Without this, clicks
        # landing on the label (= 100% of the window in minimal
        # mode) never reach our drag/close handlers because Qt
        # doesn't auto-propagate mouse events from a child
        # widget to its parent.
        try:
            self.video_label.installEventFilter(self)
            # Enable mouse-tracking so MouseMove events arrive
            # even when no button is pressed (some Linux WMs
            # only deliver button-held move events otherwise).
            self.video_label.setMouseTracking(True)
            self.setMouseTracking(True)
            # Cursor hint - shows the user this is a draggable
            # surface. Switches to OpenHandCursor over the
            # picture; ClosedHandCursor would be nicer during
            # the actual drag but Qt's startSystemMove takes
            # control of the cursor mid-drag on most platforms
            # so the extra state machine isn't worth it.
            from PyQt6.QtCore import Qt as _Qt
            self.video_label.setCursor(_Qt.CursorShape.OpenHandCursor)
        except Exception:
            pass

        # --- Restore saved position from cfg if available -----
        # Wayland note: this is a no-op under Wayland because
        # the compositor refuses programmatic move() calls
        # (window placement is the compositor's job there, not
        # the app's). We still try - the call just silently
        # does nothing on Wayland. Under X11/Windows/macOS the
        # move() works fine.
        saved_pos = self._load_minimal_position()
        is_wayland = self._is_running_under_wayland()

        # Defer all the geometry tweaks - Qt processes the
        # setVisible(False) calls + the window-flag change
        # asynchronously, and a synchronous resize/move here
        # gets overridden by a late layout pass.
        def _apply_geometry():
            # setFixedSize is stronger than resize() - it both
            # resizes AND prevents Qt's layout from snapping
            # the window back to its sizeHint.
            self.setFixedSize(video_w, video_h)
            if saved_pos is not None and not is_wayland:
                # Under X11 / Windows / macOS the WM honors
                # this. Under Wayland it's a no-op so we skip
                # noisy attempts and print a one-time hint.
                self.move(saved_pos[0], saved_pos[1])
            elif saved_pos is not None and is_wayland:
                # Tell the user once that Wayland is in charge
                # of window position - if they wanted persistent
                # placement, they need to be on X11.
                if not getattr(self, '_wayland_position_warned',
                                False):
                    print("  [streamer] Wayland session "
                          "detected - saved window position "
                          f"({saved_pos[0]}, {saved_pos[1]}) "
                          f"cannot be restored, Wayland "
                          f"compositor controls window "
                          f"placement. Drag the window once "
                          f"after start; position is still "
                          f"saved for the day you switch to "
                          f"X11.")
                    self._wayland_position_warned = True
            # show() is required after toggling FramelessWindowHint
            # for the change to take effect on Win/X11.
            self.show()
            # Force one more video repaint at the new label size
            # so the picture re-scales to fill the now-shrunk
            # window.
            try:
                self._refresh_video_widget()
            except Exception:
                pass
        QTimer.singleShot(0, _apply_geometry)

    def _is_running_under_wayland(self) -> bool:
        """Detect whether the current Qt-platform is Wayland.

        Order of checks:
          1. The XDG_SESSION_TYPE env var (most reliable on
             modern distros - GDM/SDDM/etc. set this explicitly)
          2. Qt's own QGuiApplication.platformName() which
             returns 'wayland' / 'xcb' depending on what Qt
             was actually told to use (this catches WAYLAND_DISPLAY
             override scenarios where the session is X11 but
             Qt was asked to use the Wayland backend via
             QT_QPA_PLATFORM=wayland)
        Either match counts as Wayland.
        """
        import os
        if os.environ.get("XDG_SESSION_TYPE", "").lower() \
                == "wayland":
            return True
        try:
            from PyQt6.QtGui import QGuiApplication
            app = QGuiApplication.instance()
            if app is not None:
                pf = app.platformName()
                if pf and 'wayland' in pf.lower():
                    return True
        except Exception:
            pass
        return False

    # --- Saved-position helpers (minimal mode only) -------------
    def _minimal_position_path(self):
        """Where the saved position lives. Same config dir as
        Quopus itself, in a small dedicated file so we don't
        have to load and rewrite the whole quopus.cfg every
        time the user drags the window."""
        try:
            from .config import CONFIG_DIR
            from pathlib import Path
            return Path(CONFIG_DIR) / "u64_streamer_minimal_pos.json"
        except Exception:
            from pathlib import Path
            return Path.home() / ".u64_streamer_minimal_pos.json"

    def _load_minimal_position(self):
        """Return [x, y] from the saved-position file, or None
        if no saved position exists or the file is unreadable."""
        import json
        p = self._minimal_position_path()
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            x = int(data.get("x"))
            y = int(data.get("y"))
            return (x, y)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save_minimal_position(self):
        """Persist the current window position to the saved-
        position file. Called from mouseReleaseEvent at the end
        of a drag - we don't write on every mouseMove because
        that would hammer the disk."""
        import json
        p = self._minimal_position_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            pos = self.pos()
            p.write_text(
                json.dumps({"x": pos.x(), "y": pos.y()}),
                encoding="utf-8")
        except OSError:
            pass

    # --- Mouse-drag support (minimal mode only) ------------------
    def mousePressEvent(self, ev):
        """Start a window-drag when the user left-clicks in
        minimal mode. In other modes mouse press has its normal
        behavior (focus widget, etc) - we only intercept when
        _minimal_mode is True.

        Implementation: prefer Qt's startSystemMove() over a
        manual move loop. That's the right pattern for frameless
        windows since Qt 5.15 - it asks the underlying window
        manager (X11, Wayland, win32, AppKit) to handle the drag
        natively. Big advantages:

          - Works on Wayland, where apps are NOT allowed to set
            their own window position via self.move(). Wayland
            clients can only request a drag-and-let-the-
            compositor-handle-it via this exact API.
          - Native snap-to-edge behavior on Windows, Aero Snap,
            half-screen tiling on GNOME etc - all happen for
            free because the WM owns the drag.
          - No fight between our move() loop and the WM's own
            window movement events.

        Fallback: if startSystemMove() returns False (very old
        Qt, unusual platform), fall back to the manual offset-
        based move loop. mouseMoveEvent then uses _minimal_drag_offset.
        """
        from PyQt6.QtCore import Qt
        if (getattr(self, '_minimal_mode', False)
                and ev.button() == Qt.MouseButton.LeftButton):
            wh = self.windowHandle()
            if wh is not None and hasattr(wh, 'startSystemMove'):
                # Native path - WM takes over from here. Our
                # mouseMoveEvent will not even be called for
                # this drag.
                started = wh.startSystemMove()
                if started:
                    ev.accept()
                    # We won't get a mouseReleaseEvent for the
                    # drag because the WM is handling it - so
                    # save the position right after the move
                    # finishes via a short timer.
                    from PyQt6.QtCore import QTimer
                    QTimer.singleShot(
                        300, self._save_minimal_position)
                    return
            # Manual fallback path
            self._minimal_drag_offset = (
                ev.globalPosition().toPoint() - self.pos())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        """Continue a window-drag while the left button is held
        and we're in minimal mode."""
        from PyQt6.QtCore import Qt
        if (getattr(self, '_minimal_mode', False)
                and self._minimal_drag_offset is not None
                and (ev.buttons() & Qt.MouseButton.LeftButton)):
            new_pos = (ev.globalPosition().toPoint()
                       - self._minimal_drag_offset)
            self.move(new_pos)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        """Finish a window-drag and persist the new position."""
        from PyQt6.QtCore import Qt
        if (getattr(self, '_minimal_mode', False)
                and self._minimal_drag_offset is not None
                and ev.button() == Qt.MouseButton.LeftButton):
            self._minimal_drag_offset = None
            self._save_minimal_position()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        """Right-click anywhere in the window.

        In minimal mode (set by enter_minimal_mode), pop a
        "Close streamer?" Yes/No confirmation. Yes closes the
        window, No simply dismisses the popup. In any other
        mode the event falls through to Qt's default (which is
        a no-op for QDialog - none of the chrome widgets handle
        a context menu either).
        """
        if not getattr(self, '_minimal_mode', False):
            super().contextMenuEvent(ev)
            return
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Close Streamer?",
            "Close the U64 streamer?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.close()
        ev.accept()

    def _on_ontop_toggle(self, checked: bool):
        """Toggle Qt's WindowStaysOnTopHint at runtime."""
        self._always_on_top = checked
        self._apply_always_on_top()
        # Persist to main config.
        self._save_config_value('u64_always_on_top', checked)

    def _apply_always_on_top(self):
        """Apply the current self._always_on_top to the window
        flags. Has to re-show() the window because Qt rebuilds the
        native window when flags change."""
        from PyQt6.QtCore import Qt
        flags = self.windowFlags()
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()    # re-show after flag change

    def resizeEvent(self, ev):
        """When the window is resized, repaint at the new size so
        the picture follows. Cheap because we just rescale the
        existing framebuf - no UDP work involved.

        Also reposition the floating cinema-toggle button if
        cinema mode is currently active, so it stays in the top-
        right corner of the new size.
        """
        super().resizeEvent(ev)
        try:
            self._refresh_video_widget()
        except Exception:
            pass
        if self._cinema_mode and self.btn_cinema.parent() is self:
            self.btn_cinema.move(self.width() - 170, 6)

    def _save_config_value(self, key, value):
        """Helper to persist one key into the main Quopus config
        from anywhere (toolbar toggles, cinema mode, etc)."""
        try:
            mw = self.parent()
            if mw is not None and hasattr(mw, 'config'):
                mw.config[key] = value
                from .config import save_config
                save_config(mw.config)
        except Exception:
            pass

    def _on_config(self):
        """Open the U64Config dialog. If the user hits OK we apply
        the new values, persist them in the main Quopus config, and
        update the on-screen status. If a stream is currently
        running we stop and restart it so the workers pick up the
        new ports."""
        # Pre-fill the screenshot field from the main config if any
        screenshot_dir = ""
        try:
            mw = self.parent()
            if mw is not None and hasattr(mw, 'config'):
                screenshot_dir = mw.config.get(
                    'u64_screenshot_dir', '')
        except Exception:
            pass
        dlg = U64ConfigDialog(
            self._host, self._video_port, self._audio_port,
            self._telnet_port,
            http_port=self._http_port,
            password=self._password,
            video_only=self._video_only,
            always_on_top=self._always_on_top,
            screenshot_dir=screenshot_dir,
            parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        was_running = self.btn_stop.isEnabled()
        if was_running:
            # Stop cleanly so sockets unbind before we rebind on
            # the new ports.
            self._on_stop()
        self._host = v['u64_host']
        self._video_port = v['u64_video_port']
        self._audio_port = v['u64_audio_port']
        self._telnet_port = v['u64_telnet_port']
        self._http_port = v['u64_http_port']
        self._password = v['u64_password']
        self._video_only = v['u64_video_only']
        # If the keyboard worker is alive and the user just
        # changed host/port/password, tear it down so the next
        # keystroke spawns a fresh worker pointed at the new
        # target. Cheaper than trying to mutate the worker's
        # state from outside the thread.
        kb = getattr(self, "_kb_worker", None)
        if kb is not None:
            try:
                kb.stop()
                kb.wait(1500)
            except Exception:
                pass
            self._kb_worker = None
        # Always-on-top: apply NOW + sync the toolbar toggle.
        new_top = v['u64_always_on_top']
        if new_top != self._always_on_top:
            self._always_on_top = new_top
            self.btn_ontop.blockSignals(True)
            self.btn_ontop.setChecked(new_top)
            self.btn_ontop.blockSignals(False)
            self._apply_always_on_top()
        self.lbl_host.setText(self._host_display())
        # Persist to main config so the next Quopus session
        # remembers them. Multi-device aware: if the dialog
        # returned a devices list (any modern build), persist
        # the whole list AND the active index. Otherwise just
        # the legacy keys.
        try:
            mw = self.parent()
            if mw is not None and hasattr(mw, 'config'):
                # New schema first - takes effect on next read
                # via u64_devices.get_devices().
                if 'u64_devices' in v:
                    mw.config['u64_devices'] = v['u64_devices']
                if 'u64_active_device' in v:
                    mw.config['u64_active_device'] = (
                        v['u64_active_device'])
                # Legacy keys (mirror the active device).
                mw.config['u64_host']           = self._host
                mw.config['u64_video_port']     = self._video_port
                mw.config['u64_audio_port']     = self._audio_port
                mw.config['u64_telnet_port']    = self._telnet_port
                mw.config['u64_http_port']      = self._http_port
                mw.config['u64_password']       = self._password
                mw.config['u64_video_only']     = self._video_only
                mw.config['u64_always_on_top']  = self._always_on_top
                mw.config['u64_screenshot_dir'] = v['u64_screenshot_dir']
                # Final sync: make sure the legacy keys match
                # the active device in the new list, in case the
                # user picked a different active slot.
                try:
                    from .u64_devices import sync_legacy_keys
                    sync_legacy_keys(mw.config)
                    # Re-read self._host etc from the synced
                    # config so the lbl_host display lines up.
                    self._host = mw.config.get('u64_host', '')
                    self._video_port = int(
                        mw.config.get('u64_video_port',
                                      PORT_VIDEO))
                    self._audio_port = int(
                        mw.config.get('u64_audio_port',
                                      PORT_AUDIO))
                    self._telnet_port = int(
                        mw.config.get('u64_telnet_port',
                                      PORT_TELNET))
                    self._http_port = int(
                        mw.config.get('u64_http_port',
                                      PORT_HTTP))
                    self._password = mw.config.get(
                        'u64_password', '')
                except Exception:
                    pass
                self.lbl_host.setText(self._host_display())
                from .config import save_config
                save_config(mw.config)
        except Exception:
            pass
        if was_running:
            self._on_start()

    def set_autoclose_watch(self, address: int, value: int,
                              interval_seconds: int = 60):
        """Poll an address every `interval_seconds` seconds. When the
        byte at that address equals `value`, close the streamer window.

        Used in standalone mode where the streamer is launched by an
        external process (e.g. a BBS user session) and should
        auto-quit when that process signals it's done by clearing /
        setting a flag in C64 memory.

        Implementation: simple QTimer that runs u64_readmem with
        length=1. The first read happens after `interval_seconds`,
        not immediately - so the user has time to see the stream come
        up. We re-create the watch on every Start so a Reset+Restart
        cycle keeps polling.
        """
        self._autoclose_addr = address & 0xFFFF
        self._autoclose_value = value & 0xFF
        self._autoclose_interval_ms = interval_seconds * 1000
        if not hasattr(self, '_autoclose_timer'):
            self._autoclose_timer = QTimer(self)
            self._autoclose_timer.timeout.connect(self._on_autoclose_tick)
        self._autoclose_timer.start(self._autoclose_interval_ms)

    def _on_autoclose_tick(self):
        """Timer-Tick: 1 Byte von der Auto-Close-Adresse lesen, mit
        dem Trigger-Value vergleichen. Wenn match -> close().

        Fehlerfall (kein Host, Netzwerkfehler, etc): wir loggen kurz
        im Status-Label und ticken weiter. Anders als bei Live-Mode
        ist der Tick-Interval hier 60s, da koennen wir uns einen
        gelegentlich gescheiterten Read leisten ohne dass der
        Mechanismus haengt.
        """
        if not self._host:
            return
        try:
            ok, result = u64_readmem(
                self._host, self._autoclose_addr, 1,
                password=self._password, port=self._http_port)
            if not ok:
                # Status-Hinweis aber nicht abbrechen.
                self.lbl_stats.setText(
                    f"  autoclose read failed: {result}  ")
                return
            current = result[0] if result else None
            if current == self._autoclose_value:
                # Match - Fenster schliessen. Wir stoppen erst den
                # Timer damit kein zweiter Tick anlaeuft waehrend
                # close() processed wird.
                self._autoclose_timer.stop()
                self.close()
        except Exception as e:
            self.lbl_stats.setText(
                f"  autoclose error: {e}  ")

    def _on_start(self):
        """Spin up the workers AND tell the U64 to start streaming
        if we have a host configured.

        Two paths to start the stream on the U64:
          1. Modern REST API (firmware >= 3.11):
             PUT /v1/streams/video:start + /v1/streams/audio:start
             One HTTP call each, no timing-sensitive byte
             sequences. Preferred if it works.
          2. Old telnet menu navigation:
             Send F5 + N down-arrows + Enter to the menu via TCP
             port 23. Used as fallback if HTTP fails (usually
             because the firmware is too old or the HTTP service
             is disabled in U64 settings).

        We always start our own UDP receiver workers regardless of
        whether the U64-side stream-start succeeds - so if the user
        starts it manually from the menu, video still appears here.
        """
        # Start workers if not already running. Each gets the
        # currently-configured port at construction time.
        if self._video_worker is None:
            self._video_worker = _VideoWorker(
                self._framebuf, port=self._video_port)
            self._video_worker.frame_ready.connect(
                self._refresh_video_widget)
            self._video_worker.stats.connect(self._on_stats)
            self._video_worker.error.connect(self._on_error)
            self._video_worker.start()
        # Skip audio in video-only mode - don't bind UDP, don't
        # start the QAudioSink, don't ask the U64 for an audio
        # stream below.
        if (AUDIO_AVAILABLE and self._audio_worker is None
                and not self._video_only):
            self._audio_worker = _AudioWorker(port=self._audio_port)
            self._audio_worker.audio_chunk.connect(self._on_audio)
            self._audio_worker.error.connect(self._on_error)
            self._audio_worker.start()
            self._init_audio_sink()

        if self._host:
            # Try the REST API first - one PUT each for video and
            # audio. Don't pass target_ip; the U64 will default to
            # the source IP of the HTTP request which is us. If
            # the user has set non-default UDP ports we DO have to
            # pass them so the U64 knows where to send.
            ok_v, err_v = u64_stream_start(
                self._host, "video",
                target_port=(self._video_port
                                if self._video_port != PORT_VIDEO else 0),
                password=self._password, port=self._http_port)
            if self._video_only:
                # Don't request the audio stream at all - skip the
                # API call so the U64 doesn't bother sending audio
                # packets. Treat as if audio "succeeded" for the
                # combined-status logic below.
                ok_a, err_a = True, ""
            else:
                ok_a, err_a = u64_stream_start(
                    self._host, "audio",
                    target_port=(self._audio_port
                                    if self._audio_port != PORT_AUDIO else 0),
                    password=self._password, port=self._http_port)
            mode_tag = " (video only)" if self._video_only else ""
            if ok_v and ok_a:
                self.lbl_stats.setText(
                    f"  REST API: streams started{mode_tag}  ")
            elif ok_v or ok_a:
                missing = "audio" if ok_v else "video"
                self.lbl_stats.setText(
                    f"  REST API: video+audio partial "
                    f"({missing} failed)  ")
            else:
                # REST failed - fall back to telnet menu. Common
                # reasons: HTTP service disabled, old firmware,
                # network password wrong.
                self.lbl_stats.setText(
                    "  REST failed, trying telnet menu...  ")
                ok_t, err_t = send_telnet_sequence(
                    self._host, SEQ_START_STREAM,
                    port=self._telnet_port)
                if not ok_t:
                    QMessageBox.warning(
                        self, "Start stream",
                        f"Could not start the U64 stream.\n\n"
                        f"REST API: {err_v}\n"
                        f"Telnet: {err_t}\n\n"
                        f"The receiver is still listening - if you "
                        f"start the stream from the U64 menu manually, "
                        f"video should still appear here.")
                    self.lbl_stats.setText(
                        "  start failed - listening anyway  ")
                else:
                    self.lbl_stats.setText(
                        "  Telnet: stream started (REST failed)  ")
        else:
            self.lbl_stats.setText(
                "  Listening on UDP - start stream from U64 menu  ")

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _on_stop(self):
        """Tell the U64 to stop, then shut down our workers.

        REST API first (PUT /v1/streams/video:stop +
        /v1/streams/audio:stop), telnet fallback if that fails."""
        if self._host:
            ok_v, _ = u64_stream_stop(
                self._host, "video",
                password=self._password, port=self._http_port)
            if self._video_only:
                ok_a = True   # didn't start it, don't stop it
            else:
                ok_a, _ = u64_stream_stop(
                    self._host, "audio",
                    password=self._password, port=self._http_port)
            if not (ok_v or ok_a):
                # Both REST stops failed - try telnet sequence.
                send_telnet_sequence(
                    self._host, SEQ_STOP_STREAM,
                    port=self._telnet_port)
        self._shutdown_workers()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._fill_no_signal()
        self._refresh_video_widget()
        self.lbl_stats.setText("  stopped  ")

    def _on_reset(self):
        """Reset the C64. Uses REST API when possible (firmware >=
        3.11) and falls back to telnet menu navigation otherwise."""
        if not self._host:
            QMessageBox.information(
                self, "Reset",
                "Set the U64's IP first (Config...) so we can talk "
                "to it.")
            return
        # Try REST first
        ok, err = u64_reset(self._host, password=self._password,
                              port=self._http_port)
        if ok:
            return
        # Fallback to telnet menu navigation (older firmware)
        ok, err = send_telnet_sequence(
            self._host, SEQ_RESET, port=self._telnet_port)
        if not ok:
            QMessageBox.warning(self, "Reset", err)

    def _on_pause_toggle(self, checked):
        """Pause = freeze CPU via DMA line. Unpause = release DMA."""
        if not self._host:
            self.btn_pause.blockSignals(True)
            self.btn_pause.setChecked(False)
            self.btn_pause.blockSignals(False)
            QMessageBox.information(
                self, "Pause",
                "Set the U64's IP first (Config...).")
            return
        if checked:
            ok, err = u64_pause(self._host, password=self._password,
                                  port=self._http_port)
            if not ok:
                self.btn_pause.blockSignals(True)
                self.btn_pause.setChecked(False)
                self.btn_pause.blockSignals(False)
                QMessageBox.warning(self, "Pause",
                    f"Failed to pause:\n{err}")
            else:
                self.btn_pause.setText("Resume")
        else:
            ok, err = u64_resume(self._host, password=self._password,
                                    port=self._http_port)
            if not ok:
                QMessageBox.warning(self, "Resume",
                    f"Failed to resume:\n{err}")
            self.btn_pause.setText("Pause")

    def _on_menu_button(self):
        if not self._host:
            QMessageBox.information(
                self, "Menu",
                "Set the U64's IP first (Config...).")
            return
        ok, err = u64_menu_button(self._host,
                                      password=self._password,
                                      port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Menu button",
                f"Failed:\n{err}")

    def _on_reboot(self):
        if not self._host:
            QMessageBox.information(
                self, "Reboot",
                "Set the U64's IP first (Config...).")
            return
        reply = QMessageBox.question(
            self, "Reboot Ultimate firmware",
            "Reboot the Ultimate firmware?\n\n"
            "This restarts the whole Ultimate (re-init's "
            "cartridge config and resets the C64).",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, err = u64_reboot(self._host, password=self._password,
                                port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Reboot",
                f"Failed to reboot:\n{err}")

    def _on_poweroff(self):
        if not self._host:
            QMessageBox.information(
                self, "Power off",
                "Set the U64's IP first (Config...).")
            return
        reply = QMessageBox.question(
            self, "Power off Ultimate 64",
            "Power off the Ultimate 64?\n\n"
            "You will need to physically power it back on.\n"
            "(Ultimate-II+ does not support this command.)",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, err = u64_poweroff(self._host, password=self._password,
                                  port=self._http_port)
        if not ok:
            QMessageBox.warning(self, "Power off",
                f"Failed to power off:\n{err}")

    def _on_screenshot(self):
        """Grab the current video frame and save as PNG.

        Save location resolution order:
        1. The path stored under config key `u64_screenshot_dir`
           (set via the Streamer's Config... dialog, "Screenshot
           folder..." button).
        2. <quopus_project>/screenshots/ if the project root is
           writable (default for a normal install).
        3. ~/Pictures/Ultimate64/ as a portable fallback.
        4. ~/ as a last resort.

        Filename: u64_<timestamp>.png with millisecond precision so
        rapid clicks don't collide.

        Requires the video stream to be active (we capture the last
        rendered pixmap from the video widget).
        """
        # Find pixmap source - the video widget renders frames into
        # self.video_label (a QLabel with a QPixmap)
        pm = None
        if hasattr(self, 'video_label') and self.video_label:
            pm = self.video_label.pixmap()
        if pm is None or pm.isNull():
            QMessageBox.information(
                self, "Screenshot",
                "No video frame to capture.\n\n"
                "Start the video stream first (Start button).")
            return
        # Resolve output folder
        import os, datetime
        out_dir = self._resolve_screenshot_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Screenshot",
                f"Failed to create output folder:\n{out_dir}\n{e}")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"u64_{ts}.png"
        full_path = os.path.join(out_dir, filename)
        if pm.save(full_path, "PNG"):
            # Brief status in the stats label, no popup
            if hasattr(self, 'lbl_stats'):
                self.lbl_stats.setText(f"  saved {filename}  ")
        else:
            QMessageBox.warning(self, "Screenshot",
                f"Qt couldn't write:\n{full_path}")

    def _resolve_screenshot_dir(self):
        """Pick a screenshot output folder per the lookup order
        documented on _on_screenshot. Always returns a path string -
        directory creation is the caller's responsibility."""
        import os
        # 1) Explicit config setting (absolute path)
        try:
            from .config import load_config
            cfg = load_config()
            explicit = cfg.get("u64_screenshot_dir", "").strip()
        except Exception:
            explicit = ""
        if explicit:
            return explicit
        # 2) <quopus_project>/screenshots/
        try:
            from .config import SCRIPT_DIR
            proj = SCRIPT_DIR / "screenshots"
            # Verify the project root is writable - if quopus is in
            # an install-only location (e.g. /opt/quopus), fall
            # through to the portable fallback.
            if os.access(str(SCRIPT_DIR), os.W_OK):
                return str(proj)
        except Exception:
            pass
        # 3) ~/Pictures/Ultimate64/
        home = os.path.expanduser("~")
        pictures = os.path.join(home, "Pictures")
        if os.path.isdir(pictures):
            return os.path.join(pictures, "Ultimate64")
        # 4) Last resort
        return home

    # =================================================================
    # Video recording
    # =================================================================
    def _on_record_toggle(self, checked: bool):
        """Rec button clicked. Toggle between idle and recording."""
        # Don't trust just the checked state - we also need a worker
        # to consider ourselves "recording".
        is_running = self._video_recorder is not None
        if checked and not is_running:
            self._start_recording()
        elif not checked and is_running:
            self._stop_recording()
        else:
            # State drift (e.g. Stop button was hit during a
            # recording). Sync the button to reality.
            self.btn_record.setChecked(is_running)

    def _on_record_context_menu(self, pos):
        """Right-click on Rec: pick output format for the next
        recording. Disabled while a recording is in progress."""
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtGui import QCursor
        if self._video_recorder is not None:
            self.lbl_stats.setText(
                "  Stop the current recording before changing format  ")
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #a0a0a0; color: #000000; "
            "border: 1px solid #000000; "
            "font-family: 'Topaz','Courier New',monospace; } "
            "QMenu::item { padding: 4px 24px; } "
            "QMenu::item:selected { background-color: #2040a0; "
            "color: white; }")
        # Show current selection with a marker. Items set the
        # preference + persist it.
        mark_mp4 = "● " if self._record_format == "mp4" else "  "
        mark_seq = "● " if self._record_format == "png_seq" else "  "
        a_mp4 = menu.addAction(f"{mark_mp4}MP4 (H.264 via ffmpeg)")
        a_seq = menu.addAction(
            f"{mark_seq}PNG sequence (no ffmpeg, larger)")
        menu.addSeparator()
        # Quick info action - tells the user whether ffmpeg is
        # actually available so they don't pick MP4 in vain.
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            menu.addAction(f"ffmpeg: {ffmpeg}").setEnabled(False)
        else:
            menu.addAction(
                "ffmpeg: NOT FOUND on PATH"
            ).setEnabled(False)
        chosen = menu.exec(QCursor.pos())
        if chosen is a_mp4:
            self._record_format = "mp4"
        elif chosen is a_seq:
            self._record_format = "png_seq"
        else:
            return
        # Persist so the choice survives a restart.
        try:
            from .config import load_config, save_config
            cfg = load_config()
            cfg["u64_record_format"] = self._record_format
            save_config(cfg)
        except Exception:
            pass
        self.lbl_stats.setText(
            f"  Next recording: {self._record_format.upper()}  ")

    def _start_recording(self):
        """Spin up a _VideoRecorder thread and wire it to the
        live frame stream. The video must be running - otherwise
        the recorder would start with the no-signal pattern as
        its first frame."""
        if self._video_worker is None:
            self.btn_record.setChecked(False)
            self.lbl_stats.setText(
                "  Start the video stream first (Start)  ")
            return
        # Resolve output path
        import os, datetime
        out_dir = self._resolve_screenshot_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            self.btn_record.setChecked(False)
            QMessageBox.warning(self, "Record",
                f"Failed to create output folder:\n{out_dir}\n{e}")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if self._record_format == "mp4":
            out_path = os.path.join(out_dir, f"u64_{ts}.mp4")
        else:
            # PNG-sequence: each recording gets its own folder so
            # frame numbers don't collide across captures.
            out_path = os.path.join(out_dir, f"u64_{ts}")
        # Construct + wire up the worker. stats/error/stopped are
        # all routed to dedicated GUI-thread slots. Audio capture
        # is auto-disabled when the streamer is in video-only mode
        # since no audio packets will ever arrive in that case -
        # avoids producing a final MP4 with an empty audio track.
        record_audio = (self._record_format == "mp4"
                         and not self._video_only
                         and AUDIO_AVAILABLE)
        self._video_recorder = _VideoRecorder(
            out_path, mode=self._record_format,
            record_audio=record_audio)
        self._video_recorder.stats.connect(self._on_record_stats)
        self._video_recorder.error.connect(self._on_record_error)
        self._video_recorder.stopped.connect(self._on_record_stopped)
        self._video_recorder.start()
        # Mark the button as "recording" (red) by switching styles.
        # We use the existing red button style for visual consistency
        # with the Off / Delete buttons.
        self.btn_record.setText("● Rec")
        self.btn_record.setStyleSheet(button_qss("red"))
        self.btn_record.setChecked(True)
        if self._record_format == "mp4":
            audio_note = "+audio" if record_audio else "video-only"
            suffix = f".mp4 ({audio_note})"
        else:
            suffix = " (PNG-seq)"
        self.lbl_stats.setText(
            f"  ● REC starting -> {os.path.basename(out_path)}{suffix}  ")

    def _stop_recording(self):
        """Signal the recorder to drain and exit. Don't block the
        GUI - the stopped() signal lands in _on_record_stopped
        when the encoder has finished."""
        if self._video_recorder is None:
            return
        try:
            self._video_recorder.stop()
        except Exception:
            pass
        self.btn_record.setText("Rec…")
        self.btn_record.setEnabled(False)
        # We DON'T setStyleSheet back to blue here - leave it red
        # until stopped() arrives, so the user can see the encoder
        # is still flushing. _on_record_stopped resets the look.

    def _on_record_stats(self, frames: int, secs: float):
        """Recorder progress tick. Shown in the same stats label
        that normally holds packet counts; restored when recording
        stops."""
        # Round to 1 decimal for less flicker. Avg fps = frames/secs
        # gives a useful health indicator (drops below ~45 = the
        # GUI is choking on something).
        if secs > 0:
            avg = frames / secs
            self.lbl_stats.setText(
                f"  ● REC {secs:5.1f}s  {frames:5d}f  "
                f"{avg:4.1f} fps  ")

    def _on_record_error(self, msg: str):
        """Non-fatal recorder error (e.g. ffmpeg missing, falling
        back to PNG-seq). Show in the stats label and a popup
        only for show-stoppers."""
        self.lbl_stats.setText(f"  REC: {msg[:120]}  ")
        # If the error mentions ffmpeg fallback, don't pop a dialog -
        # the recorder will keep going with PNG-seq.
        if "Falling back" in msg or "PNG sequence" in msg:
            return
        QMessageBox.warning(self, "Recording", msg)

    def _on_record_stopped(self, final_path: str):
        """Recorder thread has finished its run() and emitted
        stopped(). Reset the button to idle and report the saved
        path."""
        # Wait for the QThread to truly exit so we can dispose
        # safely. Short timeout since stop() should have been
        # called already.
        try:
            self._video_recorder.wait(3000)
        except Exception:
            pass
        self._video_recorder = None
        self.btn_record.setText("Rec")
        self.btn_record.setStyleSheet(button_qss("blue"))
        self.btn_record.setChecked(False)
        self.btn_record.setEnabled(True)
        import os
        if final_path:
            name = os.path.basename(final_path)
            self.lbl_stats.setText(f"  saved {name}  ")
        else:
            self.lbl_stats.setText("  recording aborted  ")

    def _on_reset_old_telnet(self):
        """Legacy telnet-menu-based reset, kept as fallback."""
        if not self._host:
            QMessageBox.information(
                self, "Reset",
                "Set the U64's IP first (Config...) so we can talk "
                "to its telnet menu.")
            return
        ok, err = send_telnet_sequence(
            self._host, SEQ_RESET, port=self._telnet_port)
        if not ok:
            QMessageBox.warning(self, "Reset", err)

    def _on_mount_dialog(self):
        if not self._host:
            QMessageBox.information(self, "Mount",
                "Set the U64's IP first (Config...).")
            return
        dlg = U64MountDialog(
            self._host, self._http_port, self._password, self)
        dlg.exec()

    def _on_drives_dialog(self):
        if not self._host:
            QMessageBox.information(self, "Drives",
                "Set the U64's IP first (Config...).")
            return
        dlg = U64DriveStatusDialog(
            self._host, self._http_port, self._password, self)
        dlg.exec()

    def _on_cfg_editor(self):
        if not self._host:
            QMessageBox.information(self, "Config Editor",
                "Set the U64's IP first (Config...).")
            return
        dlg = U64ConfigEditorDialog(
            self._host, self._http_port, self._password, self)
        dlg.exec()

    def _on_backup_dialog(self):
        if not self._host:
            QMessageBox.information(self, "Backup",
                "Set the U64's IP first (Config...).")
            return
        dlg = U64BackupDialog(
            self._host, self._http_port, self._password, self)
        dlg.exec()

    def _on_basic_editor(self):
        if not self._host:
            QMessageBox.information(self, "BASIC Editor",
                "Set the U64's IP first (Config...) so we can\n"
                "send programs to it.")
            return
        from .basic_editor import BasicEditorDialog

        def send_cb(prg_bytes, line_count):
            ok, msg = u64_run_prg(
                self._host, prg_bytes,
                password=self._password, port=self._http_port)
            if not ok:
                raise RuntimeError(f"REST API: {msg}")

        dlg = BasicEditorDialog(self, send_callback=send_cb)
        dlg.show()
        # Non-modal so the user can use both the editor and the
        # streamer at the same time.

    def _on_asm64_browser(self):
        """Open the Assembly64 browser as a modeless dialog. File
        actions are wired to the streamer's host so Run / Mount go
        directly to the device."""
        from .asm64_browser import make_browser_dialog

        host = self._host
        password = self._password
        port = self._http_port

        def run_cb(file_bytes, filename):
            if not host:
                raise RuntimeError(
                    "No U64 host configured - set it in Config...")
            ext = filename.rsplit(".", 1)[-1].lower() \
                if "." in filename else ""
            if ext in ("crt",):
                ok, msg = u64_run_crt(host, file_bytes,
                                          password=password, port=port)
            elif ext in ("sid",):
                ok, msg = u64_play_sid(host, file_bytes,
                                           password=password, port=port)
            elif ext in ("mod",):
                ok, msg = u64_play_mod(host, file_bytes,
                                           password=password, port=port)
            elif ext in ("d64", "d71", "d81", "g64", "g71", "g81"):
                # Disk image: mount on Drive A read-only, reset, wait
                # for the BASIC prompt to come up, then inject
                # LOAD"*",8,1 + RUN so the first file boots
                # automatically.
                #
                # The "*" wildcard loads the first file on the disk,
                # whatever it's called. The ",1" suffix tells the
                # KERNAL to honor the file's own load address (",0"
                # would load at $0801 regardless) - this matches what
                # most cracked games and demos expect.
                #
                # Reset->BASIC ready takes about 2.0-2.5s on a real
                # C64; the U64 boots faster but we wait 2.5s to be
                # safe. The keyboard buffer chunking adds another
                # ~0.4s per line.
                import time

                def _post_status(msg):
                    """Push status into the streamer's stats label
                    via Qt's queued-connection thread marshalling.
                    Direct .setText would touch the widget from the
                    wrong thread."""
                    try:
                        from PyQt6.QtCore import QMetaObject, Q_ARG, Qt
                        if hasattr(self, 'lbl_stats'):
                            QMetaObject.invokeMethod(
                                self.lbl_stats, "setText",
                                Qt.ConnectionType.QueuedConnection,
                                Q_ARG(str, f"  {msg}  "))
                    except Exception:
                        pass

                _post_status("mounting disk...")
                ok, msg = u64_mount_disk(
                    host, file_bytes, drive='a', mode='readonly',
                    disk_type=ext, password=password, port=port)
                if not ok:
                    raise RuntimeError(f"Mount failed: {msg}")

                _post_status("resetting C64...")
                # u64_reset returns (ok, msg). On some firmware
                # builds a :reset triggers connection-reset-by-peer
                # which we tolerate - the reset itself happens
                # before the response is flushed.
                try:
                    rok, rmsg = u64_reset(host, password=password,
                                              port=port)
                    if not rok:
                        # Log but continue - the reset packet usually
                        # made it through even if the response failed
                        _post_status(f"reset warning: {rmsg[:40]}")
                except Exception as e:
                    _post_status(f"reset exception: {str(e)[:40]}")

                _post_status("waiting for BASIC ready (2.5s)...")
                time.sleep(2.5)

                _post_status('typing LOAD"*",8,1...')
                ok, msg = u64_type_text(
                    host, 'LOAD"*",8,1\r',
                    password=password, port=port)
                if not ok:
                    raise RuntimeError(f"LOAD type failed: {msg}")

                # SEARCHING / LOADING / READY takes about 2-4s on a
                # real 1541; the U64's virtual drive is near-instant
                # but Basic's screen still needs time to print "READY."
                # and accept new keys. 2.0s is a safe middle.
                _post_status("loading (2.0s)...")
                time.sleep(2.0)

                _post_status("typing RUN...")
                ok, msg = u64_type_text(
                    host, 'RUN\r',
                    password=password, port=port)
                if not ok:
                    raise RuntimeError(f"RUN type failed: {msg}")
                _post_status("disk launched")
            else:
                # PRG / P00 / unknown - try PRG
                ok, msg = u64_run_prg(host, file_bytes,
                                          password=password, port=port)
            if not ok:
                raise RuntimeError(f"REST: {msg}")

        def mount_cb(file_bytes, filename, drive='a', mode='readonly'):
            if not host:
                raise RuntimeError("No U64 host configured")
            ext = filename.rsplit(".", 1)[-1].lower() \
                if "." in filename else ""
            ok, msg = u64_mount_disk(
                host, file_bytes, drive=drive, mode=mode,
                disk_type=ext, password=password, port=port)
            if not ok:
                raise RuntimeError(f"Mount: {msg}")
            # Reset so the C64 sees the new disk
            u64_reset(host, password=password, port=port)

        dlg = make_browser_dialog(self,
                                       on_run=run_cb,
                                       on_mount=mount_cb)
        dlg.show()

    def _on_memory_grab(self):
        """Frag Start- und End-Adresse ab, dann oeffne den
        MemoryViewDialog der den eigentlichen Read im Worker macht.

        Eingaben akzeptieren hex ($0800, 0x0800, 0800) oder dezimal
        mit '.'-Praefix. Defaults: $0800..$FFFF = das ganze C64-RAM
        ab BASIC-Start (vor $0800 liegen Zero-Page + Stack + System-
        Vektoren, die will man selten dumpen).
        """
        if not self._host:
            QMessageBox.information(
                self, "Read memory",
                "Set the U64's IP first (Config...) so we can talk "
                "to the REST API.")
            return

        addr_str, ok = QInputDialog.getText(
            self, "Read memory - start address",
            "Start address (hex, e.g. $0800 or 0800):",
            text="$0800")
        if not ok or not addr_str.strip():
            return
        try:
            address = _parse_c64_address(addr_str)
        except ValueError as e:
            QMessageBox.warning(self, "Read memory", str(e))
            return

        end_str, ok = QInputDialog.getText(
            self, "Read memory - end address",
            f"End address (inclusive, must be >= ${address:04X}):",
            text="$FFFF")
        if not ok or not end_str.strip():
            return
        try:
            end_addr = _parse_c64_address(end_str)
        except ValueError as e:
            QMessageBox.warning(self, "Read memory", str(e))
            return

        if end_addr < address:
            QMessageBox.warning(
                self, "Read memory",
                f"End address ${end_addr:04X} is before "
                f"start address ${address:04X}.")
            return

        length = end_addr - address + 1

        # Non-modal mit parent=None (siehe MemoryViewDialog) - das
        # Memory-Fenster ist selbststaendig und bleibt offen wenn
        # der Streamer minimiert wird. Wir halten eine Ref am
        # Streamer-Widget damit der GC den Dialog nicht killt.
        backend = _U64MemoryBackend(
            self._host, self._http_port, self._password)
        dlg = MemoryViewDialog(
            self, backend=backend,
            address=address, length=length)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not hasattr(self, '_detached_dialogs'):
            self._detached_dialogs = []
        self._detached_dialogs.append(dlg)
        dlg.destroyed.connect(
            lambda _obj=None, d=dlg: (
                self._detached_dialogs.remove(d)
                if d in self._detached_dialogs else None))
        dlg.show()

    def _on_scale_changed(self, idx):
        self._scale = self.cmb_scale.itemData(idx)
        self.resize(FRAME_W * self._scale + 20,
                      FRAME_H * self._scale + 90)
        self._refresh_video_widget()

    def _on_stats(self, pps, bps):
        self._video_pps = pps
        self._video_bps = bps
        kb = bps / 1024.0
        if kb > 1024:
            kb_s = f"{kb/1024.0:.1f} MB/s"
        else:
            kb_s = f"{kb:.0f} KB/s"
        self.lbl_stats.setText(
            f"  {pps} pkt/s   {kb_s}  ")

    def _on_error(self, msg):
        # Don't pop a dialog - that would block. Just update stats.
        self.lbl_stats.setText(f"  ERROR: {msg}  ")

    # ---- audio ------------------------------------------------------
    def _init_audio_sink(self):
        """Create the QAudioSink + buffer for streaming PCM out.
        48 kHz, S16LE, stereo - the U64's fixed format."""
        if not AUDIO_AVAILABLE:
            return
        try:
            fmt = QAudioFormat()
            fmt.setSampleRate(48000)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            device = QMediaDevices.defaultAudioOutput()
            self._audio_sink = QAudioSink(device, fmt)
            # ~100ms buffer is generous enough to absorb network
            # jitter without adding noticeable latency.
            self._audio_sink.setBufferSize(48000 * 2 * 2 // 10)
            self._audio_io = self._audio_sink.start()
        except Exception as e:
            # Audio device unavailable / busy / wrong format - just
            # disable. Video keeps going.
            self._audio_sink = None
            self._audio_io = None

    def _on_audio(self, chunk):
        """Push a 768-byte PCM chunk to the audio sink AND to the
        video recorder (if a recording is active). The recorder
        writes raw S16LE stereo into a WAV sidecar that gets muxed
        into the final MP4 on stop. Audio recording does NOT
        require the audio sink to be alive (e.g. user can record
        in video_only=False mode without playback)."""
        # First, fan out to the live audio recorder. We do this
        # BEFORE the sink write so a sink failure can't shadow
        # the recording.
        rec = getattr(self, '_video_recorder', None)
        if rec is not None:
            try:
                rec.push_audio(chunk)
            except Exception as e:
                print(f"  [u64 rec] push_audio failed: {e}")
        # Then drive the playback sink as before.
        if self._audio_io is None:
            return
        try:
            self._audio_io.write(chunk)
        except Exception:
            pass

    # ---- drag and drop autostart ------------------------------------
    # Files dropped onto the streamer window get uploaded to the
    # Ultimate64's REST API and run / mounted automatically. This
    # mirrors the behaviour of the official TSB U64Streamer.
    #
    # Supported drops:
    #   .prg           -> POST /v1/runners:run_prg     (load + run via DMA)
    #   .crt / .bin    -> POST /v1/runners:run_crt
    #   .sid           -> POST /v1/runners:sidplay
    #   .mod           -> POST /v1/runners:modplay
    #   .d64/d71/d81/g64 -> POST /v1/drives/a:mount    (drive A, readonly)
    #
    # Modifier rule for disk images:
    #   plain drop        -> mount + reset (autoboot)
    #   Ctrl+drop         -> mount only, no reset (disk swap)
    DND_RUNNERS = {
        ".prg": "run_prg",
        ".crt": "run_crt",
        ".bin": "run_crt",   # some cartridges are .bin
        ".sid": "sidplay",
        ".mod": "modplay",
    }
    DND_DISKS = {".d64", ".d71", ".d81", ".g64", ".g71"}

    def dragEnterEvent(self, ev):
        """Accept the drag if it carries file URLs. We don't filter
        by extension yet - that's done in dropEvent so we can give
        a clear error message for unsupported types instead of just
        silently rejecting the cursor."""
        md = ev.mimeData()
        if md is not None and md.hasUrls():
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        # Same logic as dragEnter; required for the cursor to stay
        # in "accept" state while moving over the window.
        if ev.mimeData() is not None and ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        """File(s) dropped: pick the first one with a recognised
        extension and run/mount it on the U64. Ctrl modifier gates
        the 'reset after mount' for disk images."""
        from PyQt6.QtCore import Qt as _Qt
        md = ev.mimeData()
        if md is None or not md.hasUrls():
            ev.ignore(); return
        if not self._host:
            QMessageBox.warning(
                self, "Drop file",
                "Cannot send file to U64 - no host configured.\n"
                "Use Config... to set the U64's IP address first.")
            ev.ignore(); return
        if self._dnd_worker is not None and self._dnd_worker.isRunning():
            QMessageBox.information(
                self, "Drop file",
                "Another file is still being uploaded - wait until "
                "it finishes.")
            ev.ignore(); return

        ctrl_held = bool(
            ev.modifiers() & _Qt.KeyboardModifier.ControlModifier)
        # Pick the first URL that's a real file with a known ext.
        chosen = None
        for url in md.urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in self.DND_RUNNERS or ext in self.DND_DISKS:
                chosen = p
                break
        if chosen is None:
            QMessageBox.information(
                self, "Drop file",
                "Drop a .prg, .crt, .sid, .mod, .d64, .d71, .d81 "
                "or .g64 file to send it to the U64.\n\n"
                "Hold Ctrl and drop a disk image to mount without "
                "resetting (manual disk swap).")
            ev.ignore(); return

        ev.acceptProposedAction()
        self._dispatch_dnd_file(chosen, ctrl_held)

    def _dispatch_dnd_file(self, path: Path, ctrl_held: bool):
        """Read the file, decide which API call to make, kick off
        the upload worker. Worker emits done(ok, msg) when finished."""
        ext = path.suffix.lower()
        try:
            data = path.read_bytes()
        except Exception as e:
            QMessageBox.warning(
                self, "Drop file",
                f"Cannot read {path.name}:\n{e}")
            return

        # 50 MB cap so the user doesn't accidentally upload a DVD
        # image. The U64 wouldn't accept it anyway.
        if len(data) > 50 * 1024 * 1024:
            QMessageBox.warning(
                self, "Drop file",
                f"{path.name} is {len(data)//1024//1024} MB - too "
                f"large to send to the U64. Limit is 50 MB.")
            return

        if ext in self.DND_RUNNERS:
            kind = self.DND_RUNNERS[ext]
            label = f"Sending {path.name} -> {kind}..."
        elif ext in self.DND_DISKS:
            kind = "mount_disk"
            mode_word = "mount only" if ctrl_held else "mount + run"
            label = f"Sending {path.name} -> {mode_word}..."
        else:
            return    # caught earlier but defensive

        self.lbl_stats.setText(f"  {label}  ")
        self._dnd_worker = _DnDUploadWorker(
            host=self._host, port=self._http_port,
            password=self._password,
            kind=kind, file_bytes=data,
            file_ext=ext,
            mount_and_run=(not ctrl_held),
            parent=self)
        self._dnd_worker.done.connect(self._on_dnd_done)
        self._dnd_worker.start()

    def _on_dnd_done(self, ok, msg):
        if ok:
            self.lbl_stats.setText(f"  {msg}  ")
        else:
            self.lbl_stats.setText(f"  Drop FAILED: {msg}  ")
            QMessageBox.warning(self, "U64 drop", msg)
        self._dnd_worker = None

    def _send_petscii_byte(self, pet_byte: int):
        """Public-ish helper for the F-key/control-key buttons.
        Just delegates to the same fire-and-forget path as direct
        key capture, but with no host check - if the host is empty
        we show a friendly message instead of silently doing
        nothing."""
        if not self._host:
            QMessageBox.warning(self, "Send key",
                "No U64 host configured. Use Config... to set the IP.")
            return
        self._fire_keypress(pet_byte)

    def _on_space_burst(self):
        """SPC button handler: tries every possible way to inject a
        SPACE press so even intros/demos that scan the keyboard
        matrix directly (not via KERNAL buffer) might receive it.

        Macht 6 writemem-Roundtrips in einem Worker - das laeuft im
        Hintergrund damit der Video-Stream nicht stockt. Bei Erfolg
        zeigen wir nichts; bei Fehler nur eine Status-Meldung.
        """
        if not self._host:
            QMessageBox.warning(self, "Send key",
                "No U64 host configured. Use Config... to set the IP.")
            return
        if (self._space_worker is not None
                and self._space_worker.isRunning()):
            # alter Burst laeuft noch - ignorieren statt doppelt feuern
            return
        self._space_worker = _SpaceBurstWorker(
            host=self._host, port=self._http_port,
            password=self._password, parent=self)
        self._space_worker.done.connect(self._on_space_burst_done)
        self._space_worker.start()

    def _on_space_burst_done(self, ok, msg):
        self._space_worker = None
        if not ok and msg:
            # Subtle Meldung im Stats-Label, kein Modal - das stoert
            # nur. Falls der User mehrfach hintereinander drueckt soll
            # die UI fluessig bleiben.
            try:
                self.lbl_stats.setText(f"  space burst: {msg}  ")
            except Exception:
                pass

    # ---- keyboard injection ----------------------------------------
    # Two paths to "type" on the C64 from this window:
    #
    #   1. The "Type to U64" line edit + Send button: user types a
    #      line on the PC, hits Enter, the whole string + RETURN
    #      gets pushed into the C64's keyboard buffer at $0277. This
    #      is the reliable path - works with BASIC, FILEBROWSER, any
    #      KERNAL-using software.
    #
    #   2. "Capture keys" checkbox: turns on direct key forwarding.
    #      While the streamer window has focus, keyPressEvent on
    #      regular keys gets translated to PETSCII and forwarded
    #      immediately. Feels more interactive but is limited - the
    #      C64 keyboard buffer is only 10 chars deep and the KERNAL
    #      pulls ~60/sec, so very fast typing or held-key autorepeat
    #      can drop chars. Real-time games that scan the keyboard
    #      matrix directly (most action games) won't see these keys
    #      at all - the buffer only feeds KERNAL-based software.

    def _on_type_send(self):
        """Send the line in ed_type to the U64 keyboard buffer.
        Runs on a background QThread so the GUI doesn't freeze
        during the writemem chunks (each chunk = 2 HTTP requests
        + 180ms sleep)."""
        if not self._host:
            QMessageBox.warning(self, "Type to U64",
                "No U64 host configured. Use Config... to set the IP.")
            return
        text = self.ed_type.text()
        if not text:
            return
        # Append RETURN so a typed BASIC line gets executed.
        text_with_cr = text + "\n"
        # Disable the send controls while the worker runs to avoid
        # double-sends.
        self.btn_send.setEnabled(False)
        self.ed_type.setEnabled(False)
        self._type_worker = _TypeWorker(
            host=self._host, port=self._http_port,
            password=self._password,
            text=text_with_cr, parent=self)
        self._type_worker.done.connect(self._on_type_done)
        self._type_worker.start()

    def _on_type_done(self, ok, msg):
        self.btn_send.setEnabled(True)
        self.ed_type.setEnabled(True)
        if ok:
            self.ed_type.clear()
            self.ed_type.setFocus()
            self.lbl_stats.setText("  text sent  ")
        else:
            self.lbl_stats.setText(f"  type FAILED: {msg}  ")
            QMessageBox.warning(self, "Type to U64", msg)

    def _quick_type(self, text: str):
        """Inject a pre-canned BASIC command + RETURN into the C64.
        Used by the Quick: LOAD/LIST/RUN buttons below the keys row.
        Shares the same worker pipeline as _on_type_send."""
        if not self._host:
            QMessageBox.warning(self, "Quick command",
                "No U64 host configured. Use Config... to set the IP.")
            return
        text_with_cr = text + "\n"
        # Re-use the existing worker pipeline so quick-clicks queue
        # politely if you mash several in a row. Each one disables
        # the type-line until the previous chunk drains.
        if (self._type_worker is not None
                and self._type_worker.isRunning()):
            # Cheap rate-limit: ignore the click if a previous Quick
            # is still draining. With ~0.5s per command this is fine
            # for a human at the keyboard.
            self.lbl_stats.setText("  busy - try again  ")
            return
        self.btn_send.setEnabled(False)
        self.ed_type.setEnabled(False)
        self._type_worker = _TypeWorker(
            host=self._host, port=self._http_port,
            password=self._password,
            text=text_with_cr, parent=self)
        self._type_worker.done.connect(self._on_type_done)
        self._type_worker.start()
        self._type_worker = None

    # Map of Qt key -> single-byte PETSCII for direct forwarding.
    # The Qt key codes here are the ones in PyQt6.QtCore.Qt.Key.
    # We compute these lazily inside keyPressEvent because importing
    # at class-body time would force QtCore to import before the
    # QApplication exists.
    @staticmethod
    def _qt_key_to_petscii(key, modifiers, text):
        """Translate a Qt keyPressEvent into a single PETSCII byte
        (or 0 if no sensible mapping). Used for direct key capture."""
        from PyQt6.QtCore import Qt
        K = Qt.Key
        # Special navigation / control
        special = {
            K.Key_Return:    0x0D, K.Key_Enter:    0x0D,
            K.Key_Backspace: 0x14, K.Key_Delete:   0x14,
            K.Key_Up:        0x91, K.Key_Down:     0x11,
            K.Key_Left:      0x9D, K.Key_Right:    0x1D,
            K.Key_Home:      0x13, K.Key_Insert:   0x94,
            K.Key_Escape:    0x03,    # RUN/STOP
            K.Key_F1:        0x85, K.Key_F2:        0x89,
            K.Key_F3:        0x86, K.Key_F4:        0x8A,
            K.Key_F5:        0x87, K.Key_F6:        0x8B,
            K.Key_F7:        0x88, K.Key_F8:        0x8C,
            K.Key_Tab:       0x09,
            K.Key_Space:     0x20,
        }
        if key in special:
            return special[key]
        # Plain printable text - prefer the event's `text()` because
        # it already accounts for shift state, dead keys, etc.
        if text and len(text) == 1:
            b = _ascii_char_to_petscii(text)
            return max(0, b)
        return 0

    def eventFilter(self, obj, ev):
        """Pre-empt events on installed-on widgets before they
        reach the widget's own handlers.

        Two unrelated jobs handled here:

        1. Type-line key forwarding when 'Capture keys' is on -
           every keystroke goes live to the C64 instead of
           accumulating in the QLineEdit buffer.

        2. Minimal-mode window dragging - mouse events on the
           video QLabel are converted into window-drag commands
           on the dialog (Qt does NOT auto-propagate mouse
           events from QLabel to its parent QDialog, so a
           plain mousePressEvent override on the dialog never
           sees clicks that land on the label - which is most
           clicks because the label fills 100% of the minimal-
           mode window). We watch for ButtonPress/MouseMove/
           ButtonRelease/ContextMenu on the video_label here
           and forward them to our own dialog-level handlers.
        """
        from PyQt6.QtCore import QEvent, Qt

        # --- Job 2: Minimal-mode mouse forwarding ----------------
        # Only kicks in if we're actually in minimal mode AND the
        # event source is our video label (or any widget the
        # method installed the filter on). In every other mode
        # mouse handling is unchanged.
        if (getattr(self, '_minimal_mode', False)
                and obj is getattr(self, 'video_label', None)):
            t = ev.type()
            if t == QEvent.Type.MouseButtonPress:
                self.mousePressEvent(ev)
                return ev.isAccepted()
            elif t == QEvent.Type.MouseMove:
                self.mouseMoveEvent(ev)
                return ev.isAccepted()
            elif t == QEvent.Type.MouseButtonRelease:
                self.mouseReleaseEvent(ev)
                return ev.isAccepted()
            elif t == QEvent.Type.ContextMenu:
                self.contextMenuEvent(ev)
                return ev.isAccepted()

        # --- Job 1: Type-line key forwarding ---------------------
        if (obj is getattr(self, "ed_type", None)
                and ev.type() == QEvent.Type.KeyPress
                and self._host
                and self.chk_capture is not None
                and self.chk_capture.isChecked()):
            key = ev.key()
            # Let Tab through so focus can leave the line edit
            # (the user might want to refocus the video area).
            if key == Qt.Key.Key_Tab or key == Qt.Key.Key_Backtab:
                return False
            pet = self._qt_key_to_petscii(
                key, ev.modifiers(), ev.text())
            if pet:
                self._fire_keypress(pet)
                # If the user pressed Enter, clear the line
                # edit's stale text so the visual matches the
                # actually-sent stream (which is "live, one
                # char at a time, no buffer").
                if key in (Qt.Key.Key_Return,
                            Qt.Key.Key_Enter):
                    try:
                        self.ed_type.clear()
                    except Exception:
                        pass
                # Consume - don't let the line edit modify its
                # internal text buffer with this keystroke.
                return True
        return super().eventFilter(obj, ev)

    def event(self, ev):
        """Catch key events at the dialog level BEFORE Qt's default
        focus-widget routing. Without this, F-keys get eaten by the
        QLineEdit, ComboBox, or by Qt's dialog-default shortcut
        handling (F1=Help on some platforms, F4=close-combo, ...)
        before our keyPressEvent ever sees them.

        Plus: in cinema mode, Esc leaves cinema regardless of focus
        (otherwise the user has to click the floating button which
        defeats the keyboard-friendly "press a key, see the C64"
        workflow).

        Only intercepts when Capture-keys is on and we have a host
        configured. Everything else falls through to Qt normally,
        so the Type-line edit, button focus traversal, and dialog
        shortcuts all keep working when capture is off.
        """
        from PyQt6.QtCore import QEvent, Qt
        if ev.type() == QEvent.Type.KeyPress:
            # Cinema-mode Esc handling - works regardless of capture
            # mode or focus. Lets the user always escape back to
            # full chrome.
            if (ev.key() == Qt.Key.Key_Escape
                    and self._cinema_mode):
                self.btn_cinema.setChecked(False)
                return True
            # Direct-capture key forwarding for F-keys etc.
            if (self._host
                    and self.chk_capture is not None
                    and self.chk_capture.isChecked()):
                key = ev.key()
                # F-keys, arrow keys, RUN/STOP and a few others
                # should ALWAYS go to the C64 when capture is on -
                # even if the type-line has focus. Otherwise the
                # user would have to click into the video area
                # first to get F1 working, which is awkward.
                grab_keys = {
                    Qt.Key.Key_F1, Qt.Key.Key_F2, Qt.Key.Key_F3,
                    Qt.Key.Key_F4, Qt.Key.Key_F5, Qt.Key.Key_F6,
                    Qt.Key.Key_F7, Qt.Key.Key_F8,
                    Qt.Key.Key_Up, Qt.Key.Key_Down,
                    Qt.Key.Key_Left, Qt.Key.Key_Right,
                    Qt.Key.Key_Escape,    # RUN/STOP
                    Qt.Key.Key_Home, Qt.Key.Key_Insert,
                }
                if key in grab_keys:
                    pet = self._qt_key_to_petscii(
                        key, ev.modifiers(), ev.text())
                    if pet:
                        self._fire_keypress(pet)
                        return True    # consumed, don't let Qt see it
        return super().event(ev)

    def keyPressEvent(self, ev):
        """When 'Capture keys' is on AND we have a host, forward
        every printable / mapped key directly to the C64's
        keyboard buffer. This works regardless of whether the
        video area or the Type-line has focus - if you tick
        Capture keys, you want every keystroke going to the C64
        without having to click anywhere first.

        The Type-line stays useful for paste-and-send workflows:
        capture is OFF by default, so typing into the Type-line
        builds up a string locally and sends the whole thing on
        Enter. With capture ON, every individual character goes
        live, just like a real C64 keyboard, and the Type-line
        sees an empty string from us (we eat the event).
        """
        if (self.chk_capture.isChecked() and self._host):
            pet = self._qt_key_to_petscii(
                ev.key(), ev.modifiers(), ev.text())
            if pet:
                # Fire-and-forget on the type worker thread. We
                # don't queue here - if the user mashes keys
                # faster than the U64 can drain, some get
                # dropped, just like a real C64 with a slow IRQ.
                self._fire_keypress(pet)
                ev.accept()
                return
        super().keyPressEvent(ev)

    def _fire_keypress(self, pet_byte: int):
        """Send a single PETSCII keypress to the U64 via the
        long-lived keyboard worker. Creates the worker on first
        keypress and reuses it across the session - one TCP
        connection for everything, much lower per-key latency
        than the old "new urllib.request per key" approach.

        If the worker thread isn't yet alive (first key after
        capture was enabled), it's started here. The worker stays
        running until the streamer closes or capture is toggled
        off.
        """
        # Lazily spin up the persistent worker the first time we
        # need it. Tearing it down happens in _shutdown_workers
        # and (optionally) when the user unchecks Capture keys.
        worker = getattr(self, "_kb_worker", None)
        if (worker is None
                or not worker.isRunning()):
            worker = _PersistentKeyboardWorker(
                host=self._host,
                port=self._http_port,
                password=self._password,
                parent=self)
            worker.error.connect(self._on_keypress_error)
            worker.start()
            self._kb_worker = worker
        worker.enqueue(pet_byte)

    def _on_capture_toggled(self, checked: bool):
        """Stop the persistent keyboard worker when capture is
        turned off. Idempotent if no worker exists (first toggle
        before any key was sent yet). The worker is recreated on
        the first keystroke after capture is re-enabled."""
        if checked:
            return
        kb = getattr(self, "_kb_worker", None)
        if kb is None:
            return
        try:
            kb.stop()
            kb.wait(1500)
        except Exception:
            pass
        self._kb_worker = None

    def _on_keypress_error(self, msg: str):
        """Surface a keyboard send failure in the status line.
        Doesn't kill the worker - it'll just try the next key on
        a fresh connection. Throttle the status update so a flood
        of errors during a network blip doesn't repaint 100x."""
        try:
            self.lbl_stats.setText(f"  key FAILED: {msg}  ")
        except Exception:
            pass

    def _on_keypress_done(self, ok, msg):
        # Legacy slot for the per-key _TypeWorker pattern. With
        # the persistent worker we no longer fire one of these per
        # key, but the slot is kept connectable in case other code
        # paths still wire it up (e.g. the explicit Type field).
        self._type_worker = None
        if not ok:
            self.lbl_stats.setText(f"  key FAILED: {msg}  ")
            return
        # Drain the pending queue if there's anything waiting
        # (used by code paths that go through the old worker).
        pending = getattr(self, '_pending_keys', None)
        if pending:
            next_key = pending.pop(0)
            self._fire_keypress(next_key)

    # ---- shutdown ---------------------------------------------------
    def _shutdown_workers(self):
        # Stop the recorder first so it gets any final frames before
        # the video worker goes away. Wait briefly for the encoder
        # to flush; if it's slow (very long capture, slow disk) we
        # disown it so the close path doesn't hang for ages. The
        # encoder will finish writing its file in the background.
        if self._video_recorder is not None:
            try:
                self._video_recorder.stop()
                self._video_recorder.wait(5000)
            except Exception:
                pass
            self._video_recorder = None
            # Reset the Rec button to idle in case we got here via
            # Stop button or window close rather than the user
            # clicking Rec a second time.
            if hasattr(self, 'btn_record'):
                try:
                    self.btn_record.setText("Rec")
                    self.btn_record.setStyleSheet(button_qss("blue"))
                    self.btn_record.setChecked(False)
                    self.btn_record.setEnabled(True)
                except Exception:
                    pass
        if self._video_worker is not None:
            self._video_worker.stop()
            self._video_worker.wait(2000)
            self._video_worker = None
        if self._audio_worker is not None:
            self._audio_worker.stop()
            self._audio_worker.wait(2000)
            self._audio_worker = None
        if self._dnd_worker is not None:
            # Can't really cancel an HTTP upload mid-flight but we
            # can at least disown the worker so it doesn't keep us
            # alive.
            try:
                self._dnd_worker.wait(2000)
            except Exception:
                pass
            self._dnd_worker = None
        if self._type_worker is not None:
            try:
                self._type_worker.wait(2000)
            except Exception:
                pass
            self._type_worker = None
        # Tear down the persistent keyboard worker (if active).
        # It holds an open TCP connection to the U64 so we should
        # close cleanly rather than letting the socket linger.
        kb = getattr(self, "_kb_worker", None)
        if kb is not None:
            try:
                kb.stop()
                kb.wait(2000)
            except Exception:
                pass
            self._kb_worker = None
        if self._audio_sink is not None:
            try:
                self._audio_sink.stop()
            except Exception:
                pass
            self._audio_sink = None
            self._audio_io = None

    def closeEvent(self, ev):
        """Clean up workers + tell the U64 to stop streaming
        (if it was us who told it to start)."""
        try:
            if self._host and self.btn_stop.isEnabled():
                # Stream is running - politely ask the U64 to stop.
                # Don't block long; we're closing.
                send_telnet_sequence(
                    self._host, SEQ_STOP_STREAM,
                    port=self._telnet_port, timeout=1.0)
        except Exception:
            pass
        self._shutdown_workers()
        super().closeEvent(ev)
