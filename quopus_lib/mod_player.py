"""ProTracker-style module player (.mod / .xm / .s3m / .it / .mptm).

Uses libopenmpt via ctypes for decoding (the only sensible way to get
broad format support) plus sounddevice for audio output. On Windows,
the user needs to drop `libopenmpt.dll` next to the executable (or
into the python install) - the DLL is freely available from
https://lib.openmpt.org/libopenmpt/download.html.

The UI is modeled on Amiga ProTracker: a black/grey window, monospace
Topaz-8 font, a pattern view that scrolls as the song plays, VU
meters per channel, and the classic transport buttons (PLAY / STOP /
FFWD / REW). Module metadata (title, author, sample list, song
length) is shown above the pattern view.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QEvent, QSize, QObject, QThread,
)
from PyQt6.QtGui import (
    QFont, QFontDatabase, QColor, QPainter, QPen, QBrush, QKeySequence,
    QShortcut, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QWidget, QFrame, QSizePolicy, QSlider, QMessageBox,
    QSplitter, QApplication,
)
from .config import scaled_font_px

# =====================================================================
# libopenmpt ctypes wrapper
# =====================================================================
# We don't ship libopenmpt - it has to be installed separately. On
# Linux it usually comes via the package manager (libopenmpt0); on
# Windows download the binary release. We look for it in a few places
# before giving up.

_lib = None
_lib_error = None


def _find_openmpt():
    """Try a list of plausible names and locations for libopenmpt.
    Returns a loaded CDLL or raises OSError.

    On Windows the official libopenmpt distribution ships the main
    DLL plus several dependency DLLs (mpg123, ogg, vorbis, zlib).
    Names have changed across versions:
       Older releases: libmpg123-0.dll, libogg-0.dll, libvorbis-0.dll,
                       libvorbisfile-3.dll
       Newer releases: openmpt-mpg123.dll, openmpt-ogg.dll,
                       openmpt-vorbis.dll, openmpt-zlib.dll
    We add the directory containing the DLLs to the Windows DLL search
    path before loading so the main DLL can find its deps."""
    global _lib_error
    candidates = []
    # Standard library lookup (Linux + macOS)
    auto = ctypes.util.find_library('openmpt')
    if auto:
        candidates.append(auto)
    # Bare names for the system loader (Linux/macOS use these)
    candidates += [
        'libopenmpt.so.0', 'libopenmpt.so',
        'libopenmpt.0.dylib', 'libopenmpt.dylib',
        # Windows DLL names - try both the unversioned and the
        # `-0` versioned spelling that older releases used.
        'libopenmpt.dll', 'libopenmpt-0.dll',
        'openmpt.dll', 'openmpt-0.dll',
    ]
    # Look next to the quopus executable for the DLLs. If we find one
    # there, also register the directory with the Windows DLL loader
    # so dependency DLLs get picked up too.
    here = Path(__file__).resolve().parent.parent
    search_dirs = [here, Path.cwd()]
    file_candidates = []
    for d in search_dirs:
        if not d.exists():
            continue
        for name in ('libopenmpt.dll', 'libopenmpt-0.dll',
                      'openmpt.dll', 'openmpt-0.dll'):
            p = d / name
            if p.exists():
                file_candidates.append(p)
    # Set up the Windows DLL search path BEFORE attempting to load
    if os.name == 'nt':
        try:
            for d in search_dirs:
                if d.exists():
                    try:
                        os.add_dll_directory(str(d))
                    except (OSError, AttributeError):
                        pass
                    os.environ['PATH'] = (str(d) + os.pathsep
                                            + os.environ.get('PATH', ''))
        except Exception:
            pass
    # Try the file-path candidates first (highest priority)
    candidates = [str(p) for p in file_candidates] + candidates
    last = None
    tried = []
    for c in candidates:
        tried.append(c)
        try:
            return ctypes.CDLL(c)
        except OSError as e:
            last = e
            continue
    # Build a more informative error message including a directory
    # listing of where we expected the DLL to be plus the running
    # Python's architecture (this is the #1 reason for DLL load
    # failures on Windows: an arch mismatch).
    listing_lines = []
    for d in search_dirs:
        try:
            listing_lines.append(f"  {d}:")
            if not d.exists():
                listing_lines.append(f"    (does not exist)")
                continue
            entries = sorted((p.name, p.stat().st_size)
                              for p in d.glob("*.dll"))
            if entries:
                for name, size in entries:
                    # ~1.5 MB libopenmpt.dll is usually x86; ~1.8+ MB
                    # is usually x64. This is a hint, not a guarantee.
                    listing_lines.append(
                        f"    {name}  ({size:,} bytes)")
            else:
                listing_lines.append("    (no .dll files found)")
        except Exception as e:
            listing_lines.append(f"    (error listing: {e})")
    import platform as _pl
    arch_info = (f"Python: {_pl.python_version()}  "
                 f"Architecture: {_pl.architecture()[0]}\n"
                 f"OS: {_pl.system()} {_pl.release()}")
    msg = (f"libopenmpt could not be loaded.\n\n"
           f"{arch_info}\n\n"
           f"Last error from CDLL():\n  {last}\n\n"
           f"Tried (first 10 candidates):\n  " +
           "\n  ".join(tried[:10]) +
           "\n\nDLL files in search dirs:\n" +
           "\n".join(listing_lines))
    raise OSError(msg)


def _load_lib():
    """Lazy-load libopenmpt and set up function signatures.
    Returns the loaded library or None if unavailable."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    try:
        lib = _find_openmpt()
    except OSError as e:
        _lib_error = str(e)
        return None
    # Type setup
    lib.openmpt_module_create_from_memory2.restype = ctypes.c_void_p
    lib.openmpt_module_create_from_memory2.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_void_p,
    ]
    lib.openmpt_module_destroy.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_destroy.restype = None

    lib.openmpt_module_read_stereo.restype = ctypes.c_size_t
    lib.openmpt_module_read_stereo.argtypes = [
        ctypes.c_void_p, ctypes.c_int32, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.POINTER(ctypes.c_int16),
    ]

    lib.openmpt_module_get_duration_seconds.restype = ctypes.c_double
    lib.openmpt_module_get_duration_seconds.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_set_position_seconds.restype = ctypes.c_double
    lib.openmpt_module_set_position_seconds.argtypes = [
        ctypes.c_void_p, ctypes.c_double]
    lib.openmpt_module_get_position_seconds.restype = ctypes.c_double
    lib.openmpt_module_get_position_seconds.argtypes = [ctypes.c_void_p]

    lib.openmpt_module_get_current_pattern.restype = ctypes.c_int32
    lib.openmpt_module_get_current_pattern.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_current_row.restype = ctypes.c_int32
    lib.openmpt_module_get_current_row.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_current_order.restype = ctypes.c_int32
    lib.openmpt_module_get_current_order.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_current_speed.restype = ctypes.c_int32
    lib.openmpt_module_get_current_speed.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_current_tempo.restype = ctypes.c_int32
    lib.openmpt_module_get_current_tempo.argtypes = [ctypes.c_void_p]

    lib.openmpt_module_get_num_channels.restype = ctypes.c_int32
    lib.openmpt_module_get_num_channels.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_num_patterns.restype = ctypes.c_int32
    lib.openmpt_module_get_num_patterns.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_num_orders.restype = ctypes.c_int32
    lib.openmpt_module_get_num_orders.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_num_instruments.restype = ctypes.c_int32
    lib.openmpt_module_get_num_instruments.argtypes = [ctypes.c_void_p]
    lib.openmpt_module_get_num_samples.restype = ctypes.c_int32
    lib.openmpt_module_get_num_samples.argtypes = [ctypes.c_void_p]

    lib.openmpt_module_get_pattern_num_rows.restype = ctypes.c_int32
    lib.openmpt_module_get_pattern_num_rows.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]
    lib.openmpt_module_get_order_pattern.restype = ctypes.c_int32
    lib.openmpt_module_get_order_pattern.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]

    lib.openmpt_module_format_pattern_row_channel.restype = ctypes.c_char_p
    lib.openmpt_module_format_pattern_row_channel.argtypes = [
        ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_size_t, ctypes.c_int]

    lib.openmpt_module_get_metadata.restype = ctypes.c_char_p
    lib.openmpt_module_get_metadata.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.openmpt_module_get_sample_name.restype = ctypes.c_char_p
    lib.openmpt_module_get_sample_name.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]
    lib.openmpt_module_get_instrument_name.restype = ctypes.c_char_p
    lib.openmpt_module_get_instrument_name.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]

    lib.openmpt_module_set_render_param.restype = ctypes.c_int
    lib.openmpt_module_set_render_param.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int32]
    # render param 1 = master gain in millibel; 2 = stereo separation %;
    # 3 = interpolation filter; 4 = volume ramping strength

    # Per-channel VU meters
    lib.openmpt_module_get_current_channel_vu_mono.restype = ctypes.c_float
    lib.openmpt_module_get_current_channel_vu_mono.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]
    lib.openmpt_module_get_current_channel_vu_left.restype = ctypes.c_float
    lib.openmpt_module_get_current_channel_vu_left.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]
    lib.openmpt_module_get_current_channel_vu_right.restype = ctypes.c_float
    lib.openmpt_module_get_current_channel_vu_right.argtypes = [
        ctypes.c_void_p, ctypes.c_int32]

    lib.openmpt_free_string.argtypes = [ctypes.c_char_p]
    lib.openmpt_free_string.restype = None
    _lib = lib
    return lib


def _decode_str(b):
    if not b: return ""
    if isinstance(b, bytes):
        try:
            return b.decode('utf-8', errors='replace').rstrip('\x00')
        except Exception:
            return b.decode('latin-1', errors='replace').rstrip('\x00')
    return str(b)


# =====================================================================
# OpenMPTModule - thin OO wrapper around the ctypes pointer
# =====================================================================
class OpenMPTModule:
    """Wraps an openmpt module handle. Use as a context manager or call
    .close() explicitly. All methods are thread-safe by virtue of
    libopenmpt's internal locking (we serialize reads anyway via the
    audio thread)."""

    SAMPLERATE = 48000

    def __init__(self, file_bytes: bytes):
        self._lib = _load_lib()
        if self._lib is None:
            raise RuntimeError(
                f"libopenmpt not available: {_lib_error}\n\n"
                "Linux:   apt install libopenmpt0\n"
                "macOS:   brew install libopenmpt\n"
                "Windows:\n"
                "  1. Download the libopenmpt 'windows' release ZIP\n"
                "     (NOT the *-dev.zip) from\n"
                "       https://lib.openmpt.org/files/libopenmpt/bin/\n"
                "  2. Open the ZIP, navigate to bin/x86_64/ for 64-bit\n"
                "     Python or bin/x86/ for 32-bit Python\n"
                "  3. Copy ALL the .dll files from that subfolder\n"
                "     into the same directory as quopus.py:\n"
                "       libopenmpt-0.dll\n"
                "       libmpg123-0.dll\n"
                "       libogg-0.dll\n"
                "       libvorbis-0.dll\n"
                "       libvorbisfile-3.dll\n"
                "\n"
                "  NOTE: libopenmpt.lib is the STATIC IMPORT LIBRARY for\n"
                "  C/C++ linkers - it cannot be loaded by Python.\n"
                "  You need the .DLL files from the runtime release.")
        err = ctypes.c_int(0)
        errmsg = ctypes.c_char_p(None)
        h = self._lib.openmpt_module_create_from_memory2(
            file_bytes, len(file_bytes),
            None, None, None, None,
            ctypes.byref(err), ctypes.byref(errmsg), None)
        if not h:
            msg = _decode_str(errmsg.value) or "unknown error"
            raise ValueError(f"failed to load module: {msg}")
        self._h = h
        # Set good defaults
        # (param IDs from openmpt_module_render_param enum)
        self._lib.openmpt_module_set_render_param(self._h, 1, 0)  # gain 0 mB
        self._lib.openmpt_module_set_render_param(self._h, 2, 100)  # stereo
        self._lib.openmpt_module_set_render_param(self._h, 3, 8)  # interp 8tap
        self._lock = threading.Lock()

    def close(self):
        if self._h:
            self._lib.openmpt_module_destroy(self._h)
            self._h = None

    def __del__(self):
        try: self.close()
        except Exception: pass

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    # ---- Read audio chunk ----
    def read_stereo(self, n_frames: int):
        """Decode `n_frames` samples of stereo audio. Returns an
        int16 numpy array of shape (n_frames, 2). Returns fewer
        frames at the end of the song; returns 0 frames once played
        out (caller should check)."""
        if not self._h:
            return np.zeros((0, 2), dtype=np.int16)
        L = (ctypes.c_int16 * n_frames)()
        R = (ctypes.c_int16 * n_frames)()
        with self._lock:
            got = self._lib.openmpt_module_read_stereo(
                self._h, self.SAMPLERATE, n_frames, L, R)
        out = np.empty((got, 2), dtype=np.int16)
        out[:, 0] = np.frombuffer(L, dtype=np.int16, count=got)
        out[:, 1] = np.frombuffer(R, dtype=np.int16, count=got)
        return out

    # ---- Position / state queries ----
    @property
    def duration(self):
        return self._lib.openmpt_module_get_duration_seconds(self._h)

    @property
    def position(self):
        return self._lib.openmpt_module_get_position_seconds(self._h)

    @position.setter
    def position(self, secs):
        with self._lock:
            self._lib.openmpt_module_set_position_seconds(
                self._h, float(secs))

    @property
    def current_pattern(self):
        return self._lib.openmpt_module_get_current_pattern(self._h)

    @property
    def current_row(self):
        return self._lib.openmpt_module_get_current_row(self._h)

    @property
    def current_order(self):
        return self._lib.openmpt_module_get_current_order(self._h)

    @property
    def current_speed(self):
        return self._lib.openmpt_module_get_current_speed(self._h)

    @property
    def current_tempo(self):
        return self._lib.openmpt_module_get_current_tempo(self._h)

    @property
    def num_channels(self):
        return self._lib.openmpt_module_get_num_channels(self._h)

    @property
    def num_patterns(self):
        return self._lib.openmpt_module_get_num_patterns(self._h)

    @property
    def num_orders(self):
        return self._lib.openmpt_module_get_num_orders(self._h)

    @property
    def num_instruments(self):
        return self._lib.openmpt_module_get_num_instruments(self._h)

    @property
    def num_samples(self):
        return self._lib.openmpt_module_get_num_samples(self._h)

    def pattern_num_rows(self, pattern_idx):
        return self._lib.openmpt_module_get_pattern_num_rows(
            self._h, pattern_idx)

    def order_pattern(self, order_idx):
        return self._lib.openmpt_module_get_order_pattern(
            self._h, order_idx)

    def format_row_channel(self, pattern, row, channel, width=13):
        """Return the ProTracker-style formatted string for one cell.
        Width 13 = "C-5 01 .. F08" with spaces; lower widths abbreviate."""
        b = self._lib.openmpt_module_format_pattern_row_channel(
            self._h, pattern, row, channel, width, 0)
        return _decode_str(b)

    def metadata(self, key):
        b = self._lib.openmpt_module_get_metadata(self._h, key.encode('utf-8'))
        return _decode_str(b)

    def sample_name(self, idx):
        return _decode_str(
            self._lib.openmpt_module_get_sample_name(self._h, idx))

    def instrument_name(self, idx):
        return _decode_str(
            self._lib.openmpt_module_get_instrument_name(self._h, idx))

    def get_channel_vu_levels(self):
        """Return a list of mono VU levels (0.0..1.0) for every
        channel. Used for VU-meter animation."""
        if not self._h:
            return []
        n = self.num_channels
        return [
            float(self._lib.openmpt_module_get_current_channel_vu_mono(
                self._h, c))
            for c in range(n)
        ]


# =====================================================================
# Audio thread - decodes and pushes to sounddevice
# =====================================================================
class AudioThread(QThread):
    """Pumps decoded samples from the module into the audio output
    using sounddevice's blocking stream. Runs in its own thread so
    the UI stays responsive.

    Also emits VU-meter data per channel: after each block we compute
    the per-channel RMS amplitude (via libopenmpt's per-channel VU
    queries) and emit that as a list of floats 0.0..1.0 for the UI.

    Signals:
       finished_playing  - song has played to the end
       error_occurred(str) - audio error
       vu_levels(list)   - per-channel VU 0..1 (one item per channel)
    """
    finished_playing = pyqtSignal()
    error_occurred = pyqtSignal(str)
    vu_levels = pyqtSignal(list)
    # Stereo PCM block emitted every audio loop iteration. Used by
    # the dialog's master VU meter (separate from the per-channel
    # vu_levels above which feeds the channel-grid view).
    master_block = pyqtSignal(np.ndarray)

    BLOCK_FRAMES = 1024

    def __init__(self, module: 'OpenMPTModule', parent=None):
        super().__init__(parent)
        self._mod = module
        self._stop = False
        self._paused = False
        self._volume = 1.0
        self._lock = threading.Lock()

    def stop(self):
        self._stop = True

    def set_paused(self, paused: bool):
        self._paused = paused

    def set_volume(self, v: float):
        """Volume 0.0 .. 1.0"""
        with self._lock:
            self._volume = max(0.0, min(1.0, v))

    def run(self):
        try:
            import sounddevice as sd
        except Exception as e:
            self.error_occurred.emit(
                f"sounddevice not available: {e}\n\n"
                "Install with:\n  pip install sounddevice")
            return
        try:
            stream = sd.OutputStream(
                samplerate=OpenMPTModule.SAMPLERATE,
                channels=2, dtype='int16',
                blocksize=self.BLOCK_FRAMES)
            stream.start()
        except Exception as e:
            self.error_occurred.emit(
                f"Failed to open audio output: {e}")
            return
        try:
            silence = np.zeros((self.BLOCK_FRAMES, 2), dtype=np.int16)
            n_chans = self._mod.num_channels
            vu_decay = 0.85       # per-block decay factor
            vu_levels = [0.0] * n_chans
            block_count = 0
            while not self._stop:
                if self._paused:
                    stream.write(silence)
                    # Decay VU during pause too
                    vu_levels = [v * vu_decay for v in vu_levels]
                    if block_count % 2 == 0:
                        self.vu_levels.emit(list(vu_levels))
                    block_count += 1
                    continue
                chunk = self._mod.read_stereo(self.BLOCK_FRAMES)
                if chunk.shape[0] == 0:
                    self.finished_playing.emit()
                    break
                with self._lock:
                    vol = self._volume
                if vol != 1.0:
                    chunk = (chunk.astype(np.int32) * int(vol * 256)
                             // 256).astype(np.int16)
                if chunk.shape[0] < self.BLOCK_FRAMES:
                    pad = np.zeros(
                        (self.BLOCK_FRAMES - chunk.shape[0], 2),
                        dtype=np.int16)
                    chunk = np.vstack([chunk, pad])
                stream.write(chunk)
                # Master VU - feed the post-volume mixed stereo to
                # the dialog's L/R meter
                self.master_block.emit(chunk)
                # Per-channel VU from libopenmpt
                new_vu = self._mod.get_channel_vu_levels()
                if len(new_vu) != n_chans:
                    new_vu = (new_vu + [0.0] * n_chans)[:n_chans]
                # Peak-hold with decay so the bars don't twitch wildly
                for i in range(n_chans):
                    decayed = vu_levels[i] * vu_decay
                    vu_levels[i] = max(decayed, new_vu[i])
                # Emit every other block (~21 Hz at 1024-frame blocks /
                # 48 kHz) - smooth enough but not flooding the UI
                if block_count % 2 == 0:
                    self.vu_levels.emit(list(vu_levels))
                block_count += 1
        except Exception as e:
            self.error_occurred.emit(
                f"Audio error: {e}\n\n{traceback.format_exc()}")
        finally:
            try: stream.stop()
            except Exception: pass
            try: stream.close()
            except Exception: pass


# =====================================================================
# ProTracker 2.3D - style UI
# =====================================================================
# Color scheme + custom widgets that recreate the iconic Amiga
# ProTracker look. Reference: ProTracker 2.3D screenshot circa 1994
# (Workbench-grey window, black pattern view with orange/yellow note
# text, LCD-style position/pattern/length displays, knubbelige 3D
# transport buttons, horizontal VU meters per channel).

# ProTracker palette
PT_BG          = "#A8A8A8"     # Workbench grey window background
PT_BG_DARK     = "#888888"     # button face
PT_BEVEL_HI    = "#DDDDDD"     # 3D bevel highlight
PT_BEVEL_LO    = "#444444"     # 3D bevel shadow
PT_TITLE_BG    = "#0055AA"     # blue title bar
PT_TITLE_FG    = "#FFFFFF"
PT_PANEL_BG    = "#000000"     # pattern + LCD background
PT_NOTE_NORMAL = "#FFB050"     # orange note text (most rows)
PT_NOTE_DIM    = "#A07030"     # darker orange (off-rows in PT 4-row beat)
PT_NOTE_CUR    = "#FFFFFF"     # current row note text
PT_CUR_BG      = "#5050B0"     # current row background blue
PT_LCD_GREEN   = "#22DD22"     # LCD green segment
PT_LCD_DIM     = "#114411"     # LCD off segment
PT_VU_GREEN    = "#22DD22"
PT_VU_YELLOW   = "#FFCC44"
PT_VU_RED      = "#FF4444"
PT_VU_DIM      = "#222222"
PT_LED_RED_ON  = "#FF3333"
PT_LED_RED_OFF = "#441111"
PT_TEXT        = "#000000"
PT_BORDER      = "#000000"


def _bevel_paint(p, rect, raised=True):
    """Paint a 3D bevel border into rect. raised=True for buttons up,
    False for buttons being pressed (or LCD-style sunken panels)."""
    hi = QColor(PT_BEVEL_HI if raised else PT_BEVEL_LO)
    lo = QColor(PT_BEVEL_LO if raised else PT_BEVEL_HI)
    p.setPen(hi)
    p.drawLine(rect.left(),  rect.top(),    rect.right(),  rect.top())
    p.drawLine(rect.left(),  rect.top(),    rect.left(),   rect.bottom())
    p.setPen(lo)
    p.drawLine(rect.left()+1, rect.bottom(), rect.right(),  rect.bottom())
    p.drawLine(rect.right(),  rect.top()+1,  rect.right(),  rect.bottom())


# ---------------------------------------------------------------------
# LCD-style numeric display (Position / Pattern / Length / Speed)
# ---------------------------------------------------------------------
class LCDDisplay(QWidget):
    """Black panel with green LED-style digits. Used for the four
    counters on the left of the pattern view."""

    def __init__(self, label: str, value: str = "00", width_chars: int = 3,
                  parent=None):
        super().__init__(parent)
        self._label = label
        self._value = value
        self._width_chars = width_chars
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "Courier New", "monospace"])
        self._font.setPixelSize(20)
        self._font.setBold(True)
        self._label_font = QFont()
        self._label_font.setFamilies(["Topaz-8", "Cascadia Mono",
                                        "Consolas", "monospace"])
        self._label_font.setPixelSize(10)
        self._label_font.setBold(True)
        self.setMinimumWidth(80)
        self.setMinimumHeight(50)

    def set_value(self, val: str):
        if val != self._value:
            self._value = val
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        # Outer panel: light grey with sunken bevel
        p.fillRect(rect, QColor(PT_BG_DARK))
        _bevel_paint(p, rect.adjusted(0, 0, -1, -1), raised=False)
        # Label across the top in black
        p.setFont(self._label_font)
        p.setPen(QColor(PT_TEXT))
        label_h = 14
        p.drawText(rect.adjusted(4, 1, -4, -(rect.height()-label_h)),
                    Qt.AlignmentFlag.AlignLeft, self._label)
        # LCD area below: black with sunken bevel
        lcd_rect = rect.adjusted(4, label_h, -4, -4)
        p.fillRect(lcd_rect, QColor(PT_PANEL_BG))
        # Inner 1px shadow
        p.setPen(QColor(PT_BEVEL_LO))
        p.drawRect(lcd_rect.adjusted(0, 0, -1, -1))
        # Digits in green
        p.setFont(self._font)
        p.setPen(QColor(PT_LCD_GREEN))
        # Right-pad with the value
        text = self._value.rjust(self._width_chars)
        p.drawText(lcd_rect, Qt.AlignmentFlag.AlignCenter, text)


# ---------------------------------------------------------------------
# 3D bevelled button (ProTracker-style transport)
# ---------------------------------------------------------------------
class PTButton(QPushButton):
    """Custom-painted bevelled button. Two visual states (raised /
    pressed) with the raised state thicker than QSS bevels can produce
    consistently across themes."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFlat(True)   # disable native styling
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "monospace"])
        self._font.setPixelSize(11)
        self._font.setBold(True)
        self.setFont(self._font)
        self.setMinimumHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        pressed = self.isDown()
        # Body
        p.fillRect(rect, QColor(PT_BG_DARK))
        # Bevel (pressed = inset)
        _bevel_paint(p, rect.adjusted(0, 0, -1, -1), raised=not pressed)
        _bevel_paint(p, rect.adjusted(1, 1, -2, -2), raised=not pressed)
        # Text - shifted by 1px when pressed
        p.setFont(self._font)
        p.setPen(QColor(PT_TEXT) if self.isEnabled()
                  else QColor("#666666"))
        offset = 1 if pressed else 0
        p.drawText(rect.adjusted(offset, offset, offset, offset),
                    Qt.AlignmentFlag.AlignCenter, self.text())


# ---------------------------------------------------------------------
# Title bar (Amiga-style blue strip across the top)
# ---------------------------------------------------------------------
class PTTitleBar(QWidget):
    """Blue 1-line strip with white text. Two text fields: app name
    on the left, file name on the right (just like the original ASL
    file requester window title)."""

    def __init__(self, app_text: str, file_text: str, parent=None):
        super().__init__(parent)
        self._app = app_text
        self._file = file_text
        self.setFixedHeight(18)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "monospace"])
        self._font.setPixelSize(11)
        self._font.setBold(True)

    def set_file(self, name: str):
        self._file = name
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(PT_TITLE_BG))
        p.setPen(QColor(PT_TITLE_FG))
        p.setFont(self._font)
        p.drawText(rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignLeft, self._app)
        p.drawText(rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignRight, self._file)


# ---------------------------------------------------------------------
# VU meter row (one horizontal bar per channel)
# ---------------------------------------------------------------------
class VUMeterRow(QWidget):
    """One row of horizontal VU bars, one per channel. ProTracker
    showed these as little segmented bars under the channel header.
    We render them with green segments turning yellow then red as
    the level approaches max."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._levels = []
        self._n_chans = 4
        self.setFixedHeight(20)

    def set_channel_count(self, n: int):
        self._n_chans = max(1, n)
        if len(self._levels) != self._n_chans:
            self._levels = [0.0] * self._n_chans
        self.update()

    def set_levels(self, levels):
        self._levels = list(levels)
        if len(self._levels) < self._n_chans:
            self._levels += [0.0] * (self._n_chans - len(self._levels))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(PT_BG))
        n = self._n_chans
        if n <= 0:
            return
        # Each cell: bar height 12, padding 4; segments computed to fit
        cell_w = (rect.width() - 8) // n
        for c in range(n):
            x = 4 + c * cell_w
            bar_w = cell_w - 6
            bar_rect_x = x
            bar_rect_y = 4
            bar_rect_h = 12
            # Frame (sunken)
            p.fillRect(bar_rect_x, bar_rect_y, bar_w, bar_rect_h,
                        QColor(PT_PANEL_BG))
            p.setPen(QColor(PT_BEVEL_LO))
            p.drawRect(bar_rect_x, bar_rect_y, bar_w-1, bar_rect_h-1)
            # Segments
            level = self._levels[c] if c < len(self._levels) else 0.0
            level = min(1.0, max(0.0, level))
            n_segs = max(8, bar_w // 4)
            seg_w = max(1, (bar_w - 4) // n_segs)
            lit = int(level * n_segs)
            for s in range(n_segs):
                sx = bar_rect_x + 2 + s * seg_w
                if s >= lit:
                    col = QColor(PT_VU_DIM)
                elif s > n_segs * 0.8:
                    col = QColor(PT_VU_RED)
                elif s > n_segs * 0.6:
                    col = QColor(PT_VU_YELLOW)
                else:
                    col = QColor(PT_VU_GREEN)
                p.fillRect(sx, bar_rect_y + 2, seg_w - 1, bar_rect_h - 4,
                            col)


# ---------------------------------------------------------------------
# Disk activity LED (red, blinks on activity)
# ---------------------------------------------------------------------
class PTLED(QWidget):
    def __init__(self, label: str = "DISK", parent=None):
        super().__init__(parent)
        self._on = False
        self._label = label
        self.setFixedSize(54, 22)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "monospace"])
        self._font.setPixelSize(10)
        self._font.setBold(True)

    def set_on(self, on: bool):
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(PT_BG))
        # LED dot on the left
        led_d = 12
        led_x = 4
        led_y = (rect.height() - led_d) // 2
        col = QColor(PT_LED_RED_ON if self._on else PT_LED_RED_OFF)
        p.setBrush(col)
        p.setPen(QColor(PT_BEVEL_LO))
        p.drawEllipse(led_x, led_y, led_d, led_d)
        # Label to the right
        p.setFont(self._font)
        p.setPen(QColor(PT_TEXT))
        p.drawText(rect.adjusted(led_x + led_d + 4, 0, -2, 0),
                    Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignLeft, self._label)


# ---------------------------------------------------------------------
# Pattern view (scrolling, current row centred)
# ---------------------------------------------------------------------
class PatternView(QWidget):
    """ProTracker-style pattern display. Black background, orange
    note text on dim/bright alternation per beat-row, current row
    highlighted in white-on-blue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.module: 'OpenMPTModule | None' = None
        self._row = 0
        self._pattern = 0
        self._num_rows = 64
        self._num_chans = 4
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "Courier New", "monospace"])
        self._font.setPixelSize(13)
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(self._font)

    def set_module(self, mod):
        self.module = mod
        self._num_chans = mod.num_channels
        self.update_position()

    def update_position(self):
        if not self.module:
            return
        new_pat = self.module.current_pattern
        new_row = self.module.current_row
        if new_pat == self._pattern and new_row == self._row:
            return
        self._pattern = new_pat
        self._row = new_row
        if self._pattern >= 0:
            self._num_rows = max(
                1, self.module.pattern_num_rows(self._pattern))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        # Sunken panel: black BG with bevel border
        p.fillRect(self.rect(), QColor(PT_PANEL_BG))
        _bevel_paint(p, self.rect().adjusted(0, 0, -1, -1), raised=False)
        if not self.module or self._pattern < 0:
            p.setPen(QColor(PT_NOTE_DIM))
            p.setFont(self._font)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                        "(no pattern)")
            return
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        line_h = fm.height() + 1
        # Row-number column width
        rownum_w = fm.horizontalAdvance("000  ")
        # Cell width: fits "C-5 01 v40 F08 "
        cell_w = fm.horizontalAdvance("C-5 01 .. F08 ")
        avail = self.width() - rownum_w - 12
        chans_to_show = max(1, min(self._num_chans, avail // cell_w))
        # Center current row
        center_y = self.height() // 2
        rows_above = center_y // line_h
        first_row = self._row - rows_above
        n_rows = (self.height() // line_h) + 2
        for i in range(n_rows):
            r = first_row + i
            y = i * line_h
            if r < 0 or r >= self._num_rows:
                continue
            is_cur = (r == self._row)
            if is_cur:
                p.fillRect(2, y, self.width() - 4, line_h,
                            QColor(PT_CUR_BG))
                fg = QColor(PT_NOTE_CUR)
                rownum_fg = QColor(PT_NOTE_CUR)
            else:
                fg = QColor(PT_NOTE_NORMAL if (r % 4 == 0)
                              else PT_NOTE_DIM)
                rownum_fg = QColor("#FFCC44" if (r % 4 == 0)
                                     else "#A07030")
            # Row number
            p.setPen(rownum_fg)
            p.drawText(8, y + fm.ascent(), f"{r:03d}")
            # Channels
            x = rownum_w
            for c in range(chans_to_show):
                txt = self.module.format_row_channel(self._pattern, r, c)
                p.setPen(fg)
                p.drawText(x, y + fm.ascent(), txt)
                # Subtle separator between channels
                if c < chans_to_show - 1:
                    p.setPen(QColor("#222222") if not is_cur
                              else QColor(PT_CUR_BG).lighter(110))
                    p.drawLine(x + cell_w - 4, y, x + cell_w - 4,
                                 y + line_h)
                x += cell_w


# ---------------------------------------------------------------------
# Sample list (right-side panel, ProTracker shows 31 numbered slots)
# ---------------------------------------------------------------------
class SampleListView(QWidget):
    """Numbered, monospaced sample list. Each entry is `NN: name`
    in the classic Workbench-grey-on-black look. Two columns when
    there are 16+ samples (typical XM/IT modules)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []   # list of (idx, name)
        self._highlight_idx = -1
        self.setMinimumWidth(260)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                                  "monospace"])
        self._font.setPixelSize(12)
        self._font.setBold(False)
        self.setFont(self._font)

    def set_samples(self, items):
        """items = list of (idx_int, name_str)"""
        self._items = list(items)
        self.update()

    def set_highlight(self, idx: int):
        if idx != self._highlight_idx:
            self._highlight_idx = idx
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(PT_PANEL_BG))
        _bevel_paint(p, rect.adjusted(0, 0, -1, -1), raised=False)
        if not self._items:
            return
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        line_h = fm.height() + 1
        n = len(self._items)
        # How many lines fit vertically?
        max_lines = max(1, (rect.height() - 8) // line_h)
        # How many columns do we need so every sample is shown?
        # Round up.
        n_cols = max(1, (n + max_lines - 1) // max_lines)
        # Cap at 4 columns - any more is unreadable
        n_cols = min(4, n_cols)
        rows_per_col = (n + n_cols - 1) // n_cols
        col_w = (rect.width() - 8) // n_cols
        # Truncate name length to fit cell
        char_w = max(1, fm.horizontalAdvance("M"))
        max_name_chars = max(8, (col_w - 8 - fm.horizontalAdvance("00: "))
                              // char_w)
        for col in range(n_cols):
            start = col * rows_per_col
            for i in range(rows_per_col):
                idx_i = start + i
                if idx_i >= n: break
                idx, name = self._items[idx_i]
                y = 4 + i * line_h
                x = 4 + col * col_w
                cur = (idx == self._highlight_idx)
                if cur:
                    p.fillRect(x, y, col_w - 4, line_h,
                                QColor(PT_CUR_BG))
                    p.setPen(QColor(PT_NOTE_CUR))
                else:
                    p.setPen(QColor(PT_NOTE_NORMAL))
                # ProTracker numbers samples 01..31 in decimal
                txt = f"{idx:02d}: {name[:max_name_chars]}"
                p.drawText(x + 4, y + fm.ascent(), txt)


# ---------------------------------------------------------------------
# Volume slider (custom-painted, ProTracker-style horizontal trough)
# ---------------------------------------------------------------------
class PTSlider(QSlider):
    """Slider with a sunken trough and a small raised knob - matches
    the ProTracker volume / position fader style."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #000000;
                border: 1px solid #444444;
                height: 6px;
            }
            QSlider::sub-page:horizontal {
                background: #5050B0;
                border: 1px solid #444444;
                height: 6px;
            }
            QSlider::handle:horizontal {
                background: #DDDDDD;
                border: 1px solid #444444;
                width: 12px;
                height: 14px;
                margin: -4px 0;
            }
        """)


# =====================================================================
# Main player dialog (ProTracker layout)
# =====================================================================
class ModPlayerDialog(QDialog):
    """ProTracker 2.3D-styled module player.

    Layout:
       PTTitleBar (blue strip)
       Top row: 4 LCDs (POS / PATT / LEN / SPD / TEMPO) | PatternView
       VUMeterRow
       Bottom row: SampleListView | controls (transport + volume + LED)
    """

    @staticmethod
    def check_audio_available(parent=None) -> bool:
        """Returns True iff the 'sounddevice' module is available.
        Otherwise pops up an instructional dialog and returns False.
        Use BEFORE constructing the dialog to avoid half-opening
        the UI."""
        try:
            import sounddevice as _sd  # noqa: F401
            return True
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            if "PortAudio" in str(e) or isinstance(e, OSError):
                detail = (
                    "The native PortAudio library was not found.\n\n"
                    "On Linux:\n"
                    "    apt install libportaudio2\n\n"
                    "On macOS:\n"
                    "    brew install portaudio\n\n"
                    "On Windows: PortAudio is bundled with the "
                    "sounddevice wheel; reinstall via:\n"
                    "    pip install --force-reinstall sounddevice\n\n"
                    f"Original error: {e}")
                title = "Module Player - PortAudio missing"
            else:
                detail = (
                    "The 'sounddevice' Python package is required "
                    "for MOD/XM/S3M/IT playback but is not installed."
                    "\n\nInstall it with:\n\n"
                    "    pip install sounddevice numpy\n\n"
                    "On Linux without a venv, you may need:\n"
                    "    pip install --user sounddevice numpy\n"
                    "or:\n"
                    "    apt install python3-sounddevice python3-numpy")
                title = "Module Player - missing dependency"
            QMessageBox.critical(parent, title,
                f"{detail}\n\nAfter installing, restart Quopus and try again.")
            return False

    def __init__(self, path: Path, parent=None, *,
                  shuffle_files: list | None = None):
        """Open a tracker module for playback.

        If shuffle_files is given (a list of pathlib.Path entries),
        the player runs in SHUFFLE MODE: prev/next buttons appear
        in the transport row and let the user navigate the playlist.
        When the current track ends, the next one starts automatically.
        `path` should be the first file to play (typically the file
        the user double-clicked, which is then used as the starting
        point in the shuffled list).

        `shuffle_files` is keyword-only so the legacy two-positional
        call ModPlayerDialog(path, parent_widget) keeps working."""
        super().__init__(parent)
        # Force non-modal top-level window so Quopus stays usable
        # while the player runs. See SIDPlayerDialog for the same
        # rationale - a plain .show() on a QDialog can still feel
        # modal to the user without these flags.
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.path = Path(path)
        self.setWindowTitle("Python ModPlayer V1.0 by lA-sTYLe")
        self.resize(940, 620)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "modplayer")
        # Solid grey background everywhere
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {PT_BG};
                color: {PT_TEXT};
            }}
            QLabel {{ color: {PT_TEXT}; }}
        """)

        self._mod: 'OpenMPTModule | None' = None
        self._audio: 'AudioThread | None' = None
        self._is_playing = False
        self._is_paused = False
        self._led_blink_phase = 0
        self._last_row = -1
        self._highlight_sample_idx = -1

        # Shuffle mode
        from .shuffle import ShufflePlaylist
        self._shuffle_mode = shuffle_files is not None
        self._playlist: ShufflePlaylist | None = None
        if self._shuffle_mode:
            self._playlist = ShufflePlaylist(
                files=list(shuffle_files), start=self.path)

        # Open the module file
        if not self._load_current_module():
            return

        self._build_layout()
        self._populate_metadata()
        self._setup_timer()
        self._start_playback()

    # -----------------------------------------------------------------
    def _load_current_module(self) -> bool:
        """Open the module file at self.path and create the OpenMPTModule.
        On failure shows an error dialog, builds an empty layout, and
        returns False. Used by both __init__ and shuffle prev/next."""
        try:
            data = self.path.read_bytes()
            self._mod = OpenMPTModule(data)
            return True
        except RuntimeError as e:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("ModPlay - libopenmpt not loaded")
            box.setText("Could not load libopenmpt.\n"
                         "Click 'Show Details...' below for the full diagnostic.")
            box.setDetailedText(str(e))
            box.exec()
            self._build_empty_layout()
            return False
        except Exception as e:
            QMessageBox.critical(
                self, "ModPlay",
                f"Could not load module:\n{e}\n\n{traceback.format_exc()}")
            self._build_empty_layout()
            return False

    # -----------------------------------------------------------------
    def _build_empty_layout(self):
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Module loading failed."))
        btn = PTButton("Close")
        btn.clicked.connect(self.close)
        lay.addWidget(btn)

    def _build_layout(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Title bar ----
        self._title = PTTitleBar("Python ModPlayer V1.0 by lA-sTYLe",
                                   self._title_file_text())
        outer.addWidget(self._title)

        # Body container with padding
        body = QWidget()
        body.setStyleSheet(f"background-color: {PT_BG};")
        outer.addWidget(body, stretch=1)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(8, 8, 8, 8)
        body_lay.setSpacing(6)

        # ---- Top row: LCDs + pattern view ----
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        # LCD column (left)
        lcd_col = QVBoxLayout()
        lcd_col.setSpacing(4)
        self._lcd_pos    = LCDDisplay("POSITION",  "00", 3)
        self._lcd_patt   = LCDDisplay("PATTERN",   "00", 3)
        self._lcd_length = LCDDisplay("LENGTH",    "00", 3)
        self._lcd_speed  = LCDDisplay("SPEED",     "06", 3)
        self._lcd_tempo  = LCDDisplay("TEMPO",     "125", 3)
        for w in (self._lcd_pos, self._lcd_patt, self._lcd_length,
                   self._lcd_speed, self._lcd_tempo):
            w.setFixedWidth(110)
            lcd_col.addWidget(w)
        lcd_col.addStretch(1)
        top_row.addLayout(lcd_col)
        # Pattern view (middle, fills remaining)
        self._pattern_view = PatternView()
        top_row.addWidget(self._pattern_view, stretch=1)
        # 10-band spectrum analyzer (graphic-EQ style) showing the
        # frequency content of the mixed audio. Sits to the right of
        # the pattern view; 31..16k Hz bars with peak-hold.
        from .spectrum import SpectrumAnalyzer
        self._master_vu = SpectrumAnalyzer()
        self._master_vu.setFixedWidth(280)
        self._master_vu.setMinimumHeight(180)
        top_row.addWidget(self._master_vu)
        body_lay.addLayout(top_row, stretch=1)

        # ---- VU meters row ----
        self._vu = VUMeterRow()
        body_lay.addWidget(self._vu)

        # ---- Bottom row: sample list | transport ----
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)
        # Sample list (left half)
        self._sample_list = SampleListView()
        self._sample_list.setMinimumHeight(180)
        bottom_row.addWidget(self._sample_list, stretch=1)
        # Transport panel (right half)
        transport_panel = QWidget()
        transport_panel.setStyleSheet(f"background-color: {PT_BG};")
        tp_lay = QVBoxLayout(transport_panel)
        tp_lay.setContentsMargins(0, 0, 0, 0)
        tp_lay.setSpacing(6)
        # Button grid
        btn_grid = QHBoxLayout()
        btn_grid.setSpacing(4)
        self._play_song_btn = PTButton("PLAY SONG")
        self._play_song_btn.clicked.connect(self._toggle_play)
        btn_grid.addWidget(self._play_song_btn)
        self._play_patt_btn = PTButton("PLAY PATT")
        self._play_patt_btn.clicked.connect(self._play_pattern)
        btn_grid.addWidget(self._play_patt_btn)
        self._stop_btn = PTButton("STOP")
        self._stop_btn.clicked.connect(self._stop_playback)
        btn_grid.addWidget(self._stop_btn)
        self._rew_btn = PTButton("<<")
        self._rew_btn.setMaximumWidth(40)
        self._rew_btn.clicked.connect(lambda: self._seek(-5))
        btn_grid.addWidget(self._rew_btn)
        self._ffwd_btn = PTButton(">>")
        self._ffwd_btn.setMaximumWidth(40)
        self._ffwd_btn.clicked.connect(lambda: self._seek(+5))
        btn_grid.addWidget(self._ffwd_btn)
        # Shuffle prev/next - only visible in shuffle mode
        self._shuffle_prev_btn = PTButton("|<<")
        self._shuffle_prev_btn.setMaximumWidth(40)
        self._shuffle_prev_btn.setToolTip(
            "Previous track (shuffle mode)")
        self._shuffle_prev_btn.clicked.connect(self._shuffle_prev)
        self._shuffle_prev_btn.setVisible(self._shuffle_mode)
        btn_grid.addWidget(self._shuffle_prev_btn)
        self._shuffle_next_btn = PTButton(">>|")
        self._shuffle_next_btn.setMaximumWidth(40)
        self._shuffle_next_btn.setToolTip(
            "Next track (shuffle mode)")
        self._shuffle_next_btn.clicked.connect(self._shuffle_next)
        self._shuffle_next_btn.setVisible(self._shuffle_mode)
        btn_grid.addWidget(self._shuffle_next_btn)
        # SHUFFLE-FROM-HERE button: pick a folder, start a new shuffle
        # playlist from it. Always visible - lets the user start (or
        # restart with a new folder) shuffle play without closing and
        # reopening the player.
        self._shuffle_pick_btn = PTButton("SHUFFLE")
        self._shuffle_pick_btn.setMaximumWidth(80)
        self._shuffle_pick_btn.setToolTip(
            "Shuffle-play all modules from the current track's "
            "directory and subdirectories")
        self._shuffle_pick_btn.clicked.connect(self._shuffle_pick_folder)
        btn_grid.addWidget(self._shuffle_pick_btn)
        tp_lay.addLayout(btn_grid)
        # Time + LED row
        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setStyleSheet(
            "color: black; font-family: 'Topaz-8','Cascadia Mono',"
            f"monospace; font-weight: bold; font-size: {scaled_font_px(12)}px;")
        time_row.addWidget(self._time_label)
        time_row.addStretch(1)
        self._led = PTLED("DISK")
        time_row.addWidget(self._led)
        tp_lay.addLayout(time_row)
        # Position + Volume sliders
        self._pos_slider = PTSlider()
        self._pos_slider.setMinimum(0)
        self._pos_slider.setMaximum(1000)
        self._pos_slider.sliderMoved.connect(self._on_slider_moved)
        tp_lay.addWidget(self._pos_slider)
        vol_row = QHBoxLayout()
        vol_row.setSpacing(6)
        vol_lbl = QLabel("VOL")
        vol_lbl.setStyleSheet(
            "color: black; font-family: 'Topaz-8','Cascadia Mono',"
            f"monospace; font-weight: bold; font-size: {scaled_font_px(11)}px;")
        vol_row.addWidget(vol_lbl)
        self._vol_slider = PTSlider()
        self._vol_slider.setMinimum(0)
        self._vol_slider.setMaximum(100)
        self._vol_slider.setValue(80)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        vol_row.addWidget(self._vol_slider)
        tp_lay.addLayout(vol_row)
        tp_lay.addStretch(1)
        bottom_row.addWidget(transport_panel, stretch=1)
        body_lay.addLayout(bottom_row)

        # Hotkeys
        QShortcut(QKeySequence("Space"), self, self._toggle_play)
        QShortcut(QKeySequence("Esc"),    self, self.close)
        QShortcut(QKeySequence("Left"),   self, lambda: self._seek(-5))
        QShortcut(QKeySequence("Right"),  self, lambda: self._seek(+5))
        # Shuffle navigation - active even when not in shuffle mode
        # but the methods no-op if no playlist.
        QShortcut(QKeySequence("Ctrl+Right"), self, self._shuffle_next)
        QShortcut(QKeySequence("Ctrl+Left"),  self, self._shuffle_prev)
        QShortcut(QKeySequence("N"), self, self._shuffle_next)
        QShortcut(QKeySequence("P"), self, self._shuffle_prev)

    # -----------------------------------------------------------------
    def _populate_metadata(self):
        if not self._mod: return
        title = self._mod.metadata("title") or self.path.stem
        self._title.set_file(f"{title}  ({self.path.name})")
        # Length / pattern count
        self._lcd_length.set_value(f"{self._mod.num_orders:02d}")
        # Sample list. libopenmpt indexes samples and instruments
        # 0-based for the C API even though ProTracker traditionally
        # numbers them 1-based - so we display i+1 but query at i.
        items = []
        n_inst = self._mod.num_instruments
        if n_inst > 0:
            for i in range(n_inst):
                name = self._mod.instrument_name(i) or f"<inst {i+1}>"
                items.append((i + 1, name))
        else:
            n_smp = self._mod.num_samples
            for i in range(n_smp):
                name = self._mod.sample_name(i) or ""
                # Skip totally empty unnamed slots? No - keep them
                # because ProTracker displays all 31 slots.
                items.append((i + 1, name))
        self._sample_list.set_samples(items)
        # VU
        self._vu.set_channel_count(self._mod.num_channels)
        # Pattern view
        self._pattern_view.set_module(self._mod)

    # -----------------------------------------------------------------
    def _setup_timer(self):
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        if not self._mod:
            return
        self._pattern_view.update_position()
        cur_order = self._mod.current_order
        cur_pat = self._mod.current_pattern
        self._lcd_pos.set_value(f"{cur_order:02d}")
        self._lcd_patt.set_value(f"{cur_pat:02d}")
        self._lcd_speed.set_value(f"{self._mod.current_speed:02d}")
        self._lcd_tempo.set_value(f"{self._mod.current_tempo}")
        # Time
        cur = self._mod.position
        dur = max(0.01, self._mod.duration)
        self._time_label.setText(
            f"{int(cur)//60:02d}:{int(cur)%60:02d} / "
            f"{int(dur)//60:02d}:{int(dur)%60:02d}")
        if not self._pos_slider.isSliderDown():
            self._pos_slider.blockSignals(True)
            self._pos_slider.setValue(int(cur / dur * 1000))
            self._pos_slider.blockSignals(False)
        # LED blinks at row changes (visualises play activity)
        new_row = self._mod.current_row
        if new_row != self._last_row and self._is_playing \
                and not self._is_paused:
            self._led.set_on(True)
            self._led_blink_phase = 3
            self._last_row = new_row
        elif self._led_blink_phase > 0:
            self._led_blink_phase -= 1
            if self._led_blink_phase == 0:
                self._led.set_on(False)

    # -----------------------------------------------------------------
    def _on_vu_levels(self, levels):
        self._vu.set_levels(levels)

    def _on_master_block(self, chunk):
        """Feed the post-mix stereo block to the spectrum analyzer."""
        if hasattr(self, '_master_vu'):
            self._master_vu.feed_block(
                chunk, sample_rate=OpenMPTModule.SAMPLERATE)

    # -----------------------------------------------------------------
    def _start_playback(self):
        if not self._mod or self._is_playing:
            return
        self._audio = AudioThread(self._mod, self)
        self._audio.set_volume(self._vol_slider.value() / 100.0)
        self._audio.finished_playing.connect(self._on_finished)
        self._audio.error_occurred.connect(self._on_audio_error)
        self._audio.vu_levels.connect(self._on_vu_levels)
        self._audio.master_block.connect(self._on_master_block)
        self._audio.start()
        self._is_playing = True
        self._is_paused = False
        self._play_song_btn.setText("PAUSE")

    def _stop_playback(self):
        if self._audio:
            self._audio.stop()
            # Wait up to 2 seconds for the audio thread's blocking
            # write to return and observe the stop flag. If it
            # somehow doesn't (audio device hung etc.), force-kill it
            # so the song doesn't keep playing in the background.
            if not self._audio.wait(2000):
                try: self._audio.terminate()
                except Exception: pass
                self._audio.wait(500)
            self._audio = None
        self._is_playing = False
        self._is_paused = False
        if hasattr(self, '_play_song_btn'):
            self._play_song_btn.setText("PLAY SONG")
        if hasattr(self, '_led'):
            self._led.set_on(False)
        if hasattr(self, '_vu') and self._mod:
            self._vu.set_levels([0.0] * self._mod.num_channels)
        if hasattr(self, '_master_vu'):
            self._master_vu.reset()
        if self._mod:
            try:
                self._mod.position = 0.0
                if hasattr(self, '_pattern_view'):
                    self._pattern_view.update_position()
            except Exception:
                pass

    def _toggle_play(self):
        if not self._is_playing:
            self._start_playback()
            return
        self._is_paused = not self._is_paused
        if self._audio:
            self._audio.set_paused(self._is_paused)
        self._play_song_btn.setText("RESUME" if self._is_paused
                                       else "PAUSE")

    def _play_pattern(self):
        """Play just the current pattern. We approximate this by
        rewinding to the start of the current order and letting it
        roll - libopenmpt doesn't have a single-pattern loop API
        accessible through the C interface."""
        if not self._mod: return
        self._mod.position = 0.0
        if not self._is_playing:
            self._start_playback()

    def _seek(self, delta_secs):
        if not self._mod: return
        new_pos = max(0.0, min(self._mod.duration,
                                self._mod.position + delta_secs))
        self._mod.position = new_pos
        self._pattern_view.update_position()

    def _on_slider_moved(self, val):
        if not self._mod: return
        new_pos = (val / 1000.0) * self._mod.duration
        self._mod.position = new_pos
        self._pattern_view.update_position()

    def _on_volume_changed(self, v):
        if self._audio:
            self._audio.set_volume(v / 100.0)

    def _on_finished(self):
        self._is_playing = False
        self._play_song_btn.setText("PLAY SONG")
        if self._mod:
            self._mod.position = 0.0
            self._pattern_view.update_position()
        # In shuffle mode, automatically advance to the next track
        if self._shuffle_mode and self._playlist is not None:
            self._shuffle_next()

    def _title_file_text(self) -> str:
        """The right-side title bar text. In shuffle mode includes
        the playlist position."""
        if self._shuffle_mode and self._playlist is not None:
            n = self._playlist.total
            i = self._playlist.index + 1
            return f"{self.path.name}  ({i}/{n} - SHUFFLE)"
        return self.path.name

    def _shuffle_pick_folder(self):
        """Start shuffle play from the current track's parent
        directory (recursive). If shuffle mode is already active the
        playlist is replaced with the new one. No folder picker -
        the assumption is that you want to shuffle whatever was near
        the file you opened."""
        from .shuffle import (ShuffleScanner, ShufflePlaylist)
        from PyQt6.QtWidgets import QProgressDialog, QMessageBox
        root = self.path.parent
        if not root.exists():
            return
        pd = QProgressDialog(
            f"Scanning '{root.name}' for tracker modules...",
            "Cancel", 0, 0, self)
        pd.setWindowTitle("Shuffle Mode")
        pd.setMinimumDuration(200)
        pd.setAutoClose(False)
        pd.setAutoReset(False)
        scanner = ShuffleScanner(root, is_module_file, parent=self)
        self._active_scanner = scanner
        def on_progress(n):
            pd.setLabelText(
                f"Scanning '{root.name}' for tracker modules...\n"
                f"Found: {n}")
        def on_done(files):
            pd.close()
            self._active_scanner = None
            if not files:
                QMessageBox.information(
                    self, "Shuffle",
                    f"No tracker modules found in:\n{root}")
                return
            self._shuffle_mode = True
            self._playlist = ShufflePlaylist(
                files=files, start=files[0])
            self._shuffle_prev_btn.setVisible(True)
            self._shuffle_next_btn.setVisible(True)
            if hasattr(self, '_title'):
                self._title.set_file(self._title_file_text())
            self._switch_to(files[0])
        def on_cancel():
            scanner.stop()
            scanner.wait(500)
            self._active_scanner = None
        scanner.progress.connect(on_progress)
        scanner.finished_with_files.connect(on_done)
        pd.canceled.connect(on_cancel)
        scanner.start()
        pd.show()

    def _shuffle_next(self):
        if self._playlist is None: return
        nxt = self._playlist.next()
        if nxt is None: return
        self._switch_to(nxt)

    def _shuffle_prev(self):
        if self._playlist is None: return
        prv = self._playlist.prev()
        if prv is None: return
        self._switch_to(prv)

    def _switch_to(self, new_path: Path):
        """Change to a different module file without recreating the
        whole dialog. Stops audio cleanly, loads the new module,
        rebinds it to the same widgets, and restarts playback."""
        # Tear down current playback + module
        self._stop_playback()
        if self._mod:
            try: self._mod.close()
            except Exception: pass
            self._mod = None
        # Load new file
        self.path = Path(new_path)
        try:
            data = self.path.read_bytes()
            self._mod = OpenMPTModule(data)
        except Exception as e:
            # Skip this file and try the next one in shuffle
            print(f"Skip {new_path.name}: {e}", flush=True)
            QTimer.singleShot(50, self._shuffle_next)
            return
        # Update title bar with new filename + shuffle counter
        if hasattr(self, '_title'):
            self._title.set_file(self._title_file_text())
        # Re-bind module to widgets
        if hasattr(self, '_pattern_view'):
            self._pattern_view.set_module(self._mod)
        # Refresh metadata + LCDs
        self._populate_metadata()
        self._last_row = -1
        self._highlight_sample_idx = -1
        # Restart audio
        self._start_playback()

    def _on_audio_error(self, msg):
        QMessageBox.critical(self, "ModPlay - Audio Error", msg)
        self._stop_playback()

    # -----------------------------------------------------------------
    def closeEvent(self, ev):
        self._cleanup()
        super().closeEvent(ev)

    def done(self, result):
        """Called by accept(), reject(), and the system close button.
        Whichever path triggers dialog dismissal, we run the audio
        cleanup here BEFORE Qt processes the close - otherwise the
        AudioThread keeps draining samples to the speaker even after
        the window is hidden."""
        self._cleanup()
        super().done(result)

    def _cleanup(self):
        """Idempotent shutdown: stop audio thread, free libopenmpt
        handle, kill the UI tick timer."""
        if getattr(self, '_cleaned_up', False):
            return
        self._cleaned_up = True
        try:
            self._stop_playback()
        except Exception:
            pass
        if self._mod:
            try: self._mod.close()
            except Exception: pass
            self._mod = None
        if hasattr(self, '_timer'):
            try: self._timer.stop()
            except Exception: pass




# =====================================================================
# File-type detection for the auto-open dispatcher
# =====================================================================
MOD_EXTENSIONS = {
    '.mod', '.xm', '.s3m', '.it', '.mptm',
    '.med', '.mtm', '.stm', '.ult', '.669', '.amf',
    '.dbm', '.digi', '.dsm', '.far', '.imf', '.itp',
    '.j2b', '.mt2', '.okt', '.psm', '.umx',
    # Common alternate extensions
    '.module',
}


def is_module_file(path: Path) -> bool:
    """Quick check by extension. We could also peek at magic bytes
    (M.K., FLT4, CIAA at offset 1080 for MOD; "Extended Module:" for
    XM; etc.) but extension is reliable for the typical file naming
    conventions used in the demoscene."""
    if not path.is_file():
        return False
    ext = path.suffix.lower()
    if ext in MOD_EXTENSIONS:
        return True
    # Magic check for headerless MODs (4-channel): bytes 1080..1084
    try:
        with open(path, 'rb') as f:
            f.seek(1080)
            magic = f.read(4)
        if magic in (b'M.K.', b'M!K!', b'M&K!', b'FLT4', b'FLT8',
                      b'4CHN', b'6CHN', b'8CHN', b'CD81', b'OKTA'):
            return True
    except Exception:
        pass
    # XM magic
    try:
        with open(path, 'rb') as f:
            head = f.read(17)
        if head == b'Extended Module: ':
            return True
    except Exception:
        pass
    return False
