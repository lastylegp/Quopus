#!/usr/bin/env python3
# date_time: 2026-05-28 08:47
"""Standalone launcher for the U64 video-stream viewer.

Lets you start the U64 streamer without Quopus - e.g. from a
BBS door that pops the viewer up after a successful login and
closes it again on logout.

Host, ports and password come from the normal Quopus config
(~/.quopus/quopus.cfg) - the same values the built-in streamer
uses. The script has no connection settings of its own.

Usage:
    python quopus_streamer.py
        Start the streamer, no auto-close. Same as 'U64 streamer'
        inside Quopus, just without the Quopus window.

    python quopus_streamer.py --minimal
        Start the streamer in "kiosk" mode: only the video
        picture, NO buttons, toolbar, host row or F-key row, and
        NO OS title bar. Left-click in the picture and drag to
        move the window (the picture is the drag handle since
        there's no title bar). Right-click anywhere in the
        window opens a "Close Streamer?" Yes/No prompt. The
        window position is saved to
        CONFIG_DIR/u64_streamer_minimal_pos.json as soon as you
        release the mouse - the next --minimal start places the
        window in the same spot. Default scale: 4 (768x544).

    python quopus_streamer.py --minimal=N
        Like --minimal but with an explicit scale factor N
        (1..8) from this table:
          --minimal=1      192 x  136   (half VIC-II, smallest useful)
          --minimal=2      384 x  272   (native VIC-II 1:1)
          --minimal=3      576 x  408
          --minimal=4      768 x  544   (default)
          --minimal=5      960 x  680
          --minimal=6     1152 x  816
          --minimal=7     1536 x 1088
          --minimal=8     1920 x 1360   (~Full HD)
        Handy for BBS doors that want to use a specific spot on
        a multi-monitor setup.

    python quopus_streamer.py BBS WATCH_ADDR WATCH_VALUE
        Start the streamer plus poll WATCH_ADDR every 60 seconds.
        As soon as the byte there equals WATCH_VALUE, close the
        window.

        Example:
            python quopus_streamer.py BBS 0400 0

        Polls $0400; once it reads $00 there (e.g. because the
        BBS door writes a 0 to it on logout) the streamer exits.

    python quopus_streamer.py --minimal BBS 0400 0
        Both can be combined - kiosk mode plus auto-close watch.

    python quopus_streamer.py --device=N
    python quopus_streamer.py --device=NAME
        Explicitly choose which Ultimate-64 from the config to
        stream from. N is the 1-based index (--device=1 = first
        device), NAME is the device name (case-insensitive,
        --device=u64lab). Without --device:
          - exactly 1 device configured -> it is used
          - multiple devices + normal start -> a chooser dialog
            appears, remembering your last pick (pre-selected on
            the next start)
          - multiple devices + --minimal -> the last-used device
            is taken WITHOUT a dialog (kiosk/BBS must never block
            on user input)
        For BBS-door automation ALWAYS set --device explicitly so
        that no dialog can ever appear. Example:
            python quopus_streamer.py --minimal=4 --device=2 BBS 0400 0

WATCH_ADDR and WATCH_VALUE accept $hex, 0xhex, or decimal. Pure
digits with a leading 0 and 3+ places (e.g. '0400') are treated
as hex, otherwise decimal.

The first polling tick happens 60 seconds after start - so the
user has time to see the stream before the auto-close system
kicks in.
"""

import os
import sys


def parse_byte_value(s: str) -> int:
    """Parse eine Byte/Adresse-Eingabe in dezimal oder hex.

    Erkennt $hex, 0xhex, hex-Buchstaben (a-f) als hex, sonst:
    - 3+ Stellen mit fuehrender 0 -> hex (typisch '0400' = $0400)
    - >255 -> hex (Adressen)
    - sonst dezimal

    Damit funktionieren typische BBS-Aufrufe wie '0400 0' (Adresse
    in Hex-Konvention, Value als dezimal 0) ohne weitere Prefix-
    Tippung. Wer ungluecklich konvertiert hat, kann immer auf
    '$0400 $00' / '0x0400 0x00' ausweichen.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty value")
    if s.startswith('$'):
        return int(s[1:], 16)
    if s.startswith(('0x', '0X')):
        return int(s[2:], 16)
    if any(c in 'abcdefABCDEF' for c in s):
        return int(s, 16)
    if len(s) >= 3 and s[0] == '0':
        return int(s, 16)
    val = int(s)
    if val > 255:
        return int(s, 16)
    return val


def main():
    argv = sys.argv[1:]

    if argv and argv[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)

    # --minimal flag: hide all chrome, just show the video.
    # Right-click pops "Close Streamer?" Yes/No. Can be combined
    # with BBS auto-close. The flag can take an optional scale
    # value 1..8 after an = sign (picks from a fixed table of
    # window sizes; see u64_streamer._MINIMAL_SIZES):
    #   --minimal       -> scale 4 (768x544, default)
    #   --minimal=1     ->  192x 136 (half VIC-II)
    #   --minimal=2     ->  384x 272 (native 1:1)
    #   --minimal=3     ->  576x 408
    #   --minimal=4     ->  768x 544
    #   --minimal=5     ->  960x 680
    #   --minimal=6     -> 1152x 816
    #   --minimal=7     -> 1536x1088
    #   --minimal=8     -> 1920x1360 (~FullHD)
    minimal_mode = False
    minimal_scale = 4          # default if --minimal without =N
    # --device=N (1-based index) or --device=name selects which
    # configured Ultimate-64 to stream from when several are in
    # the config. Without it: if exactly one device, use it; if
    # several, show a picker (unless --minimal, see below). For
    # BBS-door automation pass --device explicitly so no dialog
    # ever appears.
    device_selector = None
    new_argv = []
    for a in argv:
        if a == '--minimal':
            minimal_mode = True
        elif a.startswith('--minimal='):
            minimal_mode = True
            val = a.split('=', 1)[1]
            try:
                minimal_scale = int(val)
            except ValueError:
                print(f"Invalid --minimal scale: {val!r} "
                      f"(use 1..8)", file=sys.stderr)
                sys.exit(2)
            if not (1 <= minimal_scale <= 8):
                print(f"--minimal scale {minimal_scale} out of "
                      f"range (use 1..8)", file=sys.stderr)
                sys.exit(2)
        elif a.startswith('--device='):
            device_selector = a.split('=', 1)[1]
        else:
            new_argv.append(a)
    argv = new_argv

    # Argumente parsen: drei Modi
    #   []                           -> normaler Start, kein Auto-Close
    #   ['BBS', addr, value]         -> Start + Auto-Close
    #   alles andere                 -> Syntax-Fehler
    watch_addr = None
    watch_value = None
    if not argv:
        pass    # plain start
    elif (len(argv) == 3
            and argv[0].upper() == 'BBS'):
        try:
            watch_addr = parse_byte_value(argv[1])
        except ValueError as e:
            print(f"Invalid WATCH_ADDR {argv[1]!r}: {e}", file=sys.stderr)
            sys.exit(2)
        try:
            watch_value = parse_byte_value(argv[2])
        except ValueError as e:
            print(f"Invalid WATCH_VALUE {argv[2]!r}: {e}", file=sys.stderr)
            sys.exit(2)
        if not (0 <= watch_addr <= 0xFFFF):
            print(f"WATCH_ADDR out of range: ${watch_addr:X}",
                    file=sys.stderr)
            sys.exit(2)
        if not (0 <= watch_value <= 0xFF):
            print(f"WATCH_VALUE out of range: ${watch_value:X}",
                    file=sys.stderr)
            sys.exit(2)
    else:
        print("Usage: quopus_streamer.py [--minimal[=1..8]] "
              "[--device=N|NAME] [BBS ADDR VALUE]\n"
              "       (use --help for details)", file=sys.stderr)
        sys.exit(2)

    # quopus_lib aus dem Skript-Verzeichnis importierbar machen.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtCore import QTimer
    from quopus_lib.u64_streamer import (
        U64Streamer, PORT_VIDEO, PORT_AUDIO, PORT_TELNET, PORT_HTTP,
    )
    from quopus_lib.config import load_config

    cfg = load_config()

    app = QApplication(sys.argv)
    app.setApplicationName("U64 Streamer")
    app.setApplicationDisplayName("U64 Streamer")
    app.setOrganizationName("lA-sTYLe")
    # Standalone-Streamer benutzt dasselbe Quopus-Icon, sonst zeigt
    # Windows/Linux einen generischen Python-Pinguin.
    try:
        from PyQt6.QtGui import QIcon
        from quopus_lib.config import BUNDLE_DIR
        icon_path = BUNDLE_DIR / "quopus_lib" / "icons" / "quopus.png"
        if icon_path.is_file():
            app.setWindowIcon(QIcon(str(icon_path)))
    except Exception:
        pass

    # ---- Resolve which Ultimate-64 device to stream from -------
    # The config can hold several devices (u64_devices list). We
    # pick one of them and copy its connection details into the
    # local host/port/etc variables. Resolution order:
    #   1. --device=N (1-based) or --device=name  -> explicit pick
    #   2. exactly one device configured           -> use it
    #   3. multiple devices, NOT minimal mode      -> show picker
    #      (remembers last choice via u64_active_device)
    #   4. multiple devices, minimal mode          -> use the
    #      active/last-used device silently (kiosk/BBS use must
    #      not block on a dialog)
    # Falls through to the legacy single-key read if the multi-
    # device module isn't available for some reason.
    host = ""
    video_port = PORT_VIDEO
    audio_port = PORT_AUDIO
    telnet_port = PORT_TELNET
    http_port = PORT_HTTP
    password = ""
    video_only = False
    always_on_top = False

    chosen_device = None
    try:
        from quopus_lib.u64_devices import (
            get_devices, get_active_index, set_active_index,
            device_display_name, pick_device)
        devices = get_devices(cfg)
    except Exception:
        devices = []

    if devices:
        if device_selector is not None:
            # Explicit --device=N or --device=name
            sel = device_selector.strip()
            chosen_device = None
            # Try 1-based numeric index first
            if sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(devices):
                    chosen_device = devices[idx]
                    set_active_index(cfg, idx)
                else:
                    print(f"--device={sel}: index out of range "
                          f"(have {len(devices)} device(s))",
                          file=sys.stderr)
                    sys.exit(2)
            else:
                # Match by name (case-insensitive)
                for i, d in enumerate(devices):
                    if (d.get('name', '') or '').lower() \
                            == sel.lower():
                        chosen_device = d
                        set_active_index(cfg, i)
                        break
                if chosen_device is None:
                    names = ", ".join(
                        d.get('name', '?') for d in devices)
                    print(f"--device={sel!r}: no device with "
                          f"that name. Configured: {names}",
                          file=sys.stderr)
                    sys.exit(2)
        elif len(devices) == 1:
            chosen_device = devices[0]
        elif minimal_mode:
            # Kiosk/BBS: never block on a dialog. Use the active
            # (last-used) device silently.
            ai = get_active_index(cfg)
            if 0 <= ai < len(devices):
                chosen_device = devices[ai]
            else:
                chosen_device = devices[0]
        else:
            # Multiple devices, interactive: show the picker.
            # pick_device remembers the choice as the new active
            # device (persisted), so next launch pre-selects it.
            chosen_device = pick_device(
                None, cfg,
                title="U64 Streamer",
                prompt="Which Ultimate-64 should the streamer "
                       "connect to?")
            if chosen_device is None:
                # User cancelled - nothing to stream.
                print("No device selected - exiting.")
                sys.exit(0)

    if chosen_device is not None:
        host = (chosen_device.get('host', '') or '').strip()
        try:
            video_port = int(chosen_device.get(
                'video_port', PORT_VIDEO))
            audio_port = int(chosen_device.get(
                'audio_port', PORT_AUDIO))
            telnet_port = int(chosen_device.get(
                'telnet_port', PORT_TELNET))
            http_port = int(chosen_device.get(
                'http_port', PORT_HTTP))
        except (ValueError, TypeError):
            pass
        password = chosen_device.get('password', '') or ''
        video_only = bool(chosen_device.get('video_only', False))
        always_on_top = bool(
            chosen_device.get('always_on_top', False))
    else:
        # No multi-device list available - fall back to the
        # legacy single-device keys so old configs still work.
        host = cfg.get('u64_host', '') or ""
        try:
            video_port = int(cfg.get('u64_video_port', PORT_VIDEO))
            audio_port = int(cfg.get('u64_audio_port', PORT_AUDIO))
            telnet_port = int(cfg.get('u64_telnet_port', PORT_TELNET))
            http_port = int(cfg.get('u64_http_port', PORT_HTTP))
        except (ValueError, TypeError):
            video_port = PORT_VIDEO
            audio_port = PORT_AUDIO
            telnet_port = PORT_TELNET
            http_port = PORT_HTTP
        password = cfg.get('u64_password', '') or ""
        video_only = bool(cfg.get('u64_video_only', False))
        always_on_top = bool(cfg.get('u64_always_on_top', False))

    # Wenn kein Host konfiguriert ist: erstmal melden statt einfach
    # ein leeres Fenster aufmachen. Der User soll wissen dass er erst
    # in Quopus den Host eintragen muss (oder eben einen via Streamer-
    # Config-Button setzt - der Standalone-Streamer hat den ja auch).
    if not host:
        QMessageBox.warning(
            None, "U64 Streamer",
            "No U64 host configured in Quopus settings "
            "(u64_host).\n\nThe streamer will open empty - use "
            "'Config...' button to set host/ports, or run Quopus "
            "first and configure there.")

    streamer = U64Streamer(
        default_host=host,
        video_port=video_port,
        audio_port=audio_port,
        telnet_port=telnet_port,
        http_port=http_port,
        password=password,
        video_only=video_only,
        always_on_top=always_on_top)
    streamer.show()

    # Minimal mode: hide every chrome element so only the video
    # picture is visible. Has to happen AFTER show() because Qt
    # only finishes its initial layout pass at that point - hiding
    # things earlier sometimes leaves stray space where the widgets
    # used to be. The right-click "Close Streamer?" confirmation
    # is wired up inside enter_minimal_mode itself.
    if minimal_mode:
        streamer.enter_minimal_mode(scale=minimal_scale)

    # Stream automatisch starten - simuliert den 'Start'-Klick.
    # 50ms Verzoegerung damit das Fenster fertig gelayouted ist.
    if host:
        QTimer.singleShot(50, streamer._on_start)

    # Auto-Close-Watch nur registrieren wenn 'BBS addr value' kam.
    if watch_addr is not None and watch_value is not None:
        if not host:
            print("Warning: BBS auto-close requested but no host "
                  "configured. The watch won't work until a host "
                  "is set.", file=sys.stderr)
        else:
            streamer.set_autoclose_watch(
                watch_addr, watch_value, interval_seconds=60)
            print(f"Auto-close watch: ${watch_addr:04X} == "
                  f"${watch_value:02X} (polls every 60s)")

    print(f"Streaming from {host or '(no host set)'}")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
