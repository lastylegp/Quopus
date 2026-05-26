"""SID file player with GoatTracker-style 3-channel display.

Uses libsidplayfp via the libsidwrapper C-API shim for true real-time
SID emulation - no pre-rendering, no FFT-based fake voice splitting.
Per-voice oscilloscope data comes from the actual SID engine via the
mute API: we render small bursts with two voices muted to capture
each voice's individual waveform.

Build the wrapper once (you need libsidplayfp installed):
    Linux:   g++ -O2 -fPIC -shared sidwrapper.cpp \\
                 -o libsidwrapper.so -lsidplayfp -lstdc++
    macOS:   clang++ -O2 -fPIC -shared sidwrapper.cpp \\
                 -o libsidwrapper.dylib -lsidplayfp -lstdc++
    Windows: see comment header in sidwrapper.cpp

Drop libsidwrapper.so / sidwrapper.dll next to quopus.py (along with
libsidplayfp itself if you're on Windows)."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import threading
import traceback
from pathlib import Path

import numpy as np

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QThread,
)
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QKeySequence, QShortcut, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QMessageBox, QSpinBox, QSlider, QSizePolicy, QApplication,
    QCheckBox,
)
from .config import scaled_font_px


# =====================================================================
# libsidwrapper loading
# =====================================================================
_lib = None
_lib_error = None


def _find_wrapper():
    """Look for the wrapper library next to quopus.py and in the
    current working directory. We use platform-specific name lists
    so we don't try to load a `.so` on Windows (which produces a
    nasty 0xC000012F bad-image-format error in the loader instead
    of a clean OSError that we could swallow)."""
    if os.name == 'nt':
        names = ('sidwrapper.dll', 'libsidwrapper.dll')
    elif sys.platform == 'darwin':
        names = ('libsidwrapper.dylib', 'libsidwrapper.so')
    else:
        names = ('libsidwrapper.so',)
    candidates = []
    here = Path(__file__).resolve().parent.parent
    for d in (here, Path.cwd()):
        for n in names:
            candidates.append(str(d / n))
    # System search path fallback (bare names)
    candidates += list(names)
    # On Windows, register the search dirs so dependency DLLs (if any)
    # can be picked up next to the wrapper.
    if os.name == 'nt':
        for d in (here, Path.cwd()):
            if d.exists():
                try: os.add_dll_directory(str(d))
                except (OSError, AttributeError): pass
                os.environ['PATH'] = (str(d) + os.pathsep
                                        + os.environ.get('PATH', ''))
    last = None
    tried = []
    for c in candidates:
        tried.append(c)
        try:
            return ctypes.CDLL(c)
        except OSError as e:
            last = e
            continue
    # Build a useful diagnostic: list everything that looks like a
    # wrapper library in the search dirs, with sizes.
    listing = []
    for d in (here, Path.cwd()):
        if d.exists():
            entries = []
            for p in sorted(d.iterdir()):
                if 'sidwrapper' in p.name.lower():
                    try: sz = p.stat().st_size
                    except Exception: sz = 0
                    entries.append(f"{p.name} ({sz} bytes)")
            listing.append(f"  {d}: "
                            + (", ".join(entries) if entries
                                else "(no sidwrapper.* found)"))
    import platform as _pl
    arch_info = (f"Python: {_pl.python_version()}  "
                 f"Architecture: {_pl.architecture()[0]}\n"
                 f"OS: {_pl.system()} {_pl.release()}")

    # On Linux/macOS, "cannot open shared object file: No such file or
    # directory" usually means a DEPENDENCY of libsidwrapper is missing,
    # not the wrapper itself - the linker just doesn't tell you which
    # one. If the .so file actually exists in a search dir, run ldd on
    # it so the user sees which library is "not found".
    extra_diag = ""
    if os.name != 'nt':
        existing_so = None
        for d in (here, Path.cwd()):
            for n in names:
                p = d / n
                if p.is_file():
                    existing_so = p; break
            if existing_so: break
        if existing_so is not None:
            try:
                import subprocess as _sp
                ldd_out = _sp.run(
                    ['ldd', str(existing_so)],
                    capture_output=True, text=True, timeout=5).stdout
                missing = [ln.strip() for ln in ldd_out.splitlines()
                            if 'not found' in ln]
                if missing:
                    extra_diag = (
                        "\n\nThe wrapper exists but its DEPENDENCIES "
                        "are not satisfied:\n  "
                        + "\n  ".join(missing)
                        + "\n\nYou likely need to install the matching "
                        "libsidplayfp version for your distro:\n"
                        "  Debian/Ubuntu:  sudo apt install libsidplayfp6  "
                        "(or libsidplayfp5 on older releases)\n"
                        "  Fedora:         sudo dnf install libsidplayfp\n"
                        "  Arch:           sudo pacman -S libsidplayfp\n\n"
                        "If the wrapper was built on a NEWER distro than "
                        "yours, the .so is ABI-incompatible. In that case\n"
                        "rebuild it locally:\n"
                        "  sudo apt install build-essential libsidplayfp-dev\n"
                        "  ./build_sidwrapper_linux.sh")
                else:
                    extra_diag = (
                        "\n\nldd says all dependencies are satisfied, "
                        "but ctypes still can't load the library.\n"
                        "This is unusual - check file permissions and "
                        "SELinux/AppArmor logs.")
            except Exception:
                pass

    raise OSError(
        f"libsidwrapper could not be loaded.\n\n"
        f"{arch_info}\n\n"
        f"Last error: {last}\n\n"
        f"Tried these paths/names:\n  " + "\n  ".join(tried) +
        "\n\nFiles in search dirs:\n" + "\n".join(listing) +
        extra_diag +
        "\n\nThe Windows DLL ships in the quopus.zip as sidwrapper.dll\n"
        "and should sit next to quopus.py. If it's there but still\n"
        "won't load, the most likely cause is an architecture\n"
        "mismatch (32-bit Python with the 64-bit DLL or vice versa).\n"
        "The shipped sidwrapper.dll is x86-64 (~3 MB).")


def _load_lib():
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    try:
        lib = _find_wrapper()
    except OSError as e:
        _lib_error = str(e)
        return None
    lib.sid_create.restype = ctypes.c_void_p
    lib.sid_create.argtypes = [ctypes.c_uint32]
    lib.sid_destroy.argtypes = [ctypes.c_void_p]
    lib.sid_destroy.restype = None
    lib.sid_load.restype = ctypes.c_int
    lib.sid_load.argtypes = [ctypes.c_void_p,
                                ctypes.c_char_p, ctypes.c_size_t]
    lib.sid_select_subsong.restype = ctypes.c_int
    lib.sid_select_subsong.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sid_get_subsongs.restype = ctypes.c_int
    lib.sid_get_subsongs.argtypes = [ctypes.c_void_p]
    lib.sid_get_default_subsong.restype = ctypes.c_int
    lib.sid_get_default_subsong.argtypes = [ctypes.c_void_p]
    lib.sid_get_info_string.restype = ctypes.c_char_p
    lib.sid_get_info_string.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sid_get_num_sids.restype = ctypes.c_int
    lib.sid_get_num_sids.argtypes = [ctypes.c_void_p]
    lib.sid_get_chip_model.restype = ctypes.c_char_p
    lib.sid_get_chip_model.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sid_play.restype = ctypes.c_int
    lib.sid_play.argtypes = [ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_int16),
                                ctypes.c_uint32]
    lib.sid_is_playing.restype = ctypes.c_int
    lib.sid_is_playing.argtypes = [ctypes.c_void_p]
    lib.sid_stop.argtypes = [ctypes.c_void_p]
    lib.sid_stop.restype = None
    lib.sid_get_time_ms.restype = ctypes.c_uint32
    lib.sid_get_time_ms.argtypes = [ctypes.c_void_p]
    lib.sid_mute.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int]
    lib.sid_mute.restype = None
    lib.sid_get_error.restype = ctypes.c_char_p
    lib.sid_get_error.argtypes = [ctypes.c_void_p]
    # MD5 helpers - HVSC Songlengths.md5 lookup. The wrapper writes
    # 32 hex chars + NUL into the caller's 33-byte buffer.
    # sid_md5_new: HVSC #68+ format (register-write-stream hash)
    # sid_md5_old: pre-#68 format (header+data hash)
    if hasattr(lib, 'sid_md5_new'):
        lib.sid_md5_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.sid_md5_new.restype = ctypes.c_int
    if hasattr(lib, 'sid_md5_old'):
        lib.sid_md5_old.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.sid_md5_old.restype = ctypes.c_int
    # ROM injection - new in this build. Optional binding so an older
    # libsidwrapper without sid_set_roms still loads and the player
    # falls back to "no ROMs" mode (most simple PSIDs work, RSIDs and
    # KERNAL-using tunes will be silent or fail to load).
    if hasattr(lib, 'sid_set_roms'):
        lib.sid_set_roms.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_size_t,   # kernal
            ctypes.c_char_p, ctypes.c_size_t,   # basic
            ctypes.c_char_p, ctypes.c_size_t,   # chargen
        ]
        lib.sid_set_roms.restype = ctypes.c_int
    _lib = lib
    return lib


# =====================================================================
# C64 ROM loader
# =====================================================================
# libsidplayfp needs the three C64 ROMs (kernal/basic/chargen) for any
# non-trivial tune - RSIDs, BASIC tunes, and most tunes that use
# KERNAL routines for IRQ setup. Without the ROMs, those tunes either
# fail to load or play silence.
#
# We look for the ROMs in the locations distros conventionally put
# them, plus next to quopus.py for users who want to ship their own.
# All three must match the standard C64 dumps:
#
#   kernal.901227-03.bin   8192 bytes
#   basic.901226-01.bin    8192 bytes
#   chargen.901225-01.bin  4096 bytes
#
# (Actual filenames vary - we accept any file at the candidate paths
# and validate by size only.)

_c64_roms_cache = None  # (kernal_bytes, basic_bytes, chargen_bytes) or None


def _find_c64_roms():
    """Look for C64 KERNAL/BASIC/CHARGEN ROMs in standard locations.
    Returns (kernal, basic, chargen) tuple of bytes, with None for any
    ROM that couldn't be located. Result is cached after the first call.
    """
    global _c64_roms_cache
    if _c64_roms_cache is not None:
        return _c64_roms_cache

    here = Path(__file__).resolve().parent.parent
    home = Path.home()
    # Locations + filename patterns. Order = priority. First match wins
    # per ROM. The VICE paths cover Linux and Windows vice installs;
    # the sidplayfp paths cover users who already set up sidplay2.
    search_dirs = [
        here / "roms",                       # bundled with quopus
        here,                                # next to quopus.py
        home / ".config" / "sidplayfp",
        home / ".sidplayfp",
        home / ".vice" / "C64",
        Path("/usr/share/vice/C64"),
        Path("/usr/lib/vice/C64"),
        Path("/usr/local/share/vice/C64"),
        Path("/usr/share/sidplayfp"),
        Path("/usr/local/share/sidplayfp"),
        # Windows VICE default install paths
        Path(r"C:\Program Files\WinVICE\C64"),
        Path(r"C:\Program Files (x86)\WinVICE\C64"),
        Path(r"C:\vice\C64"),
    ]
    # Filename hints. We search case-insensitively and also accept
    # files without the dotted suffix used by VICE.
    name_hints = {
        'kernal':  ['kernal', 'KERNAL'],
        'basic':   ['basic', 'BASIC'],
        'chargen': ['chargen', 'CHARGEN'],
    }
    expected_size = {
        'kernal':  8192,
        'basic':   8192,
        'chargen': 4096,
    }
    found = {'kernal': None, 'basic': None, 'chargen': None}

    def try_load(path: Path, role: str):
        try:
            if not path.is_file():
                return False
            data = path.read_bytes()
            if len(data) == expected_size[role]:
                found[role] = data
                return True
            # Some VICE distributions ship a 9 KB kernal with a small
            # header - try the standard size from the tail.
            if len(data) > expected_size[role]:
                tail = data[-expected_size[role]:]
                if len(tail) == expected_size[role]:
                    found[role] = tail
                    return True
        except Exception:
            return False
        return False

    for d in search_dirs:
        try:
            if not d.exists():
                continue
            for entry in d.iterdir():
                if not entry.is_file():
                    continue
                low = entry.name.lower()
                for role, hints in name_hints.items():
                    if found[role] is not None:
                        continue
                    if any(h.lower() in low for h in hints):
                        try_load(entry, role)
        except Exception:
            continue

    _c64_roms_cache = (found['kernal'], found['basic'], found['chargen'])
    return _c64_roms_cache


# =====================================================================
# PSID/RSID header (used for metadata even before the engine loads)
# =====================================================================
class SIDHeader:
    def __init__(self, data: bytes):
        if len(data) < 0x76:
            raise ValueError("file too small to be a SID")
        magic = data[0:4]
        if magic not in (b'PSID', b'RSID'):
            raise ValueError(f"not a SID file (magic: {magic!r})")
        self.magic = magic.decode('ascii')
        self.version = struct.unpack('>H', data[4:6])[0]
        self.data_offset = struct.unpack('>H', data[6:8])[0]
        self.load_address = struct.unpack('>H', data[8:10])[0]
        self.init_address = struct.unpack('>H', data[10:12])[0]
        self.play_address = struct.unpack('>H', data[12:14])[0]
        self.songs = struct.unpack('>H', data[14:16])[0]
        self.default_song = struct.unpack('>H', data[16:18])[0]
        self.speed = struct.unpack('>I', data[18:22])[0]
        self.name = self._cstr(data[0x16:0x36])
        self.author = self._cstr(data[0x36:0x56])
        self.released = self._cstr(data[0x56:0x76])
        if self.version >= 2 and len(data) >= 0x7C:
            self.flags = struct.unpack('>H', data[0x76:0x78])[0]
        else:
            self.flags = 0

    @staticmethod
    def _cstr(b: bytes) -> str:
        end = b.find(b'\x00')
        if end >= 0: b = b[:end]
        try: return b.decode('latin-1').strip()
        except Exception: return ""


# =====================================================================
# SIDPlayer engine wrapper
# =====================================================================
class SIDPlayer:
    """Real-time SID emulator wrapping libsidwrapper.

    Thread-safety: all engine calls are serialized through an internal
    lock so the audio thread and UI thread don't step on each other."""

    def __init__(self, sample_rate: int = 48000):
        lib = _load_lib()
        if lib is None:
            raise RuntimeError(
                f"libsidwrapper not loaded:\n{_lib_error}")
        self._lib = lib
        self._h = lib.sid_create(sample_rate)
        if not self._h:
            raise RuntimeError("sid_create() returned null")
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        # Inject the C64 ROMs if we have them and the wrapper is new
        # enough to expose sid_set_roms. Without ROMs, RSIDs and
        # tunes that use KERNAL routines will fail or be silent.
        if hasattr(lib, 'sid_set_roms'):
            kernal, basic, chargen = _find_c64_roms()
            if kernal or basic or chargen:
                lib.sid_set_roms(
                    self._h,
                    kernal,  len(kernal)  if kernal  else 0,
                    basic,   len(basic)   if basic   else 0,
                    chargen, len(chargen) if chargen else 0,
                )

    def __del__(self):
        try: self.close()
        except Exception: pass

    def close(self):
        if self._h:
            self._lib.sid_destroy(self._h)
            self._h = None

    def load(self, file_data: bytes) -> bool:
        with self._lock:
            ok = self._lib.sid_load(self._h, file_data, len(file_data))
        return bool(ok)

    def select_subsong(self, n: int) -> bool:
        with self._lock:
            ok = self._lib.sid_select_subsong(self._h, n)
        return bool(ok)

    @property
    def num_subsongs(self) -> int:
        return self._lib.sid_get_subsongs(self._h)

    @property
    def default_subsong(self) -> int:
        return self._lib.sid_get_default_subsong(self._h)

    @property
    def num_sids(self) -> int:
        return self._lib.sid_get_num_sids(self._h)

    def info_string(self, idx: int) -> str:
        b = self._lib.sid_get_info_string(self._h, idx)
        if not b: return ""
        return b.decode('utf-8', errors='replace')

    def chip_model(self, sid_num: int = 0) -> str:
        b = self._lib.sid_get_chip_model(self._h, sid_num)
        return b.decode('ascii') if b else "unknown"

    @property
    def time_ms(self) -> int:
        return self._lib.sid_get_time_ms(self._h)

    def md5_new(self) -> str | None:
        """HVSC #68+ MD5 hash of the loaded tune. Returns 32-char
        lowercase hex string, or None if not available (older
        wrapper, or no tune loaded)."""
        if not hasattr(self._lib, 'sid_md5_new'):
            return None
        buf = ctypes.create_string_buffer(33)
        ok = self._lib.sid_md5_new(self._h, buf)
        if not ok:
            return None
        s = buf.value.decode('ascii', errors='replace').strip()
        return s.lower() if len(s) == 32 else None

    def md5_old(self) -> str | None:
        """Pre-HVSC#68 MD5 (header+data hash). Used as fallback."""
        if not hasattr(self._lib, 'sid_md5_old'):
            return None
        buf = ctypes.create_string_buffer(33)
        ok = self._lib.sid_md5_old(self._h, buf)
        if not ok:
            return None
        s = buf.value.decode('ascii', errors='replace').strip()
        return s.lower() if len(s) == 32 else None

    def stop(self):
        with self._lock:
            self._lib.sid_stop(self._h)

    def play(self, num_frames: int):
        """Render num_frames stereo frames. Returns int16 numpy array
        of shape (got_frames, 2)."""
        n_total = num_frames * 2  # stereo interleaved
        buf = (ctypes.c_int16 * n_total)()
        with self._lock:
            got = self._lib.sid_play(self._h, buf, n_total)
        if got <= 0:
            return np.zeros((0, 2), dtype=np.int16)
        n_frames = got // 2
        arr = np.frombuffer(buf, dtype=np.int16, count=got)
        return arr.reshape(n_frames, 2)

    def skip(self, num_frames: int):
        """Render num_frames frames and discard the output. Used to
        keep visualization engines in sync with the audio engine
        without paying the cost of materialising a numpy array.
        About 3-4x faster than play() for the same frame count."""
        n_total = num_frames * 2
        if not hasattr(self, '_skip_buf') or len(self._skip_buf) < n_total:
            # Allocate once, reuse forever (each engine has its own)
            self._skip_buf = (ctypes.c_int16 * n_total)()
        with self._lock:
            self._lib.sid_play(self._h, self._skip_buf, n_total)

    def play_tail_mono(self, num_frames: int, tail: int):
        """Render num_frames frames, return only the LAST `tail`
        frames as mono float32 (DC-removed). The intermediate frames
        are still rendered (engine state advances) but only the
        scope-visible tail is returned. Avoids allocating a big
        numpy array when we only need a few hundred samples for
        display."""
        n_total = num_frames * 2
        if not hasattr(self, '_play_buf') or len(self._play_buf) < n_total:
            self._play_buf = (ctypes.c_int16 * n_total)()
        with self._lock:
            got = self._lib.sid_play(self._h, self._play_buf, n_total)
        if got <= 0:
            return np.zeros(tail, dtype=np.float32)
        # got = number of int16 samples written (stereo interleaved)
        n_got = got // 2
        # Read directly from the c_int16 buffer's tail
        take_frames = min(tail, n_got)
        # Last take_frames stereo frames -> last take_frames*2 ints
        start = (n_got - take_frames) * 2
        arr = np.frombuffer(
            self._play_buf, dtype=np.int16,
            count=take_frames * 2, offset=start * 2)
        stereo = arr.reshape(take_frames, 2)
        mono = stereo.astype(np.float32).mean(axis=1) / 32768.0
        mono = mono - mono.mean()
        if take_frames < tail:
            pad = np.zeros(tail - take_frames, dtype=np.float32)
            mono = np.concatenate([pad, mono])
        return mono

    def play_voice_only(self, num_frames: int, voice: int):
        """Render num_frames frames with all voices except `voice`
        muted on chip 0. Restores all voices afterwards. (Use
        play_chip_voice_only() for multi-SID.)"""
        with self._lock:
            for v in range(3):
                self._lib.sid_mute(self._h, 0, v, 0 if v == voice else 1)
        out = self.play(num_frames)
        with self._lock:
            for v in range(3):
                self._lib.sid_mute(self._h, 0, v, 0)
        return out

    def play_chip_voice_only(self, num_frames: int, chip: int, voice: int):
        """Render num_frames frames with ONLY the given (chip, voice)
        unmuted on the visualization engine. All other voices on all
        chips are muted. Returns the mono mix of that voice as a
        float32 numpy array (length=got_frames), DC-offset removed.

        IMPORTANT: this method changes the engine-wide mute state.
        It is intended to be called only on the *visualization*
        engine which is dedicated to this kind of voice-soloing.
        Calling it on the audio engine would scramble the user's
        mute checkboxes.

        After the render, every voice EXCEPT the one we just rendered
        is left muted - but the next call will overwrite the state
        again. The audio engine has its own user-managed mute state
        that this method never touches."""
        n_chips = max(1, self.num_sids)
        with self._lock:
            for c in range(n_chips):
                for v in range(3):
                    enabled = (c == chip and v == voice)
                    self._lib.sid_mute(self._h, c, v,
                                         0 if enabled else 1)
            # Render WHILE holding the lock so user-mute calls on
            # the same engine (shouldn't happen since vis is private,
            # but defensive) can't race with the per-voice solo.
            n_total = num_frames * 2
            buf = (ctypes.c_int16 * n_total)()
            got = self._lib.sid_play(self._h, buf, n_total)
        if got <= 0:
            return np.zeros(num_frames, dtype=np.float32)
        n_got = got // 2
        arr = np.frombuffer(buf, dtype=np.int16, count=got)
        stereo = arr.reshape(n_got, 2)
        mono = stereo.astype(np.float32).mean(axis=1) / 32768.0
        # Remove DC offset - libsidplayfp's voice-soloed output
        # contains a sizable DC component from the master volume
        # register and unfiltered residual. Subtracting the mean
        # gives a clean centered waveform that the oscilloscope
        # can scale around zero.
        mono = mono - mono.mean()
        return mono

    def mute(self, voice: int, chip: int = -1, muted: bool = True):
        """Mute/unmute a voice. If chip < 0, applies to ALL chips
        (the same logical voice on all SID slots). Otherwise applies
        only to the named chip."""
        with self._lock:
            if chip < 0:
                n = max(1, self.num_sids)
                for c in range(n):
                    self._lib.sid_mute(self._h, c, voice,
                                         1 if muted else 0)
            else:
                self._lib.sid_mute(self._h, chip, voice,
                                     1 if muted else 0)


# =====================================================================
# Audio thread - real-time SID render -> sounddevice -> visualizer
# =====================================================================
class SIDAudioThread(QThread):
    """Audio playback + per-voice visualization rendering thread.

    Renders the audio engine into the sounddevice output stream and,
    when visualization is enabled, also renders all vis engines in
    lockstep on each audio block so the per-voice oscilloscopes stay
    perfectly synchronized with the audio.

    Each vis engine (one per physical voice) renders BLOCK_FRAMES
    samples per audio block, mirroring exactly what the audio engine
    just consumed. When the vis toggle is off, vis engines don't run
    at all - audio still plays through smoothly at minimal CPU."""
    buffer_block = pyqtSignal(np.ndarray)
    voice_blocks = pyqtSignal(list)
    # Per-tune output blocks for the lite multi-SID visualizer.
    # Emitted when there are 2+ players; carries a list of stereo
    # int16 chunks, one per tune, BEFORE they were averaged into the
    # final mix. The UI feeds each chunk into a small per-tune
    # spectrum analyzer - effectively free since the chunks already
    # exist as a side product of the mixing.
    tune_blocks = pyqtSignal(list)
    error_occurred = pyqtSignal(str)
    # Emitted when the SID engine returns zero frames - i.e. the
    # tune has reached its natural end (CPU trap, song-end signal).
    # The dialog uses this to reset its play/pause state and rewind
    # to the start so the user can hit PLAY again.
    finished_playing = pyqtSignal()

    BLOCK_FRAMES = 1024
    VOICE_BLOCK_FRAMES = 384

    def __init__(self, player,
                  vis_engines: 'list | None' = None, parent=None):
        """player can be a single SIDPlayer (classic single-tune
        playback) or a list of SIDPlayers for multi-SID parallel
        play. In multi mode each engine is rendered in lockstep and
        their outputs are averaged to a single stereo mix."""
        super().__init__(parent)
        # Normalise to a list for the render loop
        if isinstance(player, list):
            self._players = list(player)
        else:
            self._players = [player]
        self._vis_engines = list(vis_engines) if vis_engines else []
        self._stop = False
        self._paused = False
        self._volume = 1.0
        self._vis_enabled = True
        self._lock = threading.Lock()

    @property
    def _player(self):
        # Backwards-compat for single-player code paths that still
        # poke the original attribute.
        return self._players[0] if self._players else None

    def stop(self):
        self._stop = True

    def set_paused(self, p: bool):
        self._paused = p

    def set_volume(self, v: float):
        with self._lock:
            self._volume = max(0.0, min(1.0, v))

    def set_vis_enabled(self, enabled: bool):
        """Toggle per-voice visualization rendering. When disabled,
        vis engines stop running entirely - CPU usage drops to just
        the audio engine cost."""
        self._vis_enabled = enabled

    def run(self):
        try:
            import sounddevice as sd
        except Exception as e:
            # User-actionable error message instead of just "ModuleNotFoundError".
            # The audio thread can't show a dialog directly (Qt is single-threaded),
            # so we emit the structured error string and let the GUI thread handle it.
            self.error_occurred.emit(
                "MISSING_SOUNDDEVICE: The 'sounddevice' Python "
                "package is required for SID audio playback but is "
                "not installed.\n\n"
                "Quick fix - run in your terminal:\n\n"
                "    pip install sounddevice numpy\n\n"
                "(In a virtualenv: activate it first, then run pip "
                "install. On Linux without venv you may need "
                "'pip install --user sounddevice numpy' or "
                "'apt install python3-sounddevice'.)\n\n"
                f"Original error: {e}")
            return
        try:
            stream = sd.OutputStream(
                samplerate=self._player.sample_rate,
                channels=2, dtype='int16',
                blocksize=self.BLOCK_FRAMES)
            stream.start()
        except Exception as e:
            self.error_occurred.emit(f"audio open: {e}")
            return
        try:
            silence = np.zeros((self.BLOCK_FRAMES, 2), dtype=np.int16)
            while not self._stop:
                if self._paused:
                    stream.write(silence)
                    continue
                # Audio render. Single-player path stays simple;
                # multi-player path renders each engine for the same
                # frame count and averages their int16 outputs to
                # one stereo mix. Averaging (rather than summing)
                # prevents clipping when all four tunes hit a loud
                # transient simultaneously - a sum of 4 full-scale
                # int16 streams would be 16x past the int16 ceiling.
                if len(self._players) == 1:
                    chunk = self._players[0].play(self.BLOCK_FRAMES)
                else:
                    parts = [p.play(self.BLOCK_FRAMES)
                              for p in self._players]
                    # Each engine returns its own length; clip to
                    # the shortest so we don't emit garbage if one
                    # ended early (CPU trap, song-end).
                    n = min(c.shape[0] for c in parts)
                    if n == 0:
                        chunk = np.zeros((0, 2), dtype=np.int16)
                    else:
                        # Sum in int32, then divide by player count
                        acc = np.zeros((n, 2), dtype=np.int32)
                        for c in parts:
                            acc += c[:n].astype(np.int32)
                        # Average instead of sum - keeps level the
                        # same as a single-tune playback regardless
                        # of how many tunes are loaded
                        acc //= len(self._players)
                        chunk = acc.astype(np.int16)
                        # Side-product of the mix: per-tune chunks
                        # are emitted for the lite visualizer (each
                        # tune's pre-mix output, clipped to the
                        # common length n). This is the cheap path
                        # for a per-tune spectrum/level display -
                        # zero extra engine cost over the audio mix.
                        if self._vis_enabled:
                            self.tune_blocks.emit(
                                [c[:n].copy() for c in parts])
                if chunk.shape[0] == 0:
                    # Engine returned zero frames - tune has reached
                    # its natural end (CPU trap, end-of-song detected).
                    # Tell the dialog so it can reset its play state;
                    # otherwise the UI thinks we're still playing and
                    # subsequent PLAY clicks won't restart anything.
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
                self.buffer_block.emit(chunk)
                # Visualization (only when enabled and engines exist)
                if not self._vis_engines or not self._vis_enabled:
                    continue
                try:
                    voices = []
                    for eng in self._vis_engines:
                        # Render exactly the same number of frames
                        # the audio engine just consumed - this keeps
                        # all engines in lockstep on the same musical
                        # timeline. Then take the last VOICE_BLOCK_FRAMES
                        # samples for the scope.
                        tail = eng.play_tail_mono(
                            self.BLOCK_FRAMES,
                            self.VOICE_BLOCK_FRAMES)
                        voices.append(tail)
                    self.voice_blocks.emit(voices)
                except Exception:
                    pass
        except Exception as e:
            self.error_occurred.emit(
                f"audio error: {e}\n{traceback.format_exc()}")
        finally:
            try: stream.stop()
            except Exception: pass
            try: stream.close()
            except Exception: pass


# =====================================================================
# GoatTracker palette
# =====================================================================
GT_BG          = "#000000"
GT_FRAME       = "#202028"
GT_PANEL       = "#000000"
GT_NOTE_NORMAL = "#00CCCC"
GT_NOTE_BEAT   = "#FFFFFF"
GT_NOTE_CUR    = "#FFFFFF"
GT_CUR_BG      = "#7B0058"
GT_INST        = "#FFFF00"
GT_CMD         = "#88FF88"
GT_DIM         = "#444466"
GT_ROWNUM      = "#888888"
GT_HEADER      = "#FFCC00"
GT_OSCI        = "#00FF00"
GT_OSCI_BG     = "#000018"
GT_OSCI_GRID   = "#003300"
GT_TITLE_BG    = "#0055AA"
GT_TITLE_FG    = "#FFFFFF"


class Oscilloscope(QWidget):
    """Per-voice oscilloscope - actual SID voice waveform via the
    engine's mute-and-render trick."""

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self._samples = np.zeros(256, dtype=np.float32)
        self._label = label
        self._gain = 5.0
        self.setMinimumSize(180, 80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Fixed)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono",
                                  "Consolas", "monospace"])
        self._font.setPixelSize(10)
        self._font.setBold(True)

    def set_samples(self, samples: np.ndarray):
        if samples is None or len(samples) == 0:
            return
        peak = float(np.abs(samples).max())
        if peak > 0.001:
            target = 0.7
            self._gain = 0.7 * self._gain + 0.3 * (target / peak)
            self._gain = max(0.5, min(50.0, self._gain))
        self._samples = samples
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(GT_OSCI_BG))
        p.setPen(QColor(GT_FRAME))
        p.drawRect(rect.adjusted(0, 0, -1, -1))
        cx, cy = rect.width() // 2, rect.height() // 2
        p.setPen(QPen(QColor(GT_OSCI_GRID), 1, Qt.PenStyle.DotLine))
        p.drawLine(0, cy, rect.width(), cy)
        p.drawLine(cx, 0, cx, rect.height())
        if self._label:
            p.setFont(self._font)
            p.setPen(QColor(GT_OSCI))
            p.drawText(4, 12, self._label)
        n = len(self._samples)
        if n < 2:
            return
        p.setPen(QPen(QColor(GT_OSCI), 1))
        h = rect.height()
        scale = self._gain * (h / 2 - 4)
        prev_x = prev_y = None
        for i in range(n):
            x = int(i * (rect.width() - 1) / (n - 1))
            v = self._samples[i] * scale
            y = cy - int(v)
            y = max(2, min(h - 2, y))
            if prev_x is not None:
                p.drawLine(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y


class GTPatternView(QWidget):
    """GoatTracker-style multi-voice pattern view. Number of voices
    is dynamic: 3 for 1SID, 6 for 2SID, 9 for 3SID."""

    NUM_ROWS = 64
    NOTE_NAMES = ['C-', 'C#', 'D-', 'D#', 'E-', 'F-',
                   'F#', 'G-', 'G#', 'A-', 'A#', 'B-']

    def __init__(self, num_voices: int = 3, parent=None):
        super().__init__(parent)
        self._num_voices = max(1, num_voices)
        self._row = 0
        # Goes True the first time advance_row() rolls past the end
        # of the pattern. Until then we don't have history wrapping
        # around to show in the "above the cursor" region for the
        # very first few rows - so paintEvent suppresses those wrap-
        # arounds to avoid showing stale '---' content as if it were
        # real notes. After the first wrap, the entire ring buffer
        # is meaningful and we let paintEvent draw modulo-indexed.
        self._have_wrapped = False
        self._pattern_data = [
            [('---', 0, 0, 0)] * self.NUM_ROWS
            for _ in range(self._num_voices)
        ]
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono",
                                  "Consolas", "monospace"])
        self._font.setPixelSize(13)
        self._font.setBold(True)
        self.setFont(self._font)

    def set_num_voices(self, n: int):
        n = max(1, n)
        if n == self._num_voices:
            return
        self._num_voices = n
        self._pattern_data = [
            [('---', 0, 0, 0)] * self.NUM_ROWS for _ in range(n)
        ]
        self._row = 0
        self._have_wrapped = False
        self.update()

    def push_voice_event(self, voice: int, note_idx, intensity: float):
        if voice < 0 or voice >= self._num_voices:
            return
        if note_idx is not None:
            octave = (note_idx // 12) - 1
            if octave < 0: octave = 0
            if octave > 8: octave = 8
            note_name = self.NOTE_NAMES[note_idx % 12]
            note_str = f"{note_name}{octave}"
            inst = 1 + int(intensity * 14) & 0x0F
            cmd = 0
            param = int(intensity * 0xff) & 0xff
        else:
            note_str = '---'; inst = 0; cmd = 0; param = 0
        self._pattern_data[voice][self._row] = (
            note_str, inst, cmd, param)

    def advance_row(self):
        new_row = (self._row + 1) % self.NUM_ROWS
        if new_row == 0:
            self._have_wrapped = True
        self._row = new_row
        self.update()

    def reset(self):
        self._pattern_data = [
            [('---', 0, 0, 0)] * self.NUM_ROWS
            for _ in range(self._num_voices)
        ]
        self._row = 0
        self._have_wrapped = False
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(GT_BG))
        p.setPen(QColor(GT_FRAME))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        line_h = fm.height() + 1
        rownum_w = fm.horizontalAdvance("0000 ")
        # Cell format depends on voice count - more voices = compact
        if self._num_voices <= 3:
            cell_template = "C-4 0F 0820  "
            full_format = True
        elif self._num_voices <= 6:
            cell_template = "C-4 0F  "
            full_format = False
        else:
            cell_template = "C-4 0F "
            full_format = False
        cell_w = fm.horizontalAdvance(cell_template)
        avail = self.width() - rownum_w - 8
        if avail < cell_w * self._num_voices:
            cell_w = max(fm.horizontalAdvance("C-4  "),
                          avail // self._num_voices)
        header_y = 4 + fm.ascent()
        p.setPen(QColor(GT_HEADER))
        p.drawText(4, header_y, "ROW")
        for c in range(self._num_voices):
            x = rownum_w + c * cell_w
            # Header label depending on voice count
            if self._num_voices == 3:
                label = f"VOICE {c+1}"
            elif self._num_voices == 6:
                # 2SID: chip 1 voices V1-V3, chip 2 V4-V6
                label = f"V{c+1}"
            else:
                # 3SID
                label = f"V{c+1}"
            p.drawText(x, header_y, label)
        body_top = line_h + 6
        # The pattern view fills via push_voice_event() one row at a
        # time, so rows below the cursor would stay blank until the
        # ring-buffer wrapped around for the first time. Place the
        # cursor near the bottom of the visible area: only history
        # (= already-detected notes) shows above it, no blank
        # "future" lines below. This way the panel is meaningfully
        # populated as soon as a few rows have been recorded, not
        # only after the first 64-row wrap.
        usable_h = self.height() - body_top
        # Reserve ~3 rows below the cursor so the highlighted line
        # has some breathing room and a clearly visible separator,
        # but keep most of the panel for history above.
        rows_below = 3
        rows_above = max(1, (usable_h // line_h) - rows_below - 1)
        center_y = body_top + rows_above * line_h
        first_row = self._row - rows_above
        n_rows = rows_above + rows_below + 1
        # Pre-compute text-x offsets within a cell
        note_x = 0
        inst_x = fm.horizontalAdvance("C-4 ")
        cmd_x  = fm.horizontalAdvance("C-4 0F ")
        for i in range(n_rows):
            r = first_row + i
            y = body_top + i * line_h
            # Resolve negative r through the ring buffer once we've
            # wrapped at least once - otherwise the rows above the
            # cursor would stay '---' until the first wrap-around.
            # Before the first wrap there's genuinely no history to
            # show for r < 0, so we skip those rows.
            if r < 0:
                if not self._have_wrapped:
                    continue
                r = r % self.NUM_ROWS
            elif r >= self.NUM_ROWS:
                continue
            is_cur = (r == self._row)
            is_beat = (r % 4 == 0)
            if is_cur:
                p.fillRect(2, y, self.width() - 4, line_h,
                            QColor(GT_CUR_BG))
                base_col = QColor(GT_NOTE_CUR)
                inst_col = QColor("#FFFF88")
                cmd_col = QColor("#AAFFAA")
                rownum_col = QColor(GT_NOTE_CUR)
            elif is_beat:
                base_col = QColor(GT_NOTE_BEAT)
                inst_col = QColor(GT_INST)
                cmd_col = QColor(GT_CMD)
                rownum_col = QColor(GT_HEADER)
            else:
                base_col = QColor(GT_NOTE_NORMAL)
                inst_col = QColor(GT_DIM)
                cmd_col = QColor(GT_DIM)
                rownum_col = QColor(GT_ROWNUM)
            p.setPen(rownum_col)
            p.drawText(4, y + fm.ascent(), f"{r:02X}")
            for c in range(self._num_voices):
                note_str, inst, cmd, param = self._pattern_data[c][r]
                cx = rownum_w + c * cell_w
                p.setPen(base_col if note_str != '---'
                          else QColor(GT_DIM))
                p.drawText(cx + note_x, y + fm.ascent(), note_str)
                if inst:
                    p.setPen(inst_col)
                    p.drawText(cx + inst_x, y + fm.ascent(),
                                 f"{inst:02X}")
                else:
                    p.setPen(QColor(GT_DIM))
                    p.drawText(cx + inst_x, y + fm.ascent(), "..")
                if full_format:
                    cmdtxt = (f"{cmd:01X}{param:03X}"[:4]
                                if cmd else "....")
                    p.setPen(cmd_col)
                    p.drawText(cx + cmd_x, y + fm.ascent(), cmdtxt)


class SIDVisualizer:
    """Receives REAL per-voice waveform data from the parallel
    visualization SID engine. No band-splitting tricks - each voice
    has its own oscilloscope showing exactly what that physical
    voice produces.

    Pattern note detection: FFT pitch detection on each voice's
    samples gives the dominant note, which is pushed into the
    GoatTracker pattern grid."""

    SAMPLE_RATE = 44100

    def __init__(self):
        self._oscilloscopes = []
        self._pattern_view = None
        self._last_notes = []
        self._update_counter = 0
        self._n_voices = 3

    def attach(self, scopes, pattern_view):
        self._oscilloscopes = list(scopes)
        self._pattern_view = pattern_view
        self._n_voices = len(scopes)
        self._last_notes = [None] * self._n_voices

    def feed_voices(self, voices):
        """voices: list of np.float32 mono arrays, one per voice
        (length: SIDAudioThread.VOICE_BLOCK_FRAMES)."""
        if not voices:
            return
        # Update each oscilloscope with its voice's samples
        for i, scope in enumerate(self._oscilloscopes):
            if scope is None or i >= len(voices):
                continue
            scope.set_samples(voices[i])
        # Pattern note detection every few blocks
        self._update_counter += 1
        if (self._update_counter >= 2
                and self._pattern_view is not None):
            self._update_counter = 0
            self._update_pattern(voices)

    def reset(self):
        if self._pattern_view:
            self._pattern_view.reset()
        self._last_notes = [None] * self._n_voices

    def _update_pattern(self, voices):
        for i, samples in enumerate(voices):
            if i >= self._n_voices:
                break
            if samples is None or len(samples) < 64:
                continue
            if np.abs(samples).max() < 0.005:
                if self._last_notes[i] is not None:
                    self._pattern_view.push_voice_event(i, None, 0.0)
                    self._last_notes[i] = None
                continue
            win = np.hanning(len(samples))
            spec = np.abs(np.fft.rfft(samples * win))
            freqs = np.fft.rfftfreq(len(samples),
                                       1.0 / self.SAMPLE_RATE)
            # SID range
            mask = (freqs >= 30) & (freqs <= 6000)
            spec_masked = spec.copy()
            spec_masked[~mask] = 0
            peak = int(np.argmax(spec_masked))
            peak_freq = freqs[peak]
            peak_mag = spec_masked[peak]
            if peak_freq < 30 or peak_mag < 1.0:
                continue
            note = int(round(69 + 12 * np.log2(peak_freq / 440.0)))
            note = max(0, min(127, note))
            if note != self._last_notes[i]:
                intensity = float(min(1.0, peak_mag / 30.0))
                self._pattern_view.push_voice_event(i, note, intensity)
                self._last_notes[i] = note
        self._pattern_view.advance_row()


class GTTitleBar(QWidget):
    def __init__(self, app_text: str, file_text: str = "", parent=None):
        super().__init__(parent)
        self._app = app_text
        self._file = file_text
        self.setFixedHeight(18)
        self._font = QFont()
        self._font.setFamilies(["Topaz-8", "Cascadia Mono",
                                  "Consolas", "monospace"])
        self._font.setPixelSize(11)
        self._font.setBold(True)

    def set_file(self, name: str):
        self._file = name
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, QColor(GT_TITLE_BG))
        p.setPen(QColor(GT_TITLE_FG))
        p.setFont(self._font)
        p.drawText(rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignLeft, self._app)
        p.drawText(rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignRight, self._file)


class GTButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {GT_FRAME};
                color: {GT_NOTE_NORMAL};
                border: 2px solid {GT_NOTE_NORMAL};
                padding: 4px 12px;
                font-family: 'Topaz-8','Cascadia Mono',monospace;
                font-weight: bold;
                font-size: {scaled_font_px(11)}px;
                min-width: 60px;
            }}
            QPushButton:pressed {{
                background-color: {GT_CUR_BG};
                color: {GT_NOTE_CUR};
            }}
            QPushButton:disabled {{
                color: {GT_DIM};
                border-color: {GT_DIM};
            }}
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class _DownloadSonglengthsWorker(QThread):
    """Background worker that downloads the HVSC songlengths DB."""
    progress = pyqtSignal(int, int, str)   # read, total_or_-1, status
    done = pyqtSignal(bool, str)            # ok, path-or-error

    def __init__(self, dest_path, parent=None):
        super().__init__(parent)
        self._dest = dest_path

    def run(self):
        from .songlengths import download_songlengths

        def cb(read, total, status):
            self.progress.emit(read, total if total else -1, status)
        ok, info = download_songlengths(
            self._dest, progress_callback=cb)
        self.done.emit(ok, info)


class SongLengthsDialog(QDialog):
    """Manage the HVSC Songlengths.md5 database.

    - Shows current path + status (loaded / missing / entry count)
    - Download from HVSC mirror with progress bar
    - Manual file picker for a locally-stored copy
    - Delete to revert to "not loaded"
    """

    def __init__(self, current_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HVSC Songlengths Database")
        self.resize(560, 0)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "songlengths")
        self._dest_path = current_path
        self._worker = None

        from PyQt6.QtWidgets import (
            QPushButton, QLabel, QDialogButtonBox, QProgressBar,
            QFileDialog as _QFileDialog,
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        title = QLabel("<b>HVSC Songlengths Database</b>")
        outer.addWidget(title)

        info = QLabel(
            "When loaded, the player shows real song durations and "
            "auto-advances to the next subsong / track when each "
            "tune ends.<br><br>"
            "<b>Location:</b><br>"
            f"<code>{current_path}</code>")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: #ddd; font-size: {scaled_font_px(11)}px;")
        outer.addWidget(info)

        self.lbl_status = QLabel("checking...")
        self.lbl_status.setStyleSheet(
            "color: #fc0; padding: 6px; "
            "background: #222; font-family: monospace;")
        outer.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        bar = QHBoxLayout()
        self.btn_download = QPushButton("Download from HVSC")
        self.btn_download.clicked.connect(self._on_download)
        self.btn_download.setToolTip(
            "Fetch a fresh Songlengths.md5 from a HVSC mirror.\n"
            "Tries transbyte.org, hvsc.de, and c64.com in order.")
        bar.addWidget(self.btn_download)
        self.btn_pick = QPushButton("Pick existing file...")
        self.btn_pick.clicked.connect(self._on_pick)
        self.btn_pick.setToolTip(
            "Use a Songlengths.md5 you already have. It gets\n"
            "copied to the standard config location.")
        bar.addWidget(self.btn_pick)
        self.btn_delete = QPushButton("Remove")
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_delete.setToolTip(
            "Delete the current Songlengths.md5. Player will\n"
            "fall back to library-default tune durations.")
        bar.addWidget(self.btn_delete)
        bar.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(
            QDialogButtonBox.StandardButton.Close
        ).clicked.connect(self.close)
        bar.addWidget(bb)
        outer.addLayout(bar)

        self._refresh_status()

    def _refresh_status(self):
        import os
        from .songlengths import SongLengthsDB
        if not os.path.isfile(self._dest_path):
            self.lbl_status.setText(
                "  not found - no song durations available  ")
            self.btn_delete.setEnabled(False)
            return
        size_kb = os.path.getsize(self._dest_path) / 1024
        # Parse to get entry count (fresh, ignoring cache)
        try:
            db = SongLengthsDB(self._dest_path)
            n = len(db)
            self.lbl_status.setText(
                f"  loaded: {n} entries, {size_kb:.0f} KB  ")
        except Exception as e:
            self.lbl_status.setText(
                f"  found but unreadable: {e}  ")
        self.btn_delete.setEnabled(True)

    def _on_download(self):
        self.btn_download.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)   # indeterminate until we know
        self.progress.setValue(0)
        self.lbl_status.setText("  starting download...  ")
        self._worker = _DownloadSonglengthsWorker(
            str(self._dest_path), self)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_progress(self, read, total, status):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(read)
            pct = 100 * read // total
            self.lbl_status.setText(
                f"  {status}  ({pct}%, {read // 1024} KB)")
        else:
            self.lbl_status.setText(
                f"  {status}  ({read // 1024} KB)")

    def _on_done(self, ok, info):
        self.progress.setVisible(False)
        self.btn_download.setEnabled(True)
        self.btn_pick.setEnabled(True)
        if not ok:
            self.lbl_status.setText(
                f"  download failed: {info}  ")
        self._refresh_status()
        self._worker = None

    def _on_pick(self):
        from PyQt6.QtWidgets import QFileDialog
        import shutil, os
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick a Songlengths.md5",
            "", "Songlengths (*.md5);;All files (*)")
        if not path:
            return
        try:
            os.makedirs(
                os.path.dirname(os.path.abspath(self._dest_path)),
                exist_ok=True)
            shutil.copyfile(path, str(self._dest_path))
        except (OSError, shutil.SameFileError) as e:
            self.lbl_status.setText(f"  copy failed: {e}  ")
            return
        self._refresh_status()

    def _on_delete(self):
        import os
        try:
            os.unlink(self._dest_path)
        except OSError as e:
            self.lbl_status.setText(f"  delete failed: {e}  ")
            return
        self._refresh_status()


class SIDPlayerDialog(QDialog):
    # Class-level flag: True once the ROM-help dialog has been
    # auto-shown in this process. Prevents nagging the user every
    # time they open another RSID in the same Quopus session.
    _rom_help_shown = False

    @staticmethod
    def check_audio_available(parent=None) -> bool:
        """Returns True iff the 'sounddevice' module is available.
        Otherwise pops up an instructional dialog and returns False.

        Callers should use this BEFORE constructing the dialog so the
        UI never half-opens. Example:

            if SIDPlayerDialog.check_audio_available(self):
                SIDPlayerDialog(p, self).exec()
        """
        try:
            import sounddevice as _sd  # noqa: F401
            return True
        except Exception as e:
            # Catches both ImportError (module not installed) and
            # OSError ("PortAudio library not found" - sounddevice
            # imports cleanly but its native dep is missing).
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
                title = "SID Player - PortAudio missing"
            else:
                detail = (
                    "The 'sounddevice' Python package is required "
                    "for SID playback but is not installed.\n\n"
                    "Install it with:\n\n"
                    "    pip install sounddevice numpy\n\n"
                    "On Linux without a venv, you may need:\n"
                    "    pip install --user sounddevice numpy\n"
                    "or:\n"
                    "    apt install python3-sounddevice python3-numpy")
                title = "SID Player - missing dependency"
            QMessageBox.critical(parent, title,
                f"{detail}\n\nAfter installing, restart Quopus and try again.")
            return False

    """Real-time SID player. Uses libsidplayfp under the hood for
    actual emulation - no pre-rendering, instant subsong switching,
    real per-voice oscilloscopes via the engine's mute API."""

    # Class-level cached SongLengthsDB. Populated lazily on first
    # access so the file-parse only happens once per app session.
    _SONGLENGTHS_DB = None

    @classmethod
    def _get_songlengths_db(cls):
        """Return the (lazily-loaded) HVSC SongLengthsDB. Looks for
        config/Songlengths.md5 next to the quopus.cfg file. Cached on
        the class so multiple player dialogs share one parsed DB."""
        if cls._SONGLENGTHS_DB is not None:
            return cls._SONGLENGTHS_DB
        from .config import CONFIG_DIR
        from .songlengths import SongLengthsDB
        cls._SONGLENGTHS_DB = SongLengthsDB(
            CONFIG_DIR / "Songlengths.md5")
        return cls._SONGLENGTHS_DB

    @classmethod
    def _reset_songlengths_db(cls):
        """Force-reload of the HVSC songlengths DB next time it's
        requested. Used after a download so the new file is picked up
        without restarting the player."""
        cls._SONGLENGTHS_DB = None

    def _hvsc_button_text(self) -> str:
        """Compact label for the HVSC button showing DB status:
        '+HVSC N' = N entries loaded, '-HVSC' = not loaded."""
        try:
            n = len(self._songlengths) if self._songlengths else 0
        except Exception:
            n = 0
        if n > 0:
            return f"+HVSC {n // 1000}k" if n >= 1000 else f"+HVSC {n}"
        return "-HVSC"

    def _on_hvsc_clicked(self):
        """Open the songlengths dialog and on close refresh the
        loaded DB + button label."""
        from .config import CONFIG_DIR
        dlg = SongLengthsDialog(CONFIG_DIR / "Songlengths.md5", self)
        dlg.exec()
        # Always refresh after dialog closes - the user might have
        # downloaded, changed path, or deleted.
        self._reset_songlengths_db()
        self._songlengths = self._get_songlengths_db()
        try:
            self._hvsc_btn.setText(self._hvsc_button_text())
        except Exception:
            pass

    @staticmethod
    def _wrap_filename(name: str, width: int = 14) -> str:
        """Wrap a long filename onto multiple lines for the multi-SID
        TUNE-tag display. Tries to break at natural separator chars
        (underscore, hyphen, dot) so words stay together; falls back
        to a hard break at `width` if no separator is near.

        Example:
            'Tuneful_Eight_tune_1_2SID' -> 'Tuneful_Eight_\\ntune_1_2SID'
        """
        if not name:
            return ""
        if len(name) <= width:
            return name
        SEPS = "_-. "
        out_lines = []
        rest = name
        while len(rest) > width:
            # Pick the split point: latest separator within [width//2 .. width]
            cut = -1
            for i in range(width, max(0, width // 2) - 1, -1):
                if i < len(rest) and rest[i] in SEPS:
                    cut = i + 1   # include the separator on the previous line
                    break
            if cut <= 0:
                # No good break - hard wrap at width
                cut = width
            out_lines.append(rest[:cut])
            rest = rest[cut:]
        if rest:
            out_lines.append(rest)
        return "\n ".join(out_lines)

    def __init__(self, path: Path, parent=None, *,
                  shuffle_files: list | None = None,
                  multi_files: list | None = None):
        """Open a SID file for playback.

        If shuffle_files is given (a list of pathlib.Path entries),
        the player runs in SHUFFLE MODE: prev/next buttons appear
        in the transport row and let the user navigate the playlist.
        When the current track ends, the next one starts automatically.

        If multi_files is given (also a list of pathlib.Path entries),
        the player runs in MULTI-SID MODE: all listed files are loaded
        into separate libsidplayfp engines and rendered in parallel,
        their audio averaged into a single stereo mix. Used for
        productions like 'The Tuneful Eight' where 4 separate SID
        files form one composition. The first file in the list is
        used for header / metadata display.

        `shuffle_files` and `multi_files` are keyword-only so the
        legacy two-positional call SIDPlayerDialog(path, parent_widget)
        keeps working."""
        super().__init__(parent)
        # Force this dialog to behave as a regular non-modal top-level
        # window: setModal(False) covers the explicit modality flag,
        # and the Qt.Window window-type makes it a proper top-level
        # frame with its own taskbar entry / minimise button instead
        # of staying chained to the parent's modality. Together this
        # keeps Quopus fully usable while the player is running.
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # Audio dependency was already checked by the caller via
        # SIDPlayerDialog.check_audio_available(); no need to repeat
        # it here. (See class staticmethod above.)
        self.path = Path(path)
        # Multi-SID mode: store the full file list. The dialog will
        # load `len(multi_files)` parallel engines instead of one.
        self._multi_mode = (multi_files is not None
                              and len(multi_files) > 1)
        self._multi_files = ([Path(p) for p in multi_files]
                              if self._multi_mode else [self.path])
        self.setWindowTitle("Python SidPlayer V1.0 by lA-sTYLe")
        self.resize(960, 720)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "sidplayer")
        self.setStyleSheet(f"""
            QDialog {{ background-color: {GT_BG}; color: {GT_NOTE_NORMAL}; }}
            QLabel {{ color: {GT_NOTE_NORMAL};
                       font-family: 'Topaz-8','Cascadia Mono',monospace;
                       font-weight: bold; }}
            QSpinBox {{
                background-color: {GT_PANEL};
                color: {GT_HEADER};
                border: 1px solid {GT_NOTE_NORMAL};
                font-family: 'Topaz-8','Cascadia Mono',monospace;
                font-weight: bold;
                padding: 2px;
            }}
            QCheckBox {{
                color: {GT_NOTE_NORMAL};
                font-family: 'Topaz-8','Cascadia Mono',monospace;
                font-weight: bold;
            }}
            QSlider::groove:horizontal {{
                background: {GT_PANEL};
                border: 1px solid {GT_FRAME};
                height: 6px;
            }}
            QSlider::sub-page:horizontal {{
                background: {GT_NOTE_NORMAL};
                height: 6px;
            }}
            QSlider::handle:horizontal {{
                background: {GT_NOTE_CUR};
                width: 12px;
                margin: -4px 0;
            }}
        """)

        self._header: SIDHeader | None = None
        self._engine: SIDPlayer | None = None
        self._vis_engines: list = []
        self._audio: SIDAudioThread | None = None
        self._cur_subsong = 1
        self._is_playing = False
        self._is_paused = False
        self._cleaned_up = False
        self._visualizer = SIDVisualizer()

        # Shuffle mode
        from .shuffle import ShufflePlaylist
        self._shuffle_mode = shuffle_files is not None
        self._playlist: ShufflePlaylist | None = None
        if self._shuffle_mode:
            self._playlist = ShufflePlaylist(
                files=list(shuffle_files), start=self.path)

        # HVSC Songlengths database for real song-end detection.
        # Cached on the class so we only parse the file once per session,
        # not per dialog. Lookup keys are MD5 hashes computed via
        # libsidplayfp's createMD5New() (HVSC #68+ format).
        self._songlengths = self._get_songlengths_db()
        # Cached duration for the current (file, subsong) - in
        # milliseconds, or None if unknown.
        self._cur_duration_ms: int | None = None
        # Flag set when the song-end has been detected and we're
        # waiting out the 2-second grace period before restarting
        # or advancing. Prevents the tick from re-firing the action
        # repeatedly during the wait.
        self._end_pending = False

        try:
            data = self.path.read_bytes()
            self._header = SIDHeader(data)
        except Exception as e:
            QMessageBox.critical(self, "SID Player",
                                   f"Could not parse SID header:\n{e}")
            self._build_empty_layout()
            return
        try:
            # In multi-SID mode we load N independent libsidplayfp
            # engines. Each renders its own tune; the audio thread
            # mixes their outputs to a single stereo signal.
            # self._engine remains the FIRST engine for backwards
            # compatibility with all the code paths that just want
            # to read num_sids / num_subsongs / time_ms etc. The
            # full list lives in self._engines.
            self._engines: list[SIDPlayer] = []
            for f in self._multi_files:
                d = f.read_bytes()
                eng = SIDPlayer(sample_rate=44100)
                if not eng.load(d):
                    err = eng._lib.sid_get_error(eng._h)
                    err_s = err.decode('utf-8', errors='replace') if err else ""
                    raise RuntimeError(
                        f"engine load ({f.name}): {err_s}")
                self._engines.append(eng)
            # Primary engine = first; that's what subsong/time UI
            # binds against. In multi mode the others run in parallel
            # but driven from the same subsong index (we issue
            # select_subsong on all of them).
            self._engine = self._engines[0]
            # Per-voice visualization engines. In SINGLE-tune mode we
            # create one vis-engine per physical voice (3 for 1SID,
            # 6 for 2SID, 9 for 3SID). In MULTI-tune mode we'd need
            # 24+ vis-engines which is way too much CPU - so we skip
            # vis-engine creation entirely there and use a per-tune
            # spectrum-analyzer ("lite vis") instead. That display
            # is fed from the per-tune audio chunks that the audio
            # mixer already produces, so it's effectively free.
            self._vis_engines: list[SIDPlayer] = []
            self._vis_engine_owners: list[int] = []
            if not self._multi_mode:
                for eng_idx, master in enumerate(self._engines):
                    n_sids_master = max(1, master.num_sids)
                    d = self._multi_files[eng_idx].read_bytes()
                    for chip in range(n_sids_master):
                        for v in range(3):
                            vis = SIDPlayer(sample_rate=44100)
                            if not vis.load(d):
                                err = vis._lib.sid_get_error(vis._h)
                                err_s = (err.decode('utf-8',
                                                     errors='replace')
                                          if err else "")
                                raise RuntimeError(
                                    f"vis engine load failed: {err_s}")
                            self._vis_engines.append(vis)
                            self._vis_engine_owners.append(eng_idx)
                # Apply voice mutes - each vis solo's its target voice
                # within its OWNING master tune.
                cursor = 0
                for eng_idx, master in enumerate(self._engines):
                    n_sids = max(1, master.num_sids)
                    for vidx in range(n_sids * 3):
                        vis = self._vis_engines[cursor + vidx]
                        target_chip = vidx // 3
                        target_voice = vidx % 3
                        for c in range(n_sids):
                            for v in range(3):
                                is_target = (c == target_chip
                                              and v == target_voice)
                                vis._lib.sid_mute(vis._h, c, v,
                                                    0 if is_target else 1)
                    cursor += n_sids * 3
        except Exception as e:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("SID Player")
            box.setText("libsidwrapper failed to load.\n"
                         "Click 'Show Details...' for the diagnostic.")
            box.setDetailedText(str(e))
            box.exec()
            self._build_empty_layout()
            return

        self._build_layout()
        self._cur_subsong = (self._engine.default_subsong
                             or self._header.default_song or 1)
        self._select_subsong(self._cur_subsong)
        self._start_playback()
        # Auto-pop the ROM help once if we just opened an RSID and no
        # ROMs are installed - those tunes almost always play silence
        # in that state, so the user shouldn't have to figure out why.
        # Only fires for true RSIDs (RSID magic in header), not PSIDs,
        # since simple PSIDs commonly play fine without ROMs and a
        # popup there would be noisy. Triggered once per process via
        # a class-level flag so reopening more RSIDs in the same
        # session is silent (the orange link in the header is still
        # always available).
        try:
            magic = (self._header.magic if self._header else b'')
            k, b, c = _find_c64_roms()
            if (magic == b'RSID' and not (k and b and c)
                    and not SIDPlayerDialog._rom_help_shown):
                SIDPlayerDialog._rom_help_shown = True
                from PyQt6.QtCore import QTimer as _QT
                _QT.singleShot(300, self._show_rom_help)
        except Exception:
            pass

    def _build_empty_layout(self):
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("SID load failed."))
        btn = GTButton("Close")
        btn.clicked.connect(self.close)
        lay.addWidget(btn)

    def _build_layout(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._title = GTTitleBar(
            "Python SidPlayer V1.0 by lA-sTYLe",
            self._title_file_text())
        outer.addWidget(self._title)
        body = QWidget()
        outer.addWidget(body, stretch=1)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(8, 8, 8, 8)
        body_lay.setSpacing(8)

        h = self._header
        meta_box = QVBoxLayout()
        meta_box.setSpacing(2)

        if self._multi_mode and len(self._engines) > 1:
            # Multi-SID mode: show one line per loaded file with
            # its individual metadata. Then a totals line at the end.
            title_text = f"MULTI-SID  ·  {len(self._engines)} tunes"
            self._meta_title = QLabel(title_text)
            self._meta_title.setStyleSheet(
                f"color: {GT_HEADER}; font-size: {scaled_font_px(18)}px; "
                f"font-weight: bold;")
            meta_box.addWidget(self._meta_title)
            # One line per tune. Each shows "TUNE N: filename - Title
            # by Author (Year)  -  Nx SID model"
            self._meta_tune_labels = []
            total_sids = 0
            total_voices = 0
            for i, eng in enumerate(self._engines):
                # Parse the per-file PSID header for its own metadata
                try:
                    fdata = self._multi_files[i].read_bytes()
                    fhdr = SIDHeader(fdata)
                except Exception:
                    fhdr = None
                fname = self._multi_files[i].name
                n_sids_i = max(1, eng.num_sids)
                total_sids += n_sids_i
                total_voices += n_sids_i * 3
                # Build the chip-model breakdown for this tune
                if n_sids_i >= 2:
                    chips_str = " + ".join(
                        eng.chip_model(j) for j in range(n_sids_i))
                    chip_label = f"{n_sids_i}× SID ({chips_str})"
                else:
                    chip_label = f"SID ({eng.chip_model(0)})"
                # Compose the line
                title_part = (fhdr.name if fhdr and fhdr.name
                                else self._multi_files[i].stem)
                author_part = (f" by {fhdr.author}"
                                 if fhdr and fhdr.author else "")
                year_part = (f" ({fhdr.released})"
                              if fhdr and fhdr.released else "")
                line = (f"  TUNE {i+1}:  {fname}  —  "
                        f"{title_part}{author_part}{year_part}  ·  "
                        f"{chip_label}")
                lbl = QLabel(line)
                lbl.setStyleSheet(
                    f"color: {GT_NOTE_NORMAL}; font-size: {scaled_font_px(11)}px;")
                meta_box.addWidget(lbl)
                self._meta_tune_labels.append(lbl)
            # Totals summary line
            self._meta_tech = QLabel(
                f"  TOTAL: {len(self._engines)} tunes, "
                f"{total_sids} SIDs, {total_voices} voices  ·  "
                f"all rendered in lockstep, audio averaged")
            self._meta_tech.setStyleSheet(
                f"color: {GT_DIM}; font-size: {scaled_font_px(11)}px; "
                f"font-style: italic;")
            meta_box.addWidget(self._meta_tech)
            # Stub-out the legacy author/released labels - the
            # multi-mode lines above replace them. Keep references
            # so any code that touches them still finds something.
            self._meta_author = QLabel("")
            self._meta_released = QLabel("")
        else:
            # Single-tune mode (original layout)
            title_text = (h.name if h else self.path.stem) or self.path.stem
            self._meta_title = QLabel(title_text)
            self._meta_title.setStyleSheet(
                f"color: {GT_HEADER}; font-size: {scaled_font_px(18)}px; "
                f"font-weight: bold;")
            meta_box.addWidget(self._meta_title)
            self._meta_author = QLabel(
                f"by {h.author}" if (h and h.author) else "")
            meta_box.addWidget(self._meta_author)
            self._meta_released = QLabel(
                (h.released if (h and h.released) else ""))
            meta_box.addWidget(self._meta_released)
            self._meta_tech = QLabel("")
            self._meta_tech.setStyleSheet(f"color: {GT_DIM};")
            self._meta_tech.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self._meta_tech.linkActivated.connect(
                self._show_rom_help)
            if h:
                n_sids = self._engine.num_sids if self._engine else 1
                if n_sids >= 2 and self._engine:
                    chips_str = " + ".join(
                        self._engine.chip_model(i) for i in range(n_sids))
                    chip_label = f"{n_sids}× SID ({chips_str})"
                else:
                    chip = (self._engine.chip_model(0)
                              if self._engine else 'unknown')
                    chip_label = f"SID model: {chip}"
                # Indicate whether C64 ROMs are loaded - this is the
                # difference between "RSIDs play" and "RSIDs are
                # silent". User-visible so the cause is obvious.
                # When ROMs are missing the label is rendered as a
                # clickable link so the user can read setup help
                # without having to dig through README files.
                k, b, c = _find_c64_roms()
                if k and b and c:
                    rom_label = "ROMs: OK"
                elif k or b or c:
                    parts = []
                    if k: parts.append("kernal")
                    if b: parts.append("basic")
                    if c: parts.append("chargen")
                    rom_label = (f"<a href='rom-help' "
                                  f"style='color:#ff8c00;'>"
                                  f"ROMs: partial ({'+'.join(parts)}) "
                                  f"- click for help</a>")
                else:
                    rom_label = ("<a href='rom-help' "
                                  "style='color:#ff5555;'>"
                                  "ROMs: missing - click for setup help"
                                  "</a>")
                self._meta_tech.setText(
                    f"{h.magic} v{h.version}  •  {chip_label}  "
                    f"•  {rom_label}")
            meta_box.addWidget(self._meta_tech)
        body_lay.addLayout(meta_box)

        # Determine voice layout. In single-tune mode this is just
        # the master engine's SID count; in multi-SID mode we stack
        # one row per loaded tune, each row sized to that tune's
        # voice count (3 for 1SID, 6 for 2SID, etc.).
        if self._multi_mode:
            # List of voice counts, one per loaded tune
            voices_per_tune = [max(1, e.num_sids) * 3
                                 for e in self._engines]
            n_voices = sum(voices_per_tune)
            # n_sids in this branch = total number of "rows" for the
            # mute-checkbox grouping below; we treat each tune as its
            # own visual SID for layout purposes.
            n_sids = max(1, self._engine.num_sids if self._engine else 1)
        else:
            n_sids = max(1, self._engine.num_sids if self._engine else 1)
            n_voices = n_sids * 3
            voices_per_tune = [n_voices]

        # Scopes - one per physical voice. Single-tune layout: one
        # horizontal row per chip. Multi-tune layout: one row per
        # tune (with that tune's chips inline). A spectrum analyzer
        # sits to the right of the scope block.
        scopes_and_vu = QHBoxLayout()
        scopes_and_vu.setSpacing(8)
        scope_container = QVBoxLayout()
        scope_container.setSpacing(4)
        self._scopes = []
        # Per-tune spectrum analyzers - populated only in multi mode.
        # These are the LITE visualizer: one small EQ-style spectrum
        # per tune fed from that tune's PRE-MIX audio output. ~zero
        # extra CPU because the per-tune chunks already exist as a
        # side product of the audio thread's mixing.
        self._tune_specs: list = []
        if self._multi_mode:
            # One row per tune. Within a row: a small label tag for
            # the tune (filename), then a per-tune mini spectrum
            # analyzer instead of the 24 voice oscilloscopes which
            # would need 24 parallel libsidplayfp engines.
            from .spectrum import SpectrumAnalyzer
            for tune_idx, vcount in enumerate(voices_per_tune):
                row = QHBoxLayout()
                row.setSpacing(6)
                # Tag with full filename, wrapped onto multiple
                # lines (every ~14 chars). Long SID filenames like
                # "Tuneful_Eight_tune_1_2SID" need this so the name
                # isn't truncated.
                fname = self._multi_files[tune_idx].stem
                # Soft-wrap at ~14 chars: insert a newline, but only
                # at "natural" boundary chars (_ - .) when possible.
                wrapped = self._wrap_filename(fname, 14)
                tag = QLabel(f" TUNE {tune_idx + 1} \n {wrapped} ")
                tag.setStyleSheet(
                    f"color: {GT_HEADER}; "
                    f"background: {GT_PANEL}; "
                    f"padding: 4px; border: 1px solid {GT_FRAME}; "
                    f"font-family: 'Topaz-8','Cascadia Mono',monospace; "
                    f"font-size: {scaled_font_px(11)}px;")
                tag.setAlignment(Qt.AlignmentFlag.AlignTop
                                  | Qt.AlignmentFlag.AlignLeft)
                tag.setFixedWidth(120)
                row.addWidget(tag)
                # Per-tune spectrum - compact 8-band EQ display.
                # Fixed height matches a normal scope row (~80 px).
                tune_spec = SpectrumAnalyzer()
                tune_spec.setFixedHeight(78)
                self._tune_specs.append(tune_spec)
                row.addWidget(tune_spec, stretch=1)
                scope_container.addLayout(row)
        else:
            for chip in range(n_sids):
                row = QHBoxLayout()
                row.setSpacing(8)
                for v in range(3):
                    voice_idx = chip * 3 + v
                    if n_sids == 1:
                        label = f"VOICE {v+1}"
                    else:
                        label = f"V{voice_idx+1} (SID{chip+1})"
                    osc = Oscilloscope(label=label)
                    self._scopes.append(osc)
                    row.addWidget(osc)
                scope_container.addLayout(row)
        scopes_and_vu.addLayout(scope_container, stretch=1)
        # 10-band spectrum analyzer (graphic-EQ style) showing the
        # frequency content of the mixed audio.
        from .spectrum import SpectrumAnalyzer
        self._vu = SpectrumAnalyzer()
        # Height scales with the number of scope rows so the spectrum
        # spans the same vertical space as the scope block.
        n_rows = len(voices_per_tune) if self._multi_mode else n_sids
        vu_h = 120 + (n_rows - 1) * 80
        self._vu.setFixedSize(280, vu_h)
        scopes_and_vu.addWidget(self._vu, alignment=Qt.AlignmentFlag.AlignTop)
        body_lay.addLayout(scopes_and_vu)

        # Pattern view - one column per voice. Hidden in multi-SID
        # mode because 24 voices side-by-side is too narrow to read,
        # and pattern data from 4 parallel tunes doesn't combine
        # into a single coherent score anyway.
        self._pattern = GTPatternView(num_voices=n_voices)
        if self._multi_mode:
            self._pattern.hide()
            # In multi-mode the pattern is hidden, so don't reserve
            # vertical space for it - otherwise the scopes float
            # towards the top with a big empty band below.
            body_lay.addWidget(self._pattern, stretch=0)
            body_lay.addStretch(1)
        else:
            body_lay.addWidget(self._pattern, stretch=1)
        self._visualizer.attach(self._scopes, self._pattern)

        # Mute checkboxes - one per physical voice
        mute_row = QHBoxLayout()
        mute_row.addWidget(QLabel("MUTE:"))
        self._voice_checks = []
        if self._multi_mode:
            # In multi mode, individual voice mute would be 24 boxes -
            # not useful. Instead offer per-TUNE mute: silence one of
            # the four parallel tunes entirely. Done via the master
            # engine's mute API on all of its voices.
            for tune_idx, master in enumerate(self._engines):
                fname = self._multi_files[tune_idx].stem
                if len(fname) > 18:
                    fname = fname[:17] + "…"
                cb = QCheckBox(f"Tune {tune_idx+1}: {fname}")
                cb.toggled.connect(
                    lambda checked, ti=tune_idx:
                        self._on_mute_tune(ti, checked))
                mute_row.addWidget(cb)
                self._voice_checks.append(cb)
        else:
            for chip in range(n_sids):
                if n_sids > 1:
                    # Visual separator label per chip
                    sep = QLabel(f"  SID{chip+1}:")
                    sep.setStyleSheet(f"color: {GT_HEADER};")
                    mute_row.addWidget(sep)
                for v in range(3):
                    voice_idx = chip * 3 + v
                    cb = QCheckBox(f"V{voice_idx+1}")
                    # Capture chip and voice index in lambda
                    cb.toggled.connect(
                        lambda checked, c=chip, vv=v:
                            self._on_mute_voice(c, vv, checked))
                    mute_row.addWidget(cb)
                    self._voice_checks.append(cb)
        mute_row.addStretch(1)
        # Visualization toggle - per-voice render is expensive on
        # 2SID/3SID tunes (one libsidplayfp engine per voice). User
        # can disable to drop CPU usage significantly.
        self._vis_check = QCheckBox("VIS")
        # In multi-SID mode the visualizer is the LITE per-tune
        # spectrum analyzer (effectively free), so default it ON.
        # In single-tune mode it's the full per-voice oscilloscopes
        # (expensive on 2SID/3SID), also defaulted ON.
        self._vis_check.setChecked(True)
        self._vis_check.setToolTip(
            "Toggle the per-tune spectrum analyzer. Cheap to run - "
            "fed from the audio mix output, no extra emulator engines."
            if self._multi_mode else
            "Toggle per-voice oscilloscopes. Disabling drops CPU "
            "usage by ~7x for 2SID and ~10x for 3SID tunes since "
            "each voice needs its own libsidplayfp engine.")
        self._vis_check.toggled.connect(self._on_vis_toggle)
        mute_row.addWidget(self._vis_check)
        body_lay.addLayout(mute_row)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        self._play_btn = GTButton("PLAY")
        self._play_btn.clicked.connect(self._toggle_play)
        ctrl.addWidget(self._play_btn)
        self._stop_btn = GTButton("STOP")
        self._stop_btn.clicked.connect(self._stop_playback)
        ctrl.addWidget(self._stop_btn)
        # Shuffle prev/next - only visible in shuffle mode
        self._shuffle_prev_btn = GTButton("|<<")
        self._shuffle_prev_btn.setMaximumWidth(50)
        self._shuffle_prev_btn.setToolTip("Previous track (shuffle mode)")
        self._shuffle_prev_btn.clicked.connect(self._shuffle_prev)
        self._shuffle_prev_btn.setVisible(self._shuffle_mode)
        ctrl.addWidget(self._shuffle_prev_btn)
        self._shuffle_next_btn = GTButton(">>|")
        self._shuffle_next_btn.setMaximumWidth(50)
        self._shuffle_next_btn.setToolTip("Next track (shuffle mode)")
        self._shuffle_next_btn.clicked.connect(self._shuffle_next)
        self._shuffle_next_btn.setVisible(self._shuffle_mode)
        ctrl.addWidget(self._shuffle_next_btn)
        # SHUFFLE-FROM-HERE: pick a new folder and start fresh.
        self._shuffle_pick_btn = GTButton("SHUFFLE")
        self._shuffle_pick_btn.setMaximumWidth(80)
        self._shuffle_pick_btn.setToolTip(
            "Shuffle-play all SIDs from the current track's "
            "directory and subdirectories")
        self._shuffle_pick_btn.clicked.connect(self._shuffle_pick_folder)
        ctrl.addWidget(self._shuffle_pick_btn)
        # In multi-SID mode shuffle doesn't apply - the user picked
        # exactly the N files they want to play together.
        if self._multi_mode:
            self._shuffle_pick_btn.hide()
        # SUBSONG row - we keep references to all four widgets
        # (label, spinbox, "of N" label) so _switch_to can update
        # them when shuffle play moves to a tune with a different
        # number of subsongs.
        self._subsong_label = QLabel("SUBSONG:")
        ctrl.addWidget(self._subsong_label)
        self._subsong_spin = QSpinBox()
        # In multi-SID mode the subsong spinbox covers the maximum
        # subsong count across ALL parallel tunes - if any tune has
        # 5 subsongs, the spinner goes up to 5 and tunes with fewer
        # subsongs clamp internally on select_subsong.
        if self._multi_mode and hasattr(self, '_engines'):
            n_songs = max((e.num_subsongs for e in self._engines),
                            default=1)
        else:
            n_songs = self._engine.num_subsongs if self._engine else 1
        self._subsong_spin.setMinimum(1)
        self._subsong_spin.setMaximum(max(1, n_songs))
        self._subsong_spin.setValue(self._cur_subsong)
        self._subsong_spin.valueChanged.connect(self._on_subsong_change)
        ctrl.addWidget(self._subsong_spin)
        self._subsong_total_label = QLabel(f"of {n_songs}")
        ctrl.addWidget(self._subsong_total_label)
        # Hide the whole subsong row if there's only one subsong
        if n_songs <= 1:
            self._subsong_label.hide()
            self._subsong_spin.hide()
            self._subsong_total_label.hide()
        ctrl.addStretch(1)
        # HVSC songlengths-DB indicator + download button. Click to
        # open the SongLengthsDialog for manual location, status, or
        # auto-download from an HVSC mirror.
        self._hvsc_btn = GTButton(self._hvsc_button_text())
        self._hvsc_btn.setMaximumWidth(120)
        self._hvsc_btn.setToolTip(
            "HVSC Songlengths database status.\n"
            "Click to download a fresh copy or set the path.\n"
            "When loaded, the player shows real song durations\n"
            "and auto-advances when each tune ends.")
        self._hvsc_btn.clicked.connect(self._on_hvsc_clicked)
        ctrl.addWidget(self._hvsc_btn)
        self._time_label = QLabel("00:00")
        ctrl.addWidget(self._time_label)
        ctrl.addWidget(QLabel("VOL"))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setMinimum(0)
        self._vol.setMaximum(100)
        self._vol.setValue(80)
        self._vol.setMaximumWidth(100)
        self._vol.valueChanged.connect(self._on_volume)
        ctrl.addWidget(self._vol)
        body_lay.addLayout(ctrl)

        QShortcut(QKeySequence("Space"), self, self._toggle_play)
        QShortcut(QKeySequence("Esc"),    self, self.close)
        # Subsong navigation - three sets of hotkeys for muscle memory
        QShortcut(QKeySequence("Right"),    self, self._next_subsong)
        QShortcut(QKeySequence("Left"),     self, self._prev_subsong)
        QShortcut(QKeySequence("N"),        self, self._next_subsong)
        QShortcut(QKeySequence("P"),        self, self._prev_subsong)
        # Shuffle navigation - same Ctrl+arrows + Ctrl+N/P as in
        # the MOD player. Methods no-op if not in shuffle mode.
        QShortcut(QKeySequence("Ctrl+Right"), self, self._shuffle_next)
        QShortcut(QKeySequence("Ctrl+Left"),  self, self._shuffle_prev)
        QShortcut(QKeySequence("Ctrl+N"), self, self._shuffle_next)
        QShortcut(QKeySequence("Ctrl+P"), self, self._shuffle_prev)
        QShortcut(QKeySequence("+"),        self, self._next_subsong)
        QShortcut(QKeySequence("-"),        self, self._prev_subsong)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _select_subsong(self, n: int):
        if not self._engine: return
        # In multi-SID mode all master engines step their subsong
        # together. Each tune may have a different number of subsongs
        # (we clamp per-engine) but they all start from the same
        # logical position.
        for master in getattr(self, '_engines', [self._engine]):
            sub = max(1, min(n, master.num_subsongs))
            if not master.select_subsong(sub):
                err = master._lib.sid_get_error(master._h)
                QMessageBox.warning(
                    self, "SID Player",
                    f"Could not select subsong {sub}:\n"
                    f"{err.decode('utf-8','replace') if err else 'unknown'}")
                return
        # Vis engines: reselect subsong AND re-apply mute (engine->load
        # internally resets mutes). Walk the list in chunks per owner
        # master engine so we use the right per-owner num_sids.
        if hasattr(self, '_vis_engine_owners') and self._vis_engine_owners:
            cursor = 0
            for eng_idx, master in enumerate(self._engines):
                n_sids = max(1, master.num_sids)
                sub = max(1, min(n, master.num_subsongs))
                count = n_sids * 3
                for vidx in range(count):
                    vis = self._vis_engines[cursor + vidx]
                    vis.select_subsong(sub)
                    target_chip = vidx // 3
                    target_voice = vidx % 3
                    for c in range(n_sids):
                        for v in range(3):
                            is_target = (c == target_chip
                                          and v == target_voice)
                            vis._lib.sid_mute(vis._h, c, v,
                                                0 if is_target else 1)
                cursor += count
        else:
            # Single-tune fallback (the legacy path)
            n_chips_now = max(1, self._engine.num_sids)
            for vidx, eng in enumerate(self._vis_engines):
                eng.select_subsong(n)
                target_chip = vidx // 3
                target_voice = vidx % 3
                for c in range(n_chips_now):
                    for v in range(3):
                        is_target = (c == target_chip
                                      and v == target_voice)
                        eng._lib.sid_mute(eng._h, c, v,
                                            0 if is_target else 1)
        self._cur_subsong = n
        self._visualizer.reset()
        self._refresh_duration()

    def _refresh_duration(self):
        """Look up the current (tune, subsong) song length in HVSC.
        Stores result in self._cur_duration_ms - or None if unknown.

        In multi-SID mode, take the LONGEST duration across all
        loaded tunes - we want the auto-restart trigger to fire
        only when every parallel tune has played out, not when the
        first one finishes."""
        self._cur_duration_ms = None
        if not self._engine or not self._songlengths.available:
            return
        engines = (getattr(self, '_engines', None) or [self._engine])
        max_ms = None
        for eng in engines:
            ms = None
            for md5_fn in (eng.md5_new, eng.md5_old):
                md5 = md5_fn()
                if not md5:
                    continue
                ms_try = self._songlengths.get_subsong(
                    md5, self._cur_subsong)
                if ms_try is not None:
                    ms = ms_try
                    break
            if ms is None:
                continue
            if max_ms is None or ms > max_ms:
                max_ms = ms
        self._cur_duration_ms = max_ms

    def _start_playback(self):
        if not self._engine or self._is_playing:
            return
        # Pass either the single engine (classic mode) or the full
        # list (multi-SID mode) - SIDAudioThread accepts both.
        target = (self._engines
                   if self._multi_mode and len(self._engines) > 1
                   else self._engine)
        self._audio = SIDAudioThread(target,
                                       vis_engines=self._vis_engines,
                                       parent=self)
        self._audio.set_volume(self._vol.value() / 100.0)
        self._audio.set_vis_enabled(
            self._vis_check.isChecked()
            if hasattr(self, '_vis_check') else True)
        self._audio.buffer_block.connect(self._on_master_block)
        self._audio.voice_blocks.connect(self._on_voice_blocks)
        # Lite vis: per-tune spectrum analyzers fed from the pre-mix
        # tune chunks the audio thread already produces. Only relevant
        # in multi-SID mode; harmless to connect either way.
        self._audio.tune_blocks.connect(self._on_tune_blocks)
        self._audio.error_occurred.connect(self._on_audio_error)
        self._audio.finished_playing.connect(self._on_finished_playing)
        self._audio.start()
        self._is_playing = True
        self._is_paused = False
        self._play_btn.setText("PAUSE")
        # Trial users: arm a 30-second timer that auto-stops the
        # tune. Pro users get unlimited playback. The timer is
        # restarted on every Play so seek/skip don't accidentally
        # extend the trial.
        self._arm_trial_playback_timer()

    def _arm_trial_playback_timer(self):
        """Schedule a 30-second trial cutoff. Pro users with
        FEATURE_SID get nothing (unlimited)."""
        try:
            from quopus_lib import license
            if license.has_feature(license.FEATURE_SID):
                return
        except Exception:
            # If license lookup fails, behave like Pro - we never
            # want a license bug to interrupt a paid user's tune.
            return
        if not hasattr(self, '_trial_play_timer'):
            from PyQt6.QtCore import QTimer
            self._trial_play_timer = QTimer(self)
            self._trial_play_timer.setSingleShot(True)
            self._trial_play_timer.timeout.connect(
                self._on_trial_play_timeout)
        self._trial_play_timer.start(30 * 1000)   # 30 seconds

    def _on_trial_play_timeout(self):
        """30 seconds of trial playback elapsed. Stop and show a
        polite reminder."""
        # Stop playback. Use the existing stop method so all UI
        # state stays consistent.
        if hasattr(self, '_stop_playback'):
            self._stop_playback()
        elif self._is_playing:
            # Defensive fallback if the method name changes
            self._audio.requestInterruption()
            self._is_playing = False
            self._play_btn.setText("PLAY")
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "Trial Time Limit",
            "Trial users can preview SID tunes for 30 seconds.\n\n"
            "Register Quopus to enjoy unlimited tune playback.\n"
            "Press PLAY again to preview another 30 seconds.")

    def _on_voice_blocks(self, voices: list):
        self._visualizer.feed_voices(voices)

    def _on_tune_blocks(self, tune_chunks: list):
        """Multi-SID lite visualizer: feed each tune's pre-mix audio
        chunk into its dedicated mini spectrum analyzer. Skipped in
        single-tune mode (no per-tune specs created)."""
        if not self._tune_specs:
            return
        for spec, chunk in zip(self._tune_specs, tune_chunks):
            spec.feed_block(chunk, sample_rate=44100)

    def _stop_playback(self):
        # Cancel any pending auto-restart timer
        self._end_pending = False
        # Also stop the trial-mode time-limit timer if it's
        # armed - otherwise it could fire mid-stop and call us
        # recursively.
        t = getattr(self, '_trial_play_timer', None)
        if t is not None:
            t.stop()
        if self._audio:
            self._audio.stop()
            if not self._audio.wait(1500):
                try: self._audio.terminate()
                except Exception: pass
                self._audio.wait(500)
            self._audio = None
        if self._engine:
            self._engine.stop()
        self._is_playing = False
        self._is_paused = False
        if hasattr(self, '_play_btn'):
            self._play_btn.setText("PLAY")
        for s in getattr(self, '_scopes', []):
            s.set_samples(np.zeros(64, dtype=np.float32))
        if hasattr(self, '_vu'):
            self._vu.reset()
        for spec in getattr(self, '_tune_specs', []):
            spec.reset()

    def _toggle_play(self):
        # If the audio thread has died (natural song end) but _is_playing
        # hasn't been reset yet (signal still in flight), force a restart.
        # Without this, hitting PLAY would just toggle the pause flag on
        # a dead thread and produce silence forever.
        thread_dead = (self._audio is None
                        or not self._audio.isRunning())
        if self._is_playing and thread_dead:
            self._stop_playback()  # clean up bookkeeping
            # fall through to the not-playing branch below

        if not self._is_playing:
            if self._engine:
                self._engine.select_subsong(self._cur_subsong)
            # Re-sync all vis engines to current subsong + restore mutes
            n_chips_now = max(1, self._engine.num_sids if self._engine else 1)
            for vidx, eng in enumerate(self._vis_engines):
                eng.select_subsong(self._cur_subsong)
                target_chip = vidx // 3
                target_voice = vidx % 3
                for c in range(n_chips_now):
                    for v in range(3):
                        is_target = (c == target_chip
                                      and v == target_voice)
                        eng._lib.sid_mute(eng._h, c, v,
                                            0 if is_target else 1)
            self._visualizer.reset()
            self._start_playback()
            return
        self._is_paused = not self._is_paused
        if self._audio:
            self._audio.set_paused(self._is_paused)
        self._play_btn.setText("RESUME" if self._is_paused else "PAUSE")

    def _on_vis_toggle(self, enabled: bool):
        """Enable or disable visualization. In single-tune mode this
        controls the per-voice oscilloscopes (each backed by its own
        libsidplayfp engine, so CPU-heavy). In multi-SID mode it
        controls the per-tune spectrum analyzers (fed from the audio
        mix - effectively free)."""
        if self._audio:
            self._audio.set_vis_enabled(enabled)
        if not enabled:
            # Clear scope and per-tune spectrum displays
            for s in self._scopes:
                s.set_samples(np.zeros(64, dtype=np.float32))
            for spec in getattr(self, '_tune_specs', []):
                spec.reset()

    def _on_master_block(self, chunk: np.ndarray):
        # Feed the stereo mix to the spectrum analyzer (10-band EQ
        # display). SID engine runs at 44100 Hz.
        if hasattr(self, '_vu'):
            self._vu.feed_block(chunk, sample_rate=44100)

    def _on_audio_error(self, msg: str):
        QMessageBox.critical(self, "SID Player - audio error", msg)
        self._stop_playback()

    def _restart_subsong(self):
        """Restart the current subsong from the beginning. Equivalent
        to select_subsong(current) - reloads the tune in the engine
        which resets the C64 state, CPU, and SID chips. Audio thread
        keeps running (so vis stays continuous); Engine just rewinds.

        Used both by the auto-loop in _tick (when song length reached)
        and by user clicking PLAY while already playing."""
        if not self._engine:
            return
        # Briefly pause audio output so the rewind isn't audible as
        # a glitch - select_subsong's engine->load() is synchronous
        # and only a few ms anyway.
        was_paused = self._is_paused
        if self._audio:
            self._audio.set_paused(True)
        try:
            # Reset the engine's clock back to 0 by reselecting the
            # same subsong - libsidplayfp's load() runs initialise()
            # which does m_c64.reset() and reinstalls the tune driver.
            self._select_subsong(self._cur_subsong)
        finally:
            if self._audio and not was_paused:
                self._audio.set_paused(False)

    def _on_finished_playing(self):
        """The audio thread terminated because the engine returned
        zero frames (= tune ended naturally). Reset our play state
        so the user can press PLAY again to restart from the
        beginning. In shuffle mode, advance to the next track
        instead of stopping."""
        if self._shuffle_mode and self._playlist is not None:
            self._shuffle_next()
            return
        # Normal mode: stop cleanly, the user can hit PLAY to replay
        self._stop_playback()

    def _on_subsong_change(self, val: int):
        if val == self._cur_subsong:
            return
        # Cancel any pending auto-restart - user is overriding it
        self._end_pending = False
        # Real-time engine - subsong switch is INSTANT
        was_playing = self._is_playing and not self._is_paused
        if self._audio:
            self._audio.set_paused(True)
        self._select_subsong(val)
        if self._audio and was_playing:
            self._audio.set_paused(False)

    def _next_subsong(self):
        if not self._engine:
            return
        max_n = self._engine.num_subsongs
        if max_n <= 1:
            return
        new_val = min(max_n, self._subsong_spin.value() + 1)
        if new_val != self._subsong_spin.value():
            self._subsong_spin.setValue(new_val)

    def _prev_subsong(self):
        if not self._engine:
            return
        new_val = max(1, self._subsong_spin.value() - 1)
        if new_val != self._subsong_spin.value():
            self._subsong_spin.setValue(new_val)

    def _on_mute_voice(self, chip: int, voice: int, checked: bool):
        """User clicked a MUTE checkbox for a physical voice. Mute
        on the AUDIO engine. Vis engines have their own internal
        mute state (each one solos a single voice) and are not
        touched by user mute - if you mute V1 the audio goes
        silent for V1 but the V1 oscilloscope still shows the
        actual SID voice output (since the user might want to see
        what they just muted)."""
        if self._engine:
            self._engine.mute(voice, chip=chip, muted=checked)

    def _on_mute_tune(self, tune_idx: int, checked: bool):
        """Multi-SID mode: silence/unsilence an entire tune by
        muting all its voices on its master engine. Vis engines
        keep running so the user can still see what's silenced."""
        if not hasattr(self, '_engines'): return
        if not (0 <= tune_idx < len(self._engines)): return
        master = self._engines[tune_idx]
        n_sids = max(1, master.num_sids)
        for c in range(n_sids):
            for v in range(3):
                master.mute(v, chip=c, muted=checked)

    def _on_volume(self, val: int):
        if self._audio:
            self._audio.set_volume(val / 100.0)

    def _shuffle_pick_folder(self):
        """Start shuffle play from the current track's parent
        directory (recursive). No folder picker - shuffles whatever
        was near the file you opened."""
        from .shuffle import ShuffleScanner, ShufflePlaylist
        from PyQt6.QtWidgets import QProgressDialog, QMessageBox
        root = self.path.parent
        if not root.exists():
            return
        pd = QProgressDialog(
            f"Scanning '{root.name}' for SID files...",
            "Cancel", 0, 0, self)
        pd.setWindowTitle("Shuffle Mode")
        pd.setMinimumDuration(200)
        pd.setAutoClose(False)
        pd.setAutoReset(False)
        scanner = ShuffleScanner(root, is_sid_file, parent=self)
        self._active_scanner = scanner
        def on_progress(n):
            pd.setLabelText(
                f"Scanning '{root.name}' for SID files...\n"
                f"Found: {n}")
        def on_done(files):
            pd.close()
            self._active_scanner = None
            if not files:
                QMessageBox.information(
                    self, "Shuffle",
                    f"No SID files found in:\n{root}")
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

    def _show_rom_help(self, _link=None):
        """Show a help dialog explaining what to do when the C64 ROMs
        are missing. Triggered by clicking the orange/red status link
        in the header strip. Same dialog is auto-shown the first time
        the user opens an RSID without ROMs (since RSIDs almost always
        play silence in that case).

        The text covers:
          - Why ROMs are needed (libsidplayfp boots a real C64 ROM
            environment - PSIDs may work without, RSIDs almost never).
          - Where Quopus looks for them (the `roms/` folder + system
            paths the loader checks).
          - How to obtain them legally (older VICE tarballs, vice-data-
            nonfree on Debian, original C64 hardware).
          - Per-platform helper script (setup_c64_roms.sh on Linux/
            macOS, setup_c64_roms.bat on Windows) that auto-imports
            ROMs from a VICE install if found.
        """
        from PyQt6.QtWidgets import QMessageBox
        k, b, c = _find_c64_roms()
        present = []
        if k: present.append("KERNAL")
        if b: present.append("BASIC")
        if c: present.append("CHARGEN")
        missing = [r for r in ("KERNAL", "BASIC", "CHARGEN")
                    if r not in present]

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("C64 ROMs needed")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            "<h3>C64 ROMs needed for full SID compatibility</h3>"
            f"<p>Found: <b>{', '.join(present) or 'none'}</b><br>"
            f"Missing: <b>{', '.join(missing) or 'none'}</b></p>"
            "<p>libsidplayfp emulates a real C64. RSID files (and "
            "many newer PSIDs) call into the C64 KERNAL during their "
            "init routines and play <i>silence</i> without it.</p>"
            "<p><b>How to fix this:</b></p>"
            "<ol>"
            "<li>Run <code>setup_c64_roms.sh</code> (Linux/macOS) or "
            "<code>setup_c64_roms.bat</code> (Windows) - it auto-"
            "imports ROMs from any VICE install on your system.</li>"
            "<li>Or manually drop these three files into the "
            "<code>roms/</code> folder next to quopus.py:"
            "<ul>"
            "<li><code>kernal.901227-03.bin</code> (8192 bytes)</li>"
            "<li><code>basic.901226-01.bin</code> (8192 bytes)</li>"
            "<li><code>chargen.901225-01.bin</code> (4096 bytes)</li>"
            "</ul></li>"
            "</ol>"
            "<p><b>Where to get them:</b><br>"
            "Modern VICE distributions on Linux drop the ROM dumps "
            "for licensing reasons. Easiest sources:"
            "<ul>"
            "<li>Older VICE release that still bundles them: "
            "<a href='https://sourceforge.net/projects/vice-emu/files/releases/'>"
            "sourceforge.net/projects/vice-emu</a> - "
            "any 3.x .tar.gz has them under <code>data/C64/</code></li>"
            "<li>Debian: <code>sudo apt install vice-data-nonfree</code> "
            "(if your distro carries non-free)</li>"
            "<li>Windows: VICE for Windows from the same site still "
            "ships the ROMs</li>"
            "</ul></p>"
            "<p>After installing, restart Quopus and re-open the SID. "
            "The header should change to <b>ROMs: OK</b>.</p>")
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    def _title_file_text(self) -> str:
        """Right-side title bar text. In shuffle mode includes
        playlist position; in multi-SID mode shows the count."""
        if self._multi_mode:
            return f"MULTI-SID ({len(self._multi_files)} tunes)"
        if self._shuffle_mode and self._playlist is not None:
            n = self._playlist.total
            i = self._playlist.index + 1
            return f"{self.path.name}  ({i}/{n} - SHUFFLE)"
        return self.path.name

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
        """Change to a different SID file without recreating the
        whole dialog. Tears down the current engine + vis engines,
        loads the new tune, re-creates the engines, and restarts
        playback. The dialog UI stays - subsong spinbox and voice
        layout adapt to the new tune's chip count."""
        # Stop audio + close engines. We have to walk BOTH the
        # legacy single self._engine slot AND the new self._engines
        # list (used for multi-tune SIDs). _select_subsong reads
        # _engines first via getattr(...) — if we don't reset it
        # here, iterating it after switch_to() hits engine handles
        # from the PREVIOUS file, which were just closed, and
        # libsidplayfp returns 'null handle' on subsong selection.
        # That was the "Could not select subsong 1: null handle"
        # popup at the 2nd shuffle track.
        self._stop_playback()
        old_engines = getattr(self, '_engines', None)
        if old_engines:
            for e in old_engines:
                try: e.close()
                except Exception: pass
            self._engines = []
        if self._engine:
            try: self._engine.close()
            except Exception: pass
            self._engine = None
        for eng in self._vis_engines:
            try: eng.close()
            except Exception: pass
        self._vis_engines = []
        # Same risk for _vis_engine_owners - it's a parallel list
        # to _vis_engines marking which master each visualiser
        # belongs to. Reset to keep _select_subsong's chunking
        # logic in sync with the new engine count.
        if hasattr(self, '_vis_engine_owners'):
            self._vis_engine_owners = []

        # Load the new file and re-create engines
        self.path = Path(new_path)
        try:
            data = self.path.read_bytes()
            self._header = SIDHeader(data)
            self._engine = SIDPlayer(sample_rate=44100)
            if not self._engine.load(data):
                raise RuntimeError("engine load failed")
            # Repopulate _engines so _select_subsong iterates over
            # the right thing. Single-tune shuffle = list of one.
            self._engines = [self._engine]
            n_sids = max(1, self._engine.num_sids)
            for chip in range(n_sids):
                for v in range(3):
                    eng = SIDPlayer(sample_rate=44100)
                    if eng.load(data):
                        self._vis_engines.append(eng)
            # Mirror the per-vis-engine ownership list so
            # _select_subsong's chunking logic (which walks per
            # master) finds the right engine ranges.
            if hasattr(self, '_vis_engine_owners'):
                self._vis_engine_owners = (
                    [self._engine] * (n_sids * 3))
            n_chips_now = n_sids
            for vidx, eng in enumerate(self._vis_engines):
                target_chip = vidx // 3
                target_voice = vidx % 3
                for c in range(n_chips_now):
                    for v in range(3):
                        is_target = (c == target_chip
                                      and v == target_voice)
                        eng._lib.sid_mute(eng._h, c, v,
                                            0 if is_target else 1)
        except Exception as e:
            # Skip and try the next track in shuffle
            print(f"SID skip {new_path.name}: {e}", flush=True)
            QTimer.singleShot(50, self._shuffle_next)
            return

        # Update UI
        if hasattr(self, '_title'):
            self._title.set_file(self._title_file_text())
        # In multi-SID mode the metadata box has its own per-tune
        # layout and shouldn't be rewritten by the single-tune
        # shuffle-switch logic.
        if self._multi_mode:
            return
        # Update metadata labels (title/author/released)
        if hasattr(self, '_meta_title') and self._header:
            self._meta_title.setText(self._header.name or self.path.stem)
        if hasattr(self, '_meta_author') and self._header:
            self._meta_author.setText(
                f"by {self._header.author}"
                if self._header.author else "")
        if hasattr(self, '_meta_released') and self._header:
            self._meta_released.setText(self._header.released or "")
        if hasattr(self, '_meta_tech') and self._header:
            n_sids = (self._engine.num_sids
                       if self._engine else 1)
            if n_sids >= 2 and self._engine:
                chips_str = " + ".join(
                    self._engine.chip_model(i) for i in range(n_sids))
                chip_label = f"{n_sids}× SID ({chips_str})"
            else:
                chip = (self._engine.chip_model(0)
                          if self._engine else 'unknown')
                chip_label = f"SID model: {chip}"
            k, b, c = _find_c64_roms()
            if k and b and c:
                rom_label = "ROMs: OK"
            elif k or b or c:
                rom_label = "ROMs: partial"
            else:
                rom_label = "ROMs: missing"
            self._meta_tech.setText(
                f"{self._header.magic} v{self._header.version}  •  "
                f"{chip_label}  •  {rom_label}")
        # Update subsong row for the new tune. New SID may have a
        # different subsong count; hide the row entirely if only one.
        if hasattr(self, '_subsong_spin') and self._engine:
            n = self._engine.num_subsongs
            self._subsong_spin.blockSignals(True)
            self._subsong_spin.setMinimum(1)
            self._subsong_spin.setMaximum(max(1, n))
            self._cur_subsong = (self._engine.default_subsong
                                  or self._header.default_song or 1)
            self._subsong_spin.setValue(self._cur_subsong)
            self._subsong_spin.blockSignals(False)
            # Refresh the "of N" label - it was only set once at
            # build time, so without this the user keeps seeing the
            # old tune's subsong count.
            if hasattr(self, '_subsong_total_label'):
                self._subsong_total_label.setText(f"of {n}")
            # Hide the row if the new tune has only one subsong, so
            # we don't display a useless "SUBSONG: 1 of 1" widget.
            visible = n > 1
            if hasattr(self, '_subsong_label'):
                self._subsong_label.setVisible(visible)
            self._subsong_spin.setVisible(visible)
            if hasattr(self, '_subsong_total_label'):
                self._subsong_total_label.setVisible(visible)
        self._select_subsong(self._cur_subsong)
        self._start_playback()

    def _tick(self):
        if not self._engine:
            return
        ms = self._engine.time_ms
        secs = ms // 1000
        # Time display: "MM:SS" alone if we don't know the duration,
        # "MM:SS / MM:SS" if we got the song length from HVSC's
        # Songlengths.md5. The slash format makes it instantly clear
        # how much of the song is left.
        cur_str = f"{secs // 60:02d}:{secs % 60:02d}"
        if self._cur_duration_ms is not None:
            d_secs = self._cur_duration_ms // 1000
            self._time_label.setText(
                f"{cur_str} / {d_secs // 60:02d}:{d_secs % 60:02d}")
        else:
            self._time_label.setText(cur_str)
        # Auto-action when the HVSC song length is reached. After
        # a 2-second grace period (so the tail decay/release of the
        # final note is audible), we either:
        #   - shuffle mode:  advance to the next playlist track
        #   - normal mode:   restart the current subsong, exactly as
        #                    if the user had re-clicked it in the
        #                    SUBSONG spinbox
        # If no HVSC song length is known and we ARE in shuffle mode,
        # fall back to a 180-second timeout. In normal mode without
        # an HVSC entry, do nothing - the SID just keeps looping
        # forever like a regular SID player.
        if not (self._is_playing and not self._is_paused):
            return
        # Don't re-trigger while we're already in the post-end wait
        if getattr(self, '_end_pending', False):
            return
        end_reached = False
        if self._cur_duration_ms is not None:
            end_reached = ms >= self._cur_duration_ms
        elif self._shuffle_mode and secs >= 180:
            end_reached = True
        if not end_reached:
            return
        # Schedule the action 2 seconds from now. Set the flag so
        # subsequent ticks during the wait don't re-arm it.
        self._end_pending = True
        QTimer.singleShot(2000, self._handle_song_end)

    def _handle_song_end(self):
        """Called 2 seconds after the current SID hit its HVSC end
        time. In shuffle mode advances to the next track; in normal
        mode triggers a subsong-restart equivalent to clicking the
        spinbox value to its current value."""
        self._end_pending = False
        if not self._is_playing or self._is_paused:
            # User stopped/paused during the wait - do nothing
            return
        if self._shuffle_mode and self._playlist is not None:
            self._shuffle_next()
            return
        # Normal mode: restart the current subsong. Mirror what
        # _on_subsong_change does for the user-clicks-spinbox case:
        # short audio pause, full subsong reload (engine->load()
        # resets the 6502 + SID register state), then audio resumes.
        was_playing = self._is_playing and not self._is_paused
        if self._audio:
            self._audio.set_paused(True)
        self._select_subsong(self._cur_subsong)
        if self._audio and was_playing:
            self._audio.set_paused(False)

    def closeEvent(self, ev):
        self._cleanup()
        super().closeEvent(ev)

    def done(self, result):
        self._cleanup()
        super().done(result)

    def _cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try: self._stop_playback()
        except Exception: pass
        if hasattr(self, '_timer'):
            try: self._timer.stop()
            except Exception: pass
        if self._engine:
            try: self._engine.close()
            except Exception: pass
            self._engine = None
        for eng in self._vis_engines:
            try: eng.close()
            except Exception: pass
        self._vis_engines = []


def is_sid_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
        return magic in (b'PSID', b'RSID')
    except Exception:
        return False
