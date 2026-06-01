# date_time: 2026-06-01 18:32
"""C64 graphics viewers: character set, Koala painter, Hi-Res bitmap.

Drei Dialoge in einem Modul - alle Q nicht-modal, parent=lister, mit
WA_DeleteOnClose damit Qt's GC sie aufraeumt wenn der User sie schliesst.

Format-Erkennung passiert in `show_retro_gfx_viewer(path, parent)` - die
ruft je nach Dateigroesse / Load-Adresse den richtigen Dialog auf.

Geteilte Helfer:
    C64_PALETTE - die 16 Standard-C64-Farben (RGB). Reuse aus
                    u64_streamer um Konsistenz zu halten.
    _read_prg(path) - liest .prg-Datei, returnt (load_addr, payload).
                       Liefert load_addr=None fuer raw .bin Dateien.
    _qimage_from_chars(charset, screen, ncols=40, nrows=25)
        - rendert ein Screen-Codes-Array gegen ein Charset zu einem
          QImage (mono, 1bpp transparent black).
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QFileDialog, QMessageBox, QComboBox,
    QSpinBox, QWidget, QSizePolicy, QGroupBox, QRadioButton,
    QScrollArea, QGridLayout, QButtonGroup, QFrame,
)
from PyQt6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFontDatabase, QFont, QIcon,
)
from PyQt6.QtCore import Qt, QSize

import os
from .config import scaled_font_px


# -----------------------------------------------------------------
# Folder browser helper - shared between BitmapViewer and RecoilViewer
# -----------------------------------------------------------------

_FOLDER_BROWSE_EXTS = None


def _get_browse_extensions():
    """Liste aller Extensions die der Retro GFX Viewer kennt - native
    decoders plus RECOIL. Cached."""
    global _FOLDER_BROWSE_EXTS
    if _FOLDER_BROWSE_EXTS is None:
        from .retro_gfx_decoders import RECOIL_EXTENSIONS, DECODERS
        exts = set(RECOIL_EXTENSIONS)
        for key, fn, sz, fexts, name in DECODERS:
            exts.update(fexts)
        _FOLDER_BROWSE_EXTS = frozenset(e.lower() for e in exts)
    return _FOLDER_BROWSE_EXTS


def scan_folder_for_gfx(folder):
    """Liste alle viewable Files in folder, sortiert nach name.
    Returnt absolute Pfade."""
    if not os.path.isdir(folder):
        return []
    exts = _get_browse_extensions()
    out = []
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip('.')
            if ext in exts:
                out.append(path)
    except OSError:
        return []
    out.sort(key=lambda p: os.path.basename(p).lower())
    return out


class FolderBrowserMixin:
    """Mixin fuer Viewer-Dialoge die Folder-Navigation per Pfeiltasten
    erlauben. Die Subklasse muss eine reload_path(new_path) Methode
    bereitstellen die das Bild ersetzt (re-decode + re-render).

    Pfeiltasten:
        Rechts:    +1
        Links:     -1
        Unten:     +10
        Oben:      -10
        Pos1:      erstes File
        Ende:      letztes File
        PgUp/Down: -/+10
    """

    def init_folder_browser(self, current_path):
        """Initialisiert die Folder-Liste. current_path ist das
        aktuell geoeffnete File."""
        folder = os.path.dirname(current_path)
        self._dir_files = scan_folder_for_gfx(folder)
        try:
            self._dir_index = self._dir_files.index(current_path)
        except ValueError:
            self._dir_files.insert(0, current_path)
            self._dir_index = 0
        self._dir_folder = folder

    def _nav_relative(self, delta):
        if not getattr(self, '_dir_files', None):
            return
        new_idx = max(0, min(len(self._dir_files) - 1,
                                  self._dir_index + delta))
        if new_idx == self._dir_index:
            return
        self._dir_index = new_idx
        path = self._dir_files[new_idx]
        try:
            self.reload_path(path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.setWindowTitle(
                f"[error] {os.path.basename(path)}: {e}")

    def _nav_first(self):
        if getattr(self, '_dir_files', None):
            self._dir_index = 0
            self.reload_path(self._dir_files[0])

    def _nav_last(self):
        if getattr(self, '_dir_files', None):
            self._dir_index = len(self._dir_files) - 1
            self.reload_path(self._dir_files[-1])

    def handle_arrow_key(self, event):
        """Aufruf aus keyPressEvent der Subklasse. Returnt True wenn
        das Event verarbeitet wurde."""
        k = event.key()
        if k == Qt.Key.Key_Right:
            self._nav_relative(1); return True
        if k == Qt.Key.Key_Left:
            self._nav_relative(-1); return True
        if k == Qt.Key.Key_Down:
            self._nav_relative(10); return True
        if k == Qt.Key.Key_Up:
            self._nav_relative(-10); return True
        if k == Qt.Key.Key_PageDown:
            self._nav_relative(10); return True
        if k == Qt.Key.Key_PageUp:
            self._nav_relative(-10); return True
        if k == Qt.Key.Key_Home:
            self._nav_first(); return True
        if k == Qt.Key.Key_End:
            self._nav_last(); return True
        return False

    def folder_status_text(self):
        """String fuer Status-Anzeige: 'File 5/123 in /some/folder'."""
        if not getattr(self, '_dir_files', None):
            return ""
        return (f"File {self._dir_index + 1}/{len(self._dir_files)}  "
                  f"in  {self._dir_folder}")


# -----------------------------------------------------------------
# Shared: C64 palette (PAL-style, "Pepto" calibration). Same array
# used in u64_streamer for video decoding. We duplicate it here
# rather than import to keep the gfx module standalone (no Qt-version
# of u64_streamer.py needs to be loaded just to view a Koala file).
# -----------------------------------------------------------------
C64_PALETTE = [
    (0x00, 0x00, 0x00),    # 0  black
    (0xFF, 0xFF, 0xFF),    # 1  white
    (0x88, 0x39, 0x32),    # 2  red
    (0x67, 0xB6, 0xBD),    # 3  cyan
    (0x8B, 0x3F, 0x96),    # 4  purple
    (0x55, 0xA0, 0x49),    # 5  green
    (0x40, 0x31, 0x8D),    # 6  blue
    (0xBF, 0xCE, 0x72),    # 7  yellow
    (0x8B, 0x54, 0x29),    # 8  orange
    (0x57, 0x42, 0x00),    # 9  brown
    (0xB8, 0x69, 0x62),    # 10 light red
    (0x50, 0x50, 0x50),    # 11 dark grey
    (0x78, 0x78, 0x78),    # 12 medium grey
    (0x94, 0xE0, 0x89),    # 13 light green
    (0x78, 0x69, 0xC4),    # 14 light blue
    (0x9F, 0x9F, 0x9F),    # 15 light grey
]


def _read_file(path):
    """Lies eine C64-Datei. Returnt (load_addr, payload).

    Wenn die Datei mit 2-Byte Load-Address beginnt (typisch .prg /
    .koa / .kla / .64c): Load-Addr wird extrahiert, payload ist der
    Rest. Wenn nicht (raw .bin, .chr): load_addr ist None, payload
    ist der gesamte Inhalt.

    Wir entscheiden anhand der DATEIENDUNG (am haeufigsten korrekt)
    plus PLAUSIBILITAETS-CHECK auf der Loadadress (16-bit, sollte
    bei C64-Files unter $FFFF liegen, was sowieso garantiert ist,
    aber typische Werte sind $0801, $2000, $4000, $6000, $A000,
    $C000, $E000).
    """
    with open(path, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    # Erweiterungen mit obligatorischer Load-Address
    if ext in ("prg", "koa", "kla", "64c", "bmp"):
        if len(raw) >= 2:
            load = raw[0] | (raw[1] << 8)
            return load, raw[2:]
        return None, raw
    # Raw .bin / .chr / .fnt - keine Load-Address
    return None, raw


# -----------------------------------------------------------------
# Charset-spezifische helpers
# -----------------------------------------------------------------

def _qimage_from_charset(charset_bytes, palette_fg=(255, 255, 255),
                            palette_bg=(0, 0, 0)):
    """Render einen kompletten 256-char Charset (2048 Bytes) als
    QImage, 16x16 Grid à 8x8 Pixel. Endgroesse: 128x128.

    palette_fg / palette_bg sind RGB-Tupel fuer Vordergrund und
    Hintergrund. Bei Charset-Browser typischerweise weiss/schwarz.
    """
    img = QImage(128, 128, QImage.Format.Format_RGB888)
    img.fill(QColor(*palette_bg))
    fg = QColor(*palette_fg).rgb()
    bg = QColor(*palette_bg).rgb()

    for char_idx in range(min(256, len(charset_bytes) // 8)):
        cx = (char_idx % 16) * 8
        cy = (char_idx // 16) * 8
        for row in range(8):
            byte = charset_bytes[char_idx * 8 + row]
            for col in range(8):
                if byte & (0x80 >> col):
                    img.setPixel(cx + col, cy + row, fg)
    return img


def _qimage_text_with_charset(text, charset_bytes, cols=40,
                                 palette_fg=(255, 255, 255),
                                 palette_bg=(0, 0, 0)):
    """Render einen Text-String mit dem gegebenen Charset.

    Text-Encoding: einfache ASCII->Screen-Code Mapping (Lower-Case
    Charset-Variante: Space=$20, !=$21, A=$01, a=$41).

    Wenn `cols` Zeichen pro Zeile, dann wachsen die Zeilen je nach
    Text-Laenge. Newline-Characters (\n) brechen explizit um.
    """
    # Text in Zeilen umbrechen
    lines = []
    for raw_line in text.split("\n"):
        # Lange Zeilen weiter umbrechen
        while len(raw_line) > cols:
            lines.append(raw_line[:cols])
            raw_line = raw_line[cols:]
        lines.append(raw_line)
    rows = max(1, len(lines))

    img = QImage(cols * 8, rows * 8, QImage.Format.Format_RGB888)
    img.fill(QColor(*palette_bg))
    fg = QColor(*palette_fg).rgb()

    for ry, line in enumerate(lines):
        for cx, ch in enumerate(line[:cols]):
            sc = _ascii_to_screencode(ch)
            if sc * 8 + 8 > len(charset_bytes):
                continue
            for row in range(8):
                byte = charset_bytes[sc * 8 + row]
                for col in range(8):
                    if byte & (0x80 >> col):
                        img.setPixel(cx * 8 + col,
                                       ry * 8 + row, fg)
    return img


def _ascii_to_screencode(ch):
    r"""Konvertiert ein Python-Zeichen in einen C64 Screencode.

    Screencodes ($00..$FF) sind nicht ASCII. Mapping fuer die
    haeufigsten gedruckten ASCII-Zeichen:
        A-Z (uppercase ASCII) -> $01..$1A (uppercase screen code)
        a-z (lowercase ASCII) -> $01..$1A (uppercase) - C64 hat
            kein lower-case in seinem Standard-Charset; fuer den
            Mixed-Case-Mode (charset bank 1) wuerden lower screen
            codes $01..$1A und upper $41..$5A genutzt. Wir nehmen
            hier Uppercase-Mode an (haeufigster Fall).
        0-9 -> $30..$39
        " " (space) -> $20
        Sonderzeichen so weit moeglich: !"#$%&'()*+,-./:;<=>?@[\]^_`
        Alles andere -> $20 (space)
    """
    c = ord(ch)
    # Lowercase nach Uppercase (Charset-Bank 0 hat nur Uppercase)
    if ord('a') <= c <= ord('z'):
        c = c - ord('a') + ord('A')
    if ord('A') <= c <= ord('Z'):
        return c - ord('A') + 0x01
    if ord('0') <= c <= ord('9'):
        return c
    # Direkt-Mapping fuer Printable ASCII
    mapping = {
        ' ': 0x20, '!': 0x21, '"': 0x22, '#': 0x23, '$': 0x24,
        '%': 0x25, '&': 0x26, "'": 0x27, '(': 0x28, ')': 0x29,
        '*': 0x2A, '+': 0x2B, ',': 0x2C, '-': 0x2D, '.': 0x2E,
        '/': 0x2F, ':': 0x3A, ';': 0x3B, '<': 0x3C, '=': 0x3D,
        '>': 0x3E, '?': 0x3F, '@': 0x00, '[': 0x1B, ']': 0x1D,
        '^': 0x1E, '_': 0x64, '`': 0x27,
    }
    return mapping.get(ch, 0x20)


# -----------------------------------------------------------------
# CharEditorDialog - editiert ein einzelnes 8x8 Zeichen
# -----------------------------------------------------------------

class CharEditorDialog(QDialog):
    """Editiert ein einzelnes 8x8-Pixel Zeichen aus dem Charset.

    Links: 8x8 grosses klickbares Grid (24px pro Pixel = 192x192). Mit
           Linke Maustaste = Pixel an, Rechte = Pixel aus, oder
           toggle wenn nicht gedrueckt.
    Rechts: Preview in Original-Groesse (1:1, also 8x8 Pixel) und
            in 2x Zoom (16x16 Pixel) - so sieht der User wie's auf
            dem echten C64 aussieht.
    Unten:  Hex-Anzeige der 8 Bytes, plus Apply / Revert / Cancel.

    Live: Jede Aenderung wird sofort im Preview und in den Hex-Bytes
    sichtbar.

    Die Klasse aendert NICHT das Charset des Callers direkt. Stattdessen:
    - input: bytes (8 bytes) - das original-Zeichen
    - .new_bytes: bytes (8 bytes) - das editierte Zeichen
    - exec() returnt DialogCode.Accepted wenn der User Apply geklickt
      hat, sonst Rejected. Im Accepted-Fall holt der Caller die neuen
      Bytes via dlg.new_bytes ab.
    """

    PIXEL_SIZE = 32   # Bildschirm-Pixel pro Char-Pixel im Edit-Grid

    def __init__(self, char_bytes, char_index, fg, bg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            f"Char Editor - $#{char_index:02X} ({char_index})")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(560, 380)

        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "char_editor")
        self._original = bytes(char_bytes[:8])
        if len(self._original) < 8:
            self._original = self._original + bytes(8 - len(self._original))
        # 8x8 bool array - True=FG, False=BG
        self._pixels = [[bool(self._original[row] & (0x80 >> col))
                            for col in range(8)] for row in range(8)]
        self._char_index = char_index
        self._fg = fg
        self._bg = bg
        # new_bytes wird beim Apply gesetzt und vom Caller gelesen
        self.new_bytes = None

        # Maus-Modus: wenn Maus gedrueckt wird, merkt der Editor sich ob
        # der erste Klick "set" oder "clear" war - das gleiche Verhalten
        # gilt dann beim Drag (so kann man "Pinsel-zeichnen")
        self._drag_mode = None    # None, True (set), False (clear)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Header info
        info = QLabel(
            f"Editing character <b>$#{char_index:02X}</b> "
            f"({char_index}) - left click toggles pixels, drag to "
            f"paint, right click forces clear.")
        outer.addWidget(info)

        # Body: Grid + Preview nebeneinander
        body = QHBoxLayout()
        body.setSpacing(12)

        # Edit-Grid
        edit_box = QGroupBox("Pixel grid (click to edit)")
        eg_l = QVBoxLayout(edit_box)
        self.lbl_edit = QLabel()
        self.lbl_edit.setFixedSize(8 * self.PIXEL_SIZE,
                                       8 * self.PIXEL_SIZE)
        self.lbl_edit.setStyleSheet(
            "background-color: #000; border: 1px solid #888;")
        self.lbl_edit.mousePressEvent = self._on_grid_press
        self.lbl_edit.mouseMoveEvent = self._on_grid_drag
        self.lbl_edit.mouseReleaseEvent = self._on_grid_release
        eg_l.addWidget(self.lbl_edit, 0, Qt.AlignmentFlag.AlignCenter)
        body.addWidget(edit_box)

        # Preview-Spalte
        prev_box = QGroupBox("Preview")
        pv_l = QVBoxLayout(prev_box)
        pv_l.addWidget(QLabel("<b>1:1 (8x8)</b>"))
        self.lbl_prev_1x = QLabel()
        self.lbl_prev_1x.setFixedSize(8, 8)
        self.lbl_prev_1x.setStyleSheet(
            "background-color: #000; border: 1px solid #888;")
        pv_l.addWidget(self.lbl_prev_1x)
        pv_l.addSpacing(4)
        pv_l.addWidget(QLabel("<b>2x (16x16)</b>"))
        self.lbl_prev_2x = QLabel()
        self.lbl_prev_2x.setFixedSize(16, 16)
        self.lbl_prev_2x.setStyleSheet(
            "background-color: #000; border: 1px solid #888;")
        pv_l.addWidget(self.lbl_prev_2x)
        pv_l.addSpacing(4)
        pv_l.addWidget(QLabel("<b>4x (32x32)</b>"))
        self.lbl_prev_4x = QLabel()
        self.lbl_prev_4x.setFixedSize(32, 32)
        self.lbl_prev_4x.setStyleSheet(
            "background-color: #000; border: 1px solid #888;")
        pv_l.addWidget(self.lbl_prev_4x)
        pv_l.addStretch(1)
        body.addWidget(prev_box)
        body.addStretch(1)
        outer.addLayout(body)

        # Hex bytes anzeige
        self.lbl_hex = QLabel()
        self.lbl_hex.setStyleSheet(
            "padding: 4px; background: #f0f0f0; "
            "font-family: 'Consolas', 'Courier New', monospace;")
        outer.addWidget(self.lbl_hex)

        # Reihe 1: Transform-Aktionen (Mirror/Rotate/Shift)
        tbar = QHBoxLayout()
        btn_mh = QPushButton("Mirror H")
        btn_mh.setToolTip("Flip horizontally (left ↔ right)")
        btn_mh.clicked.connect(self._action_mirror_h)
        tbar.addWidget(btn_mh)
        btn_mv = QPushButton("Mirror V")
        btn_mv.setToolTip("Flip vertically (top ↔ bottom)")
        btn_mv.clicked.connect(self._action_mirror_v)
        tbar.addWidget(btn_mv)
        btn_rcw = QPushButton("Rot 90°↻")
        btn_rcw.setToolTip("Rotate 90° clockwise")
        btn_rcw.clicked.connect(self._action_rotate_cw)
        tbar.addWidget(btn_rcw)
        btn_rccw = QPushButton("Rot 90°↺")
        btn_rccw.setToolTip("Rotate 90° counter-clockwise")
        btn_rccw.clicked.connect(self._action_rotate_ccw)
        tbar.addWidget(btn_rccw)
        btn_r180 = QPushButton("Rot 180°")
        btn_r180.setToolTip("Rotate 180°")
        btn_r180.clicked.connect(self._action_rotate_180)
        tbar.addWidget(btn_r180)
        tbar.addSpacing(8)
        btn_su = QPushButton("↑")
        btn_su.setFixedWidth(32)
        btn_su.setToolTip("Shift up by 1 row (wraps)")
        btn_su.clicked.connect(lambda: self._action_shift(0, -1))
        tbar.addWidget(btn_su)
        btn_sd = QPushButton("↓")
        btn_sd.setFixedWidth(32)
        btn_sd.setToolTip("Shift down by 1 row (wraps)")
        btn_sd.clicked.connect(lambda: self._action_shift(0, 1))
        tbar.addWidget(btn_sd)
        btn_sl = QPushButton("←")
        btn_sl.setFixedWidth(32)
        btn_sl.setToolTip("Shift left by 1 column (wraps)")
        btn_sl.clicked.connect(lambda: self._action_shift(-1, 0))
        tbar.addWidget(btn_sl)
        btn_sr = QPushButton("→")
        btn_sr.setFixedWidth(32)
        btn_sr.setToolTip("Shift right by 1 column (wraps)")
        btn_sr.clicked.connect(lambda: self._action_shift(1, 0))
        tbar.addWidget(btn_sr)
        tbar.addSpacing(8)
        btn_cp = QPushButton("Copy")
        btn_cp.setToolTip("Copy these 8 bytes to clipboard "
                            "(plain hex)")
        btn_cp.clicked.connect(self._action_copy)
        tbar.addWidget(btn_cp)
        btn_pst = QPushButton("Paste")
        btn_pst.setToolTip("Paste 8 bytes from clipboard (accepts "
                              "hex like '18 24 42 7E 42 42 42 00' or "
                              "'$18,$24,...' or '0x18 0x24 ...')")
        btn_pst.clicked.connect(self._action_paste)
        tbar.addWidget(btn_pst)
        tbar.addStretch(1)
        outer.addLayout(tbar)

        # Reihe 2: Clear/Fill/Invert/Revert + Apply/Cancel
        bar = QHBoxLayout()
        btn_clear = QPushButton("Clear all")
        btn_clear.setToolTip("Set all 64 pixels to BG")
        btn_clear.clicked.connect(self._action_clear)
        bar.addWidget(btn_clear)
        btn_fill = QPushButton("Fill all")
        btn_fill.setToolTip("Set all 64 pixels to FG")
        btn_fill.clicked.connect(self._action_fill)
        bar.addWidget(btn_fill)
        btn_invert = QPushButton("Invert")
        btn_invert.setToolTip("Flip every pixel")
        btn_invert.clicked.connect(self._action_invert)
        bar.addWidget(btn_invert)
        btn_revert = QPushButton("Revert")
        btn_revert.setToolTip("Restore original 8 bytes")
        btn_revert.clicked.connect(self._action_revert)
        bar.addWidget(btn_revert)
        bar.addStretch(1)
        btn_apply = QPushButton("Apply")
        btn_apply.setDefault(True)
        btn_apply.setStyleSheet("font-weight: bold;")
        btn_apply.clicked.connect(self._action_apply)
        bar.addWidget(btn_apply)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        bar.addWidget(btn_cancel)
        outer.addLayout(bar)

        self._render_all()

    # ---- Render ----

    def _current_bytes(self):
        """Pixel-Array zurueck zu 8 Bytes serialisieren."""
        out = bytearray(8)
        for row in range(8):
            b = 0
            for col in range(8):
                if self._pixels[row][col]:
                    b |= (0x80 >> col)
            out[row] = b
        return bytes(out)

    def _render_all(self):
        self._render_edit_grid()
        self._render_previews()
        self._render_hex()

    def _render_edit_grid(self):
        """Rendert das 8x8 Pixel-Grid mit Gridlines."""
        size = 8 * self.PIXEL_SIZE
        img = QImage(size, size, QImage.Format.Format_RGB888)
        fg_color = QColor(*self._fg)
        bg_color = QColor(*self._bg)
        grid_color = QColor(120, 120, 120)
        from PyQt6.QtGui import QPainter
        painter = QPainter(img)
        try:
            painter.fillRect(0, 0, size, size, bg_color)
            for row in range(8):
                for col in range(8):
                    if self._pixels[row][col]:
                        painter.fillRect(
                            col * self.PIXEL_SIZE,
                            row * self.PIXEL_SIZE,
                            self.PIXEL_SIZE, self.PIXEL_SIZE,
                            fg_color)
            # Gridlines
            painter.setPen(grid_color)
            for i in range(9):
                x = i * self.PIXEL_SIZE
                painter.drawLine(x, 0, x, size)
                painter.drawLine(0, x, size, x)
        finally:
            painter.end()
        self.lbl_edit.setPixmap(QPixmap.fromImage(img))

    def _render_previews(self):
        """1:1, 2x, 4x previews ohne Gridlines."""
        # 1:1 base
        base = QImage(8, 8, QImage.Format.Format_RGB888)
        fg_color = QColor(*self._fg).rgb()
        bg_color = QColor(*self._bg).rgb()
        for row in range(8):
            for col in range(8):
                base.setPixel(col, row,
                                fg_color if self._pixels[row][col]
                                else bg_color)
        self.lbl_prev_1x.setPixmap(QPixmap.fromImage(base))
        # 2x
        scaled2 = base.scaled(16, 16,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_prev_2x.setPixmap(QPixmap.fromImage(scaled2))
        # 4x
        scaled4 = base.scaled(32, 32,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_prev_4x.setPixmap(QPixmap.fromImage(scaled4))

    def _render_hex(self):
        cur = self._current_bytes()
        orig_hex = " ".join(f"{b:02X}" for b in self._original)
        new_hex = " ".join(f"{b:02X}" for b in cur)
        changed = (cur != self._original)
        flag = (" <b style='color:#A00'>(modified)</b>"
                  if changed else "")
        self.lbl_hex.setText(
            f"Original: {orig_hex}<br>Current:  {new_hex}{flag}")

    # ---- Maus / Pixel-Toggle ----

    def _grid_xy_to_cell(self, x, y):
        """Map Mausposition auf (row, col) im 8x8 Grid. None wenn
        ausserhalb."""
        if x < 0 or y < 0:
            return None
        col = x // self.PIXEL_SIZE
        row = y // self.PIXEL_SIZE
        if 0 <= col < 8 and 0 <= row < 8:
            return row, col
        return None

    def _on_grid_press(self, ev):
        pos = ev.position()
        cell = self._grid_xy_to_cell(int(pos.x()), int(pos.y()))
        if cell is None:
            return
        row, col = cell
        if ev.button() == Qt.MouseButton.RightButton:
            # Rechts loescht
            self._drag_mode = False
            self._pixels[row][col] = False
        elif ev.button() == Qt.MouseButton.LeftButton:
            # Toggle aktuelles Pixel, drag mode entspricht dem neuen
            # Zustand (so kann der User entweder zeichnen oder loeschen
            # wenn er ueber den Rand zieht)
            self._pixels[row][col] = not self._pixels[row][col]
            self._drag_mode = self._pixels[row][col]
        else:
            return
        self._render_all()

    def _on_grid_drag(self, ev):
        if self._drag_mode is None:
            return
        pos = ev.position()
        cell = self._grid_xy_to_cell(int(pos.x()), int(pos.y()))
        if cell is None:
            return
        row, col = cell
        if self._pixels[row][col] != self._drag_mode:
            self._pixels[row][col] = self._drag_mode
            self._render_all()

    def _on_grid_release(self, ev):
        self._drag_mode = None

    # ---- Actions ----

    def _action_clear(self):
        self._pixels = [[False] * 8 for _ in range(8)]
        self._render_all()

    def _action_fill(self):
        self._pixels = [[True] * 8 for _ in range(8)]
        self._render_all()

    def _action_invert(self):
        for row in range(8):
            for col in range(8):
                self._pixels[row][col] = not self._pixels[row][col]
        self._render_all()

    def _action_revert(self):
        self._pixels = [[bool(self._original[row] & (0x80 >> col))
                            for col in range(8)] for row in range(8)]
        self._render_all()

    def _action_mirror_h(self):
        """Horizontale Spiegelung - links/rechts tauschen."""
        for row in range(8):
            self._pixels[row].reverse()
        self._render_all()

    def _action_mirror_v(self):
        """Vertikale Spiegelung - oben/unten tauschen."""
        self._pixels.reverse()
        self._render_all()

    def _action_rotate_cw(self):
        """90° im Uhrzeigersinn rotieren.
        new[r][c] = old[7-c][r]"""
        old = self._pixels
        self._pixels = [
            [old[7 - col][row] for col in range(8)] for row in range(8)
        ]
        self._render_all()

    def _action_rotate_ccw(self):
        """90° gegen Uhrzeigersinn.
        new[r][c] = old[c][7-r]"""
        old = self._pixels
        self._pixels = [
            [old[col][7 - row] for col in range(8)] for row in range(8)
        ]
        self._render_all()

    def _action_rotate_180(self):
        """180° = Mirror H + Mirror V."""
        self._action_mirror_h()
        self._action_mirror_v()

    def _action_shift(self, dx, dy):
        """Shift mit Wrap-Around. dx>0 = rechts, dy>0 = unten."""
        if dx:
            for row in range(8):
                cur = self._pixels[row]
                self._pixels[row] = (cur[-dx % 8:] + cur[:-dx % 8])
        if dy:
            shift = -dy % 8
            self._pixels = (self._pixels[shift:]
                              + self._pixels[:shift])
        self._render_all()

    # ---- Copy/Paste ----

    def _action_copy(self):
        """Aktuelle 8 Bytes als Hex-String in die Clipboard kopieren."""
        from PyQt6.QtWidgets import QApplication
        cur = self._current_bytes()
        hex_str = " ".join(f"{b:02X}" for b in cur)
        QApplication.clipboard().setText(hex_str)

    def _action_paste(self):
        """Hex-Bytes aus der Clipboard einlesen. Akzeptiert verschiedene
        Formate: '18 24 42 ...', '$18 $24 ...', '0x18 0x24 ...',
        '18,24,42,...', '0x18 0x24 ...', '%n,%n,...' etc."""
        from PyQt6.QtWidgets import QApplication
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        # Parse: tokens separieren auf jedem nicht-hex Zeichen
        import re
        # Erst $..., 0x..., %... Praefixe entfernen
        cleaned = re.sub(r'[\$%]|0x', '', text, flags=re.IGNORECASE)
        # Tokens = Hex-Pairs (oder einstelliges Hex)
        tokens = re.findall(r'[0-9A-Fa-f]+', cleaned)
        if not tokens:
            QMessageBox.warning(self, "Paste",
                "Clipboard does not contain hex bytes.")
            return
        try:
            values = [int(t, 16) & 0xFF for t in tokens[:8]]
        except ValueError:
            QMessageBox.warning(self, "Paste",
                "Failed to parse hex values from clipboard.")
            return
        if len(values) < 8:
            # Padding mit 0
            values = values + [0] * (8 - len(values))
        # Pixel-Array updaten
        for row in range(8):
            b = values[row]
            for col in range(8):
                self._pixels[row][col] = bool(b & (0x80 >> col))
        self._render_all()

    def _action_apply(self):
        self.new_bytes = self._current_bytes()
        self.accept()


# -----------------------------------------------------------------
# CharsetViewer
# -----------------------------------------------------------------

class CharsetViewer(QDialog):
    """Zeigt einen C64-Charset (2048 Bytes = 256 Chars à 8x8 Pixel).

    Oberer Bereich: Grid mit allen 256 Zeichen (16x16, je 8x8 Pixel,
    skaliert auf 16x16 oder 32x32). Klick auf ein Zeichen zeigt
    seinen Hex-Code im Status.

    Unterer Bereich: Text-Input. Was hier eingegeben wird, wird mit
    dem Charset gerendert und angezeigt - so kann der User sofort
    sehen wie sein Demo-Scroller aussehen wuerde.
    """

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"C64 Charset: {os.path.basename(path)}")
        self.resize(700, 600)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "charset_viewer")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        load_addr, payload = _read_file(path)
        self._path = path
        self._payload_raw = payload   # Original ohne load-addr/strip
        self._load_addr = load_addr
        # Charsets sind exakt 2048 Bytes. Wenn die Datei groesser
        # ist (z.B. Charset + Tilemap kombiniert), nehmen wir nur
        # die ersten 2048. Wenn kleiner: zeigen was da ist.
        self._charset = bytearray(payload[:2048])
        if len(self._charset) < 2048:
            self._charset.extend(bytes(2048 - len(self._charset)))
        # Original-Bytes fuer "Revert all" + dirty-detect
        self._charset_original = bytes(self._charset)
        self._dirty = False

        # Undo/Redo history. Liste von bytes-snapshots, _hist_index
        # zeigt auf den aktuellen State. Index 0 = Original.
        self._history = [bytes(self._charset)]
        self._hist_index = 0
        self._hist_max = 100

        self._fg = (255, 255, 255)
        self._bg = (0, 0, 0)
        self._fg_index = 1
        self._bg_index = 6
        self._zoom = 3    # 1 pixel = 3 screen pixel im Grid
        self._selected_cell = None    # (row, col) im 16x16 Grid

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Info-Zeile (dynamisch wegen dirty-flag)
        self.lbl_info = QLabel()
        layout.addWidget(self.lbl_info)

        # Toolbar: Farben + Zoom
        bar = QHBoxLayout()
        bar.addWidget(QLabel("FG color:"))
        self.cmb_fg = self._make_color_combo(1)
        self.cmb_fg.currentIndexChanged.connect(self._on_fg_changed)
        bar.addWidget(self.cmb_fg)
        bar.addWidget(QLabel("BG color:"))
        self.cmb_bg = self._make_color_combo(6)
        self.cmb_bg.currentIndexChanged.connect(self._on_bg_changed)
        bar.addWidget(self.cmb_bg)
        bar.addWidget(QLabel("  Zoom:"))
        self.sp_zoom = QSpinBox()
        self.sp_zoom.setRange(1, 8)
        self.sp_zoom.setValue(self._zoom)
        self.sp_zoom.valueChanged.connect(self._on_zoom_changed)
        bar.addWidget(self.sp_zoom)
        bar.addStretch(1)
        self.btn_undo = QPushButton("⟲ Undo")
        self.btn_undo.setToolTip("Undo last change (Ctrl+Z)")
        self.btn_undo.clicked.connect(self._undo)
        self.btn_undo.setEnabled(False)
        bar.addWidget(self.btn_undo)
        self.btn_redo = QPushButton("⟳ Redo")
        self.btn_redo.setToolTip("Redo (Ctrl+Y)")
        self.btn_redo.clicked.connect(self._redo)
        self.btn_redo.setEnabled(False)
        bar.addWidget(self.btn_redo)
        btn_edit = QPushButton("Edit char...")
        btn_edit.setToolTip(
            "Edit the selected character (double-click on a cell to "
            "edit directly)")
        btn_edit.clicked.connect(self._on_edit_selected)
        bar.addWidget(btn_edit)
        btn_save_charset = QPushButton("Save charset...")
        btn_save_charset.setToolTip(
            "Save modified charset back to a binary file "
            "(.chr / .fnt / .64c / .bin). Preserves the original "
            "load address if the file had one.\n\n"
            "When overwriting an existing file, a .bak backup is "
            "created automatically.")
        btn_save_charset.clicked.connect(self._save_charset)
        bar.addWidget(btn_save_charset)
        btn_png2seq = QPushButton("PNG → seq...")
        btn_png2seq.setToolTip(
            "Import a PNG and convert it to a character sequence "
            "using this charset.\n\n"
            "The image is sliced into 8x8 blocks (line by line, "
            "left to right, top to bottom). For each block we find "
            "the closest matching character. Result is a hex/decimal "
            "sequence + a live preview.")
        btn_png2seq.clicked.connect(self._png_to_sequence)
        bar.addWidget(btn_png2seq)
        btn_save_png = QPushButton("Save as PNG...")
        btn_save_png.clicked.connect(self._save_png)
        bar.addWidget(btn_save_png)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        # Keyboard shortcuts: Ctrl+Z = Undo, Ctrl+Y / Ctrl+Shift+Z = Redo
        from PyQt6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self._undo)
        QShortcut(QKeySequence("Ctrl+Y"), self).activated.connect(self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(
            self._redo)

        # Grid-Bild
        grid_box = QGroupBox("Character set (256 chars, $00..$FF) - "
                                  "click selects, double-click edits")
        gb_l = QVBoxLayout(grid_box)
        self.lbl_grid = QLabel()
        self.lbl_grid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_grid.setStyleSheet("background-color: #222;")
        gb_l.addWidget(self.lbl_grid)

        self.lbl_grid_info = QLabel(
            "  Click a cell to see its hex code, double-click to "
            "edit it.  ")
        gb_l.addWidget(self.lbl_grid_info)
        # Klick auf das Grid-Label: select / show info
        self.lbl_grid.mousePressEvent = self._on_grid_click
        self.lbl_grid.mouseDoubleClickEvent = self._on_grid_doubleclick
        layout.addWidget(grid_box)

        # Text-Render Bereich
        txt_box = QGroupBox("Type text - rendered with this charset")
        tb_l = QVBoxLayout(txt_box)
        self.ed_text = QPlainTextEdit()
        self.ed_text.setMaximumHeight(70)
        self.ed_text.setPlainText("HELLO WORLD!\nTHIS IS QUOPUS.")
        self.ed_text.textChanged.connect(self._on_text_changed)
        tb_l.addWidget(self.ed_text)
        self.lbl_text_render = QLabel()
        self.lbl_text_render.setAlignment(Qt.AlignmentFlag.AlignLeft
                                            | Qt.AlignmentFlag.AlignTop)
        self.lbl_text_render.setStyleSheet("background-color: #222;")
        self.lbl_text_render.setMinimumHeight(80)
        tb_l.addWidget(self.lbl_text_render)
        layout.addWidget(txt_box)

        self._update_info()
        self._render_grid()
        self._render_text()

    def _update_info(self):
        info = (f"Size: {len(self._payload_raw)} bytes"
                  + (f"  ·  Load: ${self._load_addr:04X}"
                       if self._load_addr is not None else "")
                  + f"  ·  {min(256, len(self._payload_raw)//8)} chars")
        if self._dirty:
            info += "  ·  <b style='color:#A00'>modified</b>"
        self.lbl_info.setText(info)

    def _make_color_combo(self, default_idx):
        cmb = QComboBox()
        for i, (r, g, b) in enumerate(C64_PALETTE):
            # Color-Swatch als Icon + Name
            pm = QPixmap(20, 16)
            pm.fill(QColor(r, g, b))
            cmb.addItem(QIcon(pm), f"{i:2d}")
        cmb.setCurrentIndex(default_idx)
        cmb.setFixedWidth(70)
        return cmb

    def _on_fg_changed(self, idx):
        self._fg = C64_PALETTE[idx]
        self._fg_index = idx
        self._render_grid()
        self._render_text()

    def _on_bg_changed(self, idx):
        self._bg = C64_PALETTE[idx]
        self._bg_index = idx
        self._render_grid()
        self._render_text()

    def _on_zoom_changed(self, val):
        self._zoom = val
        self._render_grid()

    def _on_text_changed(self):
        self._render_text()

    def _render_grid(self):
        """Render die 256 Chars als 16x16 Grid mit optional einer
        gelben Markierung um die aktuell selectierte Zelle.

        Wichtig: Das Label bekommt exakt die Pixmap-Groesse, sonst
        wird das Pixmap durch AlignCenter zentriert und Klicks landen
        in falschen Zellen.
        """
        img = _qimage_from_charset(self._charset, self._fg, self._bg)
        # Skalieren mit Nearest-Neighbor damit's pixelig bleibt
        pm = QPixmap.fromImage(img).scaled(
            128 * self._zoom, 128 * self._zoom,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        # Selection-Highlight aufmalen
        if self._selected_cell is not None:
            from PyQt6.QtGui import QPainter, QPen
            painter = QPainter(pm)
            try:
                pen = QPen(QColor(255, 226, 138))   # gold
                pen.setWidth(2)
                painter.setPen(pen)
                cy, cx = self._selected_cell
                x = cx * 8 * self._zoom
                y = cy * 8 * self._zoom
                w = 8 * self._zoom
                painter.drawRect(x, y, w, w)
            finally:
                painter.end()
        self.lbl_grid.setPixmap(pm)
        # Label exakt auf Pixmap-Groesse: dann ist (0,0) im Klick == (0,0)
        # im Pixmap, kein Center-Offset
        self.lbl_grid.setFixedSize(pm.size())

    def _render_text(self):
        text = self.ed_text.toPlainText()
        if not text:
            self.lbl_text_render.clear()
            return
        img = _qimage_text_with_charset(
            text, self._charset, cols=40,
            palette_fg=self._fg, palette_bg=self._bg)
        # 2x Zoom damit's lesbar ist (Default-Charset ist 8x8)
        pm = QPixmap.fromImage(img).scaled(
            img.width() * 2, img.height() * 2,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_text_render.setPixmap(pm)

    def _click_to_cell(self, ev):
        """Map einen Maus-Click auf eine 16x16-Grid-Zelle.
        Returnt (cy, cx, char_idx) oder None falls ausserhalb."""
        pm = self.lbl_grid.pixmap()
        if pm is None or pm.isNull():
            return None
        # Pixmap-Groesse vs Label-Groesse: bei AlignCenter ist das
        # Pixmap zentriert. Wir muessen die Click-Position relativ zum
        # Pixmap-Origin berechnen.
        lbl_w = self.lbl_grid.width()
        lbl_h = self.lbl_grid.height()
        pm_w = pm.width()
        pm_h = pm.height()
        off_x = max(0, (lbl_w - pm_w) // 2)
        off_y = max(0, (lbl_h - pm_h) // 2)
        pos = ev.position()
        rel_x = pos.x() - off_x
        rel_y = pos.y() - off_y
        if rel_x < 0 or rel_y < 0 or rel_x >= pm_w or rel_y >= pm_h:
            return None
        # Zoom-Faktor aus pm_w / 128 ableiten (robuster als zu glauben
        # dass self._zoom aktuell stimmt)
        zoom = pm_w / 128.0
        x = int(rel_x / zoom)
        y = int(rel_y / zoom)
        if 0 <= x < 128 and 0 <= y < 128:
            cx = x // 8
            cy = y // 8
            return cy, cx, cy * 16 + cx
        return None

    def _on_grid_click(self, ev):
        """Klick auf das Grid-Label: ermittele welche Zelle und
        zeige die char ID + Hex-Bytes. Speichert die selection."""
        cell = self._click_to_cell(ev)
        if cell is None:
            return
        cy, cx, char_idx = cell
        self._selected_cell = (cy, cx)
        self._show_char_info(char_idx)
        self._render_grid()    # re-render fuer Selection-Highlight

    def _on_grid_doubleclick(self, ev):
        """Doppelklick: oeffne CharEditor fuer das Zeichen."""
        cell = self._click_to_cell(ev)
        if cell is None:
            return
        cy, cx, char_idx = cell
        self._selected_cell = (cy, cx)
        self._edit_char(char_idx)

    def _show_char_info(self, char_idx):
        char_bytes = self._charset[char_idx * 8:char_idx * 8 + 8]
        hex_str = " ".join(f"{b:02X}" for b in char_bytes)
        self.lbl_grid_info.setText(
            f"  Char #${char_idx:02X} ({char_idx})  ·  "
            f"bytes: {hex_str}  ")

    def _on_edit_selected(self):
        """Button 'Edit char...' - editiere die aktuelle Selection.
        Wenn keine, defaultet auf Char $00."""
        if self._selected_cell is None:
            char_idx = 0
        else:
            cy, cx = self._selected_cell
            char_idx = cy * 16 + cx
        self._edit_char(char_idx)

    def _edit_char(self, char_idx):
        """Oeffne CharEditorDialog fuer ein einzelnes Zeichen.
        Bei Apply schreibt der Editor die neuen 8 Bytes zurueck,
        wir re-rendern und pushen einen History-Snapshot."""
        char_bytes = bytes(
            self._charset[char_idx * 8:char_idx * 8 + 8])
        dlg = CharEditorDialog(
            char_bytes, char_idx, self._fg, self._bg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.new_bytes is None:
            return
        # Bytes zurueckschreiben
        for i in range(8):
            self._charset[char_idx * 8 + i] = dlg.new_bytes[i]
        # Nur wenn sich wirklich was geaendert hat, History-Snapshot
        if bytes(dlg.new_bytes) == char_bytes:
            return
        self._push_history()
        # Dirty flag setzen
        self._dirty = (
            bytes(self._charset) != self._charset_original)
        self._update_info()
        self._render_grid()
        self._render_text()
        self._show_char_info(char_idx)

    # ---- Undo / Redo ----

    def _push_history(self):
        """Aktuellen Charset-State auf den History-Stack pushen.
        Wenn _hist_index nicht am Ende ist (User hat undone und dann
        was neues editiert), discarden wir die Future-States."""
        # Truncate future
        self._history = self._history[:self._hist_index + 1]
        self._history.append(bytes(self._charset))
        self._hist_index = len(self._history) - 1
        # Cap history size
        if len(self._history) > self._hist_max:
            drop = len(self._history) - self._hist_max
            self._history = self._history[drop:]
            self._hist_index -= drop
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.btn_undo.setEnabled(self._hist_index > 0)
        self.btn_redo.setEnabled(
            self._hist_index < len(self._history) - 1)

    def _undo(self):
        if self._hist_index <= 0:
            return
        self._hist_index -= 1
        self._charset = bytearray(self._history[self._hist_index])
        self._dirty = (
            bytes(self._charset) != self._charset_original)
        self._update_info()
        self._render_grid()
        self._render_text()
        self._update_undo_buttons()
        # Aktualisiere char-info wenn ein cell selected ist
        if self._selected_cell is not None:
            cy, cx = self._selected_cell
            self._show_char_info(cy * 16 + cx)

    def _redo(self):
        if self._hist_index >= len(self._history) - 1:
            return
        self._hist_index += 1
        self._charset = bytearray(self._history[self._hist_index])
        self._dirty = (
            bytes(self._charset) != self._charset_original)
        self._update_info()
        self._render_grid()
        self._render_text()
        self._update_undo_buttons()
        if self._selected_cell is not None:
            cy, cx = self._selected_cell
            self._show_char_info(cy * 16 + cx)

    def _png_to_sequence(self):
        """Importiere ein PNG und konvertiere es zu einer Char-Sequenz
        mit dem aktuellen Charset.

        Workflow:
        - User waehlt ein PNG (jede Groesse - wird auf 8-aligned
          geclippt; max 320x200 wird beibehalten)
        - Threshold 128: heller = FG, dunkler = BG
        - Image in 8x8-Bloecke schneiden, line-by-line
        - Fuer jeden Block: Hamming-distance zu allen 256 Chars im
          aktuellen Charset, naechster Match gewinnt
        - Result wird in einem Dialog gezeigt: hex/dec sequence
          plus Live-Preview gerendert mit DIESEM charset
        """
        png_path, _ = QFileDialog.getOpenFileName(
            self, "Open PNG to convert",
            "", "PNG Images (*.png *.bmp *.jpg *.jpeg);;All Files (*)")
        if not png_path:
            return
        try:
            self._show_png_to_seq_dialog(png_path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "PNG to sequence",
                f"Failed to import:\n{e}")

    def _show_png_to_seq_dialog(self, png_path):
        """Modaler Dialog mit Result-Anzeige."""
        # Load image
        src = QImage(png_path)
        if src.isNull():
            QMessageBox.warning(self, "PNG to sequence",
                f"Could not load:\n{png_path}")
            return
        # Threshold + 8-align dann conversion
        dlg = PngToSequenceDialog(
            png_path, src, bytes(self._charset), self._fg, self._bg, self)
        dlg.exec()

    def _save_charset(self):
        """Speichert das aktuelle Charset (potentiell modifiziert) als
        Binaerdatei. Wenn die Original-Datei eine Load-Address hatte,
        wird sie wieder vorangestellt - so kann der User die geaenderte
        Datei direkt zurueck auf den C64 laden.

        BACKUP-LOGIK: wenn die Zieldatei schon existiert (also der User
        ueberschreibt), wird sie zuerst nach <name>.bak umkopiert.
        Existiert <name>.bak schon, wird <name>.bak.YYYYMMDD_HHMMSS
        genommen damit kein Backup verloren geht.
        """
        default_name = self._path
        out, _ = QFileDialog.getSaveFileName(
            self, "Save modified charset",
            default_name,
            "Charset (*.chr *.fnt *.64c *.bin);;All Files (*)")
        if not out:
            return
        # Backup wenn Zieldatei existiert
        backup_path = None
        if os.path.exists(out):
            backup_path = out + ".bak"
            if os.path.exists(backup_path):
                # mit Timestamp damit kein backup verloren geht
                import datetime
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = f"{out}.bak.{ts}"
            try:
                import shutil
                shutil.copy2(out, backup_path)
            except OSError as e:
                res = QMessageBox.question(
                    self, "Backup failed",
                    f"Could not create backup:\n{e}\n\n"
                    "Save anyway (without backup)?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if res != QMessageBox.StandardButton.Yes:
                    return
                backup_path = None

        data = bytearray()
        if self._load_addr is not None:
            data.append(self._load_addr & 0xFF)
            data.append((self._load_addr >> 8) & 0xFF)
        data.extend(self._charset)
        try:
            with open(out, 'wb') as f:
                f.write(data)
        except OSError as e:
            QMessageBox.warning(self, "Save charset",
                f"Failed to save:\n{e}")
            return
        msg = f"Saved {len(data)} bytes to:\n{out}"
        if backup_path:
            msg += f"\n\nBackup of previous version:\n{backup_path}"
        QMessageBox.information(self, "Save charset", msg)
        # Wenn wir die Originaldatei ueberschrieben haben, gilt
        # _charset_original jetzt als der neue on-disk State
        if out == self._path:
            self._charset_original = bytes(self._charset)
        self._dirty = False
        self._update_info()

    def closeEvent(self, ev):
        """Beim Schliessen mit unsaved changes warnen."""
        if self._dirty:
            res = QMessageBox.question(
                self, "Discard changes?",
                "The charset has unsaved changes. Discard them?",
                QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if res != QMessageBox.StandardButton.Discard:
                ev.ignore()
                return
        ev.accept()

    def _save_png(self):
        """Save the entire dialog content as a PNG: grid (4x zoom)
        plus the text render."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save charset preview as PNG",
            "charset.png", "PNG Images (*.png)")
        if not path:
            return
        # Wir speichern das Grid in 4x zoom als unabhaengiges PNG.
        # Den Text separat zu speichern ueberlassen wir dem User
        # (kann er via Screenshot machen).
        img = _qimage_from_charset(self._charset, self._fg, self._bg)
        scaled = img.scaled(
            128 * 4, 128 * 4,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        if scaled.save(path, "PNG"):
            self.lbl_grid_info.setText(f"  Saved: {path}  ")
        else:
            QMessageBox.warning(self, "Save PNG",
                                  f"Failed to save {path}")


# -----------------------------------------------------------------
# KoalaViewer - Multicolor Bitmap (160x200, 16 colors)
# -----------------------------------------------------------------

class KoalaViewer(QDialog):
    """Zeigt Koala Painter Bilder (.kla / .koa).

    Koala-Format (10003 bytes mit Load-Address $6000):
        $0000..$1F3F  bitmap (8000 bytes) - 320x200 pixel bitmap,
                       aber multicolor: 2 bits per pixel grouped in
                       8x8 blocks, also effektiv 160x200 logical
                       pixels mit 4 Farben pro 8x8 Zelle.
        $1F40..$2327  screen RAM (1000 bytes) - hi nibble + lo nibble
                       = color 01 + color 10 fuer jeden 8x8 Block
        $2328..$270F  color RAM (1000 bytes) - lo nibble = color 11
                       fuer jeden 8x8 Block
        $2710         background color (1 byte) - color 00

    Multicolor-Pixel-Decoding:
        Jedes Byte in der Bitmap kodiert 4 Pixel (jeweils 2 Bits).
        Bit-Paar Werte:
            00 = Background color (aus $2710)
            01 = Color aus Screen-RAM hi nibble fuer diesen Block
            10 = Color aus Screen-RAM lo nibble fuer diesen Block
            11 = Color aus Color-RAM lo nibble fuer diesen Block
        Im Bildschirm-Memory ist's "fat pixels" - 1 logischer Pixel
        = 2 horizontale Bildschirmpixel breit.
    """

    EXPECTED_SIZE = 10001    # nach Load-Addr-Strip

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"C64 Koala: {os.path.basename(path)}")
        self.resize(720, 600)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "koala_viewer")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        load_addr, payload = _read_file(path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        info_txt = (
            f"Size: {len(payload)} bytes"
            + (f"  ·  Load: ${load_addr:04X}"
                if load_addr is not None else "")
            + f"  ·  Expected: {self.EXPECTED_SIZE} bytes")
        layout.addWidget(QLabel(info_txt))

        # Padding/clamping damit die Decoder-Indizes nie out-of-range
        # rennen. Echte Koala-Dateien sind genau 10001 bytes - alles
        # andere ist entweder korrupt oder ein anderes Format das
        # zufaellig im Koala-Pfad gelandet ist.
        if len(payload) < self.EXPECTED_SIZE:
            payload = payload + bytes(
                self.EXPECTED_SIZE - len(payload))

        self._bitmap = payload[0x0000:0x1F40]
        self._screen = payload[0x1F40:0x2328]
        self._color  = payload[0x2328:0x2710]
        self._bg     = payload[0x2710] & 0x0F

        # Toolbar
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"Background: ${self._bg:X}"))
        bar.addStretch(1)
        btn_zoom_in = QPushButton("Zoom +")
        btn_zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom + 1))
        bar.addWidget(btn_zoom_in)
        btn_zoom_out = QPushButton("Zoom -")
        btn_zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom - 1))
        bar.addWidget(btn_zoom_out)
        btn_save = QPushButton("Save as PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        self._zoom = 2

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setStyleSheet("background-color: #222;")
        scroll.setWidget(self.lbl_img)
        layout.addWidget(scroll, 1)
        from .viewer_scroll import enable_key_scrolling
        enable_key_scrolling(self, self.scroll)

        # Decode + render
        self._image = self._decode_koala()
        self._render()

    def _decode_koala(self):
        """Decode Koala -> 320x200 QImage (multicolor: 1 logical
        pixel = 2 horizontal pixels).

        Wir rendern direkt in 320x200 statt 160x200 weil das die
        echte Bildschirmaufloesung des C64 ist - "fat pixels"
        bleiben so erhalten.
        """
        img = QImage(320, 200, QImage.Format.Format_RGB888)
        bg_rgb = QColor(*C64_PALETTE[self._bg]).rgb()
        img.fill(QColor(*C64_PALETTE[self._bg]))

        # Pre-compute color rgbs to avoid QColor allocations in inner loop
        color_rgb = [QColor(*C64_PALETTE[c]).rgb() for c in range(16)]

        # 8x8 blocks: 40 horizontal, 25 vertical
        for by in range(25):
            for bx in range(40):
                block_idx = by * 40 + bx
                screen_byte = self._screen[block_idx]
                color_byte = self._color[block_idx]
                c01 = (screen_byte >> 4) & 0x0F   # bit pair "01"
                c10 = screen_byte & 0x0F          # bit pair "10"
                c11 = color_byte & 0x0F           # bit pair "11"

                # 8 Zeilen pro Block
                for row in range(8):
                    byte_off = (bx * 8) + (by * 320) + row
                    if byte_off >= len(self._bitmap):
                        continue
                    b = self._bitmap[byte_off]
                    # 4 Pixelpaare im Byte (jew. 2 bits)
                    for px in range(4):
                        bits = (b >> (6 - px * 2)) & 0x03
                        if bits == 0b00:
                            rgb = color_rgb[self._bg]
                        elif bits == 0b01:
                            rgb = color_rgb[c01]
                        elif bits == 0b10:
                            rgb = color_rgb[c10]
                        else:
                            rgb = color_rgb[c11]
                        # fat pixel: 2 horizontal screen pixels
                        sx = bx * 8 + px * 2
                        sy = by * 8 + row
                        img.setPixel(sx, sy, rgb)
                        img.setPixel(sx + 1, sy, rgb)
        return img

    def _set_zoom(self, z):
        if z < 1 or z > 6:
            return
        self._zoom = z
        self._render()

    def _render(self):
        if self._image is None:
            return
        pm = QPixmap.fromImage(self._image).scaled(
            self._image.width() * self._zoom,
            self._image.height() * self._zoom,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_img.setPixmap(pm)
        self.lbl_img.setFixedSize(pm.size())

    def _save_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Koala as PNG",
            os.path.splitext(self.windowTitle())[0] + ".png",
            "PNG Images (*.png)")
        if not path:
            return
        if not self._image.save(path, "PNG"):
            QMessageBox.warning(self, "Save PNG",
                                  f"Failed to save {path}")


# -----------------------------------------------------------------
# HiresViewer - Standard Hi-Res Bitmap (320x200, 2 colors per 8x8)
# -----------------------------------------------------------------

class HiresViewer(QDialog):
    """Zeigt einen C64 Hi-Res Bitmap (8000+1000 bytes).

    Format:
        $0000..$1F3F   bitmap (8000 bytes) - 320x200 pixel
        $1F40..$2327   color memory (1000 bytes) - hi nibble = FG,
                        lo nibble = BG fuer jeden 8x8 Block

    Falls weniger als 9000 Bytes da sind: nehmen wir den Rest als
    Bitmap-only an und zeigen in white-on-black.
    """

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"C64 Hi-Res: {os.path.basename(path)}")
        self.resize(720, 600)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "hires_viewer")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        load_addr, payload = _read_file(path)
        # Hi-Res ist 8000 + 1000 = 9000 bytes. Wenn die Datei kleiner
        # ist, padden wir mit Nullen. Wenn sie groesser ist (z.B.
        # Art Studio mit Header), nehmen wir die ersten 9000.
        if len(payload) < 9000:
            payload = payload + bytes(9000 - len(payload))

        self._bitmap = payload[0:8000]
        self._color  = payload[8000:9000] if len(payload) >= 9000 \
            else None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        info_txt = (
            f"Size: {len(payload)} bytes"
            + (f"  ·  Load: ${load_addr:04X}"
                if load_addr is not None else "")
            + ("  ·  Color memory present"
                if self._color else
                "  ·  No color memory (white/black)"))
        layout.addWidget(QLabel(info_txt))

        bar = QHBoxLayout()
        bar.addStretch(1)
        btn_zoom_in = QPushButton("Zoom +")
        btn_zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom + 1))
        bar.addWidget(btn_zoom_in)
        btn_zoom_out = QPushButton("Zoom -")
        btn_zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom - 1))
        bar.addWidget(btn_zoom_out)
        btn_save = QPushButton("Save as PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        self._zoom = 2

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setStyleSheet("background-color: #222;")
        scroll.setWidget(self.lbl_img)
        layout.addWidget(scroll, 1)
        from .viewer_scroll import enable_key_scrolling
        enable_key_scrolling(self, self.scroll)

        self._image = self._decode_hires()
        self._render()

    def _decode_hires(self):
        img = QImage(320, 200, QImage.Format.Format_RGB888)
        color_rgb = [QColor(*C64_PALETTE[c]).rgb() for c in range(16)]

        for by in range(25):
            for bx in range(40):
                block_idx = by * 40 + bx
                if self._color:
                    cb = self._color[block_idx]
                    fg = (cb >> 4) & 0x0F
                    bg = cb & 0x0F
                else:
                    fg = 1   # white
                    bg = 0   # black
                # 8 rows per block
                for row in range(8):
                    byte_off = (bx * 8) + (by * 320) + row
                    if byte_off >= 8000:
                        continue
                    b = self._bitmap[byte_off]
                    for col in range(8):
                        rgb = color_rgb[fg] if (b & (0x80 >> col)) \
                            else color_rgb[bg]
                        img.setPixel(bx * 8 + col, by * 8 + row, rgb)
        return img

    def _set_zoom(self, z):
        if z < 1 or z > 6:
            return
        self._zoom = z
        self._render()

    def _render(self):
        pm = QPixmap.fromImage(self._image).scaled(
            self._image.width() * self._zoom,
            self._image.height() * self._zoom,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_img.setPixmap(pm)
        self.lbl_img.setFixedSize(pm.size())

    def _save_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Hi-Res as PNG",
            os.path.splitext(self.windowTitle())[0] + ".png",
            "PNG Images (*.png)")
        if not path:
            return
        if not self._image.save(path, "PNG"):
            QMessageBox.warning(self, "Save PNG",
                                  f"Failed to save {path}")


# -----------------------------------------------------------------
# Format auto-detect
# -----------------------------------------------------------------

def show_retro_gfx_viewer(path, parent=None):
    """Erkennt das Format anhand der Dateigroesse und ruft den
    richtigen Viewer auf.

    Heuristik:
        2048 bytes (raw)         -> Charset
        2050 bytes (mit load)    -> Charset im PRG-Format
        9000-9002 bytes          -> Hi-Res Bitmap
        10003 bytes              -> Koala (mit load addr $6000)
        andere -> Charset versuchen, sonst Fehlermeldung

    Returns True bei Erfolg, False wenn nicht erkannt.
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        QMessageBox.warning(parent, "C64 GFX",
                              f"Cannot read {path}:\n{e}")
        return False

    ext = os.path.splitext(path)[1].lower()

    # Explizite Extension-Bevorzugung
    if ext in (".kla", ".koa"):
        dlg = KoalaViewer(path, parent)
    elif ext in (".chr", ".fnt", ".64c"):
        dlg = CharsetViewer(path, parent)
    elif size in (10003, 10001):
        dlg = KoalaViewer(path, parent)
    elif size in (9000, 9002, 8002, 8000):
        dlg = HiresViewer(path, parent)
    elif size in (2048, 2050, 4096, 4098):
        # 4 KB Variante: 2 Charsets (Upper + Lower) - wir zeigen den
        # ersten an, User kann zum 2. mit Extension Tricks navigieren
        dlg = CharsetViewer(path, parent)
    else:
        # Fallback: nach Dateiname raten oder Auswahl anzeigen
        QMessageBox.information(
            parent, "C64 GFX",
            f"Cannot auto-detect format for {os.path.basename(path)} "
            f"(size: {size} bytes).\n\n"
            f"Expected sizes:\n"
            f"  Charset: 2048 / 2050 bytes\n"
            f"  Hi-Res:  9000 / 9002 bytes\n"
            f"  Koala:   10003 bytes\n\n"
            f"Trying charset viewer anyway...")
        dlg = CharsetViewer(path, parent)

    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return True


# -----------------------------------------------------------------
# Launcher dialog - standalone aufrufbar
# -----------------------------------------------------------------

class RetroGfxLauncherDialog(QDialog):
    """Standalone Launcher fuer den C64 GFX Viewer.

    Kleiner Dialog der **nicht** an eine konkrete Datei gebunden ist.
    User waehlt das Format (Charset / Koala / Hi-Res / Auto) und
    klickt "Open file..." - daraufhin wird per File-Dialog eine
    Datei gewaehlt und in einem SEPARATEN Viewer-Fenster geoeffnet.
    Der Launcher bleibt offen, mehrere Files koennen hintereinander
    geladen werden ohne den Launcher zu schliessen.

    Aufruf:
        - Action 'retrogfx_browser' (button/hotkey)
        - Action 'retrogfx' mit selektierter Datei -> oeffnet den
          passenden Viewer direkt (kein Launcher)

    History: zeigt die letzten geoeffneten Files damit man schnell
    zwischen ein paar Charsets wechseln kann ohne den File-Dialog
    jedes Mal neu zu navigieren.
    """

    # Globale History damit sie ueber Launcher-Open/Close ueberlebt.
    # Liste von (path, format)-Tupeln, max 10 Eintraege, neueste
    # zuerst.
    _history = []
    _last_dir = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Retro GFX Viewer")
        self.resize(520, 360)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "retro_gfx_launcher")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Info
        info = QLabel(
            "Retro Graphics Viewer - Commodore, Amiga, Atari, Apple, "
            "MSX, ZX Spectrum and many more.\n\n"
            "Auto-detect picks the right decoder by extension and "
            "file size. Native decoders handle the most common C64 "
            "formats (Koala, FLI, AFLI, Drazpaint, Doodle, Amica, "
            "Art Studio, etc).\n\n"
            "For all other retro formats (500+), install recoil2png "
            "from recoil.sourceforge.net - it is auto-detected.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: #444; font-size: {scaled_font_px(11)}px;")
        layout.addWidget(info)

        # Format-Auswahl
        fmt_box = QGroupBox("Format")
        fmt_l = QVBoxLayout(fmt_box)
        from .retro_gfx_decoders import get_decoder_list, RecoilBackend
        self.cmb_format = QComboBox()
        # Auto + Charset + alle Bitmap-Decoder + RECOIL (extern)
        self.cmb_format.addItem("Auto-detect (native + RECOIL fallback)", "auto")
        self.cmb_format.addItem("Charset (2 KB)", "charset")
        # RECOIL als generischer Fallback (alle 552 Formate)
        backend = RecoilBackend()
        if backend.available:
            self.cmb_format.addItem(
                f"RECOIL backend (any format, 552 supported)", "recoil")
        else:
            self.cmb_format.addItem(
                "RECOIL backend (not installed - download from recoil.sourceforge.net)",
                "recoil")
        # Separator (visuell)
        self.cmb_format.insertSeparator(self.cmb_format.count())
        for key, name in get_decoder_list():
            self.cmb_format.addItem(name, key)
        self.cmb_format.setStyleSheet("padding: 4px;")
        fmt_l.addWidget(self.cmb_format)
        # Hint
        hint = QLabel(
            "Auto-detect uses file size + extension. Pick a specific "
            "format to force interpretation.")
        hint.setStyleSheet(f"color: #666; font-size: {scaled_font_px(10)}px;")
        hint.setWordWrap(True)
        fmt_l.addWidget(hint)
        layout.addWidget(fmt_box)

        # Toolbar: Open + Configure
        bar = QHBoxLayout()
        self.btn_open = QPushButton("Open file...")
        self.btn_open.setStyleSheet(
            "QPushButton { padding: 6px 16px; font-weight: bold; }")
        self.btn_open.clicked.connect(self._on_open)
        bar.addWidget(self.btn_open)
        self.btn_open_folder = QPushButton("Open folder...")
        self.btn_open_folder.setStyleSheet(
            "QPushButton { padding: 6px 16px; }")
        self.btn_open_folder.setToolTip(
            "Open a folder and show the first viewable image.\n"
            "Then navigate with arrow keys:\n"
            "  ← / →    previous/next file\n"
            "  ↑ / ↓    -10 / +10\n"
            "  Home/End first / last file")
        self.btn_open_folder.clicked.connect(self._on_open_folder)
        bar.addWidget(self.btn_open_folder)
        bar.addStretch(1)
        btn_cfg = QPushButton("recoil2png path...")
        btn_cfg.clicked.connect(self._configure_recoil)
        bar.addWidget(btn_cfg)
        layout.addLayout(bar)

        # History list
        hist_box = QGroupBox("Recent files")
        hist_l = QVBoxLayout(hist_box)
        from PyQt6.QtWidgets import QListWidget
        self.lst_history = QListWidget()
        self.lst_history.setStyleSheet(
            "QListWidget { background-color: #fff; color: #000; }")
        self.lst_history.itemDoubleClicked.connect(self._on_history_open)
        hist_l.addWidget(self.lst_history)

        hbar = QHBoxLayout()
        btn_open_hist = QPushButton("Open selected")
        btn_open_hist.clicked.connect(self._on_history_open)
        hbar.addWidget(btn_open_hist)
        btn_clear_hist = QPushButton("Clear history")
        btn_clear_hist.clicked.connect(self._on_history_clear)
        hbar.addWidget(btn_clear_hist)
        hbar.addStretch(1)
        hist_l.addLayout(hbar)
        layout.addWidget(hist_box, 1)

        # Close
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        # Kein "stretchy" Layout - rechtsbuendig
        close_bar = QHBoxLayout()
        close_bar.addStretch(1)
        close_bar.addWidget(btn_close)
        layout.addLayout(close_bar)

        self._refresh_history()

    def _refresh_history(self):
        self.lst_history.clear()
        for path, fmt in self._history:
            base = os.path.basename(path)
            self.lst_history.addItem(f"[{fmt:7s}]  {base}  ({path})")

    def _selected_format(self):
        return self.cmb_format.currentData()

    def _build_recoil_filter(self):
        """Baut einen Wildcard-Filter aus allen RECOIL-Extensions plus
        unseren nativen. Bei 500+ Extensions ist der String lang, aber
        QFileDialog kommt damit klar."""
        from .retro_gfx_decoders import RECOIL_EXTENSIONS
        exts = sorted(RECOIL_EXTENSIONS | {
            'kla', 'koa', 'chr', 'fnt', '64c', 'bin', 'aas', 'art',
            'fli', 'afl', 'ifl', 'iph', 'ipt', 'drp', 'drz', 'drl',
            'dlp', 'ami', 'fun', 'fp2', 'hed', 'dd', 'jj', 'gun',
            'pi', 'ocp', 'cdu', 'vid', 'wig', 'a64', 'ims', 'ism',
            'ish', 'hlf', 'bfli', 'ffli',
        })
        wildcards = ' '.join(f"*.{e}" for e in exts)
        return f"All retro graphics ({wildcards});;All Files (*)"

    def _on_open(self):
        """File-Dialog mit Format-spezifischem Filter."""
        fmt = self._selected_format()
        all_gfx = self._build_recoil_filter()
        filters = {
            "auto":    all_gfx,
            "recoil":  all_gfx,
            "charset": "C64 Charset (*.chr *.fnt *.64c *.bin);;All Files (*)",
        }.get(fmt, all_gfx)
        start_dir = type(self)._last_dir or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open retro graphics file", start_dir, filters)
        if not path:
            return
        type(self)._last_dir = os.path.dirname(path)
        self._open_path(path, fmt)

    def _on_open_folder(self):
        """Folder-Dialog: zeigt das erste Bild im Ordner, dann
        Pfeiltasten-Navigation im Viewer."""
        start_dir = type(self)._last_dir or ""
        folder = QFileDialog.getExistingDirectory(
            self, "Open folder of retro graphics", start_dir)
        if not folder:
            return
        type(self)._last_dir = folder
        files = scan_folder_for_gfx(folder)
        if not files:
            QMessageBox.information(
                self, "Retro GFX",
                f"No viewable retro graphics files found in:\n{folder}")
            return
        # Erstes File oeffnen - Auto-detect
        self._open_path(files[0], self._selected_format())

    def _open_path(self, path, fmt):
        """Oeffne path im gewaehlten Format.

        fmt='auto':    Format-Detection. Erst native Decoder, dann
                       RECOIL falls verfuegbar und Extension passt.
        fmt='charset': CharsetViewer.
        fmt='recoil':  Erzwingt RECOIL-Backend.
        fmt=anything else: BitmapViewer mit dem entsprechenden Decoder.
        """
        try:
            if fmt == "charset":
                dlg = CharsetViewer(path, self)
            elif fmt == "recoil":
                # Erzwinge RECOIL
                dlg = self._open_via_recoil(path)
                if dlg is None:
                    return
            elif fmt == "auto":
                # 1) Native Decoder probieren
                from .retro_gfx_decoders import detect_format, can_recoil_handle
                key = detect_format(path)
                if key is not None:
                    dlg = BitmapViewer(path, key, self)
                    fmt = key   # fuer History
                else:
                    size = os.path.getsize(path)
                    if size in (2048, 2050, 4096, 4098):
                        dlg = CharsetViewer(path, self)
                        fmt = "charset"
                    elif can_recoil_handle(path):
                        # 2) RECOIL falls Extension passt
                        dlg = self._open_via_recoil(path)
                        if dlg is None:
                            return
                        fmt = "recoil"
                    else:
                        # 3) Letzter Versuch: Koala-Fallback
                        dlg = KoalaViewer(path, self)
                        fmt = "koala-fallback"
            else:
                # Spezifischer Decoder-Key
                dlg = BitmapViewer(path, fmt, self)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
            self._add_to_history(path, fmt)
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(
                self, "Retro GFX",
                f"Could not open as {fmt}:\n{e}")

    def _open_via_recoil(self, path):
        """Konvertiere via recoil2png und oeffne PNG im RecoilViewer.
        Returnt den Dialog oder None bei Fehler (zeigt dann auch eine
        Warnung an)."""
        from .retro_gfx_decoders import RecoilBackend
        # Pfad aus Config lesen wenn vorhanden
        exe_hint = None
        cfg = getattr(self.parent(), 'config', None) if self.parent() else None
        if cfg is None:
            # Suche im Top-Level-Widget
            w = self
            while w is not None:
                if hasattr(w, 'config'):
                    cfg = w.config
                    break
                w = w.parent()
        if cfg:
            exe_hint = cfg.get('recoil2png_path')
        backend = RecoilBackend(exe_hint)
        if not backend.available:
            QMessageBox.warning(
                self, "Retro GFX",
                "recoil2png is not installed or not in PATH.\n\n"
                "RECOIL is needed for 500+ retro file formats from "
                "Atari, Amiga, Apple, MSX, ZX Spectrum etc.\n\n"
                "Download from: https://recoil.sourceforge.net/\n"
                "Then set the path under Configure > Tools > "
                "'recoil2png path'.")
            return None
        try:
            png_path = backend.decode_to_png(path)
        except Exception as e:
            QMessageBox.warning(
                self, "Retro GFX",
                f"recoil2png could not decode this file:\n{e}")
            return None
        import os as _os
        ext = _os.path.splitext(path)[1].lower().lstrip('.').upper()
        return RecoilViewer(path, png_path, self,
                              format_name=f"RECOIL: .{ext}")

    def _add_to_history(self, path, fmt):
        cls = type(self)
        # Dedupe: gleiche Datei nur einmal in der History
        cls._history = [(p, f) for p, f in cls._history if p != path]
        cls._history.insert(0, (path, fmt))
        cls._history = cls._history[:10]
        self._refresh_history()

    def _on_history_open(self, *args):
        row = self.lst_history.currentRow()
        if row < 0 or row >= len(self._history):
            return
        path, fmt = self._history[row]
        if not os.path.isfile(path):
            QMessageBox.warning(
                self, "C64 GFX",
                f"File no longer exists:\n{path}")
            return
        self._open_path(path, fmt)

    def _on_history_clear(self):
        type(self)._history = []
        self._refresh_history()

    def _configure_recoil(self):
        """Konfiguriere den Pfad zu recoil2png.exe / recoil2png.

        Speichert in config['recoil2png_path']. Wenn das gesetzt ist,
        nutzt der RECOIL-Fallback diesen Pfad statt PATH-Suche.
        """
        from PyQt6.QtWidgets import QFileDialog, QInputDialog
        from .retro_gfx_decoders import RecoilBackend
        # Finde main_window mit config dict
        main = self
        while main is not None and not hasattr(main, 'config'):
            main = main.parent()
        if main is None or not hasattr(main, 'config'):
            QMessageBox.warning(self, "Retro GFX",
                "Cannot find main window config to save the path.")
            return
        cur = main.config.get('recoil2png_path', '')
        # Detect current status
        backend = RecoilBackend(cur)
        status_line = (
            f"Currently using: <b>{backend.executable_path}</b>"
            if backend.available
            else "<b>Not found.</b> Download recoil2png from "
                  "https://recoil.sourceforge.net and select it here.")
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Configure recoil2png")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"recoil2png enables decoding of 500+ retro graphics "
            f"formats from Atari, Amiga, Apple, MSX, ZX Spectrum and "
            f"many more.<br><br>{status_line}<br><br>"
            "<b>Easiest:</b> drop recoil2png.exe into the "
            "<tt>external/</tt> folder of your Quopus install. It "
            "will be found automatically without any path setting."
            "<br><br>"
            "Otherwise pick the executable, or leave empty to clear.")
        btn_browse = msg.addButton(
            "Browse...", QMessageBox.ButtonRole.AcceptRole)
        btn_clear = msg.addButton(
            "Clear", QMessageBox.ButtonRole.DestructiveRole)
        btn_cancel = msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is btn_cancel:
            return
        if clicked is btn_clear:
            main.config.pop('recoil2png_path', None)
        elif clicked is btn_browse:
            import os as _os
            sel, _ = QFileDialog.getOpenFileName(
                self, "Select recoil2png executable",
                _os.path.dirname(cur) if cur else "",
                "Executables (*.exe);;All files (*)")
            if not sel:
                return
            main.config['recoil2png_path'] = sel
        try:
            from .config import save_config
            save_config(main.config)
        except Exception:
            pass
        # Re-check verfuegbarkeit
        new = RecoilBackend(main.config.get('recoil2png_path'))
        if new.available:
            QMessageBox.information(
                self, "Retro GFX",
                f"recoil2png is now available:\n{new.executable_path}")
        else:
            QMessageBox.warning(
                self, "Retro GFX",
                "recoil2png is still not found.")
        # Combobox-Eintrag aktualisieren
        for i in range(self.cmb_format.count()):
            if self.cmb_format.itemData(i) == "recoil":
                label = ("RECOIL backend (any format, 552 supported)"
                          if new.available
                          else "RECOIL backend (not installed - download "
                               "from recoil.sourceforge.net)")
                self.cmb_format.setItemText(i, label)
                break


def show_retro_gfx_launcher(parent=None):
    """Open the standalone C64 graphics launcher. Returns the dialog
    instance for the caller to keep a reference (so it doesn't get
    garbage-collected while still visible)."""
    dlg = RetroGfxLauncherDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg


# -----------------------------------------------------------------
# Generic BitmapViewer - shows any decoded bitmap from the
# retro_gfx_decoders module.
# -----------------------------------------------------------------

class BitmapViewer(QDialog, FolderBrowserMixin):
    """Generischer Viewer fuer alle Bitmap-Formate aus
    retro_gfx_decoders. Bekommt nur einen Path und einen Decoder-Key
    (z.B. 'fli', 'ifli', 'drazpaint') und ruft den entsprechenden
    Decoder auf.

    Features:
        - Zoom +/-
        - Save as PNG
        - Bei Interlace-Formaten (mit pixels_a/pixels_b im decoder-
          Output): Toggle zwischen "Both" (Blend), "Frame A", "Frame B"
        - Folder-Navigation per Pfeiltasten (siehe FolderBrowserMixin)
    """

    def __init__(self, path, decoder_key, parent=None):
        super().__init__(parent)
        from .retro_gfx_decoders import decode_by_key

        self._decoder_key = decoder_key
        try:
            result, name = decode_by_key(path, decoder_key)
        except Exception as e:
            raise

        self._result = result
        self._format_name = name
        self._path = path
        self.setWindowTitle(
            f"Retro GFX {name}: {os.path.basename(path)}")
        self.resize(800, 660)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "bitmap_viewer")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Info-Zeile - dynamisch aktualisiert beim reload
        self.lbl_info = QLabel()
        self.lbl_info.setStyleSheet("padding: 4px; background: #f0f0f0;")
        layout.addWidget(self.lbl_info)

        # Toolbar
        bar = QHBoxLayout()
        self._zoom = 2
        btn_prev = QPushButton("◀")
        btn_prev.setFixedWidth(36)
        btn_prev.setToolTip("Previous file (←)")
        btn_prev.clicked.connect(lambda: self._nav_relative(-1))
        bar.addWidget(btn_prev)
        btn_next = QPushButton("▶")
        btn_next.setFixedWidth(36)
        btn_next.setToolTip("Next file (→)")
        btn_next.clicked.connect(lambda: self._nav_relative(1))
        bar.addWidget(btn_next)
        btn_zoom_in = QPushButton("Zoom +")
        btn_zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom + 1))
        bar.addWidget(btn_zoom_in)
        btn_zoom_out = QPushButton("Zoom -")
        btn_zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom - 1))
        bar.addWidget(btn_zoom_out)

        # Interlace-Toggle wenn pixels_a/pixels_b vorhanden
        self._has_frames = ('pixels_a' in result and 'pixels_b' in result)
        self.cmb_view = QComboBox()
        self.cmb_view.addItem("Blended (both frames)", "blend")
        self.cmb_view.addItem("Frame A only", "a")
        self.cmb_view.addItem("Frame B only", "b")
        self.cmb_view.currentIndexChanged.connect(self._render)
        self.cmb_view.setVisible(self._has_frames)
        bar.addWidget(QLabel("  View:"))
        bar.addWidget(self.cmb_view)

        bar.addStretch(1)
        btn_save = QPushButton("Save as PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        # Scroll area mit Bild
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setStyleSheet("background-color: #222;")
        scroll.setWidget(self.lbl_img)
        layout.addWidget(scroll, 1)
        from .viewer_scroll import enable_key_scrolling
        enable_key_scrolling(self, self.scroll)

        # Status bar mit Folder-Nav-Info
        self.lbl_status = QLabel()
        self.lbl_status.setStyleSheet(
            "padding: 2px 6px; background: #e8e8e8; "
            f"color: #444; font-size: {scaled_font_px(10)}px;")
        layout.addWidget(self.lbl_status)

        # Folder browser einrichten
        self.init_folder_browser(path)
        self._update_info()
        self._render()

    def reload_path(self, new_path):
        """Lade ein neues File ins gleiche Viewer-Fenster."""
        from .retro_gfx_decoders import (
            decode_by_key, detect_format, can_recoil_handle,
        )
        # Detect erneut - vielleicht ein anderes Format
        key = detect_format(new_path) or self._decoder_key
        try:
            result, name = decode_by_key(new_path, key)
        except Exception:
            # Wenn der detected key fehlschlaegt, versuche den alten
            result, name = decode_by_key(new_path, self._decoder_key)
            key = self._decoder_key
        self._result = result
        self._format_name = name
        self._path = new_path
        self._decoder_key = key
        self._has_frames = (
            'pixels_a' in result and 'pixels_b' in result)
        self.cmb_view.setVisible(self._has_frames)
        self.setWindowTitle(
            f"Retro GFX {name}: {os.path.basename(new_path)}")
        self._update_info()
        self._render()

    def _update_info(self):
        result = self._result
        size = os.path.getsize(self._path)
        info_text = (
            f"<b>{self._format_name}</b>  ·  Mode: {result['mode']}  ·  "
            f"{result['width']}x{result['height']}  ·  "
            f"File size: {size:,} bytes")
        if result.get('note'):
            info_text += f"<br>{result['note']}"
        self.lbl_info.setText(info_text)
        self.lbl_status.setText(self.folder_status_text())

    def keyPressEvent(self, event):
        if self.handle_arrow_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def _current_pixels(self):
        """Returnt das aktuelle pixel-Array basierend auf der View-
        Selection (Frame A, B oder Blend)."""
        if not self._has_frames:
            return self._result['pixels']
        view = self.cmb_view.currentData()
        if view == "a":
            return self._result['pixels_a']
        if view == "b":
            return self._result['pixels_b']
        return self._result['pixels']

    def _render(self):
        """Pixel-Array -> QImage -> Pixmap im Label.

        Wir nutzen QImage.Format_Indexed8 mit einer 16-Farb-Palette -
        das ist deutlich schneller als 64000 setPixel-Calls. QImage
        kopiert das bytearray direkt als raw scanlines.
        """
        from .retro_gfx_decoders import C64_PALETTE
        pixels = self._current_pixels()
        w = self._result['width']
        h = self._result['height']

        # QImage braucht eine ColorTable mit ARGB-Werten
        ct = [(0xFF << 24) | (r << 16) | (g << 8) | b
                for r, g, b in C64_PALETTE]
        # Padded scanlines: QImage erwartet 4-byte aligned scanlines.
        # Bei width=320 ist das schon aligned. Bei anderen Breiten
        # muessen wir padden - hier aber alle Formate 320 wide.
        img = QImage(bytes(pixels), w, h, w,
                       QImage.Format.Format_Indexed8)
        img.setColorTable(ct)
        # Convert zu RGB888 damit Pixmap-Scaling sauber funktioniert
        img = img.convertToFormat(QImage.Format.Format_RGB888)
        self._image = img
        pm = QPixmap.fromImage(img).scaled(
            w * self._zoom, h * self._zoom,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_img.setPixmap(pm)
        self.lbl_img.setFixedSize(pm.size())

    def _set_zoom(self, z):
        if z < 1 or z > 6:
            return
        self._zoom = z
        self._render()

    def _save_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as PNG",
            os.path.splitext(self._path)[0] + ".png",
            "PNG Images (*.png)")
        if not path:
            return
        if not self._image.save(path, "PNG"):
            QMessageBox.warning(self, "Save PNG",
                                  f"Failed to save {path}")


# -----------------------------------------------------------------
# RecoilViewer - zeigt PNG das von recoil2png erzeugt wurde
# -----------------------------------------------------------------

class RecoilViewer(QDialog, FolderBrowserMixin):
    """Generischer Viewer der ein PNG anzeigt das von recoil2png
    erzeugt wurde. Wird verwendet wenn:
    - der Native-Decoder kein Bild liefert (z.B. Atari/Amiga/MSX/etc)
    - der User explizit RECOIL forcen will
    Mit Folder-Navigation per Pfeiltasten.
    """

    def __init__(self, input_path, png_path, parent=None, format_name=None):
        super().__init__(parent)
        self._input_path = input_path
        self._png_path = png_path
        self._format_name = format_name or "RECOIL"
        self.setWindowTitle(
            f"{self._format_name}: {os.path.basename(input_path)}")
        self.resize(800, 660)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "recoil_viewer")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Info-Zeile - dynamisch
        self.lbl_info = QLabel()
        self.lbl_info.setStyleSheet("padding: 4px; background: #f0f0f0;")
        layout.addWidget(self.lbl_info)

        # Toolbar mit Prev/Next Buttons
        bar = QHBoxLayout()
        self._zoom = 2
        btn_prev = QPushButton("◀")
        btn_prev.setFixedWidth(36)
        btn_prev.setToolTip("Previous file (←)")
        btn_prev.clicked.connect(lambda: self._nav_relative(-1))
        bar.addWidget(btn_prev)
        btn_next = QPushButton("▶")
        btn_next.setFixedWidth(36)
        btn_next.setToolTip("Next file (→)")
        btn_next.clicked.connect(lambda: self._nav_relative(1))
        bar.addWidget(btn_next)
        btn_in = QPushButton("Zoom +")
        btn_in.clicked.connect(lambda: self._set_zoom(self._zoom + 1))
        bar.addWidget(btn_in)
        btn_out = QPushButton("Zoom -")
        btn_out.clicked.connect(lambda: self._set_zoom(self._zoom - 1))
        bar.addWidget(btn_out)
        btn_fit = QPushButton("Fit 1:1")
        btn_fit.clicked.connect(lambda: self._set_zoom(1))
        bar.addWidget(btn_fit)
        bar.addStretch(1)
        btn_save = QPushButton("Save PNG...")
        btn_save.clicked.connect(self._save_png)
        bar.addWidget(btn_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        # Image area
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setStyleSheet("background-color: #222;")
        scroll.setWidget(self.lbl_img)
        layout.addWidget(scroll, 1)
        from .viewer_scroll import enable_key_scrolling
        enable_key_scrolling(self, self.scroll)

        # Status bar mit Folder-Nav-Info
        self.lbl_status = QLabel()
        self.lbl_status.setStyleSheet(
            "padding: 2px 6px; background: #e8e8e8; "
            f"color: #444; font-size: {scaled_font_px(10)}px;")
        layout.addWidget(self.lbl_status)

        # Folder browser einrichten
        self.init_folder_browser(input_path)

        self._image = QImage(png_path)
        self._update_info()
        self._render()

    def reload_path(self, new_path):
        """Konvertiere new_path via recoil2png und lade das PNG."""
        from .retro_gfx_decoders import RecoilBackend
        # Backend per Config-Pfad oder Default-Lookup
        cfg = None
        w = self
        while w is not None:
            if hasattr(w, 'config'):
                cfg = w.config
                break
            w = w.parent()
        exe_hint = cfg.get('recoil2png_path') if cfg else None
        backend = RecoilBackend(exe_hint)
        if not backend.available:
            raise RuntimeError("recoil2png not available")
        png_path = backend.decode_to_png(new_path)
        self._input_path = new_path
        self._png_path = png_path
        ext = os.path.splitext(new_path)[1].lower().lstrip('.').upper()
        self._format_name = f"RECOIL: .{ext}"
        self.setWindowTitle(
            f"{self._format_name}: {os.path.basename(new_path)}")
        self._image = QImage(png_path)
        self._update_info()
        self._render()

    def _update_info(self):
        from PyQt6.QtGui import QImageReader
        size = os.path.getsize(self._input_path)
        reader = QImageReader(self._png_path)
        psize = reader.size()
        info_text = (
            f"<b>{self._format_name}</b>  ·  "
            f"Output: {psize.width()}x{psize.height()}  ·  "
            f"Source: {size:,} bytes (decoded via recoil2png)")
        self.lbl_info.setText(info_text)
        self.lbl_status.setText(self.folder_status_text())

    def keyPressEvent(self, event):
        if self.handle_arrow_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def _render(self):
        if self._image.isNull():
            self.lbl_img.setText("(failed to load PNG)")
            return
        pm = QPixmap.fromImage(self._image)
        w = pm.width() * self._zoom
        h = pm.height() * self._zoom
        scaled = pm.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_img.setPixmap(scaled)
        self.lbl_img.setFixedSize(scaled.size())

    def _set_zoom(self, z):
        if z < 1 or z > 6:
            return
        self._zoom = z
        self._render()

    def _save_png(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as PNG",
            os.path.splitext(self._input_path)[0] + ".png",
            "PNG Images (*.png)")
        if not path:
            return
        if not self._image.save(path, "PNG"):
            QMessageBox.warning(self, "Save PNG",
                                  f"Failed to save {path}")


# -----------------------------------------------------------------
# PngToSequenceDialog - PNG -> Char-Sequence converter
# -----------------------------------------------------------------

class PngToSequenceDialog(QDialog):
    """Konvertiert ein importiertes PNG in eine Char-Sequenz unter
    Verwendung des aktuellen Charsets.

    UI:
    - Top: Source-PNG (geclippt auf 8-aligned, optional skaliert)
    - Middle: Konvertierungsoptionen (Threshold, Invert, Max-Width)
    - Bottom: Output-Sequence (Hex + Dec + Live-Preview)
    - Save as .seq Button
    """

    def __init__(self, png_path, src_image, charset_bytes,
                  fg, bg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            f"PNG -> Char Sequence: {os.path.basename(png_path)}")
        self.resize(900, 720)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "png_to_seq")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint)

        self._png_path = png_path
        self._src = src_image
        self._charset = bytes(charset_bytes)
        self._fg = fg
        self._bg = bg
        self._threshold = 128
        self._invert = False
        self._max_width = 320    # default C64 screen width
        self._max_height = 200
        self._sequence = []
        self._grid_cols = 0
        self._grid_rows = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Header
        sw, sh = src_image.width(), src_image.height()
        info = QLabel(
            f"<b>Source:</b> {os.path.basename(png_path)} ({sw}x{sh}). "
            "Threshold converts to monochrome, then sliced into 8x8 "
            "blocks. Each block is matched to the closest character "
            "in the current charset.")
        info.setWordWrap(True)
        info.setStyleSheet("padding: 4px; background: #f0f0f0;")
        outer.addWidget(info)

        # Conversion options
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Threshold:"))
        self.sp_thresh = QSpinBox()
        self.sp_thresh.setRange(1, 255)
        self.sp_thresh.setValue(128)
        self.sp_thresh.setSingleStep(8)
        self.sp_thresh.valueChanged.connect(self._on_options_changed)
        opts.addWidget(self.sp_thresh)
        opts.addSpacing(12)
        from PyQt6.QtWidgets import QCheckBox
        self.cb_invert = QCheckBox("Invert (swap FG/BG)")
        self.cb_invert.toggled.connect(self._on_options_changed)
        opts.addWidget(self.cb_invert)
        opts.addSpacing(12)
        opts.addWidget(QLabel("Max width (px):"))
        self.sp_maxw = QSpinBox()
        self.sp_maxw.setRange(8, 640)
        self.sp_maxw.setSingleStep(8)
        self.sp_maxw.setValue(320)
        self.sp_maxw.valueChanged.connect(self._on_options_changed)
        opts.addWidget(self.sp_maxw)
        opts.addWidget(QLabel("Max height (px):"))
        self.sp_maxh = QSpinBox()
        self.sp_maxh.setRange(8, 480)
        self.sp_maxh.setSingleStep(8)
        self.sp_maxh.setValue(200)
        self.sp_maxh.valueChanged.connect(self._on_options_changed)
        opts.addWidget(self.sp_maxh)
        opts.addStretch(1)
        outer.addLayout(opts)

        # Side by side: source threshold preview + rendered output
        body = QHBoxLayout()

        src_box = QGroupBox("Source (thresholded)")
        sb_l = QVBoxLayout(src_box)
        self.lbl_src = QLabel()
        self.lbl_src.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_src.setStyleSheet("background-color: #222;")
        sb_l.addWidget(self.lbl_src)
        body.addWidget(src_box, 1)

        out_box = QGroupBox("Rendered via current charset")
        ob_l = QVBoxLayout(out_box)
        self.lbl_out = QLabel()
        self.lbl_out.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_out.setStyleSheet("background-color: #222;")
        ob_l.addWidget(self.lbl_out)
        body.addWidget(out_box, 1)

        outer.addLayout(body, 1)

        # Output text area
        self.lbl_dim = QLabel()
        outer.addWidget(self.lbl_dim)

        out_text_box = QGroupBox("Character sequence (line by line)")
        otb_l = QVBoxLayout(out_text_box)
        self.ed_hex = QPlainTextEdit()
        self.ed_hex.setMaximumHeight(120)
        self.ed_hex.setStyleSheet(
            "QPlainTextEdit { font-family: 'Consolas', "
            f"'Courier New', monospace; font-size: {scaled_font_px(11)}px; }}")
        self.ed_hex.setReadOnly(True)
        otb_l.addWidget(self.ed_hex)
        # Format selector for the text output
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format:"))
        self.cmb_fmt = QComboBox()
        self.cmb_fmt.addItems([
            "Hex (00 01 02 ...)",
            "Hex with $ ($00,$01,$02,...)",
            "Decimal (0,1,2,...)",
            "C/asm bytes (.byte $00, $01, ...)",
            "BASIC DATA (DATA 0,1,2,...)",
            "Raw bytes (binary)",
        ])
        self.cmb_fmt.currentIndexChanged.connect(self._refresh_text)
        fmt_row.addWidget(self.cmb_fmt)
        fmt_row.addStretch(1)
        btn_copy = QPushButton("Copy to clipboard")
        btn_copy.clicked.connect(self._copy_text)
        fmt_row.addWidget(btn_copy)
        otb_l.addLayout(fmt_row)
        outer.addWidget(out_text_box)

        # Action bar
        bar = QHBoxLayout()
        btn_save_seq = QPushButton("Save .seq...")
        btn_save_seq.setToolTip(
            "Save the raw byte sequence (one byte per char) to a "
            "file. Useful for direct loading into a C64 program.")
        btn_save_seq.clicked.connect(self._save_seq)
        bar.addWidget(btn_save_seq)
        btn_save_text = QPushButton("Save text...")
        btn_save_text.setToolTip(
            "Save the formatted text (hex, asm, BASIC, etc.) to a "
            ".txt / .asm / .bas file.")
        btn_save_text.clicked.connect(self._save_text)
        bar.addWidget(btn_save_text)
        btn_save_png = QPushButton("Save preview PNG...")
        btn_save_png.clicked.connect(self._save_preview_png)
        bar.addWidget(btn_save_png)
        bar.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)
        outer.addLayout(bar)

        # Initial conversion
        self._convert()

    # ----- Konversion -----

    def _on_options_changed(self):
        self._threshold = self.sp_thresh.value()
        self._invert = self.cb_invert.isChecked()
        self._max_width = self.sp_maxw.value() // 8 * 8
        self._max_height = self.sp_maxh.value() // 8 * 8
        self._convert()

    def _convert(self):
        """Vollstaendige Konversion: PNG -> threshold -> blocks ->
        char-match -> sequence. Aktualisiert beide Previews und den
        Output-Text."""
        # 1. Source skalieren auf 8-aligned, max_width x max_height
        src = self._src
        w0, h0 = src.width(), src.height()
        # Skalieren auf max_width/max_height beibehalten von aspect ratio
        scale = min(self._max_width / w0, self._max_height / h0, 10.0)
        tw = max(8, int(w0 * scale) // 8 * 8)
        th = max(8, int(h0 * scale) // 8 * 8)
        scaled = src.scaled(
            tw, th,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        # 2. Grayscale + Threshold -> bitmap
        gray = scaled.convertToFormat(QImage.Format.Format_Grayscale8)
        bitmap = bytearray(tw * th)
        for y in range(th):
            sl = gray.scanLine(y).asarray(tw)
            line_bytes = bytes(sl)
            for x in range(tw):
                v = line_bytes[x] >= self._threshold
                if self._invert:
                    v = not v
                bitmap[y * tw + x] = 1 if v else 0
        # 3. In 8x8 Bloecke schneiden + Char-Match
        cols = tw // 8
        rows = th // 8
        self._grid_cols = cols
        self._grid_rows = rows
        sequence = []
        for by in range(rows):
            for bx in range(cols):
                block = self._extract_block(bitmap, tw, bx, by)
                idx = self._find_best_char(block)
                sequence.append(idx)
        self._sequence = sequence
        # 4. Source-Preview (Threshold-Bild als 1bpp Bild rendern)
        src_img = QImage(tw, th, QImage.Format.Format_Grayscale8)
        src_img.fill(0)
        for y in range(th):
            sl = src_img.scanLine(y).asarray(tw)
            for x in range(tw):
                sl[x] = 255 if bitmap[y * tw + x] else 0
        # 2x zoom damit's lesbar ist
        pm_src = QPixmap.fromImage(src_img).scaled(
            tw * 2, th * 2,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_src.setPixmap(pm_src)
        # 5. Render-Preview (rendere die Sequenz mit dem aktuellen Charset)
        rendered = self._render_sequence(sequence, cols, rows)
        # In Farbe (FG/BG) konvertieren
        from PyQt6.QtGui import QColor
        fg_rgb = QColor(*self._fg).rgb()
        bg_rgb = QColor(*self._bg).rgb()
        ct = [bg_rgb | 0xFF000000, fg_rgb | 0xFF000000]
        out_img = QImage(
            bytes(rendered), cols * 8, rows * 8, cols * 8,
            QImage.Format.Format_Indexed8)
        out_img.setColorTable(ct)
        out_img = out_img.convertToFormat(QImage.Format.Format_RGB888)
        pm_out = QPixmap.fromImage(out_img).scaled(
            cols * 16, rows * 16,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.lbl_out.setPixmap(pm_out)
        self._preview_png = out_img    # gemerkt fuer Save-Button
        # 6. Info-Zeile
        self.lbl_dim.setText(
            f"Grid: {cols} cols x {rows} rows = "
            f"<b>{len(sequence)} characters</b>  "
            f"(target size {tw}x{th} pixels)")
        # 7. Output text aktualisieren
        self._refresh_text()

    def _extract_block(self, bitmap, w, bx, by):
        """8 Bytes fuer den 8x8 Block bei (bx,by)."""
        block = bytearray(8)
        for row in range(8):
            b = 0
            for col in range(8):
                if bitmap[(by * 8 + row) * w + (bx * 8 + col)]:
                    b |= (0x80 >> col)
            block[row] = b
        return bytes(block)

    def _find_best_char(self, target):
        """Minimum Hamming distance gegen alle 256 Chars im Charset."""
        best = 0
        best_dist = 65
        for idx in range(256):
            char = self._charset[idx * 8:idx * 8 + 8]
            if len(char) < 8:
                continue
            d = 0
            for i in range(8):
                d += bin(target[i] ^ char[i]).count('1')
                if d >= best_dist:
                    break
            if d < best_dist:
                best_dist = d
                best = idx
                if d == 0:
                    return best
        return best

    def _render_sequence(self, sequence, cols, rows):
        """Rendere die Sequenz mit dem aktuellen Charset als Indexed8
        bitmap (1 byte per pixel, 0=BG 1=FG)."""
        out = bytearray(cols * 8 * rows * 8)
        for i, char_idx in enumerate(sequence):
            bx = i % cols
            by = i // cols
            char = self._charset[char_idx * 8:char_idx * 8 + 8]
            if len(char) < 8:
                continue
            for row in range(8):
                b = char[row]
                row_base = (by * 8 + row) * (cols * 8) + bx * 8
                for col in range(8):
                    out[row_base + col] = 1 if (b & (0x80 >> col)) else 0
        return out

    # ----- Text-Output -----

    def _refresh_text(self):
        idx = self.cmb_fmt.currentIndex()
        cols = self._grid_cols
        seq = self._sequence
        lines = []
        if idx == 0:
            # Hex 00 01 02
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                lines.append(" ".join(f"{b:02X}" for b in row))
        elif idx == 1:
            # $00,$01,$02
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                lines.append(",".join(f"${b:02X}" for b in row))
        elif idx == 2:
            # Decimal 0,1,2
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                lines.append(",".join(str(b) for b in row))
        elif idx == 3:
            # ASM .byte $00,$01
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                lines.append("    .byte " + ",".join(
                    f"${b:02X}" for b in row))
        elif idx == 4:
            # BASIC DATA - C64 BASIC line numbers ab 1000, step 10
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                ln = 1000 + r * 10
                lines.append(f"{ln} DATA " + ",".join(
                    str(b) for b in row))
        elif idx == 5:
            # Raw bytes als space-separated hex preview
            lines.append("(raw bytes - use 'Save .seq' to write binary)")
            for r in range(self._grid_rows):
                row = seq[r * cols:(r + 1) * cols]
                lines.append(" ".join(f"{b:02X}" for b in row))
        self.ed_hex.setPlainText("\n".join(lines))

    def _copy_text(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.ed_hex.toPlainText())

    # ----- Saves -----

    def _save_seq(self):
        """Speichert die Sequenz als rohe Byte-Datei (1 Byte pro
        Char). Default-Extension .seq."""
        default = os.path.splitext(self._png_path)[0] + ".seq"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save sequence as raw bytes", default,
            "Sequence (*.seq *.bin);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'wb') as f:
                f.write(bytes(self._sequence))
        except OSError as e:
            QMessageBox.warning(self, "Save sequence",
                f"Failed to save:\n{e}")
            return
        QMessageBox.information(self, "Save sequence",
            f"Saved {len(self._sequence)} bytes to:\n{path}\n\n"
            f"Grid: {self._grid_cols}x{self._grid_rows}")

    def _save_text(self):
        """Speichert den formatierten Text wie in der Combobox
        ausgewaehlt."""
        idx = self.cmb_fmt.currentIndex()
        ext_map = {0: ".txt", 1: ".txt", 2: ".txt", 3: ".asm",
                     4: ".bas", 5: ".txt"}
        default = os.path.splitext(self._png_path)[0] + ext_map.get(idx, ".txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save formatted text", default,
            "Text files (*.txt *.asm *.bas *.s);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.ed_hex.toPlainText())
                f.write("\n")
        except OSError as e:
            QMessageBox.warning(self, "Save text",
                f"Failed to save:\n{e}")
            return

    def _save_preview_png(self):
        """Speichere das gerenderte Preview-PNG (rendert die
        Sequenz mit dem aktuellen Charset)."""
        default = os.path.splitext(self._png_path)[0] + ".preview.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save preview PNG", default,
            "PNG Images (*.png)")
        if not path:
            return
        if not self._preview_png.save(path, "PNG"):
            QMessageBox.warning(self, "Save preview PNG",
                f"Failed to save {path}")
