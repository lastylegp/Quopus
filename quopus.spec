# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Quopus Commander.

Builds a Windows onedir bundle:
    dist/Quopus_Commander/
        Quopus_Commander.exe
        _internal/        (frozen modules + Qt libs)
        fonts/            (TTFs - read-only)
        roms/             (C64 ROMs - SID player needs these)
        external/         (DLLs + exe like recoil2png)
        icons/            (only quopus.ico - bundled in exe metadata,
                           plus quopus.png for runtime use)
        sidwrapper.dll    (next to the exe, since lib loaders look
                           in the directory of the executable)
        config/           (auto-created on first run)
        cache/            (auto-created on first run)

Why onedir instead of onefile:
    1. Startup is ~10x faster - onefile extracts to a temp dir on
       every launch which adds 2-5 seconds.
    2. Antivirus software flags onefile much more often.
    3. Easier to inspect / patch / hot-swap individual files.
    4. PyQt's plugin system is happier when libs are real files
       on disk instead of unpacked from a self-extracting blob.

Usage:
    pyinstaller --noconfirm quopus.spec
    pyinstaller --noconfirm quopus.spec -- --debug   # console window

Or use the helper batch/shell scripts:
    build_exe_windows.bat
    build_exe_windows.bat --debug   # to keep a console window open

This file is identical on Windows and Linux - the same spec
produces a Windows .exe with sidwrapper.dll on Windows, and a
Linux ELF binary with libsidwrapper.so on Linux. PyInstaller
picks the right binary based on the host platform.

DEBUG BUILDS
============
When the .exe silently does nothing on double-click (Windows
hides the Python traceback behind console=False), build with
the --debug flag to get a console window that stays open and
shows any import / startup error.
"""
import os
import sys
from pathlib import Path

# Resolve the project root from the spec file's location. This
# makes the spec runnable from any working directory.
HERE = Path(SPECPATH).resolve() if 'SPECPATH' in dir() else Path('.').resolve()

# Detect "debug" build: pyinstaller passes extra args after `--`
# but it also looks at the QUOPUS_DEBUG env var so the batch
# scripts can flip it without messing with argv parsing.
DEBUG_BUILD = (
    os.environ.get("QUOPUS_DEBUG", "").lower() in ("1", "true", "yes")
)

# Collect all data files that should ship next to the exe.
# Format: (source_path, target_dir_in_bundle)
# Target "." means "right next to the exe".
datas = []

# Icons - both formats. The .ico is also embedded in the exe via
# `icon=` below for the taskbar / window decoration look on
# Windows, but we keep the file around so the running app can
# load it via QIcon for runtime use.
if (HERE / "quopus_lib" / "icons" / "quopus.png").is_file():
    datas.append(
        (str(HERE / "quopus_lib" / "icons" / "quopus.png"),
         "quopus_lib/icons"))
if (HERE / "quopus_lib" / "icons" / "quopus.ico").is_file():
    datas.append(
        (str(HERE / "quopus_lib" / "icons" / "quopus.ico"),
         "quopus_lib/icons"))

# Embedded data file the C64 disasm / cbm files modules need
if (HERE / "quopus_lib" / "c64_chargen.bin").is_file():
    datas.append(
        (str(HERE / "quopus_lib" / "c64_chargen.bin"),
         "quopus_lib"))

# Fonts directory - bundles every TTF we can find. We don't list
# them one by one because the user may have added their own.
fonts_dir = HERE / "fonts"
if fonts_dir.is_dir():
    for f in fonts_dir.iterdir():
        if f.is_file():
            datas.append((str(f), "fonts"))

# C64 ROMs
roms_dir = HERE / "roms"
if roms_dir.is_dir():
    for f in roms_dir.iterdir():
        if f.is_file():
            datas.append((str(f), "roms"))

# External tools (recoil2png.exe, thumbrecoil.dll, etc).
# These are platform-specific binaries; including the Windows
# .exe/.dll on a Linux build is harmless (they just won't run
# but at least they're shipped if you cross-build).
external_dir = HERE / "external"
if external_dir.is_dir():
    for f in external_dir.iterdir():
        if f.is_file():
            datas.append((str(f), "external"))

# libsidwrapper - native code that wraps libsidplayfp. PyInstaller
# treats DLLs/SOs differently from regular data files, so we list
# them in the binaries section to ensure they get the LD_LIBRARY
# path treatment on Linux.
binaries = []
for name in ("sidwrapper.dll", "libsidwrapper.so",
             "libsidwrapper.dylib"):
    p = HERE / name
    if p.is_file():
        binaries.append((str(p), "."))

# Hidden imports - PyInstaller's static analyzer misses modules
# that are imported via importlib or string-based lazy imports.
# We list the trouble-makers explicitly. paramiko is included if
# the user has it installed so SSH works in the binary; if not
# installed the import line silently degrades.
hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtNetwork",
    "PyQt6.QtPrintSupport",
    # System info - some PyInstaller versions miss psutil's
    # platform-specific submodules
    "psutil",
    "psutil._psplatform",
    # Numerical - used by sid_player, mod_player, vumeter, spectrum
    # for FFT and waveform processing. PyInstaller usually finds
    # numpy automatically, but list it explicitly so any submodule
    # auto-loaded via __getattr__ comes along.
    "numpy",
    "numpy.core",
    "numpy.core._multiarray_umath",
    "numpy.fft",
    # Stuff actions.py / sid_player.py loads at runtime
    "quopus_lib.sid_player",
    "quopus_lib.mod_player",
    "quopus_lib.vumeter",
    "quopus_lib.spectrum",
    "quopus_lib.asm64_browser",
    "quopus_lib.basic_editor",
    "quopus_lib.cbmfiles",
    "quopus_lib.image_viewer",
    "quopus_lib.archive_viewer",
    "quopus_lib.c64_disasm",
    "quopus_lib.telnet_client",
    "quopus_lib.u64_streamer",
    "quopus_lib.retro_gfx_viewer",
    "quopus_lib.petscii_tables",
    "quopus_lib.encodings",
    "quopus_lib.window_state",
    "quopus_lib.database",
    "quopus_lib.db_scanner",
    "quopus_lib.db_browser",
    "quopus_lib.db_watcher",
    "quopus_lib.ingest_queue",
    "quopus_lib.license",
    "quopus_lib.license_ui",
    "quopus_lib.crypto",
    "quopus_lib.ftp_backend",
    "quopus_lib.ftp_browser",
]

# Optional - only include if installed. We probe via importlib;
# at runtime they're imported lazily so absence is tolerated.
# Each of these adds a feature; without the import the
# corresponding action just stays disabled at runtime.
#
#   paramiko       - SSH support in the telnet client
#   cryptography   - dependency of paramiko
#   bcrypt         - dependency of paramiko
#   nacl           - dependency of paramiko
#   sounddevice    - audio output for SID/MOD players
#   soundcard      - alternate audio output backend
#   pyaudio        - alternate audio output backend
#   lhafile        - LHA archive extraction (Amiga formats)
#   rarfile        - RAR archive extraction
#   py7zr          - 7z archive extraction in DB scanner
#   watchdog       - native FS notifications for DB watcher
#   pywin32 (win32com) - Windows shortcut creation
def _try_import(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


for opt in ("paramiko", "cryptography", "bcrypt", "nacl",
            "sounddevice", "soundcard", "pyaudio",
            "lhafile", "rarfile", "py7zr", "watchdog",
            "win32com", "win32com.client", "pythoncom"):
    if _try_import(opt):
        hiddenimports.append(opt)


# === MAIN APP ===
block_cipher = None

a = Analysis(
    ['quopus.py'],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # These ship via PyQt or Python stdlib and we don't need
        # the duplicates; cuts ~50 MB off the bundle.
        "tkinter", "test", "unittest", "pydoc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Quopus_Commander',
    debug=DEBUG_BUILD,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX often trips antivirus
    # Console window:
    #   - Release build: hidden (windowed app, Workbench look)
    #   - Debug build: shown, stays open after exit so you can
    #     read any traceback that crashes the startup.
    console=DEBUG_BUILD,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "quopus_lib" / "icons" / "quopus.ico")
        if (HERE / "quopus_lib" / "icons" / "quopus.ico").is_file()
        else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Quopus_Commander',
)
