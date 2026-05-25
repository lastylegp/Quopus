"""
Viewers:
  TextReader - plain text + color ANSI + color PETSCII
               (with /X Dump button to generate DIR.LST for the file)
  HexReader  - paginated hex dump
"""
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QTextEdit, QComboBox, QInputDialog, QMessageBox,
    QColorDialog,
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS, SCROLLBAR_QSS,
    button_qss, get_topaz_font, get_c64_font, has_c64_pro_mono, fmt_size
)
from .encodings import amiga_to_unicode, cp437_to_unicode, parse_ansi, parse_petscii
from .petscii_tables import petscii_byte_to_unicode


def _html_escape_ch(ch):
    if ch == '<': return '&lt;'
    if ch == '>': return '&gt;'
    if ch == '&': return '&amp;'
    if ch == ' ': return '&nbsp;'
    if ch == '"': return '&quot;'
    return ch


def _nfo_text_to_html(text, font_family, font_size, fg, bg):
    """Render CP437/NFO text as HTML so block-drawing chars (▀ ▄ █)
    sit flush against each other. Fonts that have correctly-sized block
    glyphs are preferred (Cascadia Mono, Consolas, DejaVu Sans Mono);
    Topaz and Courier New leave pixel gaps.
    Pass `font_family` as a comma-separated list for fallback."""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    body_parts = []
    for ln in lines:
        # HTML escape + convert spaces so leading/trailing spaces are
        # preserved verbatim
        esc = (ln.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;')
                 .replace(' ', '&nbsp;'))
        if not esc: esc = '&nbsp;'
        body_parts.append(esc)
    body = '<br>'.join(body_parts)
    # Pull each char 1px left and each line 2px up so adjacent block-
    # drawing chars (▀ ▄ █ ░ ▒ ▓) paint over the font's cell padding.
    # Without this you see thin background-coloured lines between glyphs
    # ("the grid effect").
    return (
        f'<html><head><style>'
        f'html,body{{margin:0;padding:0;background-color:{bg};}}'
        f'pre{{margin:0;padding:0;background-color:{bg};color:{fg};'
        f'font-family:{font_family},monospace;font-size:{font_size}px;'
        f'line-height:{font_size - 2}px;letter-spacing:-1px;}}'
        f'</style></head><body>'
        f'<pre>{body}</pre>'
        f'</body></html>'
    )


def _ansi_cells_to_html(grid, font_family, font_size, default_bg):
    lines = []
    for row in grid:
        parts = []
        cur_fg = None; cur_bg = None
        buf = []

        def flush():
            if not buf: return
            parts.append(f'<span style="color:{cur_fg};background-color:{cur_bg};">')
            parts.append(''.join(buf))
            parts.append('</span>')
            buf.clear()

        for cell in row:
            ch = cell["char"]
            fg = cell["fg"]; bg = cell["bg"]
            if cell.get("reverse"):
                fg, bg = bg, fg
            if fg != cur_fg or bg != cur_bg:
                flush(); cur_fg = fg; cur_bg = bg
            buf.append(_html_escape_ch(ch))
        flush()
        lines.append(''.join(parts) if parts else '&nbsp;')
    body = '<br>'.join(lines)
    return (
        f'<html><head><style>'
        f'html,body{{margin:0;padding:0;background-color:{default_bg};}}'
        f'pre{{margin:0;padding:0;background-color:{default_bg};'
        f'font-family:\'{font_family}\',monospace;font-size:{font_size}px;'
        f'line-height:1.0;}}'
        f'</style></head><body>'
        f'<pre>{body}</pre>'
        f'</body></html>'
    )


def _petscii_cells_to_html(grid, default_bg, font_family, font_size, use_pua):
    """
    Render PETSCII color grid as HTML using Style64 "Direct PETSCII" PUA pages:
      U+E000 + byte = upper mode, reverse off
      U+E100 + byte = lower mode, reverse off
      U+E200 + byte = upper mode, reverse on
      U+E300 + byte = lower mode, reverse on
    In PUA mode the font provides the reverse glyphs (different PUA page),
    so we keep fg/bg as-is. In fallback mode we swap fg/bg for reverse.
    """
    lines = []
    for row in grid:
        parts = []
        cur_fg = None; cur_bg = None
        buf = []

        def flush():
            if not buf: return
            parts.append(f'<span style="color:{cur_fg};background-color:{cur_bg};">')
            parts.append(''.join(buf))
            parts.append('</span>')
            buf.clear()

        for cell in row:
            fg = cell["fg"]; bg = cell["bg"]
            if not use_pua and cell.get("reverse"):
                fg, bg = bg, fg
            if fg != cur_fg or bg != cur_bg:
                flush(); cur_fg = fg; cur_bg = bg

            if use_pua:
                charset = cell.get("charset", "lower")
                reverse = bool(cell.get("reverse"))
                if reverse:
                    base = 0xE200 if charset == 'upper' else 0xE300
                else:
                    base = 0xE000 if charset == 'upper' else 0xE100
                b = cell["byte"] & 0xFF
                buf.append(chr(base + b))
            else:
                b = cell["byte"]
                charset = cell.get("charset", "lower")
                ch = petscii_byte_to_unicode(b, charset=charset)
                buf.append(_html_escape_ch(ch))
        flush()
        lines.append(''.join(parts) if parts else '&nbsp;')
    body = '<br>'.join(lines)
    # The grid effect comes from the C64 Pro Mono font having ~1-2px of
    # right/bottom padding inside each glyph cell. Negative letter-spacing
    # and shorter line-height make consecutive glyphs paint over that
    # padding, eliminating the visible grid.
    return (
        f'<html><head><style>'
        f'html,body{{margin:0;padding:0;background-color:{default_bg};}}'
        f'pre{{margin:0;padding:0;background-color:{default_bg};'
        f'font-family:\'{font_family}\',monospace;font-size:{font_size}px;'
        f'line-height:{font_size - 2}px;letter-spacing:-1px;}}'
        f'span{{padding:0;margin:0;}}'
        f'</style></head><body>'
        f'<pre>{body}</pre>'
        f'</body></html>'
    )


def render_petscii_grid_to_pixmap(grid, default_bg, font_family,
                                     cell_size, use_pua):
    """Render a parsed PETSCII grid (from parse_petscii()) to a
    QPixmap. Each cell of the grid has byte/fg/bg/reverse/
    charset fields - we draw the bg rectangle first, then the
    glyph on top, per-cell, so adjacent cells are pixel-flush
    against each other.

    This is the single source of truth for PETSCII rendering;
    both the standalone Text Reader dialog (readers.py) and
    the file-preview pane inside the CBM disk viewer
    (cbmfiles.py) call here. The 'rendering pipeline' in
    cbmfiles' SEQ path used to do a simpler line-by-line
    PETSCII dump that ignored color/reverse/charset control
    codes - that worked for plain text but botched BBS art
    files that depend on the control bytes.

    Args:
        grid: list[list[dict]] from parse_petscii(); each cell
            has keys 'byte', 'fg', 'bg', 'reverse', 'charset'
            (+ 'sc' which we don't need here)
        default_bg: hex color string for the canvas background
            (set by $02 + color byte, $03, or default black)
        font_family: name of the QFont family to use for glyphs.
            Should be 'C64 Pro Mono' for use_pua=True or any
            mono fallback otherwise.
        cell_size: pixel size of one square cell. PETSCII cells
            are intrinsically square at 8x8 in hardware so we
            use one dimension for both axes.
        use_pua: True if the C64 Pro Mono font is loaded and
            the Private Use Area codepoints can be addressed
            directly (giving exact authentic glyph shapes).
            False falls back to petscii_byte_to_unicode() which
            maps to Unicode codepoints that *most* mono fonts
            can render but with quality varying by font.
    """
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QImage
    from PyQt6.QtCore import QRect, Qt
    if not grid:
        return QPixmap(1, 1)
    rows = len(grid)
    cols = len(grid[0]) if grid else 0
    # Render into a QImage with explicit pixel format and
    # DevicePixelRatio=1.0. QPixmap on its own picks up the
    # window's DPR on HiDPI displays, which makes Qt upsample
    # our pixel-perfect output to (e.g.) 1.5x using bilinear
    # filtering - the source of the horizontal-line bleed
    # between cells the user kept reporting. QImage at format
    # RGB32 with DPR=1 stays at 1:1 pixels.
    img = QImage(cols * cell_size, rows * cell_size,
                  QImage.Format.Format_RGB32)
    img.setDevicePixelRatio(1.0)
    img.fill(QColor(default_bg))

    # Build the QFont for glyph drawing. Set pixel size to
    # cell_size + 2 so the glyph reliably fills the entire
    # cell (including the bottom row of pixels). Many monospace
    # fonts at setPixelSize(N) actually draw at N-1 or N-2 px
    # tall with N px of advance/line-height; that leaves a 1-2
    # px unfilled strip at the bottom of each cell which reads
    # as a horizontal line between rows when adjacent rows have
    # different background colours (especially obvious in
    # reverse-video text where the cell background is solid
    # bright colour). Drawing the glyph slightly oversize and
    # clipping to the cell rect via the AlignCenter rect gets
    # full-cell coverage without overflow visible artifacts.
    f = QFont(font_family)
    f.setPixelSize(cell_size + 2)
    # Disable any automatic letter-spacing
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100)

    painter = QPainter(img)
    try:
        painter.setFont(f)
        # Use no antialiasing for the cell rectangles - we want
        # crisp pixel boundaries between cells.
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing, False)
        # Disable TEXT antialiasing too. Sub-pixel text
        # rendering leaves faint alpha pixels at the glyph
        # edges - when the next cell paints its background
        # over the row below, the leftover alpha pixels at
        # the bottom of the previous row read as 1-px
        # horizontal lines. Pixel-perfect mode keeps every
        # cell's pixels strictly inside its own rectangle.
        painter.setRenderHint(
            QPainter.RenderHint.TextAntialiasing, False)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, False)

        for ry, row in enumerate(grid):
            for cx, cell in enumerate(row):
                fg = cell["fg"]
                bg = cell["bg"]
                # Always handle reverse-video by swapping fg/bg
                # and drawing the NORMAL (non-reverse) glyph.
                # The C64 Pro Mono font's reverse PUA codepoints
                # ($E200/$E300 range) don't render at exactly the
                # full cell height - bearing leaves 1-2 px at
                # the top and bottom unfilled. Using fillRect(bg)
                # + the normal glyph in fg gets a full-cell
                # filled block with no seams between rows.
                if cell.get("reverse"):
                    fg, bg = bg, fg
                rect = QRect(cx * cell_size, ry * cell_size,
                              cell_size, cell_size)
                # Fill the full cell with bg first.
                painter.fillRect(rect, QColor(bg))
                glyph_rect = rect

                # Pick the glyph code point. We always use the
                # NORMAL (non-reverse) glyph here; reverse-video
                # has already been handled above by swapping
                # fg/bg, so painting the regular glyph in the
                # swapped colours gives the right visual without
                # depending on the font's reverse PUA pages
                # having pixel-perfect full-cell coverage. C64
                # Pro Mono's reverse glyphs leave 1-2 px of
                # un-inked area top/bottom which read as
                # horizontal seams between rows of reverse-mode
                # text - this approach sidesteps that entirely.
                if use_pua:
                    charset = cell.get("charset", "lower")
                    base = (0xE000 if charset == 'upper'
                             else 0xE100)
                    b = cell["byte"] & 0xFF
                    ch = chr(base + b)
                else:
                    b = cell["byte"]
                    charset = cell.get("charset", "lower")
                    ch = petscii_byte_to_unicode(
                        b, charset=charset)

                painter.setPen(QColor(fg))
                painter.drawText(
                    glyph_rect,
                    Qt.AlignmentFlag.AlignCenter,
                    ch)
    finally:
        painter.end()
    # Convert the QImage to a QPixmap. setDevicePixelRatio(1.0)
    # on the result so any downstream display widget treats it
    # as a 1:1 bitmap (no DPR upsampling that would re-introduce
    # the seam artifacts).
    pix = QPixmap.fromImage(img)
    pix.setDevicePixelRatio(1.0)
    return pix


class TextReader(QDialog):
    ENCODINGS = [
        "Auto",
        "Amiga/Topaz (plain)",
        "CP437 (NFO)",
        "UTF-8",
        "Latin-1",
        "Amiga ANSI (colors)",
        "PETSCII (lower mode)",
        "PETSCII (upper mode)",
    ]

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Read: {self.path.name}")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(1000, 720)

        # Load persisted settings from the main window's config. Falls
        # back to sane defaults when no config is reachable (e.g. when
        # the dialog is opened from a tool with no parent window).
        self._cfg = self._find_config()
        self._manual_font_size = int(self._cfg.get(
            "text_reader_font_size", 11)) if self._cfg else 11
        self._fg_color = (self._cfg.get("text_reader_fg", "#FFFFFF")
                            if self._cfg else "#FFFFFF")
        self._bg_color = (self._cfg.get("text_reader_bg", "#000000")
                            if self._cfg else "#000000")
        # If the user has manually set a font size, treat it as their
        # preferred size from the start - don't auto-fit.
        self._auto_fit = (self._cfg is None
                            or "text_reader_font_size" not in self._cfg)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        size = self.path.stat().st_size
        title = QLabel(f" Read: {self.path.name}  ({fmt_size(size)}) ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(2)
        lbl = QLabel(" Encoding: ")
        lbl.setStyleSheet(INFOBAR_QSS)
        tool_row.addWidget(lbl)
        self.cb_enc = QComboBox()
        self.cb_enc.addItems(self.ENCODINGS)
        self.cb_enc.setStyleSheet(
            f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"font-family: 'Topaz','Courier New',monospace; padding: 2px; }}"
        )
        self.cb_enc.currentTextChanged.connect(self._reload)
        tool_row.addWidget(self.cb_enc, 1)

        btn_dump = QPushButton("/X Reverse")
        btn_dump.setStyleSheet(button_qss("orange"))
        btn_dump.setFixedWidth(100)
        btn_dump.setToolTip("Reverse line order of this file (like AmiExpress /X)")
        btn_dump.clicked.connect(self._dump_x)
        tool_row.addWidget(btn_dump)

        btn_zoom_out = QPushButton("-")
        btn_zoom_out.setStyleSheet(button_qss("mid"))
        btn_zoom_out.setFixedWidth(28)
        btn_zoom_out.setToolTip("Zoom out")
        btn_zoom_out.clicked.connect(lambda: self._zoom(-1))
        tool_row.addWidget(btn_zoom_out)

        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setStyleSheet(button_qss("mid"))
        btn_zoom_in.setFixedWidth(28)
        btn_zoom_in.setToolTip("Zoom in")
        btn_zoom_in.clicked.connect(lambda: self._zoom(+1))
        tool_row.addWidget(btn_zoom_in)

        # FG color picker - small swatch button. Shows current fg
        # color; click opens QColorDialog.
        self._btn_fg = QPushButton("FG")
        self._btn_fg.setFixedWidth(34)
        self._btn_fg.setToolTip(
            "Pick foreground (text) color - persisted across sessions")
        self._btn_fg.clicked.connect(self._pick_fg_color)
        tool_row.addWidget(self._btn_fg)

        self._btn_bg = QPushButton("BG")
        self._btn_bg.setFixedWidth(34)
        self._btn_bg.setToolTip(
            "Pick background color - persisted across sessions")
        self._btn_bg.clicked.connect(self._pick_bg_color)
        tool_row.addWidget(self._btn_bg)

        # Initialize the swatch styling with the loaded colors
        self._update_color_btn_styles()

        btn_hex = QPushButton("Hex")
        btn_hex.setStyleSheet(button_qss("purple"))
        btn_hex.setFixedWidth(60)
        btn_hex.clicked.connect(self._open_hex)
        tool_row.addWidget(btn_hex)

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setFixedWidth(70)
        btn_close.clicked.connect(self.accept)
        tool_row.addWidget(btn_close)
        layout.addLayout(tool_row)

        self.text_plain = QPlainTextEdit()
        self.text_plain.setReadOnly(True)
        # Note: no font-size in QSS - that would override setFont().
        # Font size is set programmatically via _fit_plain_font() / setFont().
        # Background and foreground colors come from config and can be
        # changed live via the FG/BG buttons.
        self.text_plain.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {self._bg_color};
                color: {self._fg_color};
                font-family: "Topaz-8", "Topaz", "Courier New", monospace;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.SELECTED_FG};
            }}
            {SCROLLBAR_QSS}
        """)
        self.text_plain.setFont(get_topaz_font(self._manual_font_size))
        layout.addWidget(self.text_plain, 1)

        self.text_color = QTextEdit()
        self.text_color.setReadOnly(True)
        self.text_color.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self._bg_color};
                border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.text_color.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text_color.hide()
        layout.addWidget(self.text_color, 1)

        # Pixel-rendered view for PETSCII - QPainter-drawn QPixmap inside
        # a QScrollArea. No font-metric padding => no grid effect.
        from PyQt6.QtWidgets import QScrollArea
        self.bitmap_scroll = QScrollArea()
        self.bitmap_scroll.setWidgetResizable(False)
        self.bitmap_scroll.setStyleSheet(f"""
            QScrollArea {{
                background-color: {C.BLACK};
                border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.bitmap_label = QLabel()
        self.bitmap_label.setStyleSheet(f"background-color: {C.BLACK};")
        self.bitmap_scroll.setWidget(self.bitmap_label)
        self.bitmap_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bitmap_scroll.hide()
        layout.addWidget(self.bitmap_scroll, 1)

        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Rendering notice overlay - centered, shown during slow renders
        self._notice = QLabel(" Rendering data, please wait... ", self)
        self._notice.setStyleSheet(f"""
            QLabel {{
                background-color: {C.ACTIVE_BG};
                color: {C.ACTIVE_FG};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: 14px;
                font-weight: bold;
                padding: 14px 28px;
                border: 2px solid {C.BLACK};
            }}
        """)
        self._notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notice.hide()
        self._notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Load initially, then re-render after layout settles
        self._reload("Auto")
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(80, lambda: self._reload(self.cb_enc.currentText()))

    def _show_notice(self):
        """Show a centered 'Rendering...' notice overlay."""
        self._notice.adjustSize()
        pw, ph = self.width(), self.height()
        nw, nh = self._notice.width(), self._notice.height()
        self._notice.move((pw - nw) // 2, (ph - nh) // 2)
        self._notice.raise_()
        self._notice.show()
        # Force immediate repaint so notice is visible before render starts
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

    def _hide_notice(self):
        self._notice.hide()

    def _zoom(self, delta):
        """Manual zoom override - disables auto-fit for the session
        and persists the size to config so the next reader opens at
        the same zoom."""
        if not hasattr(self, '_manual_font_size'):
            self._manual_font_size = 16
        self._manual_font_size = max(6, min(72, self._manual_font_size + delta * 2))
        self._auto_fit = False
        # Plain view: use Qt's own zoom
        plain_font = self.text_plain.font()
        plain_font.setPointSize(max(6, self._manual_font_size - 4))
        self.text_plain.setFont(plain_font)
        # Color view: re-render HTML if currently visible
        if self.text_color.isVisible():
            self._reload(self.cb_enc.currentText())
        # Persist immediately so a crash/abrupt close still saves
        self._persist_settings()

    def _find_config(self):
        """Walk the parent chain looking for a window with .config.
        Returns the config dict or None if no main window found."""
        w = self.parent()
        while w is not None:
            if hasattr(w, 'config') and isinstance(w.config, dict):
                return w.config
            w = w.parent() if hasattr(w, 'parent') else None
        # Fall back to the top-level window via window()
        try:
            w = self.window()
            if w and hasattr(w, 'config') and isinstance(w.config, dict):
                return w.config
        except Exception:
            pass
        return None

    def _persist_settings(self):
        """Write the current font-size + colors back into the config
        and save it to disk. Cheap to call - JSON serialisation +
        file write of a small file."""
        if self._cfg is None:
            return
        self._cfg["text_reader_font_size"] = int(self._manual_font_size)
        self._cfg["text_reader_fg"] = self._fg_color
        self._cfg["text_reader_bg"] = self._bg_color
        try:
            from .config import save_config
            save_config(self._cfg)
        except Exception:
            # Don't blow up the reader if config save fails - the
            # in-memory config still has the new values for this session.
            pass

    def _update_color_btn_styles(self):
        """Repaint the FG/BG buttons to show the current color."""
        # FG button: use the FG color as text on a contrasting bg
        # so the user sees the actual color choice.
        fg = self._fg_color
        bg = self._bg_color
        # Pick a readable label color for the button itself
        self._btn_fg.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; "
            f"border: 1px solid {C.BLACK}; font-weight: bold; "
            f"padding: 2px; }}"
            f"QPushButton:hover {{ border: 2px solid {C.SELECTED}; }}")
        self._btn_bg.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; "
            f"border: 1px solid {C.BLACK}; font-weight: bold; "
            f"padding: 2px; }}"
            f"QPushButton:hover {{ border: 2px solid {C.SELECTED}; }}")

    def _apply_colors(self):
        """Re-apply current FG/BG colors to the visible text widgets."""
        self.text_plain.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {self._bg_color};
                color: {self._fg_color};
                font-family: "Topaz-8", "Topaz", "Courier New", monospace;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.SELECTED_FG};
            }}
            {SCROLLBAR_QSS}
        """)
        # Color view background
        self.text_color.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self._bg_color};
                border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self._update_color_btn_styles()
        # If the color (HTML) view is showing, re-render so embedded
        # ANSI/PETSCII spans pick up the new background.
        if self.text_color.isVisible():
            self._reload(self.cb_enc.currentText())

    def _pick_fg_color(self):
        """Show the system color picker for the foreground color.
        Persists immediately when the user accepts."""
        cur = QColor(self._fg_color)
        c = QColorDialog.getColor(cur, self, "Pick text color")
        if c.isValid():
            self._fg_color = c.name()
            self._apply_colors()
            self._persist_settings()

    def _pick_bg_color(self):
        cur = QColor(self._bg_color)
        c = QColorDialog.getColor(cur, self, "Pick background color")
        if c.isValid():
            self._bg_color = c.name()
            self._apply_colors()
            self._persist_settings()

    def closeEvent(self, ev):
        # Defensive save on window-close (X button, alt-F4, etc.)
        try: self._persist_settings()
        except Exception: pass
        super().closeEvent(ev)

    def _tighten_text_color_spacing(self, font_family, font_size,
                                     letter_pct=92, line_pct=85):
        """Qt's QTextEdit ignores CSS letter-spacing and line-height,
        so we apply them here at the Qt level instead. Walks every
        block in the document and sets:
          - font with negative QFont.letterSpacing (percentage type)
          - QTextBlockFormat.lineHeight (percentage type)
        Result: glyphs paint over the C64 Pro Mono / Cascadia Mono cell
        padding, eliminating the visible "grid" between cells.
        """
        from PyQt6.QtGui import (
            QFont, QTextCursor, QTextBlockFormat, QTextCharFormat,
        )
        # Build a font with tighter letter-spacing
        f = QFont(font_family)
        f.setPixelSize(font_size)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, letter_pct)
        # Apply font + line-height to every block
        doc = self.text_color.document()
        doc.setDefaultFont(f)
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        bf = QTextBlockFormat()
        bf.setLineHeight(line_pct,
                         QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        bf.setTopMargin(0); bf.setBottomMargin(0)
        cf = QTextCharFormat()
        cf.setFont(f)
        cur.mergeBlockFormat(bf)
        cur.mergeCharFormat(cf)

    def _fit_plain_font(self, text):
        """
        Pick a font size for text_plain that makes the widest line fit
        the viewport. Keeps size between 8 and 36pt.
        """
        if getattr(self, '_auto_fit', True) is False:
            return
        # Find the widest line (scan first 500 lines for speed)
        max_len = 0
        for i, line in enumerate(text.split("\n")):
            if i > 500:
                break
            if len(line) > max_len:
                max_len = len(line)
        if max_len < 20:
            max_len = 80  # fallback

        vp_w = self.text_plain.viewport().width()
        if vp_w <= 0:
            vp_w = self.width() - 40
        vp_w = max(300, vp_w - 12)

        # Each monospace char is ~0.60 * font-size-px wide
        # font point size ~= font px * 0.75 for 96dpi
        size_px = int(vp_w / (max_len * 0.60))
        size_pt = max(8, min(36, int(size_px * 0.75)))
        f = self.text_plain.font()
        f.setPointSize(size_pt)
        self.text_plain.setFont(f)

    def _auto_fit_font_size(self, cols, rows=None, char_factor=0.60,
                            force_fit_height=False):
        """
        Return font size so `cols` columns fit the current viewport width.

        - force_fit_height=True (PETSCII art): try to fit full height too,
          but never below 10px (below that details are unreadable; better
          to let the user scroll).
        - rows <= 60: constrain by height for readability.
        - longer: only width, user scrolls vertically.
        """
        if getattr(self, '_auto_fit', True) is False:
            return getattr(self, '_manual_font_size', 16)

        vp = self.text_color.viewport()
        vp_w = vp.width() if vp.width() > 0 else self.width() - 40
        vp_h = vp.height() if vp.height() > 0 else self.height() - 140
        # Reserve room for potential scrollbars that may appear after
        # rendering (18-20px each). Without this margin, content that
        # *just* fits triggers scrollbars which then crop the content.
        vp_w = max(300, vp_w - 22)
        vp_h = max(200, vp_h - 22)

        fs_w = int(vp_w / (cols * char_factor))

        if rows and rows > 0 and (force_fit_height or rows <= 60):
            # Qt QTextEdit tends to add extra vertical spacing around <pre>
            # blocks and between lines even with line-height:1.0 in CSS.
            # Factor 1.25 accounts for that so the full art fits without
            # being cut off at the bottom.
            fs_h = int(vp_h / (rows * 1.25))
            if force_fit_height:
                # For PETSCII: honor height strictly but never below 10px
                # (so you can still see the full art with tiny but visible chars
                # for long files like END 8.seq with 100 rows)
                fs_h = max(10, fs_h)
                fs = min(fs_w, fs_h)
            else:
                # For short ANSI: only apply height constraint if it yields
                # readable size; otherwise let user scroll
                if fs_h >= 10:
                    fs = min(fs_w, fs_h)
                else:
                    fs = fs_w
        else:
            fs = fs_w

        return max(8, min(48, fs))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'text_color') or not hasattr(self, 'cb_enc'):
            return
        # Re-position the notice overlay if shown
        if hasattr(self, '_notice') and self._notice.isVisible():
            pw, ph = self.width(), self.height()
            nw, nh = self._notice.width(), self._notice.height()
            self._notice.move((pw - nw) // 2, (ph - nh) // 2)

        if getattr(self, '_auto_fit', True) is False:
            return

        from PyQt6.QtCore import QTimer
        if self.text_color.isVisible():
            # Re-render color view with new viewport dimensions
            if not hasattr(self, '_resize_timer'):
                self._resize_timer = QTimer(self)
                self._resize_timer.setSingleShot(True)
                self._resize_timer.timeout.connect(
                    lambda: self._reload(self.cb_enc.currentText()))
            self._resize_timer.start(100)
        elif self.text_plain.isVisible():
            # Re-fit plain text font on resize
            if not hasattr(self, '_resize_plain_timer'):
                self._resize_plain_timer = QTimer(self)
                self._resize_plain_timer.setSingleShot(True)
                self._resize_plain_timer.timeout.connect(
                    lambda: self._fit_plain_font(self.text_plain.toPlainText()))
            self._resize_plain_timer.start(80)

    def _show_plain(self):
        self.text_color.hide(); self.bitmap_scroll.hide()
        self.text_plain.show()

    def _show_color(self):
        self.text_plain.hide(); self.bitmap_scroll.hide()
        self.text_color.show()

    def _show_bitmap(self):
        self.text_plain.hide(); self.text_color.hide()
        self.bitmap_scroll.show()

    def _render_petscii_to_pixmap(self, grid, default_bg, font_family,
                                    cell_size, use_pua):
        """Backwards-compat method - delegates to the module-
        level render_petscii_grid_to_pixmap so other modules
        (cbmfiles' file preview pane in particular) can reuse
        the same rendering pipeline without inheriting from
        this class.
        """
        return render_petscii_grid_to_pixmap(
            grid, default_bg=default_bg,
            font_family=font_family,
            cell_size=cell_size, use_pua=use_pua)

    def _dump_x(self):
        """
        /X Reverse: AmiExpress-style dir listing reversal.
        Entries in a dir listing span multiple lines: the first line starts
        in column 0 (filename), continuation lines are indented (description).
        /X Reverse flips the ORDER of entries while keeping each entry's
        internal line order intact.

        Toggles between original and reversed view.
        """
        if getattr(self, '_reversed', False):
            # Restore
            self._reversed = False
            self._reload(self.cb_enc.currentText())
            return

        # Use the currently displayed plain text, or read file if we're
        # showing color output
        if self.text_plain.isVisible():
            text = self.text_plain.toPlainText()
        else:
            try:
                raw = self.path.read_bytes()
                text = raw.decode("latin-1", errors="replace")
                text = text.replace("\r\n", "\n").replace("\r", "\n")
            except Exception as e:
                QMessageBox.warning(self, "/X", f"Cannot read: {e}")
                return

        lines = text.splitlines()
        entries = self._split_ami_entries(lines)
        reversed_text = "\n".join(
            ln for entry in reversed(entries) for ln in entry)
        self.text_plain.setPlainText(reversed_text)
        self._show_plain()
        self._reversed = True
        self.lbl_status.setText(
            f" /X REVERSED | {len(entries)} entries | click /X Reverse again to restore ")

    @staticmethod
    def _split_ami_entries(lines):
        """
        Split an AmiExpress DIR listing into entries.

        An entry STARTS with a line that has a non-whitespace first character
        (the filename column). Continuation lines (description, "Sent by:")
        are indented so they start with whitespace.

        Leading blank lines / lines before the first entry become a synthetic
        'header' entry so they stay at the top.
        """
        entries = []
        current = []
        header = []
        saw_first = False

        for line in lines:
            is_entry_start = line and not line[0].isspace()
            if is_entry_start:
                if not saw_first:
                    # Everything collected so far is the file's header
                    if header:
                        entries.append(header)
                        header = []
                    saw_first = True
                if current:
                    entries.append(current)
                current = [line]
            else:
                if saw_first:
                    current.append(line)
                else:
                    header.append(line)

        if current:
            entries.append(current)
        elif header and not saw_first:
            # File has no entries at all; just a block of text
            entries.append(header)
        return entries

    def _open_hex(self):
        HexReader(self.path, self).exec()

    def _reload(self, enc_choice):
        # Show rendering notice immediately for responsiveness
        self._show_notice()
        try:
            self._reload_inner(enc_choice)
        finally:
            self._hide_notice()

    def _reload_inner(self, enc_choice):
        try:
            raw = self.path.read_bytes()
        except Exception as e:
            self.text_plain.setPlainText(f"<Error: {e}>")
            self._show_plain(); return

        ext = self.path.suffix.lower()
        if enc_choice == "Auto":
            if ext in (".ans",) or (ext == ".asc" and b"\x1b[" in raw[:8192]):
                resolved = "Amiga ANSI (colors)"
            elif ext in (".seq", ".pet", ".c64"):
                # .seq can be either PETSCII or plain text logs (callers.seq etc.)
                # Detect real PETSCII by presence of PETSCII-specific control bytes
                sample = raw[:8192]
                petscii_markers = (0x93, 0x0E, 0x8E, 0x12, 0x92,
                                   0x05, 0x1C, 0x9E, 0x9A, 0x9F,  # colors
                                   0x81, 0x95, 0x96, 0x97, 0x98,
                                   0x99, 0x9B, 0x9C)
                has_petscii = any(b in sample for b in petscii_markers)
                # Also require not too many "normal" 7-bit text chars
                # (logs are mostly 7-bit ASCII with LF/CR)
                if has_petscii and ext in (".pet", ".c64"):
                    resolved = "PETSCII (lower mode)"
                elif has_petscii and ext == ".seq":
                    # Count high-bit bytes; PETSCII uses 0x80+ for graphics
                    high_bit = sum(1 for b in sample if b >= 0x80)
                    if high_bit > len(sample) * 0.02:
                        resolved = "PETSCII (lower mode)"
                    else:
                        resolved = "Amiga/Topaz (plain)"
                else:
                    resolved = "Amiga/Topaz (plain)"
            elif ext in (".nfo", ".diz"):
                resolved = "CP437 (NFO)"
            elif b"\x1b[" in raw[:4096]:
                # File has ESC sequences. Real ANSI art has many of them.
                # Heuristic: for files up to ~500KB render as ANSI.
                # For bigger files, only render as ANSI if ESC density is
                # very high (lots of colors per line). Otherwise plain text
                # (scrollable, readable) is better than a massive ANSI grid.
                esc_count = raw.count(b"\x1b[")
                approx_lines = raw.count(b"\n") + 1
                esc_per_line = esc_count / max(1, approx_lines)
                if len(raw) < 500_000 and esc_count >= 10:
                    resolved = "Amiga ANSI (colors)"
                elif esc_per_line >= 1.0 and len(raw) < 5_000_000:
                    # Dense color usage (avg 1+ ESC per line) - likely ANSI
                    # dir listing with colored per-file signatures
                    resolved = "Amiga ANSI (colors)"
                else:
                    resolved = "Amiga/Topaz (plain)"
            elif ext in (".guide", ".readme", ".doc"):
                resolved = "Amiga/Topaz (plain)"
            else:
                try:
                    raw.decode("utf-8")
                    resolved = "UTF-8"
                except UnicodeDecodeError:
                    resolved = "Amiga/Topaz (plain)"
        else:
            resolved = enc_choice

        try:
            if resolved == "Amiga/Topaz (plain)":
                t = amiga_to_unicode(raw)
                self.text_plain.setPlainText(t); self._show_plain()
                self._fit_plain_font(t)
                self.lbl_status.setText(
                    f" Encoding: {resolved} | {t.count(chr(10))+1} lines, {fmt_size(len(raw))} ")
            elif resolved == "CP437 (NFO)":
                t = cp437_to_unicode(raw)
                # NFO art needs a font where the block-drawing chars
                # ▀▄█ ░▒▓ render *flush* against each other. Topaz and
                # Courier New both leave pixel gaps. Fonts that work:
                # Cascadia Mono, Consolas, DejaVu Sans Mono, Liberation
                # Mono. Color scheme: black on white like the classic
                # FILE_ID.DIZ viewers and the TC Lister.
                lines = t.split('\n')
                w = max((len(ln) for ln in lines), default=80)
                h = len(lines)
                fs = self._auto_fit_font_size(cols=w, rows=h, char_factor=0.60)
                html = _nfo_text_to_html(
                    t,
                    font_family="Cascadia Mono, Consolas, DejaVu Sans Mono, Liberation Mono, Courier New",
                    font_size=fs,
                    fg="#000000", bg="#ffffff")
                self.text_color.setHtml(html); self._show_color()
                self._tighten_text_color_spacing(
                    "Cascadia Mono", fs,
                    letter_pct=95, line_pct=100)
                from PyQt6.QtGui import QTextCursor
                self.text_color.moveCursor(QTextCursor.MoveOperation.Start)
                self.text_color.verticalScrollBar().setValue(0)
                self.text_color.horizontalScrollBar().setValue(0)
                self.lbl_status.setText(
                    f" Encoding: {resolved} | {h} lines, {fmt_size(len(raw))} ")
            elif resolved == "UTF-8":
                t = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                self.text_plain.setPlainText(t); self._show_plain()
                self._fit_plain_font(t)
                self.lbl_status.setText(
                    f" Encoding: {resolved} | {t.count(chr(10))+1} lines, {fmt_size(len(raw))} ")
            elif resolved == "Latin-1":
                t = raw.decode("latin-1", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                self.text_plain.setPlainText(t); self._show_plain()
                self._fit_plain_font(t)
                self.lbl_status.setText(
                    f" Encoding: {resolved} | {t.count(chr(10))+1} lines, {fmt_size(len(raw))} ")
            elif resolved == "Amiga ANSI (colors)":
                grid, w, h = parse_ansi(raw)
                fs = self._auto_fit_font_size(cols=w, rows=h, char_factor=0.60)
                html = _ansi_cells_to_html(
                    grid, font_family="Courier New",
                    font_size=fs, default_bg=self._bg_color)
                self.text_color.setHtml(html); self._show_color()
                from PyQt6.QtGui import QTextCursor
                self.text_color.moveCursor(QTextCursor.MoveOperation.Start)
                self.text_color.verticalScrollBar().setValue(0)
                self.text_color.horizontalScrollBar().setValue(0)
                self.lbl_status.setText(
                    f" Encoding: {resolved} | {h}x{w} | font {fs}px | {fmt_size(len(raw))} ")
            elif resolved.startswith("PETSCII"):
                init_charset = 'upper' if "upper" in resolved else 'lower'
                result = parse_petscii(raw, initial_charset=init_charset)
                grid = result["grid"]
                w = result["width"]
                h = result["height"]
                sbg = result["screen_bg"]
                use_pua = has_c64_pro_mono()
                c64f = get_c64_font(14)
                # Choose a cell size that fits the viewport. PETSCII cells
                # are square so width and height get the same value.
                vp = self.bitmap_scroll.viewport()
                vp_w = (vp.width() if vp.width() > 0
                        else self.width() - 40) - 22
                vp_h = (vp.height() if vp.height() > 0
                        else self.height() - 140) - 22
                cell_w = max(8, vp_w // max(w, 1))
                cell_h = max(8, vp_h // max(h, 1))
                cell_size = min(cell_w, cell_h)
                # Always fit height (PETSCII art needs to be fully visible)
                if cell_size * h > vp_h:
                    cell_size = max(8, vp_h // max(h, 1))
                pix = self._render_petscii_to_pixmap(
                    grid, default_bg=sbg,
                    font_family=c64f.family(),
                    cell_size=cell_size, use_pua=use_pua)
                self.bitmap_label.setPixmap(pix)
                self.bitmap_label.resize(pix.size())
                self._show_bitmap()
                self.bitmap_scroll.horizontalScrollBar().setValue(0)
                self.bitmap_scroll.verticalScrollBar().setValue(0)
                fnote = "C64 Pro Mono PUA (Direct)" if use_pua else "Unicode fallback - install C64 Pro Mono in fonts/"
                self.lbl_status.setText(
                    f" {resolved} | {h}x{w} | cell {cell_size}px | {fnote} | {fmt_size(len(raw))} ")
            else:
                self.text_plain.setPlainText(raw.decode("latin-1", errors="replace"))
                self._show_plain()
        except Exception as e:
            self.text_plain.setPlainText(f"<decode error: {e}>")
            self._show_plain()
            self.lbl_status.setText(f" Error: {e} ")


class _HexEditWidget(QPlainTextEdit):
    """A QPlainTextEdit subclass that behaves like a true hex editor
    when its `hex_edit_mode` flag is True.

    In hex-edit mode the cursor can park on EITHER side:
      * Hex side (cols 10..57): only 0-9 a-f A-F are accepted; each
        typed digit overwrites the digit under the cursor and
        advances. The matching ASCII char on the right side is
        updated in real time as the byte's value changes.
      * ASCII side (cols 60..75 / 60..67+68..75 with the half-gap):
        any printable character (0x20..0x7e) is accepted; it
        replaces the byte and the matching pair on the hex side is
        updated in real time.

    The layout chrome - offset column, inter-pair spaces, the
    half-line gap, the gap between hex and ASCII - is COMPLETELY
    protected. Cursor movement (arrows, mouse click) snaps to the
    nearest editable position. Backspace/Delete/Insert/Enter and
    any other length-changing key is swallowed.

    Outside hex-edit mode it's a plain read-only viewer.

    Layout in each rendered line (HexReader._render produces this):
        OFFSET<2sp>HEX_LEFT<sp>HEX_RIGHT<2sp>ASCII
        00000000  50 53 49 44 00 03 00 7c  00 7e a7 e0 00 e3 00 01  PSID...|.~......
        ^0       ^10                    ^32                       ^60..^75

    Hex-digit columns for pair n (0..15):
        n in 0..7   -> col = 10 + n*3        (digit 1 at col, digit 2 at col+1)
        n in 8..15  -> col = 10 + n*3 + 1
    ASCII column for byte n: col = 60 + n  (no half-gap on the ASCII side).
    """

    HEX_FIRST_COL = 10                    # first hex digit position
    BYTES_PER_LINE = 16                   # always 16 bytes per row
    HALF_BREAK_AFTER = 8                  # extra space goes after pair 7
    # ASCII column starts after: HEX (47 chars) + extra-half-space (1) +
    # two gap spaces (2) -> col 10+47+1+2 = 60. _render uses this.
    ASCII_FIRST_COL = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hex_edit_mode = False
        # Number of valid bytes on the LAST line (for files where
        # size is not a multiple of 16). 16 means the last line is
        # full. The HexReader sets this after each render.
        self._last_line_bytes = self.BYTES_PER_LINE
        # Number of FULL lines in the current page. The HexReader
        # sets this after each render. Used to clamp navigation.
        self._line_count = 0
        # While we're patching ASCII <-> hex sync, suppress
        # textChanged so HexReader doesn't see it as a fresh edit
        # multiple times for one logical change.
        self._sync_in_progress = False

    @classmethod
    def hex_col_for_pair(cls, pair_idx):
        """Return the (digit1_col, digit2_col) for hex pair index
        0..15 within a rendered line."""
        base = cls.HEX_FIRST_COL + pair_idx * 3
        if pair_idx >= cls.HALF_BREAK_AFTER:
            base += 1   # the extra space between halves
        return base, base + 1

    @classmethod
    def col_to_pair(cls, col):
        """Inverse of hex_col_for_pair. Returns (pair_idx,
        digit_within_pair) for the given column, or None if the
        column isn't on a hex digit. digit_within_pair is 0 (high
        nibble) or 1 (low nibble)."""
        for n in range(cls.BYTES_PER_LINE):
            d1, d2 = cls.hex_col_for_pair(n)
            if col == d1: return n, 0
            if col == d2: return n, 1
        return None

    @classmethod
    def ascii_col_for_byte(cls, byte_idx):
        """Return the column where byte byte_idx (0..15) is shown
        in the ASCII column on the right."""
        return cls.ASCII_FIRST_COL + byte_idx

    @classmethod
    def col_to_ascii_byte(cls, col):
        """Inverse: return the byte index for an ASCII-column col,
        or None if the column isn't in the ASCII region."""
        if cls.ASCII_FIRST_COL <= col < cls.ASCII_FIRST_COL + cls.BYTES_PER_LINE:
            return col - cls.ASCII_FIRST_COL
        return None

    def _byte_index_at(self, line_idx, col):
        """Return (byte_idx_in_line, side) for the given (line, col),
        or None if the position isn't on any byte. `side` is 'hex'
        or 'ascii'. Used for both navigation and ASCII <-> hex sync.
        """
        info = self.col_to_pair(col)
        if info is not None:
            return info[0], 'hex'
        a = self.col_to_ascii_byte(col)
        if a is not None:
            return a, 'ascii'
        return None

    def _max_pair_on_line(self, line_idx):
        """How many bytes are valid on a given line? All lines
        except possibly the last have the full BYTES_PER_LINE bytes.
        The last line may have fewer if file_size % 16 != 0."""
        if line_idx < self._line_count - 1:
            return self.BYTES_PER_LINE
        return self._last_line_bytes

    def _all_editable_cols_on_line(self, line_idx, side='both'):
        """Return a sorted list of column indices that the cursor
        is allowed to occupy on this line, in left-to-right order.
        `side` filters to 'hex', 'ascii', or 'both'."""
        max_b = self._max_pair_on_line(line_idx)
        if max_b <= 0:
            return []
        cols = []
        if side in ('hex', 'both'):
            for n in range(max_b):
                d1, d2 = self.hex_col_for_pair(n)
                cols.append(d1); cols.append(d2)
        if side in ('ascii', 'both'):
            for n in range(max_b):
                cols.append(self.ascii_col_for_byte(n))
        cols.sort()
        return cols

    def _snap_to_editable(self, line_idx, col, direction=1, side='both'):
        """Snap the (line, col) to the nearest valid editable column
        (hex or ascii). `direction` is +1 to prefer right, -1 left."""
        editable = self._all_editable_cols_on_line(line_idx, side)
        if not editable:
            return None
        if col in editable:
            return col
        if direction > 0:
            for c in editable:
                if c > col:
                    return c
            return editable[-1]
        else:
            for c in reversed(editable):
                if c < col:
                    return c
            return editable[0]

    # Backwards-compat alias (older code called this name).
    def _snap_to_hex(self, line_idx, col, direction=1):
        return self._snap_to_editable(line_idx, col, direction, side='hex')

    def _cursor_line_col(self):
        """Return (line_idx, col_in_line) of the current cursor."""
        cur = self.textCursor()
        block = cur.block()
        return block.blockNumber(), cur.positionInBlock()

    def _set_cursor_line_col(self, line_idx, col):
        """Move the cursor to (line, col), clamped to file bounds."""
        if line_idx < 0:
            line_idx = 0
        if line_idx >= self.blockCount():
            line_idx = self.blockCount() - 1
        block = self.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        line_len = block.length() - 1   # exclude terminating newline
        if col > line_len: col = line_len
        if col < 0: col = 0
        cur = self.textCursor()
        cur.setPosition(block.position() + col)
        self.setTextCursor(cur)

    def _replace_char_at(self, line_idx, col, ch):
        """Overwrite a single character at (line, col) with ch.
        Used by the ASCII<->hex sync to update the OTHER side after
        the user types on one side."""
        block = self.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        cur = self.textCursor()
        cur.setPosition(block.position() + col)
        cur.deleteChar()
        cur.insertText(ch)

    def _byte_value_from_hex(self, line_idx, byte_idx):
        """Read the current byte value (0..255) from the hex pair
        at (line, byte_idx) by parsing the two hex digits in place.
        Returns None if the digits aren't valid hex."""
        block = self.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return None
        line_text = block.text()
        d1, d2 = self.hex_col_for_pair(byte_idx)
        if d2 >= len(line_text):
            return None
        try:
            return int(line_text[d1:d2+1], 16)
        except ValueError:
            return None

    def _sync_ascii_for_byte(self, line_idx, byte_idx):
        """After a hex digit was edited, update the ASCII char on
        the right of the same line to reflect the new byte value.
        Non-printable bytes (< 0x20 or >= 0x7f) display as '.'."""
        v = self._byte_value_from_hex(line_idx, byte_idx)
        if v is None:
            return
        ch = chr(v) if 0x20 <= v < 0x7f else '.'
        col = self.ascii_col_for_byte(byte_idx)
        self._sync_in_progress = True
        try:
            self._replace_char_at(line_idx, col, ch)
        finally:
            self._sync_in_progress = False

    def _sync_hex_for_byte(self, line_idx, byte_idx, byte_val):
        """After an ASCII char was typed, update both hex digits on
        the left to reflect the new byte value."""
        d1, d2 = self.hex_col_for_pair(byte_idx)
        hex_str = f"{byte_val:02x}"
        self._sync_in_progress = True
        try:
            self._replace_char_at(line_idx, d1, hex_str[0])
            self._replace_char_at(line_idx, d2, hex_str[1])
        finally:
            self._sync_in_progress = False

    # ---- key handling -------------------------------------------------
    def keyPressEvent(self, ev):
        if not self.hex_edit_mode:
            return super().keyPressEvent(ev)
        from PyQt6.QtCore import Qt as _Qt
        key = ev.key()
        text = ev.text()

        # Navigation keys: let Qt move the cursor, then snap to a
        # valid editable column if it landed on chrome.
        nav_keys = {
            _Qt.Key.Key_Left, _Qt.Key.Key_Right,
            _Qt.Key.Key_Up,   _Qt.Key.Key_Down,
            _Qt.Key.Key_Home, _Qt.Key.Key_End,
            _Qt.Key.Key_PageUp, _Qt.Key.Key_PageDown,
        }
        if key in nav_keys:
            super().keyPressEvent(ev)
            line_idx, col = self._cursor_line_col()
            if key in (_Qt.Key.Key_Left, _Qt.Key.Key_Up,
                         _Qt.Key.Key_Home, _Qt.Key.Key_PageUp):
                direction = -1
            else:
                direction = 1
            new_col = self._snap_to_editable(line_idx, col, direction)
            if new_col is not None and new_col != col:
                self._set_cursor_line_col(line_idx, new_col)
            return

        # Modifier-only keys: pass through.
        if key in (_Qt.Key.Key_Shift, _Qt.Key.Key_Control,
                     _Qt.Key.Key_Alt, _Qt.Key.Key_Meta):
            super().keyPressEvent(ev)
            return

        # Allow Ctrl+C (copy). Block paste/cut/undo - they would
        # change buffer length.
        if ev.modifiers() & _Qt.KeyboardModifier.ControlModifier:
            if key == _Qt.Key.Key_C:
                super().keyPressEvent(ev)
            return

        line_idx, col = self._cursor_line_col()
        target = self._byte_index_at(line_idx, col)
        if target is None:
            # Cursor sits on chrome - snap to the nearest editable
            # column, this keypress is otherwise lost.
            new_col = self._snap_to_editable(line_idx, col, direction=1)
            if new_col is not None:
                self._set_cursor_line_col(line_idx, new_col)
            return
        byte_idx, side = target

        # Verify byte_idx is within the valid range for this line.
        if byte_idx >= self._max_pair_on_line(line_idx):
            return

        if side == 'hex':
            # Only accept a hex digit on the hex side.
            if not (len(text) == 1 and text in "0123456789abcdefABCDEF"):
                return
            self._handle_hex_input(line_idx, col, byte_idx, text.lower())
        else:  # 'ascii'
            # Accept any printable character on the ASCII side.
            # We check ev.text() so modifier-only events without
            # printable output (Caps Lock, etc.) don't slip in.
            if not (len(text) == 1 and 0x20 <= ord(text) < 0x7f):
                return
            self._handle_ascii_input(line_idx, byte_idx, text)

    def _handle_hex_input(self, line_idx, col, byte_idx, ch):
        """User typed a hex digit on the hex side. Replace the digit
        under the cursor, sync the matching ASCII char on the right,
        advance the cursor to the next hex position."""
        # Replace the digit under the cursor.
        cur = self.textCursor()
        cur.deleteChar()
        cur.insertText(ch)
        # Update the ASCII side to reflect the new byte value.
        self._sync_ascii_for_byte(line_idx, byte_idx)
        # Advance the cursor: deleteChar+insertText leaves it
        # positioned AFTER the inserted character, which is digit 1
        # if the user was on digit 0, or in the chrome after digit 1
        # if they were on digit 1. Snap forward to land on the next
        # valid hex position.
        new_line, new_col = self._cursor_line_col()
        info = self.col_to_pair(new_col)
        if info is None:
            # We landed past the end of a pair - find next pair.
            snap = self._snap_to_editable(
                new_line, new_col, direction=1, side='hex')
            if snap is not None and snap > new_col:
                self._set_cursor_line_col(new_line, snap)
            elif new_line + 1 < self._line_count:
                # End of line - move to start of next line's hex.
                d1, _ = self.hex_col_for_pair(0)
                self._set_cursor_line_col(new_line + 1, d1)

    def _handle_ascii_input(self, line_idx, byte_idx, ch):
        """User typed a printable character on the ASCII side.
        Replace the ASCII char, update both hex digits to the new
        byte value, advance the cursor one position right."""
        v = ord(ch)
        # Replace ASCII column.
        col = self.ascii_col_for_byte(byte_idx)
        self._replace_char_at(line_idx, col, ch)
        # Update hex side.
        self._sync_hex_for_byte(line_idx, byte_idx, v)
        # Advance: next byte's ASCII column, or wrap to next line's
        # ASCII column 0.
        next_byte = byte_idx + 1
        max_b = self._max_pair_on_line(line_idx)
        if next_byte >= max_b:
            if line_idx + 1 < self._line_count:
                self._set_cursor_line_col(
                    line_idx + 1, self.ascii_col_for_byte(0))
            else:
                # End of file - leave cursor on the just-edited char.
                self._set_cursor_line_col(line_idx, col)
        else:
            self._set_cursor_line_col(
                line_idx, self.ascii_col_for_byte(next_byte))

    # Block any kind of paste/drop while in edit mode.
    def insertFromMimeData(self, source):
        if self.hex_edit_mode:
            return    # silently refuse
        super().insertFromMimeData(source)

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        if not self.hex_edit_mode:
            return
        # Snap the click to the nearest editable column - either
        # hex or ASCII, whichever is closer.
        line_idx, col = self._cursor_line_col()
        target = self._byte_index_at(line_idx, col)
        if target is None:
            new_col = self._snap_to_editable(
                line_idx, col, direction=1)
            if new_col is not None:
                self._set_cursor_line_col(line_idx, new_col)


class HexReader(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Hex Read: {self.path.name}")
        self.resize(820, 600)
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self.file_size = self.path.stat().st_size
        title = QLabel(f" Hex Read: {self.path.name}  ({self.file_size} bytes) ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        nav = QHBoxLayout()
        nav.setSpacing(2)
        self.lbl_pos = QLabel(" Offset: 0 ")
        self.lbl_pos.setStyleSheet(INFOBAR_QSS)
        nav.addWidget(self.lbl_pos)
        for label, delta in [("<<<", -0x10000), ("<<", -0x1000), ("<", -0x100),
                             (">", 0x100), (">>", 0x1000), (">>>", 0x10000)]:
            btn = QPushButton(label)
            btn.setStyleSheet(button_qss("blue"))
            btn.setFixedWidth(50)
            btn.clicked.connect(lambda chk, d=delta: self._move(d))
            nav.addWidget(btn)
        btn_goto = QPushButton("Goto")
        btn_goto.setStyleSheet(button_qss("orange"))
        btn_goto.setFixedWidth(60)
        btn_goto.clicked.connect(self._goto)
        nav.addWidget(btn_goto)
        # Edit toggle: flips the text edit's read-only state and
        # enables Save. Locked behind a confirmation toggle so a
        # stray click doesn't put the user in a position where
        # they're editing live binary data unintentionally.
        self.btn_edit = QPushButton("Edit")
        self.btn_edit.setStyleSheet(button_qss("yellow"))
        self.btn_edit.setFixedWidth(60)
        self.btn_edit.setCheckable(True)
        self.btn_edit.toggled.connect(self._toggle_edit)
        nav.addWidget(self.btn_edit)
        # Save: only enabled while in edit mode AND the buffer is
        # dirty. Writes the current page (the bytes shown in the
        # editor) back to the file at self.offset, after creating
        # a one-shot .bak backup the FIRST time the file is saved
        # in this dialog session.
        self.btn_save = QPushButton("Save")
        self.btn_save.setStyleSheet(button_qss("orange"))
        self.btn_save.setFixedWidth(60)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        nav.addWidget(self.btn_save)
        nav.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setFixedWidth(70)
        btn_close.clicked.connect(self._on_close)
        nav.addWidget(btn_close)
        layout.addLayout(nav)

        self.text = _HexEditWidget()
        self.text.setReadOnly(True)
        self.text.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {C.BLACK};
                color: {C.WHITE};
                font-family: "Topaz-8", "Topaz", "Courier New", monospace;
                font-size: 12px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.SELECTED_FG};
            }}
            {SCROLLBAR_QSS}
        """)
        self.text.setFont(get_topaz_font(10))
        # Track edits so Save only lights up when there's actually
        # something to save.
        self.text.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text, 1)

        self.page_size = 0x1000
        self.offset = 0
        # Edit-mode state
        self._edit_mode = False
        self._dirty = False              # text differs from rendered page
        self._suppress_dirty = False     # set while we render programmatically
        self._backup_made = False        # one .bak per dialog session
        self._render()

    def _render(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(self.page_size)
        except Exception as e:
            self.text.setPlainText(f"<read error: {e}>"); return
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(47)
            hex_part = hex_part[:23] + " " + hex_part[23:]
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{self.offset + i:08x}  {hex_part}  {ascii_part}")
        # Tell the hex-edit widget how many lines we have and how
        # many bytes are on the last line - it uses these to clamp
        # cursor movement / hex input to actual file bytes (no
        # editing past EOF on a partial last line).
        if isinstance(self.text, _HexEditWidget):
            self.text._line_count = len(lines)
            if data:
                last = len(data) % 16
                self.text._last_line_bytes = last if last else 16
            else:
                self.text._last_line_bytes = 0
        # _suppress_dirty prevents textChanged from flagging this
        # programmatic update as a user edit (it would otherwise
        # light up Save immediately after every page navigation).
        self._suppress_dirty = True
        try:
            self.text.setPlainText("\n".join(lines))
        finally:
            self._suppress_dirty = False
        self._dirty = False
        self.btn_save.setEnabled(False)
        self.lbl_pos.setText(f" Offset: 0x{self.offset:08x} / 0x{self.file_size:08x} ")

    def _move(self, delta):
        if self._dirty and not self._confirm_discard("page navigation"):
            return
        new_off = max(0, min(max(self.file_size - 1, 0), self.offset + delta))
        new_off -= (new_off % 16)
        self.offset = new_off
        self._render()

    def _goto(self):
        if self._dirty and not self._confirm_discard("Goto"):
            return
        val, ok = QInputDialog.getText(self, "Goto offset", "Offset (hex 0x... or dec):")
        if not ok: return
        val = val.strip()
        try:
            off = int(val, 16) if val.lower().startswith("0x") else int(val)
            off = max(0, min(max(self.file_size - 1, 0), off))
            off -= (off % 16)
            self.offset = off
            self._render()
        except Exception as e:
            QMessageBox.warning(self, "Goto", str(e))

    # ---- edit mode -----------------------------------------------------
    def _toggle_edit(self, checked):
        """Flip between view and edit mode.

        When entering edit mode we just unlock the QPlainTextEdit;
        the existing rendered hex is now editable. The Save button
        stays grey until the user actually changes something
        (textChanged -> _dirty -> Save enabled).

        When LEAVING edit mode with unsaved changes, prompt before
        discarding; revert the buffer by re-rendering. This matches
        what most hex editors (HxD, 010) do: edit-on-toggle,
        explicit save.
        """
        if checked:
            self._edit_mode = True
            self.text.setReadOnly(False)
            if isinstance(self.text, _HexEditWidget):
                self.text.hex_edit_mode = True
                # Position the cursor on the first hex digit of
                # the page so the user can start typing right away.
                d1, _ = self.text.hex_col_for_pair(0)
                self.text._set_cursor_line_col(0, d1)
            self.btn_edit.setText("View")
            self.lbl_pos.setText(
                f" Offset: 0x{self.offset:08x} / 0x{self.file_size:08x}"
                f"  -- EDIT MODE -- ")
        else:
            # Leaving edit mode
            if self._dirty:
                if not self._confirm_discard("switching to view"):
                    # Re-check the button so the visual state stays
                    # in sync with our model.
                    self.btn_edit.blockSignals(True)
                    self.btn_edit.setChecked(True)
                    self.btn_edit.blockSignals(False)
                    return
                # User confirmed discard - re-render to restore the
                # on-disk content.
                self._render()
            self._edit_mode = False
            self.text.setReadOnly(True)
            if isinstance(self.text, _HexEditWidget):
                self.text.hex_edit_mode = False
            self.btn_edit.setText("Edit")
            self.btn_save.setEnabled(False)
            self.lbl_pos.setText(
                f" Offset: 0x{self.offset:08x} / 0x{self.file_size:08x} ")

    def _on_text_changed(self):
        """Called on any QPlainTextEdit change. We use _suppress_dirty
        to ignore programmatic re-renders (page navigation, save
        completion) so Save only lights up for actual user edits."""
        if self._suppress_dirty: return
        if not self._edit_mode: return
        self._dirty = True
        self.btn_save.setEnabled(True)

    def _save(self):
        """Parse the current editor buffer back into bytes and write
        them to the file at self.offset. Creates a one-shot .bak
        backup the first time we save during this dialog session.

        Strict parser: each non-empty line must start with the
        original 8-hex-digit offset, then 16 hex byte pairs (with
        the half-line space). The ASCII column is allowed but
        ignored; we only trust the hex side. Any line that doesn't
        parse cleanly aborts the save with an error (no partial
        writes).
        """
        if not self._dirty:
            return
        try:
            new_bytes = self._parse_editor_to_bytes()
        except ValueError as e:
            QMessageBox.warning(
                self, "Hex save",
                f"Cannot parse edited bytes:\n{e}\n\n"
                f"Fix the line and try again, or toggle Edit off to "
                f"discard your changes.")
            return
        # Make a one-shot backup before the first write of this
        # session. Successive saves of the same file go to the same
        # .bak target - we deliberately don't keep a chain of
        # backups, just a single safety net.
        if not self._backup_made:
            try:
                self._make_backup()
                self._backup_made = True
            except Exception as e:
                # Ask the user whether to proceed without a backup.
                # Their choice; they're in a hex editor.
                ret = QMessageBox.question(
                    self, "Hex save",
                    f"Could not create backup:\n{e}\n\n"
                    f"Save anyway? (without backup)",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if ret != QMessageBox.StandardButton.Yes:
                    return
        # Write the bytes back at self.offset. Use 'r+b' so we
        # don't truncate the file - we're just patching a window.
        try:
            with open(self.path, "r+b") as f:
                f.seek(self.offset)
                f.write(new_bytes)
        except Exception as e:
            QMessageBox.warning(self, "Hex save",
                                f"Write failed:\n{e}")
            return
        # Re-render so the offset column is exact and any
        # whitespace differences settle. Also clears _dirty.
        self._render()
        # We're still in edit mode; user can keep going.
        if self._edit_mode:
            self.text.setReadOnly(False)
            self.lbl_pos.setText(
                f" Offset: 0x{self.offset:08x} / 0x{self.file_size:08x}"
                f"  -- saved -- ")

    def _parse_editor_to_bytes(self):
        """Convert the QPlainTextEdit buffer back to bytes.

        Each line we render has the layout:
          OFFSET<2sp>HEX_PAIRS_LEFT_8<sp>HEX_PAIRS_RIGHT_8<2sp>ASCII

        For parsing we ignore the offset (it's a comment) and the
        ASCII tail (it's lossy, can't round-trip non-printable
        bytes). We pull 16 hex byte tokens from columns 10..58 of
        each line and concatenate.
        """
        out = bytearray()
        text = self.text.toPlainText()
        lines = text.split('\n')
        # Strip a possible trailing empty line from the edit buffer
        # without losing intentional empty lines mid-buffer.
        while lines and not lines[-1].strip():
            lines.pop()
        for line_idx, line in enumerate(lines):
            if not line.strip():
                continue
            # Take columns 10..58 - that's the hex region in our
            # render format ("OFFSET  " is 10 chars, then 47 chars
            # of hex with one extra space gap between halves = 48,
            # plus a guard of ~10 chars to be safe).
            #   "00000000  ff ee dd cc ... bb aa  ASCII..."
            #   ^0       ^10                    ^58
            if len(line) < 10:
                raise ValueError(
                    f"line {line_idx+1} too short to contain hex data")
            hex_region = line[10:58]
            tokens = hex_region.split()
            if len(tokens) > 16:
                raise ValueError(
                    f"line {line_idx+1}: too many hex bytes "
                    f"({len(tokens)} > 16)")
            for tok in tokens:
                if len(tok) != 2:
                    raise ValueError(
                        f"line {line_idx+1}: bad hex token {tok!r} "
                        f"(must be exactly 2 hex digits)")
                try:
                    out.append(int(tok, 16))
                except ValueError:
                    raise ValueError(
                        f"line {line_idx+1}: not a hex byte: {tok!r}")
        return bytes(out)

    def _make_backup(self):
        """Create a <file>.bak copy. If a .bak already exists from
        a prior session, leave it alone - we don't want to clobber
        a user's actual safety net. Subsequent saves in THIS session
        skip the backup step entirely (self._backup_made guard)."""
        import shutil
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(self.path, bak)

    def _confirm_discard(self, what: str) -> bool:
        """Ask the user whether to lose unsaved hex edits.
        Returns True if the user wants to proceed (discard)."""
        ret = QMessageBox.question(
            self, "Unsaved hex edits",
            f"You have unsaved changes in this page.\n\n"
            f"Discard them and continue with {what}?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ret == QMessageBox.StandardButton.Yes

    def _on_close(self):
        """Close button + window close: prompt about unsaved edits."""
        if self._dirty and not self._confirm_discard("closing"):
            return
        self.accept()

    def closeEvent(self, ev):
        """Window close button (X) goes through the same prompt."""
        if self._dirty and not self._confirm_discard("closing"):
            ev.ignore()
            return
        super().closeEvent(ev)
