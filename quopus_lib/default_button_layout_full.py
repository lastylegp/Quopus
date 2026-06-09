# date_time: 2026-06-07 23:55
"""Reference default button-layout for Quopus Commander with
every action in the catalog wired to a button.

There are 93 actions (as of June 2026) and 3 layers x 6 rows
x 6 columns = 108 buttons. Layout strategy:

  Layer 1 - Main          : everyday file operations + viewers +
                            navigation. The F-keys you press
                            constantly.

  Layer 2 - Shift         : networking, cloud, execute, system,
                            text conversion, non-C64 viewers.
                            Things you don't reach for daily but
                            want one keypress away.

  Layer 3 - Shift + Alt   : C64 / Amiga / Retro toolchain. CBM
                            disk editors, SID / MOD players,
                            Retro GFX, emulator + hardware
                            launchers. Placed here last per
                            user request (C64-stuff hidden behind
                            an extra modifier so the main grid
                            stays general-purpose).

How to use:

  Copy DEFAULT_BUTTONS, DEFAULT_BUTTONS_SHIFT, and
  DEFAULT_BUTTONS_SHIFT_ALT below into quopus_lib/config.py
  to replace the existing assignments (line ~453). Or save
  this file as-is and import the three lists from your own
  startup code:

      from default_button_layout_full import (
          DEFAULT_BUTTONS,
          DEFAULT_BUTTONS_SHIFT,
          DEFAULT_BUTTONS_SHIFT_ALT,
      )

Empty button slots are represented as
  {"label": "", "action": "", "color": "gray"}
which Quopus renders as an inactive placeholder.

Action `hotkey` is deliberately omitted: it's a dispatcher
that simulates pressing a built-in keyboard shortcut and
needs a `param` field. The Button Config dialog adds it
automatically when you pick a built-in hotkey from the
Hotkey dropdown - you don't set this one in the layout
table by hand.
"""

# ---------------------------------------------------------------
# Layer 1 - Main: daily file operations
#
# Colour scheme (consistent across all three layers):
#   orange  = standard "do something" command (file ops, viewers,
#             execute, convert, smart-fill, search) - the main
#             work buttons you hit constantly
#   blue    = navigation / selection / read-only info / config
#             (Parent/Root/Back/Forward, Select All/None, Info,
#             Sizes, Buffers, system Config/About/License)
#   green   = remote / network (FTP, Telnet, Telegram, IRC,
#             QDrive, Database, Rclone, Cloud)
#   purple  = media / player / viewer-with-output (SID, MOD,
#             YouTube, image / archive / Retro-GFX viewers)
#   red     = destructive (Delete, Quit)
#   gray    = empty placeholder
# ---------------------------------------------------------------
DEFAULT_BUTTONS = [
    # Row 1 - Classic F-key file ops
    [
        {"label": "Copy",         "action": "copy",          "color": "orange"},
        {"label": "Move",         "action": "move",          "color": "orange"},
        {"label": "Delete",       "action": "delete",        "color": "red"},
        {"label": "Rename",       "action": "rename",        "color": "orange"},
        {"label": "Multi Rename", "action": "multi_rename",  "color": "orange"},
        {"label": "Makedir",      "action": "makedir",       "color": "orange"},
    ],
    # Row 2 - Viewers (read, hex, edit, etc. - all standard ops)
    [
        {"label": "Read",         "action": "read",          "color": "orange"},
        {"label": "Hex Read",     "action": "hexread",       "color": "orange"},
        {"label": "Edit",         "action": "edit",          "color": "orange"},
        {"label": "Show",         "action": "show",          "color": "orange"},
        {"label": "Play",         "action": "play",          "color": "orange"},
        {"label": "Info",         "action": "info",          "color": "blue"},
    ],
    # Row 3 - Navigation (all blue)
    [
        {"label": "Parent",       "action": "parent",        "color": "blue"},
        {"label": "Root",         "action": "root",          "color": "blue"},
        {"label": "Back",         "action": "back",          "color": "blue"},
        {"label": "Forward",      "action": "forward",       "color": "blue"},
        {"label": "Swap",         "action": "swap",          "color": "blue"},
        {"label": "Reread",       "action": "reread",        "color": "blue"},
    ],
    # Row 4 - Selection (blue) + search/find/compare (orange)
    [
        {"label": "All",          "action": "select_all",    "color": "blue"},
        {"label": "None",         "action": "select_none",   "color": "blue"},
        {"label": "Search",       "action": "search",        "color": "orange"},
        {"label": "Hunt",         "action": "find",          "color": "orange"},
        {"label": "Compare",      "action": "compare",       "color": "orange"},
        {"label": "Goto Dir",     "action": "goto_dir",      "color": "blue"},
    ],
    # Row 5 - Smart-fill (orange) + size tools (blue, read-only)
    [
        {"label": "SmartCopy",    "action": "smart_fill_copy","color":"orange"},
        {"label": "SmartMove",    "action": "smart_fill_move","color":"orange"},
        {"label": "Sizes",        "action": "getsizes",      "color": "blue"},
        {"label": "CheckFit",     "action": "checkfit",      "color": "blue"},
        {"label": "DirReverse",   "action": "dir_reverse",   "color": "blue"},
        {"label": "Hide 8+3",     "action": "toggle_non_dos83","color": "blue"},
    ],
    # Row 6 - Archive (orange) + metadata edit (orange) + buffers (blue)
    [
        {"label": "Archive",      "action": "archive",       "color": "orange"},
        {"label": "Extract",      "action": "extract",       "color": "orange"},
        {"label": "Comment",      "action": "comment",       "color": "orange"},
        {"label": "Datestamp",    "action": "datestamp",     "color": "orange"},
        {"label": "Protect",      "action": "protect",       "color": "orange"},
        {"label": "Buffers",      "action": "buffers",       "color": "blue"},
    ],
]


# ---------------------------------------------------------------
# Layer 2 - Shift: networking, execute, system, text conversion,
#                   non-C64 viewers
# ---------------------------------------------------------------
DEFAULT_BUTTONS_SHIFT = [
    # Row 1 - Networking (green = remote)
    [
        {"label": "FTP",          "action": "ftp",           "color": "green"},
        {"label": "FTP Site",     "action": "ftp_site",      "color": "green"},
        {"label": "FTP Upload",   "action": "ftp_upload",    "color": "green"},
        {"label": "Telnet",       "action": "telnet",        "color": "green"},
        {"label": "Telnet Site",  "action": "telnet_site",   "color": "green"},
        {"label": "QDrive",       "action": "qdrive",        "color": "green"},
    ],
    # Row 2 - More networking + DB + cloud (green = remote)
    [
        {"label": "QDrive Site",  "action": "qdrive_site",   "color": "green"},
        {"label": "Database",     "action": "database",      "color": "green"},
        {"label": "Telegram",     "action": "telegram",      "color": "green"},
        {"label": "IRC",          "action": "irc",           "color": "green"},
        {"label": "Rclone",       "action": "rclone",        "color": "green"},
        {"label": "Rclone Setup", "action": "rclone_setup",  "color": "green"},
    ],
    # Row 3 - Execute / shell / scripts (orange = standard run command)
    [
        {"label": "Run",          "action": "run",           "color": "orange"},
        {"label": "Shell",        "action": "shell",         "color": "orange"},
        {"label": "Print",        "action": "print",         "color": "orange"},
        {"label": "Exec Cmd",     "action": "execute_command","color":"orange"},
        {"label": "Ext Script",   "action": "external_script","color":"orange"},
        {"label": "Custom Cmd",   "action": "custom_cmd",    "color": "orange"},
    ],
    # Row 4 - Viewers / media players (purple = renders content)
    [
        {"label": "Image View",   "action": "image_viewer",  "color": "purple"},
        {"label": "Archive View", "action": "archive_viewer","color": "purple"},
        {"label": "YouTube",      "action": "youtube_audio", "color": "purple"},
        {"label": "AmigaGuide",   "action": "amigaguide_viewer","color":"purple"},
        {"label": "ADF View",     "action": "adf_viewer",    "color": "purple"},
        {"label": "ADF New",      "action": "adf_new",       "color": "orange"},
    ],
    # Row 5 - Text conversions (orange = standard convert)
    [
        {"label": "PETSCII Conv", "action": "petscii_convert","color":"orange"},
        {"label": "ASCII->PET",   "action": "ascii_to_petscii","color":"orange"},
        {"label": "PET->ASCII",   "action": "petscii_to_ascii","color":"orange"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
    ],
    # Row 6 - System (blue = info/config, red = destructive Quit)
    [
        {"label": "Config",       "action": "config",        "color": "blue"},
        {"label": "ListerColors", "action": "lister_colors", "color": "blue"},
        {"label": "Drives",       "action": "show_drives",   "color": "blue"},
        {"label": "License",      "action": "license",       "color": "blue"},
        {"label": "About",        "action": "about",         "color": "blue"},
        {"label": "Quit",         "action": "quit",          "color": "red"},
    ],
]


# ---------------------------------------------------------------
# Layer 3 - Shift + Alt: C64 / Amiga / Retro toolchain
# Placed last per user request: keep the daily grid general-
# purpose, push retro-specific tooling behind an extra modifier.
# ---------------------------------------------------------------
DEFAULT_BUTTONS_SHIFT_ALT = [
    # Row 1 - CBM disk / cassette / cartridge tools (orange = standard ops)
    [
        {"label": "D64 Editor",   "action": "d64editor",     "color": "orange"},
        {"label": "BASIC Editor", "action": "basic_editor",  "color": "orange"},
        {"label": "Disasm",       "action": "disasm",        "color": "orange"},
        {"label": "Asm64 Browse", "action": "asm64",         "color": "orange"},
        {"label": "CRT Toolkit",  "action": "crt_toolkit",   "color": "orange"},
        {"label": "TAP Toolkit",  "action": "tap_toolkit",   "color": "orange"},
    ],
    # Row 2 - Ultimate 64 + VICE: launch actions orange, configs blue
    [
        {"label": "U64 Streamer", "action": "u64view",       "color": "purple"},
        {"label": "Run on U64",   "action": "run_u64",       "color": "orange"},
        {"label": "U64 Config",   "action": "u64_config",    "color": "blue"},
        {"label": "Run in Emu",   "action": "run_emu",       "color": "orange"},
        {"label": "C64 Emu Cfg",  "action": "c64_emu_config","color": "blue"},
        {"label": "VICE Mon",     "action": "vice_memory",   "color": "orange"},
    ],
    # Row 3 - SID audio (purple = media player)
    [
        {"label": "SID Player",   "action": "sidplayer",     "color": "purple"},
        {"label": "SID Playlist", "action": "sidplayer_playlist","color":"purple"},
        {"label": "Multi SID",    "action": "multi_sid",     "color": "purple"},
        {"label": "Shuffle SIDs", "action": "shuffle_sids",  "color": "purple"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
    ],
    # Row 4 - MOD audio (purple = media player)
    [
        {"label": "MOD Player",   "action": "modplayer",     "color": "purple"},
        {"label": "MOD Playlist", "action": "modplayer_playlist","color":"purple"},
        {"label": "Shuffle MODs", "action": "shuffle_mods",  "color": "purple"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
    ],
    # Row 5 - Retro graphics viewers (purple = renders content)
    [
        {"label": "Retro GFX",    "action": "retrogfx",      "color": "purple"},
        {"label": "GFX Browser",  "action": "retrogfx_browser","color":"purple"},
        {"label": "GFX Open",     "action": "retrogfx_file", "color": "purple"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
    ],
    # Row 6 - Drive assign (blue = navigation) + reserved slots
    [
        {"label": "Assign Drive", "action": "assign",        "color": "blue"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
        {"label": "",             "action": "",              "color": "gray"},
    ],
]


# ---------------------------------------------------------------
# Sanity check: total slots vs. catalog coverage
# ---------------------------------------------------------------

if __name__ == "__main__":
    # Two modes:
    #   no args        : coverage check (which catalog actions
    #                    are wired to which layer)
    #   --write [path] : write a complete clean quopus.cfg to
    #                    <path> (default: ./quopus.cfg). Uses
    #                    DEFAULT_CONFIG from quopus_lib/config.py
    #                    plus the three button layers from this
    #                    file. Safe to drop into config/
    #                    after backing up the existing one.
    import sys
    import os
    import json

    # Path setup so this works from repo root OR from inside
    # quopus_lib/.
    try:
        from quopus_lib.action_catalog import flat_action_keys
        from quopus_lib.config import DEFAULT_CONFIG
    except ModuleNotFoundError:
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(here) == "quopus_lib":
            parent = os.path.dirname(here)
        else:
            parent = here
        if parent not in sys.path:
            sys.path.insert(0, parent)
        from quopus_lib.action_catalog import flat_action_keys
        from quopus_lib.config import DEFAULT_CONFIG

    args = sys.argv[1:]

    if args and args[0] in ("--write", "--write-config", "-w"):
        # Build a complete config dict by copying DEFAULT_CONFIG
        # and overwriting just the three button layers. Every
        # other setting (lister colors, fonts, drives, FTP
        # bookmarks shape, etc.) stays at its default - the
        # idea is a fresh-install-style file, not a merge with
        # the user's existing one.
        cfg = dict(DEFAULT_CONFIG)
        cfg["buttons"] = DEFAULT_BUTTONS
        cfg["buttons_shift"] = DEFAULT_BUTTONS_SHIFT
        cfg["buttons_shift_alt"] = DEFAULT_BUTTONS_SHIFT_ALT

        # Strip OS-specific entries: these get computed at
        # runtime by _system_default_drives() and reflect
        # whatever machine generated this file. If we baked
        # them in, a Linux-generated file would carry Linux
        # mount points across to a Windows install. Quopus
        # re-fills them on first launch.
        for stale in ("drives", "left_path", "right_path",
                       "window_geometry",
                       "lister_splitter_sizes",
                       "u64_screenshot_dir",
                       "irc_log_dir",
                       "update_last_seen_sha"):
            cfg.pop(stale, None)

        # Pick output path. Default = ./quopus.cfg next to
        # wherever you launched python from.
        out_path = args[1] if len(args) > 1 else "quopus.cfg"

        # Existing-file safety: don't silently overwrite.
        # User is told to remove or rename first.
        if os.path.exists(out_path):
            print(
                f"refusing to overwrite existing {out_path!r} - "
                f"rename or delete it first.", file=sys.stderr)
            sys.exit(2)

        # Match Quopus' on-disk format: pretty-printed JSON
        # with 2-space indent, sort keys for stable diffs.
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True,
                      ensure_ascii=False)
            f.write("\n")
        size = os.path.getsize(out_path)
        print(f"Wrote {out_path}  ({size} bytes, "
              f"{len(cfg)} top-level keys, "
              f"3 button layers fully populated).")
        sys.exit(0)

    # ----- Coverage check mode (default) -----
    all_actions = set(flat_action_keys())
    used = set()
    for layer_name, layer in [
            ("main", DEFAULT_BUTTONS),
            ("shift", DEFAULT_BUTTONS_SHIFT),
            ("shift_alt", DEFAULT_BUTTONS_SHIFT_ALT)]:
        n_btns = 0
        for row in layer:
            for b in row:
                act = b.get("action", "")
                if act:
                    used.add(act)
                    n_btns += 1
        print(f"  {layer_name:10s}: {n_btns} buttons used")
    missing = all_actions - used
    extra = used - all_actions
    print(f"\nCatalog actions: {len(all_actions)}")
    print(f"Wired actions:   {len(used)}")
    if missing:
        print(f"Missing from layout ({len(missing)}):")
        for a in sorted(missing):
            print(f"  - {a}")
    if extra:
        print(f"In layout but NOT in catalog ({len(extra)}):")
        for a in sorted(extra):
            print(f"  - {a}")
    if not missing and not extra:
        print("Coverage: 100% - all catalog actions have a button.")
    print()
    print("Tip: pass --write [path] to emit a fresh quopus.cfg")
    print("     populated with these layers + every default")
    print("     setting from DEFAULT_CONFIG.")
