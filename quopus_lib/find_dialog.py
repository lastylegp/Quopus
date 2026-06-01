# date_time: 2026-06-01 21:44
"""Find Files dialog (Alt+F7) - Total Commander-style.

Three search modes in a single tabbed dialog:
  - Filename: glob pattern (e.g. *.sid, foo*.bin) recursive
  - Text in files: substring match against decoded file content
  - Hex in files: byte-pattern match against raw file content

Search runs in a background QThread so the UI stays responsive.
A live "Searching for ... in <folder>" line is shown above the
results list, updating as the walker descends into subdirectories.

Cancel button stops the search at the next file boundary (typically
within 50ms).

Double-click a result to jump the originating lister to that file
(navigates to the parent directory and selects the file).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QWidget, QListWidget, QCheckBox, QComboBox,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QFileDialog,
)

from .palette import C, button_qss, SCROLLBAR_QSS


# ---------------------------------------------------------------------
# Background searcher
# ---------------------------------------------------------------------
class FindWorker(QThread):
    """Walks a directory tree applying a (path, content)->bool match
    function. Emits status (folder being scanned) and matches as they
    are found, plus a final completion signal.

    The match callback gets passed the path AND the file's content
    (or None if content reading failed). For pure filename searches
    the worker doesn't read content - it passes None and the callback
    decides based on path alone.

    Match callback signature:
        match_fn(path: Path, content: bytes | None) -> bool

    `read_content` controls whether file bytes are loaded; set False
    for pure filename matching to avoid reading every file."""

    status = pyqtSignal(str)             # "Searching in <folder>"
    found = pyqtSignal(str)              # match (path string)
    finished_search = pyqtSignal(int, int)  # (matches, files_scanned)

    # Hard cap on file size to read for content matching - prevents
    # the worker from chewing on a 10 GB log file. Files larger than
    # this are skipped for content searches. Filename searches still
    # see them.
    MAX_CONTENT_BYTES = 16 * 1024 * 1024

    def __init__(self, root: Path, match_fn: Callable,
                  read_content: bool, parent=None):
        super().__init__(parent)
        self._root = root
        self._match = match_fn
        self._read_content = read_content
        self._stop = False
        # Throttle status emits - one per directory entry would
        # flood the event queue. Cache last emit time and emit at
        # most once per 50ms.
        self._last_status_ms = 0

    def stop(self):
        self._stop = True

    def run(self):
        import time
        matches = 0
        scanned = 0
        try:
            stack = [self._root]
            while stack and not self._stop:
                cur = stack.pop()
                # Status update for the current directory. Throttle
                # via wall-clock time rather than per-dir so very
                # deep trees don't spam.
                now = int(time.monotonic() * 1000)
                if now - self._last_status_ms >= 50:
                    self._last_status_ms = now
                    self.status.emit(str(cur))
                try:
                    entries = list(cur.iterdir())
                except (PermissionError, OSError):
                    continue
                # Sort for predictable order (and so the status line
                # walks alphabetically, matching what the user sees
                # in the lister).
                entries.sort(key=lambda p: p.name.lower())
                for e in entries:
                    if self._stop: break
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir():
                            stack.append(e)
                            continue
                        if not e.is_file():
                            continue
                    except (PermissionError, OSError):
                        continue
                    scanned += 1
                    # Read content if needed - skip oversize files
                    content: Optional[bytes] = None
                    if self._read_content:
                        try:
                            sz = e.stat().st_size
                            if sz > self.MAX_CONTENT_BYTES:
                                continue
                            content = e.read_bytes()
                        except (PermissionError, OSError):
                            continue
                    try:
                        if self._match(e, content):
                            matches += 1
                            self.found.emit(str(e))
                    except Exception:
                        # A buggy match callback shouldn't kill the
                        # whole search - just skip the file
                        continue
        except Exception:
            pass
        self.finished_search.emit(matches, scanned)


# ---------------------------------------------------------------------
# Match function builders
# ---------------------------------------------------------------------
def _build_glob_match(pattern: str, case_sensitive: bool):
    """Compile a fnmatch-style pattern into a path matcher. Patterns
    are matched against the file NAME only (not the full path)."""
    import fnmatch
    if not pattern:
        pattern = "*"
    if case_sensitive:
        re_pat = fnmatch.translate(pattern)
        rx = re.compile(re_pat)
    else:
        re_pat = fnmatch.translate(pattern)
        rx = re.compile(re_pat, re.IGNORECASE)
    def match(p: Path, content):
        return bool(rx.match(p.name))
    return match


def _build_text_match(needle: str, glob_pattern: str,
                       case_sensitive: bool):
    """Match files whose decoded content contains `needle`. The
    glob_pattern restricts which files are searched (e.g. only
    *.txt). Both UTF-8 and Latin-1 fallback are tried."""
    import fnmatch
    if not needle:
        return None
    if not glob_pattern:
        glob_pattern = "*"
    glob_rx = re.compile(
        fnmatch.translate(glob_pattern),
        0 if case_sensitive else re.IGNORECASE)
    if case_sensitive:
        needle_b = needle.encode('utf-8', errors='replace')
        # Also try latin-1 bytes for non-utf8 text files
        needle_l = needle.encode('latin-1', errors='replace')
    else:
        needle_lower = needle.lower()
        needle_b = needle_lower.encode('utf-8', errors='replace')
        needle_l = needle_lower.encode('latin-1', errors='replace')
    def match(p: Path, content):
        if not glob_rx.match(p.name):
            return False
        if not content:
            return False
        # Quick byte-level check first (case-sensitive) - most matches
        # will be ASCII so this is the fast path
        if case_sensitive:
            return needle_b in content or needle_l in content
        # Case-insensitive: lowercase the bytes in-place. We only do
        # this for files that pass the glob filter, so it's bounded.
        return (needle_b in content.lower()
                 or needle_l in content.lower())
    return match


def _build_hex_match(hex_str: str, glob_pattern: str):
    """Match files whose raw bytes contain the given hex pattern.
    Whitespace and 0x prefixes in hex_str are ignored."""
    import fnmatch
    cleaned = re.sub(r'[\s,]|0x', '', hex_str, flags=re.IGNORECASE)
    if not cleaned:
        return None
    if len(cleaned) % 2 != 0:
        return None  # odd nibbles
    try:
        needle = bytes.fromhex(cleaned)
    except ValueError:
        return None
    if not glob_pattern:
        glob_pattern = "*"
    glob_rx = re.compile(fnmatch.translate(glob_pattern), re.IGNORECASE)
    def match(p: Path, content):
        if not glob_rx.match(p.name):
            return False
        if not content:
            return False
        return needle in content
    return match


def _build_asm_match(asm_source: str, glob_pattern: str):
    """Build a matcher that assembles `asm_source` as 6502 code,
    then searches for the resulting byte pattern (with wildcards)
    in each file's raw content. Returns (match_fn, hex_preview)
    or raises AsmError on bad assembly.

    The hex preview is displayed in the status line so the user
    can see what byte sequence is actually being searched for
    (very helpful for debugging wildcards / addressing modes)."""
    import fnmatch
    from .asm6502 import (
        assemble_pattern, format_pattern_hex, search_pattern_in_bytes)
    pattern = assemble_pattern(asm_source)  # may raise AsmError
    hex_preview = format_pattern_hex(pattern)
    if not glob_pattern:
        glob_pattern = "*"
    glob_rx = re.compile(fnmatch.translate(glob_pattern), re.IGNORECASE)
    def match(p: Path, content):
        if not glob_rx.match(p.name):
            return False
        if not content:
            return False
        return search_pattern_in_bytes(pattern, content)
    return match, hex_preview


# ---------------------------------------------------------------------
# Find dialog (the actual UI)
# ---------------------------------------------------------------------
class FindDialog(QDialog):
    """Total Commander-style file finder.

    Lives as a child of the active lister; on result double-click
    asks the lister to jump there. Emits a goto signal but holds no
    persistent state - close + re-open starts fresh.
    """

    def __init__(self, lister, parent=None):
        super().__init__(parent)
        self._lister = lister
        self._worker: Optional[FindWorker] = None
        self._matches: list[str] = []
        # The starting directory is the lister's current path - no
        # picker. The user opened this dialog from a specific lister
        # location; that's the location they want to search. If they
        # need to search elsewhere, they can navigate the lister
        # there first and reopen the dialog.
        self._root = Path(self._lister.current_path)
        self.setWindowTitle(f"Find files in: {self._root}")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(700, 520)

        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "find_dialog")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Header showing where we're searching. Editable line + a
        # browse button, so you can search a parent (or any other)
        # directory without first navigating the lister there.
        # Total Commander has the same pattern (the "Suchen in"
        # field with the >> picker).
        root_row = QHBoxLayout()
        root_row.setSpacing(4)
        root_label = QLabel("Search in:")
        root_label.setStyleSheet(
            f"QLabel {{ color: {C.BLACK}; "
            f"font-family: 'Topaz','Courier New',monospace; }}")
        root_row.addWidget(root_label)
        self._root_edit = QLineEdit(str(self._root))
        self._root_edit.setStyleSheet(
            f"QLineEdit {{ background-color: {C.BLACK}; "
            f"color: {C.WHITE}; "
            f"font-family: 'Topaz','Courier New',monospace; "
            f"border: 1px solid {C.BLACK}; padding: 2px; }}")
        self._root_edit.setToolTip(
            "Directory to search in (recursive). Defaults to the "
            "active lister's path; edit or use the picker to "
            "search a parent or different directory.")
        root_row.addWidget(self._root_edit, 1)
        btn_pick = QPushButton(">>")
        btn_pick.setFixedWidth(36)
        btn_pick.setStyleSheet(button_qss("blue"))
        btn_pick.setToolTip("Pick a folder to search in")
        btn_pick.clicked.connect(self._pick_root)
        root_row.addWidget(btn_pick)
        outer.addLayout(root_row)

        # ---- Tabs: Filename / Text / Hex ----
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabBar::tab {{
                background-color: {C.WB_GREY}; color: {C.BLACK};
                padding: 4px 12px;
                border: 1px solid {C.BLACK};
            }}
            QTabBar::tab:selected {{
                background-color: {C.WHITE}; font-weight: bold;
            }}
            QTabWidget::pane {{ border: 1px solid {C.BLACK}; }}
        """)
        outer.addWidget(self._tabs)

        # --- Filename tab ---
        tab_name = QWidget()
        nl = QVBoxLayout(tab_name)
        h = QHBoxLayout()
        h.addWidget(QLabel("Filename pattern (e.g. *.sid, foo*):"))
        nl.addLayout(h)
        self._name_edit = QLineEdit("*")
        nl.addWidget(self._name_edit)
        self._name_case = QCheckBox("Case sensitive")
        nl.addWidget(self._name_case)
        nl.addStretch(1)
        self._tabs.addTab(tab_name, "Filename")

        # --- Text tab ---
        tab_text = QWidget()
        tl = QVBoxLayout(tab_text)
        tl.addWidget(QLabel("Text to find in files:"))
        self._text_edit = QLineEdit()
        tl.addWidget(self._text_edit)
        tl.addWidget(QLabel("Restrict by filename pattern (optional):"))
        self._text_glob = QLineEdit("*")
        tl.addWidget(self._text_glob)
        self._text_case = QCheckBox("Case sensitive")
        tl.addWidget(self._text_case)
        tl.addStretch(1)
        self._tabs.addTab(tab_text, "Text in files")

        # --- Hex tab ---
        tab_hex = QWidget()
        hl = QVBoxLayout(tab_hex)
        hl.addWidget(QLabel(
            "Hex bytes to find (e.g. 'DE AD BE EF' or 'deadbeef'):"))
        self._hex_edit = QLineEdit()
        # Monospace font for hex input
        f = QFont("Cascadia Mono"); f.setStyleHint(QFont.StyleHint.Monospace)
        self._hex_edit.setFont(f)
        hl.addWidget(self._hex_edit)
        hl.addWidget(QLabel("Restrict by filename pattern (optional):"))
        self._hex_glob = QLineEdit("*")
        hl.addWidget(self._hex_glob)
        hl.addStretch(1)
        self._tabs.addTab(tab_hex, "Hex in files")

        # --- Assembly tab (6502/6510) ---
        # Multi-line 6502 source; the assembler turns each line into
        # bytes and we search for that byte pattern. Wildcards work
        # via '?' in operand positions: "lda #?", "jsr $????" etc.
        tab_asm = QWidget()
        al = QVBoxLayout(tab_asm)
        al.addWidget(QLabel(
            "6502/6510 assembly to find (multi-line, '?' for wildcards):"))
        self._asm_edit = QPlainTextEdit()
        f2 = QFont("Cascadia Mono"); f2.setStyleHint(QFont.StyleHint.Monospace)
        self._asm_edit.setFont(f2)
        self._asm_edit.setPlaceholderText(
            "lda #$00\nsta $d021\nrts\n\n"
            "Wildcards: lda #?    matches any LDA-immediate\n"
            "           sta $????  matches any STA-absolute\n"
            "           bne $??    matches any BNE branch")
        # Reasonable initial size - 5 lines of asm fit comfortably
        self._asm_edit.setMaximumHeight(140)
        al.addWidget(self._asm_edit)
        al.addWidget(QLabel("Restrict by filename pattern (optional):"))
        self._asm_glob = QLineEdit("*")
        al.addWidget(self._asm_glob)
        al.addStretch(1)
        self._tabs.addTab(tab_asm, "Assembly (6502)")

        # ---- Action buttons ----
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Start search")
        self._btn_start.setStyleSheet(button_qss("green"))
        self._btn_start.clicked.connect(self._start_search)
        btn_row.addWidget(self._btn_start)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setStyleSheet(button_qss("red"))
        self._btn_cancel.clicked.connect(self._cancel_search)
        self._btn_cancel.setEnabled(False)
        btn_row.addWidget(self._btn_cancel)
        # Total Commander's "feed to listbox" - dump all matches
        # into the originating lister as a virtual flat directory.
        # Disabled until at least one match is in the list.
        self._btn_feed = QPushButton("Feed to listbox")
        self._btn_feed.setStyleSheet(button_qss("purple"))
        self._btn_feed.setToolTip(
            "Show all matches in the lister as a flat list with a "
            "Folder column. Right-click → Close search to exit.")
        self._btn_feed.clicked.connect(self._feed_to_listbox)
        self._btn_feed.setEnabled(False)
        btn_row.addWidget(self._btn_feed)
        btn_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(button_qss("mid"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        outer.addLayout(btn_row)

        # ---- Status line: "Searching for X in <folder>" ----
        self._status = QLabel(" ")
        self._status.setStyleSheet(
            f"QLabel {{ background-color: {C.BLACK}; color: {C.WHITE}; "
            f"padding: 4px; font-family: 'Topaz','Courier New',monospace; }}")
        outer.addWidget(self._status)

        # ---- Results list ----
        self._results = QListWidget()
        self._results.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.WHITE}; color: {C.BLACK};
                font-family: 'Topaz','Courier New',monospace;
                border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self._results.itemDoubleClicked.connect(self._on_result_dbl)
        outer.addWidget(self._results, 1)

        # ---- Counter ----
        self._counter = QLabel("Ready.")
        self._counter.setStyleSheet(f"color: {C.BLACK}; padding: 2px;")
        outer.addWidget(self._counter)

        # Enter in any single-line input field starts the search.
        # The assembly textarea is multi-line so we don't include it
        # here - users press Start search instead.
        for w in (self._name_edit, self._text_edit, self._text_glob,
                  self._hex_edit, self._hex_glob, self._asm_glob):
            w.returnPressed.connect(self._start_search)

    def _pick_root(self):
        """Open a folder picker for the search root."""
        cur = self._root_edit.text().strip() or str(self._root)
        d = QFileDialog.getExistingDirectory(
            self, "Search in folder", cur)
        if d:
            self._root_edit.setText(d)

    def _start_search(self):
        if self._worker is not None:
            return
        # Read the search root from the edit field (the user may
        # have edited it or used the picker since the dialog
        # opened). Falls back to the lister's path on empty input.
        root_text = self._root_edit.text().strip()
        if not root_text:
            root_text = str(Path(self._lister.current_path))
            self._root_edit.setText(root_text)
        root = Path(root_text).expanduser()
        self._root = root
        if not root.is_dir():
            QMessageBox.warning(
                self, "Find files",
                f"Not a directory:\n{root}")
            return
        # Build the matcher based on the active tab
        idx = self._tabs.currentIndex()
        match_fn = None
        read_content = False
        searching_for_label = ""
        # Raw content search term + kind, forwarded to the lister so
        # F3 can pre-fill the viewer's search box. Only text/hex
        # searches set these; filename/asm leave them cleared.
        self._content_term = None
        self._content_kind = None
        if idx == 0:    # Filename
            pattern = self._name_edit.text().strip() or "*"
            match_fn = _build_glob_match(
                pattern, self._name_case.isChecked())
            searching_for_label = f"filename '{pattern}'"
            read_content = False
        elif idx == 1:  # Text
            needle = self._text_edit.text()
            if not needle:
                QMessageBox.warning(self, "Find files",
                                      "Enter text to search for.")
                return
            glob = self._text_glob.text().strip() or "*"
            match_fn = _build_text_match(
                needle, glob, self._text_case.isChecked())
            searching_for_label = (
                f"text '{needle}' in files matching '{glob}'")
            self._content_term = needle
            self._content_kind = "text"
            read_content = True
        elif idx == 2:  # Hex
            hex_str = self._hex_edit.text()
            if not hex_str.strip():
                QMessageBox.warning(self, "Find files",
                                      "Enter hex bytes to search for.")
                return
            glob = self._hex_glob.text().strip() or "*"
            match_fn = _build_hex_match(hex_str, glob)
            if match_fn is None:
                QMessageBox.warning(
                    self, "Find files",
                    "Invalid hex pattern. Use pairs of hex digits "
                    "(spaces optional), e.g. 'DE AD BE EF'.")
                return
            searching_for_label = (
                f"hex '{hex_str.strip()}' in files matching '{glob}'")
            self._content_term = hex_str.strip()
            self._content_kind = "hex"
            read_content = True
        elif idx == 3:  # Assembly
            asm_src = self._asm_edit.toPlainText()
            if not asm_src.strip():
                QMessageBox.warning(self, "Find files",
                                      "Enter 6502 assembly to search for.")
                return
            glob = self._asm_glob.text().strip() or "*"
            try:
                from .asm6502 import AsmError
                match_fn, hex_preview = _build_asm_match(asm_src, glob)
            except AsmError as e:
                # Show the assembly error to the user with line info
                # if available - tells them exactly what to fix.
                line_info = (f" (line {e.line})"
                              if getattr(e, 'line', None) else "")
                QMessageBox.warning(
                    self, "Find files - Assembly error",
                    f"Cannot assemble pattern{line_info}:\n\n{e}\n\n"
                    f"Examples that work:\n"
                    f"  lda #$00\n"
                    f"  sta $d021\n"
                    f"  jsr $????    (any 16-bit target)\n"
                    f"  lda #?       (any immediate value)")
                return
            # First non-empty line of the source, for the status
            # display - keeps the line short on multi-line patterns.
            first_line = next(
                (ln.strip() for ln in asm_src.splitlines()
                 if ln.strip() and not ln.strip().startswith(';')),
                asm_src.strip())
            extra = "" if asm_src.count('\n') < 2 else " ..."
            searching_for_label = (
                f"asm '{first_line}{extra}' [{hex_preview}] "
                f"in files matching '{glob}'")
            read_content = True
        else:
            return
        # Reset results
        self._results.clear()
        self._matches = []
        self._btn_feed.setEnabled(False)
        self._counter.setText("Searching...")
        self._status.setText(f"Searching for {searching_for_label}...")
        # Start worker
        self._worker = FindWorker(root, match_fn, read_content,
                                    parent=self)
        self._worker.status.connect(self._on_status)
        self._worker.found.connect(self._on_found)
        self._worker.finished_search.connect(self._on_finished)
        self._search_label = searching_for_label
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._worker.start()

    def _cancel_search(self):
        if self._worker is None:
            return
        self._worker.stop()
        self._counter.setText("Cancelling...")

    def _on_status(self, folder: str):
        # Show the current folder being scanned. Truncate left if
        # too long so the right side (with the actual current dir)
        # stays visible.
        msg = f"Searching for {self._search_label} in: {folder}"
        if len(msg) > 200:
            msg = msg[:60] + "  ...  " + msg[-130:]
        self._status.setText(msg)

    def _on_found(self, path: str):
        self._matches.append(path)
        item = QListWidgetItem(path)
        self._results.addItem(item)
        self._counter.setText(f"Found: {len(self._matches)}")
        # Enable Feed button as soon as we have at least one result
        self._btn_feed.setEnabled(True)

    def _feed_to_listbox(self):
        """Send all matches to the originating lister as a virtual
        search-results directory. Closes this dialog afterwards so
        the user can see the lister."""
        if not self._matches:
            return
        from pathlib import Path as _P
        files = [_P(p) for p in self._matches]
        # Build a short label for the lister title bar
        label = self._search_label if hasattr(self, '_search_label') \
                  else "files"
        self._lister.set_search_results_fs(
            self._root, label, files,
            content_term=getattr(self, "_content_term", None),
            content_kind=getattr(self, "_content_kind", None))
        self.accept()

    def _on_finished(self, matches: int, scanned: int):
        self._worker = None
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._status.setText(
            f"Done. Searched {scanned} files, found {matches} matches.")
        self._counter.setText(
            f"Found: {matches}  ({scanned} files scanned)")

    def _on_result_dbl(self, item: QListWidgetItem):
        """Jump the lister to the parent directory of the matched
        file and try to highlight that file."""
        path = Path(item.text())
        if not path.exists():
            QMessageBox.information(
                self, "Find files",
                f"File no longer exists:\n{path}")
            return
        target_dir = path.parent if path.is_file() else path
        try:
            self._lister.goto(str(target_dir))
            # Try to scroll to and select the matched file
            self._lister.refresh()
            # The model rebuild is async via a signal chain - defer
            # the selection so the new entries are in the model.
            QTimer.singleShot(50,
                               lambda p=path: self._select_in_lister(p))
        except Exception:
            pass

    def _select_in_lister(self, path: Path):
        try:
            for row, e in enumerate(self._lister.model.entries):
                if Path(e.path) == path:
                    idx = self._lister.model.index(row, 0)
                    self._lister.view.setCurrentIndex(idx)
                    self._lister.view.scrollTo(idx)
                    return
        except Exception:
            pass

    def closeEvent(self, ev):
        # Stop any running search before closing
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(500)
            self._worker = None
        super().closeEvent(ev)
