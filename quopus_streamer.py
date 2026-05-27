#!/usr/bin/env python3
# date_time: 2026-05-27 16:20
"""Standalone-Starter fuer den U64-Stream-Viewer.

Erlaubt es den U64-Streamer ohne Quopus zu starten - z.B. von einem
BBS-Door, der den Viewer nach erfolgreicher Anmeldung aufpoppt und
ihn beim Logout wieder zumacht.

Host, Ports und Passwort kommen aus der normalen Quopus-Config
(~/.quopus/quopus.cfg) - dieselben Werte die auch der eingebaute
Streamer benutzt. Das Skript hat selbst keine eigenen Connection-
Settings.

Aufruf:
    python quopus_streamer.py
        Streamer starten, kein Auto-Close. Wie 'U64 streamer' in Quopus,
        nur ohne Quopus-Fenster.

    python quopus_streamer.py --minimal
        Streamer im "Kiosk"-Mode starten: nur das Video-Bild, KEINE
        Buttons, Toolbar, Hostzeile, F-Tasten-Reihe, KEINE OS-
        Titelzeile. Linksklick im Bild + ziehen verschiebt das
        Fenster (Picture = Drag-Handle, weil keine Titelzeile da).
        Rechtsklick irgendwo im Fenster oeffnet "Close Streamer?"
        Yes/No. Position wird in CONFIG_DIR/u64_streamer_minimal_pos.json
        gespeichert sobald die Maustaste losgelassen wird - beim
        naechsten --minimal-Start landet das Fenster an derselben
        Stelle. Default-Skalierung: 2x (768x544).

    python quopus_streamer.py --minimal=N
        Wie --minimal, aber mit explizitem Skalierungsfaktor N
        (1..8) aus dieser Tabelle:
          --minimal=1      192 x  136   (Halb-VIC-II, kleinste sinnvolle)
          --minimal=2      384 x  272   (native VIC-II 1:1)
          --minimal=3      576 x  408
          --minimal=4      768 x  544   (Default)
          --minimal=5      960 x  680
          --minimal=6     1152 x  816
          --minimal=7     1536 x 1088
          --minimal=8     1920 x 1360   (~FullHD)
        Bequem fuer BBS-Doors die einen bestimmten Platz auf
        einem Multi-Monitor-Setup nutzen wollen.

    python quopus_streamer.py BBS WATCH_ADDR WATCH_VALUE
        Streamer starten, plus alle 60 Sekunden WATCH_ADDR pollen.
        Sobald das Byte dort gleich WATCH_VALUE ist, Fenster schliessen.

        Beispiel:
            python quopus_streamer.py BBS 0400 0

        Pollt $0400; sobald da $00 steht (etwa weil das BBS-Door
        beim Logout dorthin eine 0 schreibt), endet der Streamer.

    python quopus_streamer.py --minimal BBS 0400 0
        Beides kombinierbar - Kiosk-Mode plus Auto-Close-Watch.

WATCH_ADDR und WATCH_VALUE akzeptieren $hex, 0xhex, oder dezimal.
Bei reinen Ziffern mit fuehrender 0 und 3+ Stellen (z.B. '0400')
wird hex angenommen, sonst dezimal.

Der erste Polling-Tick erfolgt 60 Sekunden nach dem Start - der
User hat also Zeit den Stream zu sehen bevor das Auto-Close-System
losgeht.
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
        print("Usage: quopus_streamer.py [--minimal[=1..8]] [BBS ADDR VALUE]\n"
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
    # Selbe Keys wie in actions.py:act_u64view - so ist garantiert
    # dass standalone und Quopus-eingebauter Streamer identische
    # Einstellungen sehen. Wenn der User Host/Port in Quopus aendert,
    # wirkt das automatisch hier.
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
