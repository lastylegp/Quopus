# date_time: 2026-06-04 09:41
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


def _win_drive_kind(path: str) -> str:
    """Classify a Windows drive via GetDriveTypeW. Returns one of
    home / fixed / removable / remote / cdrom / ramdisk / unknown.
    Used so per-type drive-button styles colour each button by its
    real type instead of all looking identical."""
    try:
        import ctypes
        # 2 REMOVABLE, 3 FIXED, 4 REMOTE, 5 CDROM, 6 RAMDISK
        t = ctypes.windll.kernel32.GetDriveTypeW(
            ctypes.c_wchar_p(path))
        return {
            2: "removable", 3: "fixed", 4: "remote",
            5: "cdrom",      6: "ramdisk",
        }.get(t, "fixed")
    except Exception:
        return "fixed"


def _read_linux_mounts():
    """Parse /proc/mounts and return a list of useful mountpoints.
    We skip the chaff (pseudo filesystems, snap loops, cgroup,
    docker overlay, ...) and keep block devices + bind mounts +
    network mounts + removable media. Each entry is
    ``{"label": "...", "path": "...", "kind": "..."}`` where kind
    is one of:

      "root"       - the root filesystem /
      "fixed"      - other on-disk filesystems
      "removable"  - /media/* or /run/media/* (auto-mounted USB)
      "remote"     - nfs / cifs / sshfs / fuse network mounts
      "system"     - /boot, /efi, /var, etc. (still useful but
                     less commonly opened)

    The label is derived from the mountpoint - e.g. ``/mnt/data``
    -> "data", ``/`` -> "ROOT", ``/home/me`` -> stays "HOME" as
    set by the HOME entry. On parse failure (non-Linux, no
    /proc/mounts) returns an empty list.
    """
    out = []
    # File systems we always discard - kernel/pseudo
    SKIP_TYPES = {
        "proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "ramfs",
        "cgroup", "cgroup2", "pstore", "bpf", "securityfs",
        "debugfs", "tracefs", "configfs", "fusectl", "binfmt_misc",
        "hugetlbfs", "mqueue", "autofs", "rpc_pipefs", "nsfs",
        "selinuxfs", "efivarfs",
    }
    SKIP_MOUNT_PREFIX = (
        "/proc", "/sys", "/dev", "/run/user", "/run/lock",
        "/run/snapd", "/snap", "/var/lib/docker",
        "/var/lib/containers", "/var/snap",
    )
    NET_TYPES = {
        "nfs", "nfs4", "cifs", "smbfs", "sshfs", "afpfs",
        "fuse.sshfs", "fuse.rclone", "webdav",
    }
    try:
        with open("/proc/mounts", "r", encoding="utf-8",
                   errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    seen = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mp, fstype = parts[0], parts[1], parts[2]
        # Unescape octal sequences in mountpoint (kernel writes
        # spaces as \040, tab as \011, etc.)
        try:
            mp = bytes(mp, "utf-8").decode("unicode_escape")
        except Exception:
            pass
        if fstype in SKIP_TYPES:
            continue
        if any(mp == p or mp.startswith(p + "/")
                for p in SKIP_MOUNT_PREFIX):
            continue
        if mp in seen:
            continue
        seen.add(mp)
        # Classify
        if mp == "/":
            kind = "root"
        elif fstype in NET_TYPES or fstype.startswith("fuse."):
            kind = "remote"
        elif (mp.startswith("/media/")
                or mp.startswith("/run/media/")):
            kind = "removable"
        elif mp.startswith("/boot") or mp.startswith("/efi"):
            kind = "system"
        else:
            kind = "fixed"
        # Derive a short label from the leaf directory name
        if mp == "/":
            label = "/"
        else:
            label = mp.rstrip("/").rsplit("/", 1)[-1] or "?"
        out.append({"label": label, "path": mp, "kind": kind})
    return out


def _read_macos_mounts():
    """List /Volumes/* on macOS - one entry per visible volume.
    The startup disk shows up there too as a symlink to /."""
    out = []
    try:
        from os import listdir
        for name in sorted(listdir("/Volumes")):
            p = "/Volumes/" + name
            if not Path(p).is_dir():
                continue
            out.append({"label": name, "path": p,
                         "kind": "removable"})
    except OSError:
        pass
    return out


def _system_default_drives():
    """Build a sensible list of drive buttons for the current OS.

    Linux: HOME, /, then all real mounted filesystems from
        /proc/mounts (skipping kernel pseudo-fs and snap loops).
        Removable media under /media or /run/media is included,
        as are network mounts (nfs/cifs/sshfs/fuse).
    macOS: HOME, /, /Volumes/* (each volume as its own entry).
    Windows: HOME, then C:/, D:/, ... (only the ones that exist),
        plus a TEMP shortcut.
    Falls back to a minimal HOME-only list for anything else.
    """
    home = str(Path.home())
    sys_name = platform.system()
    if sys_name == "Windows":
        # Probe drive letters; only include drives that exist.
        # Empty CD/floppy drives raise OSError instead of returning
        # False - _safe_exists catches that.
        drives = [{"label": "HOME", "path": home, "kind": "home"}]
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = f"{letter}:/"
            if _safe_exists(p):
                # Ask the OS for the real drive type so per-type
                # styles (tile/pill/mixed/folder) colour each
                # button correctly. Falls back to "fixed" if the
                # API call fails.
                drives.append({"label": f"{letter}:", "path": p,
                                 "kind": _win_drive_kind(p)})
        # Extras for typical Windows users
        drives.append({"label": "TEMP",
                         "path": str(Path(home) / "AppData"
                                     / "Local" / "Temp"),
                         "kind": "fixed"})
        return drives

    # Linux / macOS / other Unix - always start with HOME and /
    drives = [
        {"label": "HOME", "path": home,   "kind": "home"},
        {"label": "/",    "path": "/",    "kind": "root"},
    ]
    seen_paths = {home, "/"}

    # Pull real mounts from the kernel
    if sys_name == "Linux":
        mounts = _read_linux_mounts()
    elif sys_name == "Darwin":
        mounts = _read_macos_mounts()
    else:
        mounts = []
    for m in mounts:
        if m["path"] in seen_paths:
            continue
        seen_paths.add(m["path"])
        drives.append(m)

    # /tmp is always nice to have at the end
    if _safe_exists("/tmp") and "/tmp" not in seen_paths:
        drives.append({"label": "tmp", "path": "/tmp",
                         "kind": "fixed"})
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
    # Currently-active button layer. Cycled with Ctrl+T through
    # main -> shift -> shift_alt -> main. Persisted so the user's
    # last selection is restored on next start - if you'd parked
    # the grid on the shift_alt layer when you quit, the next
    # run opens with shift_alt active.
    "active_button_layer": "main",
    # Lister panel colors. Set via Settings -> Lister colors.
    # lister_bg is the panel background; lister_fg is the default
    # text color for files; lister_dir_fg is for directory rows.
    # lister_ext_colors maps lowercase extension (".prg", ".py")
    # to a hex color string - it overrides lister_fg for matching
    # files. Missing extensions fall back to lister_fg.
    "lister_bg": "#a0a0a0",
    "lister_fg": "#000000",
    "lister_dir_fg": "#0000cc",
    "lister_ext_colors": {
        # C64 / Amiga binaries
        ".prg":  "#0050d0",
        ".sid":  "#c850c0",
        ".mod":  "#50a850",
        ".d64":  "#d07020",
        ".d71":  "#d07020",
        ".d81":  "#d07020",
        ".d82":  "#d07020",
        ".g64":  "#d07020",
        ".t64":  "#d07020",
        ".tap":  "#d07020",
        ".crt":  "#a06030",
        ".nib":  "#d07020",
        ".lha":  "#707070",
        ".lzx":  "#707070",
        ".adf":  "#d07020",
        # Source code
        ".py":   "#a040c0",
        ".c":    "#c04020",
        ".h":    "#c04020",
        ".cpp":  "#c04020",
        ".s":    "#c06020",
        ".asm":  "#c06020",
        ".bas":  "#207050",
        ".js":   "#c0a020",
        ".html": "#a04060",
        ".css":  "#207090",
        # Docs / text
        ".txt":  "#505050",
        ".md":   "#505050",
        ".nfo":  "#505050",
        ".diz":  "#505050",
        ".pdf":  "#a02020",
        # Archives
        ".zip":  "#606060",
        ".7z":   "#606060",
        ".rar":  "#606060",
        ".tar":  "#606060",
        ".gz":   "#606060",
        # Images
        ".png":  "#208050",
        ".jpg":  "#208050",
        ".jpeg": "#208050",
        ".gif":  "#208050",
        ".bmp":  "#208050",
        # Audio
        ".mp3":  "#5050a0",
        ".wav":  "#5050a0",
        ".ogg":  "#5050a0",
        ".flac": "#5050a0",
    },
    # Telegram client chat-bubble colors. Outgoing (your own)
    # messages vs. incoming. Each has a background and text color.
    # Editable via the Telegram window's "Colors..." button.
    "telegram_out_bg": "#1f6e3a",   # own messages - green bubble
    "telegram_out_fg": "#eafbe7",
    "telegram_in_bg":  "#1c3f63",   # others - blue bubble
    "telegram_in_fg":  "#e7f0fb",
    # Chat IDs the user has archived; hidden from the main list and
    # shown only when the Archive view is toggled on.
    "telegram_archived": [],
    # IRC chat logging. When the global toggle is on, every IRC
    # buffer is logged unless explicitly disabled per-buffer; when
    # off, individual buffers can still be enabled. Files land
    # under irc_log_dir/<server>/<buffer>.log; empty means
    # <quopus>/config/irc_logs/.
    "irc_log_enabled": False,
    "irc_log_dir": "",
    # Per-buffer overrides: {"server/buffer": "on" | "off"}
    # "on" forces logging, "off" forces no logging, missing key
    # falls back to irc_log_enabled.
    "irc_log_overrides": {},
    # Auto-update check on startup. When True, Quopus asks GitHub
    # whether a newer version of the main branch is available a
    # moment after the window appears, and pops a dialog if so.
    # Set to False to skip the check entirely (e.g. on machines
    # without Internet, or to avoid the brief startup HTTP call).
    # The Help -> Check for updates menu entry still works either
    # way - it's an explicit click, not subject to this toggle.
    "update_check_enabled": True,
    # Remote-tip SHA that the user has already been notified about.
    # When the startup check sees this same SHA again, the dialog
    # stays closed - the user already saw this commit and chose
    # not to update yet, no need to bug them on every launch.
    # Reset to "" to get the next check to pop the dialog again.
    "update_last_seen_sha": "",
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
    # Lister drives bar (Total Commander style): one button per
    # mounted drive / mountpoint above the path edit. Default on
    # so the feature is discoverable; the user can hide it via
    # right-click → View → Drive buttons bar if they want a more
    # compact lister.
    "show_drives_bar": True,
    # Visual style for the drive buttons. One of:
    #   "amiga"  - Workbench drawer (default, matches Quopus theme)
    #   "floppy" - 3.5" diskette
    #   "hdd"    - hard-disk-drive icon
    #   "pill"   - color pill with mini glyph
    #   "led"    - round LED badge
    #   "mixed"  - per-drive type icon (house/HDD/globe/USB/CD)
    #   "plain"  - no icon, label text only (original Quopus look)
    # Configurable via Config → Drive button style...
    "drive_button_style": "amiga",
    # Lister splitter sizes (left, mid button column, right).
    # Empty / unset = 50/50 split, the QSplitter computes from
    # the window width. Persisted whenever the user drags the
    # divider.
    "lister_splitter_sizes": [],
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


def _sanitize_start_path(p):
    """Return a usable local directory for a saved lister path.

    A config moved between operating systems can hold a path that
    doesn't resolve here - the classic case is a Windows path like
    'C:\\Users\\j\\Downloads' opened on Linux, where it gets
    (wrongly) treated as relative and tacked onto the current
    working directory. We also guard against a drive that has
    since been unplugged. In any of those cases we fall back to
    the user's home directory so the lister always opens on
    something valid.
    """
    home = str(Path.home())
    if not p or not isinstance(p, str):
        return home
    # A Windows-style absolute path (drive letter + backslash, or
    # a UNC \\server\share) is never valid on POSIX. Detect it
    # cheaply so we don't even try to resolve it.
    if os.name != "nt":
        looks_windows = (
            (len(p) >= 2 and p[1] == ":" and p[0].isalpha())
            or p.startswith("\\\\")
            or "\\" in p
        )
        if looks_windows:
            return home
    try:
        cand = Path(p).expanduser()
        if cand.is_dir():
            return str(cand.resolve())
    except Exception:
        pass
    return home


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
            # Validate the saved lister paths. A config carried
            # over from another OS (e.g. a Windows "C:\Users\j\
            # Downloads" opened on Linux) holds a path that can't
            # be resolved here - left as-is it makes the lister
            # try to open a bogus directory at startup and throw.
            # Fall back to HOME for any path that doesn't exist as
            # a real directory on this machine.
            cfg["left_path"] = _sanitize_start_path(
                cfg.get("left_path"))
            cfg["right_path"] = _sanitize_start_path(
                cfg.get("right_path"))
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
    # Give dialogs a sane minimum so they aren't clipped. On
    # Linux/X11 a QMessageBox with a custom (often wider)
    # monospace font and a multi-line text sometimes lays itself
    # out too narrow/short - the window manager decorates the
    # frame at the size QMessageBox first asked for, before the
    # label has wrapped, and the bottom line + title get cut off.
    # Forcing a minimum width on the message label makes the box
    # size itself around readable text. We scale the minimum with
    # the font so big-font users get a proportionally wider box.
    apply_dialog_metrics(app, base_pt)
    return True


def apply_dialog_metrics(app=None, base_pt=10):
    """Make message boxes and dialogs size themselves correctly.

    Some Linux window managers clip QMessageBox content - either
    the bottom text line ends up hidden behind the buttons, or
    the frame is decorated too small. The fix is a global event
    filter that, when a dialog is shown, sets a sensible minimum
    width on its TEXT labels (as widget properties, not via
    stylesheet - stylesheet min-width interferes with the
    word-wrap heightForWidth calculation) and then calls
    adjustSize() so Qt recomputes the height for that width and
    the dialog grows to fit. We also pin the resulting minimum
    size so the WM can't shrink it back.

    base_pt drives the minimum width so big-font users get a
    proportionally wider (and therefore shorter) dialog.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    # Stash the per-font label minimum width on the app so the
    # event filter can read the current value (it re-runs when
    # the user changes the font scale).
    min_label = max(300, min(620, int(base_pt * 36)))
    app._quopus_dialog_min_label = min_label
    _install_dialog_sizer(app)


# A module-level singleton so we install the filter exactly once.
_DIALOG_SIZER = None


def _install_dialog_sizer(app):
    """Install (once) a global event filter that forces message
    boxes and dialogs to size around their content when shown."""
    global _DIALOG_SIZER
    if _DIALOG_SIZER is not None:
        return
    try:
        from PyQt6.QtCore import QObject, QEvent, QTimer
        from PyQt6.QtWidgets import (
            QApplication, QMessageBox, QInputDialog, QLabel)
    except ImportError:
        return

    class _DialogSizer(QObject):
        def eventFilter(self, obj, ev):
            try:
                if ev.type() in (QEvent.Type.Show,
                                  QEvent.Type.Polish):
                    if isinstance(obj, (QMessageBox, QInputDialog)):
                        # Fix now and again on the next loop turn -
                        # the first pass catches the initial layout,
                        # the deferred pass catches any WM resize
                        # that happens right after show.
                        self._fix(obj)
                        QTimer.singleShot(
                            0, lambda o=obj: self._fix(o))
                        QTimer.singleShot(
                            30, lambda o=obj: self._fix(o))
            except Exception:
                pass
            return False

        def _fix(self, dlg):
            try:
                if dlg is None or not dlg.isVisible():
                    return
                min_label = getattr(
                    dlg, "_quopus_min_label_applied", None)
                if min_label is None:
                    app = QApplication.instance()
                    min_label = getattr(
                        app, "_quopus_dialog_min_label", 340)
                    # Give the text labels a real minimum width
                    # (widget property) and ensure word-wrap, so
                    # heightForWidth is well-defined. We only do
                    # this once per dialog to avoid fighting our
                    # own re-runs.
                    for lbl in dlg.findChildren(QLabel):
                        n = lbl.objectName()
                        if n in ("qt_msgbox_label",
                                  "qt_msgbox_informativelabel"):
                            lbl.setWordWrap(True)
                            if lbl.minimumWidth() < min_label:
                                lbl.setMinimumWidth(min_label)
                    dlg._quopus_min_label_applied = min_label
                # Recompute the whole layout now that the labels
                # have their widths, then grow the dialog to the
                # computed size. Never shrink below current.
                lay = dlg.layout()
                if lay is not None:
                    lay.activate()
                dlg.adjustSize()
                hint = dlg.sizeHint()
                w = max(dlg.width(), hint.width())
                h = max(dlg.height(), hint.height())
                if w > dlg.width() or h > dlg.height():
                    dlg.setMinimumSize(w, h)
                    dlg.resize(w, h)
            except Exception:
                pass

    _DIALOG_SIZER = _DialogSizer(app)
    app.installEventFilter(_DIALOG_SIZER)


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
