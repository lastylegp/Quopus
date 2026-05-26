"""File compare dialog.

Side-by-side diff viewer for two files. Two display modes:

* Text mode: line-by-line diff using difflib. Equal lines render
  in the normal foreground color, added/removed/changed lines get
  background highlighting and stay aligned across the two panes.

* Hex mode: 16-bytes-per-row hex dump of both files, byte-level
  diff with the differing bytes highlighted in the hex column AND
  the ASCII column. Files of different lengths show the longer
  file's tail in a "only in <left|right>" style.

The two panes scroll in lockstep (vertical AND horizontal) so the
user always sees corresponding content. Statistics line at the
bottom summarises the result (n diff regions, n bytes/lines
identical, etc).
"""
from pathlib import Path
import difflib

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QFont, QColor, QTextCharFormat, QTextCursor, QSyntaxHighlighter
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QMessageBox, QSplitter, QWidget,
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS, SCROLLBAR_QSS,
    button_qss, get_topaz_font, fmt_size,
)
from .config import scaled_font_px


# Colors for diff highlighting. Soft enough not to fight with the
# black-on-white text but distinctive at a glance.
DIFF_BG_LEFT  = "#552222"   # subtle dark red - "only in left / changed"
DIFF_BG_RIGHT = "#225522"   # subtle dark green - "only in right / changed"
DIFF_BG_BOTH  = "#775533"   # both sides differ - dark amber


# ---------------------------------------------------------------------
# Hex-side helpers (kept separate from the View widget for testing)
# ---------------------------------------------------------------------

def _hex_format_line(offset: int, chunk: bytes) -> str:
    """Format one 16-byte (or shorter) chunk as a hex-dump line.
    Same layout as readers.HexReader uses."""
    hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(47)
    hex_part = hex_part[:23] + " " + hex_part[23:]
    ascii_part = "".join(
        chr(b) if 32 <= b < 127 else "." for b in chunk)
    return f"{offset:08x}  {hex_part}  {ascii_part}"


def _hex_diff_columns(chunk_a: bytes, chunk_b: bytes):
    """Return a list of column indices in the rendered hex line where
    the two chunks differ. Used by the highlighter to mark the
    specific hex digits + ASCII char that changed.

    Both hex digits of a differing byte are highlighted, plus the
    matching ASCII column. Equal bytes get no highlight even if
    they sit between two differing bytes."""
    cols = []
    for i in range(max(len(chunk_a), len(chunk_b))):
        a = chunk_a[i] if i < len(chunk_a) else None
        b = chunk_b[i] if i < len(chunk_b) else None
        if a == b:
            continue
        # Hex-pair columns (matches readers._HexEditWidget layout):
        # base = 10 + i*3, +1 if i >= 8
        base = 10 + i * 3 + (1 if i >= 8 else 0)
        cols.append(base)      # high nibble
        cols.append(base + 1)  # low nibble
        # ASCII column at base 60 + i (16-byte rows, no half-gap
        # on the ASCII side).
        cols.append(60 + i)
    return cols


def _read_safely(path: Path, max_bytes: int = 50 * 1024 * 1024):
    """Read a file with a sanity limit so the user doesn't get a
    100GB file dumped into memory by accident. Returns the bytes
    plus a flag saying whether truncation happened."""
    try:
        size = path.stat().st_size
    except Exception:
        return b"", False
    truncated = size > max_bytes
    with open(path, "rb") as f:
        data = f.read(max_bytes)
    return data, truncated


# ---------------------------------------------------------------------
# Compare dialog
# ---------------------------------------------------------------------


class CompareDialog(QDialog):
    """Two-pane file compare. Pass paths to two files; the dialog
    figures out the rest. Either path may be None, in which case
    the dialog displays an error and returns immediately.

    Mode is chosen via toolbar buttons - Text (default for files
    that look like text) or Hex. The user can flip between modes
    at any time.

    Selection model: the user picks the two files BEFORE opening
    the dialog. Two cases are supported by the action wrapper:
      1. Two files tagged/selected in the same panel.
      2. One file in each panel.
    The wrapper resolves these and hands us two Path objects.
    """

    def __init__(self, path_a: Path, path_b: Path, parent=None):
        super().__init__(parent)
        self.path_a = Path(path_a)
        self.path_b = Path(path_b)
        self.setWindowTitle(
            f"Compare: {self.path_a.name}  vs  {self.path_b.name}")
        self.resize(1400, 800)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "compare")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        # Read both files up front. We cap at 50 MB per file to
        # avoid pathological cases; the user gets a warning if they
        # tried to compare anything larger.
        self.bytes_a, trunc_a = _read_safely(self.path_a)
        self.bytes_b, trunc_b = _read_safely(self.path_b)

        layout = QVBoxLayout(self)
        layout.setSpacing(2)

        # Title bar with both filenames + sizes
        title = QLabel(
            f" Compare:  {self.path_a.name} ({fmt_size(len(self.bytes_a))})"
            f"  vs  {self.path_b.name} ({fmt_size(len(self.bytes_b))})")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        # Toolbar: mode toggle + close
        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.btn_text = QPushButton("Text")
        self.btn_text.setCheckable(True)
        self.btn_text.setChecked(True)
        self.btn_text.setStyleSheet(button_qss("blue"))
        self.btn_text.setFixedWidth(70)
        self.btn_text.clicked.connect(lambda: self._set_mode('text'))
        bar.addWidget(self.btn_text)
        self.btn_hex = QPushButton("Hex")
        self.btn_hex.setCheckable(True)
        self.btn_hex.setStyleSheet(button_qss("blue"))
        self.btn_hex.setFixedWidth(70)
        self.btn_hex.clicked.connect(lambda: self._set_mode('hex'))
        bar.addWidget(self.btn_hex)
        # Diff-region navigation: jump to the next/previous diff
        # block (in either mode). The render code populates
        # self._diff_lines with the line indices that contain a
        # difference; these buttons just scroll the panes there.
        self.btn_prev_diff = QPushButton("◀ Prev")
        self.btn_prev_diff.setStyleSheet(button_qss("orange"))
        self.btn_prev_diff.setFixedWidth(70)
        self.btn_prev_diff.setToolTip(
            "Jump to the previous difference (off-screen first, "
            "then wrap)")
        self.btn_prev_diff.clicked.connect(
            lambda: self._jump_to_diff(direction=-1))
        bar.addWidget(self.btn_prev_diff)
        self.btn_next_diff = QPushButton("Next ▶")
        self.btn_next_diff.setStyleSheet(button_qss("orange"))
        self.btn_next_diff.setFixedWidth(70)
        self.btn_next_diff.setToolTip(
            "Jump to the next difference (off-screen first, "
            "then wrap)")
        self.btn_next_diff.clicked.connect(
            lambda: self._jump_to_diff(direction=1))
        bar.addWidget(self.btn_next_diff)
        bar.addStretch()
        # Stats live in the toolbar so they're always visible.
        self.lbl_stats = QLabel("  Loading...  ")
        self.lbl_stats.setStyleSheet(INFOBAR_QSS)
        bar.addWidget(self.lbl_stats)
        bar.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("red"))
        btn_close.setFixedWidth(70)
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        # Pane labels (filenames over each pane).
        labels = QHBoxLayout()
        labels.setSpacing(4)
        lbl_a = QLabel(f" {self.path_a}")
        lbl_a.setStyleSheet(INFOBAR_QSS)
        labels.addWidget(lbl_a, 1)
        lbl_b = QLabel(f" {self.path_b}")
        lbl_b.setStyleSheet(INFOBAR_QSS)
        labels.addWidget(lbl_b, 1)
        layout.addLayout(labels)

        # Side-by-side QPlainTextEdits in a horizontal splitter so
        # the user can adjust the panel widths if one side has
        # noticeably longer lines.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pane_a = self._make_pane()
        self.pane_b = self._make_pane()
        splitter.addWidget(self.pane_a)
        splitter.addWidget(self.pane_b)
        splitter.setSizes([700, 700])
        layout.addWidget(splitter, 1)

        # Lockstep scrolling: when one pane scrolls (vertically OR
        # horizontally), the other follows. We use a guard flag to
        # prevent ping-pong infinite-recursion scrolls.
        self._scroll_guard = False
        self.pane_a.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll('a', 'v', v))
        self.pane_b.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll('b', 'v', v))
        self.pane_a.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll('a', 'h', v))
        self.pane_b.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll('b', 'h', v))

        # List of (start_line, end_line) tuples (inclusive ranges)
        # for each diff region, sorted ascending. Populated by both
        # renderers. Used by the Prev/Next Diff navigation buttons
        # to jump between regions and skip over equal stretches.
        self._diff_blocks: list[tuple[int, int]] = []

        # Choose initial mode based on a quick text-vs-binary
        # heuristic: if both files contain only "looks like text"
        # bytes, default to text mode; otherwise hex.
        if self._looks_like_text(self.bytes_a) and \
           self._looks_like_text(self.bytes_b):
            self._set_mode('text', initial=True)
        else:
            self._set_mode('hex', initial=True)

        if trunc_a or trunc_b:
            QMessageBox.information(
                self, "File compare",
                "One or both files are larger than 50 MB. "
                "Only the first 50 MB will be compared.")

    # ---- pane construction ------------------------------------------
    def _make_pane(self):
        """Build one side's QPlainTextEdit. Black background, mono
        font, read-only, line wrap off so hex lines keep their
        layout."""
        te = QPlainTextEdit()
        te.setReadOnly(True)
        te.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        te.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {C.BLACK};
                color: {C.WHITE};
                font-family: "Topaz-8", "Topaz", "Courier New", monospace;
                font-size: {scaled_font_px(12)}px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.SELECTED_FG};
            }}
            {SCROLLBAR_QSS}
        """)
        te.setFont(get_topaz_font(10))
        # Ensure tabs are short - hex mode never has tabs but text
        # mode might, and a default 8-char tab can throw off the
        # left/right alignment if one file uses tabs and the other
        # doesn't.
        te.setTabStopDistance(28)
        return te

    # ---- mode switching ---------------------------------------------
    def _set_mode(self, mode, initial=False):
        """Switch between 'text' and 'hex' modes. Re-renders both
        panes from scratch. The button state is kept in sync so the
        UI never shows two modes as 'active'."""
        if not initial and mode == getattr(self, '_mode', None):
            return  # no-op
        self._mode = mode
        # Update toggle buttons
        self.btn_text.blockSignals(True)
        self.btn_hex.blockSignals(True)
        self.btn_text.setChecked(mode == 'text')
        self.btn_hex.setChecked(mode == 'hex')
        self.btn_text.blockSignals(False)
        self.btn_hex.blockSignals(False)
        if mode == 'text':
            self._render_text_diff()
        else:
            self._render_hex_diff()

    @staticmethod
    def _looks_like_text(data: bytes, sample: int = 4096) -> bool:
        """Cheap text-vs-binary heuristic: if more than 1% of the
        first 4KB are NUL bytes or non-printable controls (excluding
        tab/CR/LF), call it binary."""
        sample = data[:sample]
        if not sample:
            return True
        bad = 0
        for b in sample:
            if b == 0 or (b < 32 and b not in (9, 10, 13)) or b == 127:
                bad += 1
        return bad * 100 < len(sample)

    # ---- text-mode rendering ----------------------------------------
    def _render_text_diff(self):
        """Render both files in text mode with line-level diff
        highlighting. Uses difflib.SequenceMatcher to align lines;
        equal lines stay aligned across the two panes by inserting
        blank padding rows where the other side is longer."""
        # Decode bytes to text. We try utf-8 strict first, fall
        # back to latin-1 (which never throws) so we always show
        # something even on weird encodings.
        text_a = self._decode_for_text(self.bytes_a)
        text_b = self._decode_for_text(self.bytes_b)
        lines_a = text_a.splitlines()
        lines_b = text_b.splitlines()

        # Run the diff. opcodes() returns a sequence of
        # ('equal'/'replace'/'delete'/'insert', i1, i2, j1, j2)
        # tuples that align lines_a[i1:i2] with lines_b[j1:j2].
        sm = difflib.SequenceMatcher(a=lines_a, b=lines_b, autojunk=False)
        out_a = []
        out_b = []
        # Track which OUTPUT line numbers should be highlighted (in
        # which colour) on each side. Keys are 0-based line indices
        # in the rendered text.
        hl_a = {}      # line_idx -> color
        hl_b = {}
        # Reset diff-block list for this render pass.
        self._diff_blocks = []
        n_diff_blocks = 0
        n_equal_lines = 0
        n_diff_lines_a = 0
        n_diff_lines_b = 0

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                # Lines match on both sides; add them as-is.
                for k in range(i2 - i1):
                    out_a.append(lines_a[i1 + k])
                    out_b.append(lines_b[j1 + k])
                n_equal_lines += (i2 - i1)
            elif tag == 'replace':
                # Both sides have content but it differs. Pad the
                # shorter side with blank lines so they stay
                # aligned visually.
                a_block = lines_a[i1:i2]
                b_block = lines_b[j1:j2]
                pad = max(len(a_block), len(b_block))
                block_start = len(out_a)
                for k in range(pad):
                    a_line = a_block[k] if k < len(a_block) else ""
                    b_line = b_block[k] if k < len(b_block) else ""
                    hl_a[len(out_a)] = DIFF_BG_BOTH
                    hl_b[len(out_b)] = DIFF_BG_BOTH
                    out_a.append(a_line)
                    out_b.append(b_line)
                self._diff_blocks.append(
                    (block_start, len(out_a) - 1))
                n_diff_blocks += 1
                n_diff_lines_a += (i2 - i1)
                n_diff_lines_b += (j2 - j1)
            elif tag == 'delete':
                # Lines exist only on the left.
                block_start = len(out_a)
                for k in range(i1, i2):
                    hl_a[len(out_a)] = DIFF_BG_LEFT
                    hl_b[len(out_b)] = DIFF_BG_LEFT
                    out_a.append(lines_a[k])
                    out_b.append("")
                self._diff_blocks.append(
                    (block_start, len(out_a) - 1))
                n_diff_blocks += 1
                n_diff_lines_a += (i2 - i1)
            elif tag == 'insert':
                # Lines exist only on the right.
                block_start = len(out_a)
                for k in range(j1, j2):
                    hl_a[len(out_a)] = DIFF_BG_RIGHT
                    hl_b[len(out_b)] = DIFF_BG_RIGHT
                    out_a.append("")
                    out_b.append(lines_b[k])
                self._diff_blocks.append(
                    (block_start, len(out_a) - 1))
                n_diff_blocks += 1
                n_diff_lines_b += (j2 - j1)

        self.pane_a.setPlainText("\n".join(out_a))
        self.pane_b.setPlainText("\n".join(out_b))
        self._apply_line_highlights(self.pane_a, hl_a)
        self._apply_line_highlights(self.pane_b, hl_b)

        if n_diff_blocks == 0:
            self.lbl_stats.setText(
                f"  Files are identical "
                f"({n_equal_lines} line(s))  ")
        else:
            self.lbl_stats.setText(
                f"  {n_diff_blocks} diff region(s),  "
                f"{n_equal_lines} matching line(s),  "
                f"{n_diff_lines_a} only-in-left,  "
                f"{n_diff_lines_b} only-in-right  ")

    @staticmethod
    def _decode_for_text(data: bytes) -> str:
        """Decode bytes to text for diffing. Try utf-8 strict;
        fall back to latin-1 (lossless 1-byte mapping) if that
        fails. We don't try to be clever about codepage detection -
        the goal is to show *something* readable, not perfect
        round-tripping."""
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.decode('latin-1', errors='replace')

    @staticmethod
    def _apply_line_highlights(pane, hl_map):
        """Apply per-line background colors to the given pane.
        hl_map maps 0-based line indices to color hex strings."""
        if not hl_map:
            return
        doc = pane.document()
        for line_idx, color_hex in hl_map.items():
            block = doc.findBlockByNumber(line_idx)
            if not block.isValid():
                continue
            cur = QTextCursor(block)
            cur.select(QTextCursor.SelectionType.BlockUnderCursor)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(color_hex))
            cur.mergeCharFormat(fmt)

    # ---- hex-mode rendering -----------------------------------------
    def _render_hex_diff(self):
        """Render both files as 16-byte-per-row hex dumps with
        byte-level diff highlighting. Equal bytes stay normal;
        differing bytes (and their ASCII counterparts) are bg-colored.

        Files of different lengths show the longer file's tail with
        a 'only in <side>' colour; the shorter side gets blank
        padding so vertical alignment holds."""
        a = self.bytes_a
        b = self.bytes_b
        n_lines = max((len(a) + 15) // 16, (len(b) + 15) // 16, 1)

        lines_a = []
        lines_b = []
        # Per-line list of column ranges to highlight, one list per
        # rendered line. Each entry is (start_col, end_col_exclusive,
        # color).
        spans_a = []
        spans_b = []
        # Reset diff-block list for this render pass. We collect a
        # per-line "has any diff" flag while iterating, then collapse
        # consecutive flagged lines into single (start, end) ranges
        # below.
        self._diff_blocks = []
        line_has_diff = []
        n_diff_bytes = 0
        n_equal_bytes = 0
        for line_idx in range(n_lines):
            off = line_idx * 16
            chunk_a = a[off:off + 16]
            chunk_b = b[off:off + 16]
            lines_a.append(_hex_format_line(off, chunk_a))
            lines_b.append(_hex_format_line(off, chunk_b))
            line_spans_a = []
            line_spans_b = []
            this_line_has_diff = False
            # Determine the highlight colour for differing bytes
            # in this row. If a side is missing, use the "only in
            # other side" colour. If both have bytes but differ,
            # use BOTH.
            for i in range(16):
                a_has = i < len(chunk_a)
                b_has = i < len(chunk_b)
                if a_has and b_has:
                    if chunk_a[i] == chunk_b[i]:
                        n_equal_bytes += 1
                        continue
                    color = DIFF_BG_BOTH
                    n_diff_bytes += 1
                elif a_has:
                    color = DIFF_BG_LEFT
                    n_diff_bytes += 1
                elif b_has:
                    color = DIFF_BG_RIGHT
                    n_diff_bytes += 1
                else:
                    continue   # neither side has this byte
                this_line_has_diff = True
                # Hex pair columns + ASCII column.
                base = 10 + i * 3 + (1 if i >= 8 else 0)
                # Highlight 2 hex digits.
                if a_has:
                    line_spans_a.append((base, base + 2, color))
                    line_spans_a.append((60 + i, 61 + i, color))
                if b_has:
                    line_spans_b.append((base, base + 2, color))
                    line_spans_b.append((60 + i, 61 + i, color))
            spans_a.append(line_spans_a)
            spans_b.append(line_spans_b)
            line_has_diff.append(this_line_has_diff)

        # Collapse consecutive diff-lines into ranges. A "block" is
        # a run of one or more consecutive lines where at least one
        # byte differed. This matches what the user perceives as one
        # diff region in hex view.
        i = 0
        while i < len(line_has_diff):
            if line_has_diff[i]:
                start = i
                while i < len(line_has_diff) and line_has_diff[i]:
                    i += 1
                self._diff_blocks.append((start, i - 1))
            else:
                i += 1

        self.pane_a.setPlainText("\n".join(lines_a))
        self.pane_b.setPlainText("\n".join(lines_b))
        self._apply_span_highlights(self.pane_a, spans_a)
        self._apply_span_highlights(self.pane_b, spans_b)

        if n_diff_bytes == 0 and len(a) == len(b):
            self.lbl_stats.setText(
                f"  Files are identical ({n_equal_bytes} bytes)  ")
        else:
            size_note = ""
            if len(a) != len(b):
                size_note = (f"  size diff: "
                               f"{fmt_size(len(a))} vs {fmt_size(len(b))}")
            self.lbl_stats.setText(
                f"  {n_diff_bytes} differing byte(s),  "
                f"{n_equal_bytes} matching byte(s){size_note}  ")

    @staticmethod
    def _apply_span_highlights(pane, spans_per_line):
        """Apply per-column highlighting to a pane. spans_per_line
        is a list (one entry per line) of (start_col, end_col, hex)
        tuples. We use ExtraSelections so the colours stack with
        the pane's normal foreground color cleanly."""
        from PyQt6.QtWidgets import QTextEdit as _QTE
        extras = []
        doc = pane.document()
        for line_idx, spans in enumerate(spans_per_line):
            block = doc.findBlockByNumber(line_idx)
            if not block.isValid():
                continue
            line_pos = block.position()
            line_len = block.length() - 1   # exclude newline
            for start_col, end_col, color_hex in spans:
                if start_col >= line_len:
                    continue
                end = min(end_col, line_len)
                sel = _QTE.ExtraSelection()
                cur = QTextCursor(doc)
                cur.setPosition(line_pos + start_col)
                cur.setPosition(line_pos + end,
                                  QTextCursor.MoveMode.KeepAnchor)
                sel.cursor = cur
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(color_hex))
                sel.format = fmt
                extras.append(sel)
        pane.setExtraSelections(extras)

    # ---- diff navigation -------------------------------------------
    def _visible_line_range(self):
        """Return (first_visible_line, last_visible_line) for the
        active pane (we use pane_a since both scroll in lockstep).
        Used to decide whether a diff is already in view (don't
        scroll) or off-screen (scroll there)."""
        pane = self.pane_a
        # cursorForPosition returns the cursor at a viewport
        # coordinate; with point (0,0) we get the top-left, and
        # with (0, viewport_height-1) we get the bottom-left.
        top_cur = pane.cursorForPosition(pane.viewport().rect().topLeft())
        bot_cur = pane.cursorForPosition(
            pane.viewport().rect().bottomLeft())
        return top_cur.blockNumber(), bot_cur.blockNumber()

    def _jump_to_diff(self, direction: int):
        """Scroll both panes to the next (direction=+1) or previous
        (direction=-1) diff region.

        Strategy:
          1. If there are no diffs, just flash the stats line.
          2. Find the first diff block whose start is OUTSIDE the
             current viewport in the requested direction. That's
             what the user means by "next" - skip stuff already
             visible.
          3. If we hit the end of the list, wrap around to the
             other end so repeated clicks cycle through all blocks.

        Block START is what we land on - that's where the user wants
        their eye to go (top of the next diff). The cursor in pane_a
        gets placed there too so visual cue is clear.
        """
        if not self._diff_blocks:
            # Briefly reflect the absence of diffs; otherwise the
            # buttons feel broken.
            old = self.lbl_stats.text()
            self.lbl_stats.setText("  (no differences to navigate)  ")
            from PyQt6.QtCore import QTimer
            # Parent the timer to `self` so closing the dialog
            # while the timer is still pending kills it cleanly -
            # otherwise the lambda fires 1.5s after close and
            # touches a deleted QLabel ("wrapped C/C++ object of
            # type QLabel has been deleted"). Plus a defensive
            # guard in the lambda in case Qt fires it anyway.
            def _restore():
                try:
                    self.lbl_stats.setText(old)
                except RuntimeError:
                    pass    # widget already gone, nothing to do
            QTimer.singleShot(1500, self, _restore)
            return

        top, bot = self._visible_line_range()
        target = None
        if direction > 0:
            # Next: first block whose START is below the current
            # viewport. If the first viewport-bottom-line is INSIDE
            # a block, we still jump past it because we want the
            # NEXT region, not the current one.
            for start, end in self._diff_blocks:
                if start > bot:
                    target = (start, end); break
            if target is None:
                # Wrap to the first block.
                target = self._diff_blocks[0]
        else:
            # Prev: last block whose END is above the current
            # viewport.
            for start, end in reversed(self._diff_blocks):
                if end < top:
                    target = (start, end); break
            if target is None:
                # Wrap to the last block.
                target = self._diff_blocks[-1]

        self._scroll_to_line(target[0])

    def _scroll_to_line(self, line_idx: int):
        """Scroll both panes so the given line index is at the top
        of the viewport (with a small margin so it's not flush to
        the very top edge). Also moves the text cursor there for a
        clear visual anchor."""
        if line_idx < 0:
            line_idx = 0
        max_blocks = max(self.pane_a.blockCount(),
                           self.pane_b.blockCount()) - 1
        if line_idx > max_blocks:
            line_idx = max_blocks

        # Move the cursor in pane_a; the lockstep scroll handler
        # will mirror to pane_b.
        from PyQt6.QtGui import QTextCursor
        block = self.pane_a.document().findBlockByNumber(line_idx)
        if block.isValid():
            cur = QTextCursor(block)
            self.pane_a.setTextCursor(cur)

        # Set the vertical scroll position directly. centerCursor()
        # would put the diff in the middle which is also fine but
        # most diff tools put it near the top.
        bar_a = self.pane_a.verticalScrollBar()
        # Each block is one line in our render; document's line-
        # spacing translates 1:1 with the scrollbar value range
        # because we're using fixed line wrapping (wrap=NoWrap).
        # So setting scrollbar value = line_idx - margin works.
        margin = 2
        target_value = max(0, line_idx - margin)
        bar_a.setValue(target_value)
        # The lockstep scroll mirror handles pane_b automatically
        # via _sync_scroll, but if a/b have different block counts
        # (shouldn't happen since both renderers produce the same
        # number of lines, but defensive) we set b too.
        bar_b = self.pane_b.verticalScrollBar()
        bar_b.setValue(target_value)

    # ---- scrollbar sync ---------------------------------------------
    def _sync_scroll(self, source: str, axis: str, value: int):
        """Mirror scroll position to the OTHER pane. The guard flag
        prevents the inevitable feedback loop when the mirror itself
        triggers a valueChanged signal."""
        if self._scroll_guard:
            return
        self._scroll_guard = True
        try:
            other = self.pane_b if source == 'a' else self.pane_a
            if axis == 'v':
                bar = other.verticalScrollBar()
            else:
                bar = other.horizontalScrollBar()
            bar.setValue(value)
        finally:
            self._scroll_guard = False
