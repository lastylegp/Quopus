# date_time: 2026-05-28 00:26
"""Config load/save. Drive column is separate from the action button grid."""
import json
import os
import sys
import platform
from pathlib import Path


def _resolve_script_dir():
    """Return the directory we treat as 'Quopus root' at runtime.

    Source layout: <root>/quopus_lib/config.py - parent.parent is
    the project root (where quopus.py lives, plus fonts/, roms/,
    external/, icons/, etc).

    PyInstaller onedir bundle: the application is at
    <bundle>/quopus_commander.exe with frozen modules under
    <bundle>/_internal/, and data files (fonts, roms, external)
    laid out next to the executable as <bundle>/fonts/, etc.
    In that case `sys.executable` points at the .exe and its
    parent is the right "root". `sys.frozen` is set by PyInstaller
    so we can detect this reliably.

    PyInstaller onefile bundle: same as onedir but data files
    live in `sys._MEIPASS` (a temp extraction dir). We honor
    that too, falling back to the .exe parent for any user
    data the bundle expects to be next to itself.
    """
    if getattr(sys, "frozen", False):
        # Frozen bundle - prefer the .exe directory for finding
        # config/, cache/, and any user-writable folders. Bundled
        # read-only data (fonts, icons) lives in _MEIPASS on
        # onefile builds; we still return the .exe dir as the
        # primary root because that's where settings get written.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_bundle_data_dir():
    """Return the directory that holds bundled read-only data
    (fonts, icons, roms, external tools).

    On a normal source checkout this is the same as
    `_resolve_script_dir()`. In a PyInstaller onefile bundle the
    data is extracted to a temp dir referenced by `sys._MEIPASS`.
    The two might differ - always check both when looking up an
    asset."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(meipass):
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


SCRIPT_DIR = _resolve_script_dir()
# Where bundled assets live - same as SCRIPT_DIR for source runs,
# different in PyInstaller onefile mode.
BUNDLE_DIR = _resolve_bundle_data_dir()
CONFIG_DIR = SCRIPT_DIR / "config"
# The primary config file is quopus.cfg. For backward compatibility
# with the pre-rename builds, if a legacy `quopus.cfg` exists next to
# us on first launch, we silently rename it - users keep all their
# settings, button layouts, and history.
CONFIG_FILE = CONFIG_DIR / "quopus.cfg"
_LEGACY_CONFIG_FILE = CONFIG_DIR / "quopus.cfg"
CACHE_DIR = SCRIPT_DIR / "cache"
# Fonts live in the bundle (read-only) in frozen mode. In source
# mode that path is identical to SCRIPT_DIR/fonts so we don't need
# special-casing elsewhere.
FONTS_DIR = BUNDLE_DIR / "fonts"


def _migrate_legacy_config():
    """If a pre-rename quopus.cfg exists but no quopus.cfg, move it
    over so the user doesn't lose their settings. Idempotent."""
    try:
        if _LEGACY_CONFIG_FILE.is_file() and not CONFIG_FILE.is_file():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _LEGACY_CONFIG_FILE.rename(CONFIG_FILE)
    except OSError:
        # Non-fatal - we just fall through to defaults if the
        # rename fails for any reason (permissions, race, ...).
        pass


def _root_path():
    return "C:/" if platform.system() == "Windows" else "/"


def _safe_exists(p):
    """Path.exists() that swallows OSError instead of raising.

    On Windows, probing a drive letter that points to an empty CD
    drive, an empty card-reader slot, or a disconnected network
    drive raises OSError(87) "Falscher Parameter" or OSError(21)
    "Gerät ist nicht bereit" instead of returning False. We treat
    any of those as "drive isn't usable, skip it".

    Same defensive wrap on Unix - autofs mounts that have gone
    stale can raise EIO/ENOENT in odd ways.
    """
    try:
        return Path(p).exists()
    except (OSError, ValueError):
        return False


def _system_default_drives():
    """Build a sensible list of drive buttons for the current OS.

    Linux/macOS: $HOME, /, /tmp, /mnt (if exists), /media (if exists).
    Windows: C:/, D:/, E:/ etc. (only the ones that actually exist).
    Falls back to the legacy Amiga-style labels for any unknown system.
    """
    home = str(Path.home())
    sys_name = platform.system()
    if sys_name == "Windows":
        # Probe drive letters; only include drives that exist.
        # Empty CD/floppy drives raise OSError instead of returning
        # False - _safe_exists catches that.
        drives = [{"label": "HOME", "path": home}]
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = f"{letter}:/"
            if _safe_exists(p):
                drives.append({"label": f"{letter}:", "path": p})
        # Extras for typical Windows users
        drives.append({"label": "TEMP", "path": str(Path(home) / "AppData" / "Local" / "Temp")})
        return drives
    # Linux / macOS / other Unix
    drives = [
        {"label": "HOME", "path": home},
        {"label": "ROOT", "path": "/"},
        {"label": "TMP",  "path": "/tmp"},
    ]
    for extra in ("/mnt", "/media", "/opt", "/var", "/etc"):
        if _safe_exists(extra):
            drives.append({"label": extra.lstrip("/").upper() or "?",
                           "path": extra})
    if sys_name == "Darwin":
        for vol in ("/Volumes", "/Users"):
            if _safe_exists(vol):
                drives.append({"label": vol.lstrip("/").upper(),
                                "path": vol})
    return drives


# Up to 40 drive buttons; shown in the left drive column with scrollbar.
# Auto-populated from the host OS so a Linux user gets /home/$USER, /, /tmp,
# /mnt etc., and a Windows user gets the actual drive letters that exist.
DEFAULT_DRIVES = _system_default_drives()


# Action grid: 6 rows x 6 cols, EVERY cell filled, no gaps.
# Color groups follow the Amiga original roughly:
#   blue   = selection & navigation
#   purple = file ops (copy/move/rename/shell/swap/buffers)
#   black  = destructive / create / search
#   orange = mid-level (edit/info/extract/archive)
#   red    = read/show/delete/play
#   green  = retro/U64 modules (Quopus extension - not in original)
#
# The default layout puts every module on a dedicated button on page
# 1, classic file-manager actions on page 2 (shift held). The
# original Directory Opus 4 layout is preserved in the page 2 grid
# so muscle memory still works - just one shift key away.
DEFAULT_BUTTONS = [
    # Row 1: Retro audio + U64 stack
    [
        {"label": "U64 Streamer",   "action": "u64view",          "color": "green"},
        {"label": "VICE Mon",       "action": "vice_memory",      "color": "green"},
        {"label": "SID Player",     "action": "sidplayer",        "color": "green"},
        {"label": "SID Playlist",   "action": "sidplayer_playlist","color": "green"},
        {"label": "Multi SID",      "action": "multi_sid",        "color": "green"},
        {"label": "Shuffle SIDs",   "action": "shuffle_sids",     "color": "green"},
    ],
    # Row 2: CBM disk + scene tooling
    [
        {"label": "D64 Editor",     "action": "d64editor",        "color": "green"},
        {"label": "BASIC Editor",   "action": "basic_editor",     "color": "green"},
        {"label": "Disasm",         "action": "disasm",           "color": "green"},
        {"label": "Asm64 Browse",   "action": "asm64",            "color": "green"},
        {"label": "Retro GFX",      "action": "retrogfx_browser", "color": "green"},
        {"label": "Retro GFX Open", "action": "retrogfx_file",    "color": "green"},
    ],
    # Row 3: viewers / converters / playback
    [
        {"label": "Image View",     "action": "image_viewer",     "color": "green"},
        {"label": "Archive View",   "action": "archive_viewer",   "color": "green"},
        {"label": "PETSCII Conv",   "action": "petscii_convert",  "color": "green"},
        {"label": "MOD Player",     "action": "modplayer",        "color": "green"},
        {"label": "MOD Playlist",   "action": "modplayer_playlist","color": "green"},
        {"label": "Shuffle MODs",   "action": "shuffle_mods",     "color": "green"},
    ],
    # Row 4: navigation + selection (classic blue)
    [
        {"label": "All",            "action": "select_all",       "color": "blue"},
        {"label": "Parent",         "action": "parent",           "color": "blue"},
        {"label": "Back",           "action": "back",             "color": "blue"},
        {"label": "Forward",        "action": "forward",          "color": "blue"},
        {"label": "Root",           "action": "root",             "color": "blue"},
        {"label": "Read",           "action": "read",             "color": "red"},
    ],
    # Row 5: file operations
    [
        {"label": "Copy",           "action": "copy",             "color": "purple"},
        {"label": "Move",           "action": "move",             "color": "purple"},
        {"label": "Rename",         "action": "rename",           "color": "purple"},
        {"label": "Multi Rename",   "action": "multi_rename",     "color": "purple"},
        {"label": "Makedir",        "action": "makedir",          "color": "black"},
        {"label": "Hex Read",       "action": "hexread",          "color": "red"},
    ],
    # Row 6: search/edit/run + classic ops
    [
        {"label": "Hunt",           "action": "find",             "color": "black"},
        {"label": "Search",         "action": "search",           "color": "black"},
        {"label": "Edit",           "action": "edit",             "color": "orange"},
        {"label": "Run",            "action": "run",              "color": "orange"},
        {"label": "Play",           "action": "play",             "color": "red"},
        {"label": "DELETE",         "action": "delete",           "color": "red"},
    ],
]


# Second action layer, activated when Shift is held. The original
# Directory Opus 4 layout lives here so the classic muscle memory
# still works - all 36 buttons from the original PC-Clone default
# are here in their original positions. Row 1-6 mirror what was
# on page 1 before the retro-module reorganization.
DEFAULT_BUTTONS_SHIFT = [
    # Row 1: Classic Opus row 1 (selection + copy + makedir + run +
    # comment + read)
    [
        {"label": "All",            "action": "select_all",       "color": "blue"},
        {"label": "Copy",           "action": "copy",             "color": "purple"},
        {"label": "Makedir",        "action": "makedir",          "color": "black"},
        {"label": "Run",            "action": "run",              "color": "orange"},
        {"label": "Comment",        "action": "comment",          "color": "orange"},
        {"label": "Read",           "action": "read",             "color": "red"},
    ],
    # Row 2: Classic Opus row 2
    [
        {"label": "None",           "action": "select_none",      "color": "blue"},
        {"label": "Move",           "action": "move",             "color": "purple"},
        {"label": "Hunt",           "action": "find",             "color": "black"},
        {"label": "Edit",           "action": "edit",             "color": "orange"},
        {"label": "Datestamp",      "action": "datestamp",        "color": "orange"},
        {"label": "Hex Read",       "action": "hexread",          "color": "red"},
    ],
    # Row 3: Classic Opus row 3
    [
        {"label": "Parent",         "action": "parent",           "color": "blue"},
        {"label": "Rename",         "action": "rename",           "color": "purple"},
        {"label": "Search",         "action": "search",           "color": "black"},
        {"label": "Execute",        "action": "run",              "color": "orange"},
        {"label": "Protect",        "action": "protect",          "color": "orange"},
        {"label": "Show",           "action": "show",             "color": "red"},
    ],
    # Row 4: Classic Opus row 4 - PETSCII converters + buffers/checkfit
    [
        {"label": "Root",           "action": "root",             "color": "blue"},
        {"label": "Shell",          "action": "shell",            "color": "purple"},
        {"label": "ASCII->PET",     "action": "ascii_to_petscii", "color": "orange"},
        {"label": "PET->ASCII",     "action": "petscii_to_ascii", "color": "orange"},
        {"label": "Buffers",        "action": "buffers",          "color": "purple"},
        {"label": "CheckFit",       "action": "checkfit",         "color": "black"},
    ],
    # Row 5: Classic Opus row 5 - archive ops + history
    [
        {"label": "Back",           "action": "back",             "color": "blue"},
        {"label": "Swap",           "action": "swap",             "color": "purple"},
        {"label": "GetSizes",       "action": "getsizes",         "color": "black"},
        {"label": "Arc Ext",        "action": "extract",          "color": "orange"},
        {"label": "Encrypt",        "action": "archive",          "color": "orange"},
        {"label": "/X Dump",        "action": "dir_reverse",      "color": "red"},
    ],
    # Row 6: history nav + comm tools + system actions
    [
        {"label": "Forward",        "action": "forward",          "color": "blue"},
        {"label": "FTP",            "action": "ftp",              "color": "green"},
        {"label": "Telnet",         "action": "telnet",           "color": "green"},
        {"label": "Config",         "action": "config",           "color": "orange"},
        {"label": "About",          "action": "about",            "color": "orange"},
        {"label": "DELETE",         "action": "delete",           "color": "red"},
    ],
]


# Third action layer, activated when Shift+Alt is held. Empty by
# default so the user can build their own personal workflow on it
# (e.g. a dedicated BBS-tooling page or a per-project quick-pick).
# 6x6 None grid; right-click → Edit on each cell to populate.
DEFAULT_BUTTONS_SHIFT_ALT = [
    [None] * 6 for _ in range(6)
]


DEFAULT_CONFIG = {
    # Default to the user's home folder rather than the install dir.
    # Most users want to start in their own files, not in /opt/quopus
    # or wherever the script was unpacked.
    "left_path":  str(Path.home()),
    "right_path": str(Path.home()),
    "drives":     DEFAULT_DRIVES,
    "buttons":    DEFAULT_BUTTONS,
    # Shift-layer: a second 6x6 grid of action buttons that swap in
    # while Shift is held. Lets the user double the available
    # actions without a wider button bank. Empty by default.
    "buttons_shift": DEFAULT_BUTTONS_SHIFT,
    # Shift+Alt-layer: a third 6x6 grid that swaps in while both
    # Shift AND Alt are held. Empty by default; the user populates
    # it via F10 → Action buttons or right-click → Edit. Ctrl+T
    # cycles through main -> shift -> shift_alt persistently.
    "buttons_shift_alt": DEFAULT_BUTTONS_SHIFT_ALT,
    "column_widths": {"0": 260, "1": 54, "2": 80, "3": 130},
    "window_geometry": {"w": 1280, "h": 780, "state": "normal"},
    # TextReader appearance - persisted across sessions.
    # Font size is the "manual zoom" point size; -/+ buttons in
    # the reader adjust this. fg/bg are hex color strings.
    "text_reader_font_size": 11,
    "text_reader_fg": "#FFFFFF",
    "text_reader_bg": "#000000",
    # Global UI font scaling. The app has dozens of stylesheets
    # with hardcoded font sizes (font-size: 11px;) for the
    # Workbench/Amiga look. Instead of letting QApplication.setFont
    # try to override them (which doesn't work because inline CSS
    # wins over QApplication font), we route every stylesheet
    # construction through scaled_font_px() which multiplies a
    # base size by the user's scale factor.
    #
    # app_font_scale_percent:
    #   100 = original sizes (11px stays 11px)
    #   150 = "everything 50% bigger" (11px becomes ~17px)
    #   75  = "denser" (11px becomes ~8px)
    # The valid range is 50..300.
    #
    # app_font_pointsize_override:
    #   If > 0, this overrides the *base size* for the most-common
    #   "body text" stylesheet category (the 11px ones). The %
    #   scale still applies on top - so override=14 plus
    #   scale=150% gives 21px for those entries. Set to 0 to use
    #   the per-stylesheet original base.
    "app_font_scale_percent": 100,
    "app_font_pointsize_override": 0,
    # Kept for backwards compatibility - was the previous (broken)
    # global app font system. apply_app_font still honors family
    # for QApplication.setFont, which DOES affect widgets without
    # their own stylesheet. Empty = platform default.
    "app_font_family": "",
    # Lister Size-column display mode:
    #   "bytes"  - human readable (4K, 1.2M, ...)  default
    #   "blocks" - C64 disk blocks (256B = 1 block, CBM DOS)
    "size_display": "bytes",
    # U64 Streamer settings - persisted across sessions so the user
    # doesn't have to re-type the host/ports every launch.
    "u64_host":       "",
    "u64_video_port": 11000,
    "u64_audio_port": 11001,
    "u64_telnet_port": 23,
    "u64_http_port":  80,
    "u64_password":   "",
    # Where the streamer puts screenshots. Empty string means the
    # default location: <quopus_project>/screenshots/. An absolute
    # path overrides it. The streamer auto-creates the folder.
    "u64_screenshot_dir": "",
    # Video recording format for the streamer's Rec button:
    #   "mp4"     - H.264 via ffmpeg (requires ffmpeg on PATH)
    #   "png_seq" - one PNG file per frame in a per-capture folder
    # The streamer auto-falls-back to png_seq if mp4 is selected
    # but ffmpeg is missing. Toggleable via right-click on Rec.
    "u64_record_format": "mp4",
}


def load_config():
    # Migrate any legacy quopus.cfg to quopus.cfg first, so the rest
    # of the function reads from whatever is now at CONFIG_FILE.
    _migrate_legacy_config()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Key migration: old "devices" -> "drives"
            if "drives" not in cfg and "devices" in cfg:
                cfg["drives"] = cfg.pop("devices")
            # If buttons grids don't match 6x6, rebuild from defaults.
            # Same check for all three layers - any corrupted grid
            # falls back rather than crashing _rebuild_buttons later.
            def _is_6x6(b):
                return (isinstance(b, list) and len(b) == 6 and
                        all(isinstance(r, list) and len(r) == 6
                            for r in b))
            if not _is_6x6(cfg.get("buttons", [])):
                cfg["buttons"] = DEFAULT_BUTTONS
            if "buttons_shift" in cfg and not _is_6x6(cfg["buttons_shift"]):
                cfg["buttons_shift"] = DEFAULT_BUTTONS_SHIFT
            if "buttons_shift_alt" in cfg \
                    and not _is_6x6(cfg["buttons_shift_alt"]):
                cfg["buttons_shift_alt"] = DEFAULT_BUTTONS_SHIFT_ALT
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            # Seed default file associations for missing extensions
            from .file_assoc import ensure_default_assoc
            ensure_default_assoc(cfg)
            return cfg
        except Exception as e:
            print(f"Config load error: {e}")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    from .file_assoc import ensure_default_assoc
    ensure_default_assoc(cfg)
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Config save error: {e}")


def apply_app_font(cfg, app=None):
    """Apply the global font settings (family + scale) to the
    running QApplication. Called at startup and whenever the
    user changes font settings via the Settings dialog.

    Three things happen here:
      1. The QApplication base font family/size is set so that
         every widget WITHOUT its own stylesheet picks up the
         new look.
      2. The pointsize is the app default (10pt) multiplied by
         the user's scale factor.
      3. The cfg-attached _font_scale is updated so that any
         later calls to scaled_font_px() see the new value.

    Stylesheets with hardcoded font-size: Npx; need to use
    scaled_font_px(N) to participate in scaling - that's done
    in the per-widget refactor.

    Returns True on success.
    """
    try:
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return False
    if app is None:
        app = QApplication.instance()
    if app is None:
        return False
    family = (cfg.get("app_font_family") or "").strip()
    try:
        scale = int(cfg.get("app_font_scale_percent", 100))
    except (TypeError, ValueError):
        scale = 100
    scale = max(50, min(300, scale))
    # The QApplication base size: scale the platform default 10pt
    # by the user's percentage. This affects every widget that
    # DOESN'T have its own font-size in its stylesheet.
    base_pt = max(6, min(40, round(10 * scale / 100)))
    if family:
        font = QFont(family, base_pt)
    else:
        # Inherit platform default family, just resize.
        font = app.font()
        font.setPointSize(base_pt)
    app.setFont(font)
    return True


def current_font_scale(cfg=None):
    """Return the active scale factor as a float multiplier
    (1.0 = no scaling, 1.5 = 50% bigger). Used by stylesheet
    builders that need to size something proportionally.

    Reading lazily from the LIVE config means a scale change
    via the Settings dialog takes effect at the next paint
    without explicit propagation.

    If cfg is None we look up the singleton via the lazy
    import (so this can be called from modules that don't
    already have a config reference handy).
    """
    if cfg is None:
        # Lazy: try to find the active config without forcing
        # a circular import. The main window stashes its cfg
        # on the QApplication for exactly this purpose.
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                cfg = getattr(app, '_quopus_cfg', None)
        except Exception:
            cfg = None
    if cfg is None:
        return 1.0
    try:
        pct = int(cfg.get("app_font_scale_percent", 100))
    except (TypeError, ValueError):
        return 1.0
    pct = max(50, min(300, pct))
    return pct / 100.0


def scaled_font_px(base_px, cfg=None):
    """Multiply a base pixel size by the current scale factor
    and return an integer. Use this in every stylesheet that
    has a font-size: Npx; line.

    Example:
        ssheet = f"QLabel {{ font-size: {scaled_font_px(11)}px; }}"

    The pointsize-override config key kicks in here too: if
    set (>0) AND the requested base is one of the "body text"
    sizes (10/11/12), the override replaces the base. Then
    the percentage scale is applied. This lets the user say
    "all body text should be 14pt as my new baseline, with
    +25% scaling on top of that" if they want.
    """
    scale = current_font_scale(cfg)
    # Pointsize-override: only kicks in for body-text-ish base
    # sizes (10..12 px). Larger sizes (headings 13/14/18/22)
    # keep their relative differentiation - we don't want the
    # user's override to flatten the visual hierarchy.
    if cfg is None:
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                cfg = getattr(app, '_quopus_cfg', None)
        except Exception:
            cfg = None
    if cfg is not None:
        try:
            override = int(cfg.get("app_font_pointsize_override", 0))
        except (TypeError, ValueError):
            override = 0
        if override > 0 and 10 <= base_px <= 12:
            base_px = override
    return max(6, round(base_px * scale))


def refresh_all_widgets_font(app=None):
    """Force every widget in the app to re-evaluate its
    stylesheet so font-size changes take effect without a
    restart. Qt caches computed styles; the official way
    to invalidate that cache is unpolish/polish via the
    QStyle.

    Called from the Settings dialog after the user clicks
    Apply or OK.

    Note: this only refreshes the GLOBAL stylesheet. Inline
    setStyleSheet() calls on individual widgets get a separate
    refresh path - we set the same string again, which Qt
    treats as 'might have new variables, re-parse'. That
    only works if the widget cooperates by calling a refresh
    helper from the main window - search for
    _refresh_dynamic_stylesheets() to see the participating
    list.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    # Re-apply the global stylesheet (if any was set) -
    # triggers a full restyle.
    cur = app.styleSheet()
    if cur:
        app.setStyleSheet("")
        app.setStyleSheet(cur)
    # Tell every top-level widget to re-render. This propagates
    # to all child widgets and re-evaluates their inline
    # stylesheets. We use unpolish/polish via the QStyle which
    # is the documented way to invalidate computed style.
    for w in app.allWidgets():
        try:
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()
        except Exception:
            pass
    # Last but not least: call the participating-widget refresh
    # hook on the main window so it can rebuild its dynamic
    # stylesheets that depend on scale (lister buttons,
    # action-button grid, statusbar, etc), AND call
    # refresh_fonts() directly on any top-level dialog that has
    # one (TextReader, MultiRename, BasicEditor, etc.).
    for w in app.topLevelWidgets():
        # Main window's bulk refresh first - it can re-style
        # many child widgets internally.
        refresh = getattr(w, '_refresh_dynamic_stylesheets', None)
        if callable(refresh):
            try:
                refresh()
            except Exception as e:
                print(f"[font refresh] {type(w).__name__} "
                      f"refresh failed: {e}")
        # Then dialog-level refresh for anything sitting at the
        # top level (TextReader is a QDialog, not a QMainWindow).
        # We only call refresh_fonts() on widgets that explicitly
        # define it, so the lookup is safe on arbitrary widgets.
        refresh_fonts = getattr(w, 'refresh_fonts', None)
        if callable(refresh_fonts):
            try:
                refresh_fonts()
            except Exception as e:
                print(f"[font refresh] {type(w).__name__} "
                      f".refresh_fonts() failed: {e}")
