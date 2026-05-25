"""
Sample custom module: simple text reader for the currently
selected file in the active lister.

What you can learn from this file:
  - How to read api.selected (the highlighted file(s) in the
    active panel)
  - How to use api.parent_widget for QDialog parenting so your
    window inherits Quopus's window placement / icon / modality
  - How to handle multiple text encodings with a fallback chain
    (real-world text files come in UTF-8, Windows-CP1252, Latin-1
    and the occasional broken mixture)
  - How to refuse to open files that are too big without making
    the user wait for the whole file to load just to see "too
    big" error
  - How to build a small QDialog by hand instead of relying on
    the heavyweight built-in TextReader - showing that custom
    modules can render their own UI when they want to

Copy this file and modify it as a starting point for your own
plugins.
"""

ACTION_NAME = "text_reader_sample"
ACTION_LABEL = "Sample: Text Reader"
ACTION_DESCRIPTION = (
    "Open the file highlighted in the active panel in a small "
    "read-only text viewer. Demo / template for custom modules.")
# This action takes no per-button param. We could use Param to
# carry e.g. a forced encoding ("utf-8", "cp1252", ...) - left
# as an exercise. Set ACTION_PARAM_LABEL to "" to hide the
# placeholder hint in the button editor.
ACTION_PARAM_LABEL = ""


# ---- size limit ---------------------------------------------
# Refuse to read files larger than this. A real text reader
# would stream / memory-map; for a sample plugin, 8 MiB is
# already overkill - if you're reading anything bigger you
# probably want the built-in viewer with its block-loading
# code, not this demo.
MAX_FILE_SIZE = 8 * 1024 * 1024


# Encodings tried in order. UTF-8 first because anything new
# uses it; CP1252 catches most Windows-authored .txt and .nfo
# files; Latin-1 is the universal fallback that NEVER fails on
# decoding (every byte is a valid Latin-1 codepoint) so the
# loop is guaranteed to terminate with SOMETHING readable.
ENCODINGS_TO_TRY = ["utf-8", "cp1252", "latin-1"]


def run(api):
    # ---- 1) Pick the file -----------------------------------
    # api.selected is a list[Path] of items highlighted in the
    # ACTIVE panel. If the user just clicked a file without
    # selecting anything (no Insert key, no Ctrl+click), the
    # current row is usually included anyway because Quopus
    # treats "the focused row" as selected for actions.
    if not api.selected:
        api.notify("Text Reader",
                    "Nothing selected.\n\n"
                    "Highlight a text file in the active panel "
                    "and try again.",
                    kind="warn")
        return

    # If multiple files are selected, just read the first one.
    # A more advanced plugin could open one window per file or
    # ask the user which one - for a sample we keep it simple.
    path = api.selected[0]

    if not path.is_file():
        api.notify("Text Reader",
                    f"Not a regular file:\n{path}",
                    kind="warn")
        return

    # ---- 2) Size check --------------------------------------
    try:
        size = path.stat().st_size
    except OSError as e:
        api.notify("Text Reader",
                    f"Cannot stat file:\n{path}\n{e}",
                    kind="error")
        return

    if size > MAX_FILE_SIZE:
        if not api.ask_yes_no(
                "Large file",
                f"{path.name} is "
                f"{size / 1024 / 1024:.1f} MiB.\n\n"
                f"Reading large files in the sample text reader "
                f"is slow because the whole content is loaded "
                f"into memory at once.\n\n"
                f"Open it anyway?"):
            return

    # ---- 3) Read with encoding fallback ---------------------
    text, used_encoding = _read_with_fallback(path)
    if text is None:
        api.notify("Text Reader",
                    f"Cannot read file:\n{path}\n\n"
                    f"All encodings failed. Is this actually "
                    f"a text file?",
                    kind="error")
        return

    # ---- 4) Show it in a dialog -----------------------------
    # Lazy-import Qt at the LAST moment - if the user never
    # triggers this action, importing the plugin file doesn't
    # need to touch Qt at all. (Quopus loads custom modules at
    # startup, so any import side-effects happen then. Keeping
    # heavyweight imports inside run() is a good habit.)
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
        QLabel, QPushButton, QFileDialog, QLineEdit,
    )
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont, QTextCursor, QKeySequence, QShortcut

    dlg = QDialog(api.parent_widget)
    dlg.setWindowTitle(f"Read: {path.name}")
    dlg.resize(900, 640)
    dlg.setStyleSheet("QDialog { background-color: #999999; }")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(6)

    # --- header bar with file info ---------------------------
    line_count = text.count("\n") + 1
    char_count = len(text)
    header = QLabel(
        f"  {path.name}  ·  {used_encoding}  ·  "
        f"{line_count:,} line(s)  ·  {char_count:,} char(s)  ·  "
        f"{size:,} byte(s)")
    header.setStyleSheet(
        "QLabel { background-color: #2040a0; color: white; "
        "padding: 4px 8px; "
        "font-family: 'Topaz','Courier New',monospace; }")
    layout.addWidget(header)

    # --- find bar (hidden initially, Ctrl+F toggles) ---------
    find_widget = _make_find_bar()
    find_widget.hide()
    layout.addWidget(find_widget)

    # --- the actual text view --------------------------------
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    view.setPlainText(text)
    # Topaz-ish monospace at a comfortable reading size. The
    # built-in TextReader has fancier font handling with
    # per-encoding tweaks (PETSCII / Topaz bitmap); for a
    # sample we just pick one and go.
    font = QFont("Topaz", 11)
    if not font.exactMatch():
        # Topaz not installed on this system - fall back to
        # whatever generic monospace Qt has.
        font = QFont("Courier New", 11)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
    view.setFont(font)
    view.setStyleSheet(
        "QPlainTextEdit { background-color: #1a1a1a; "
        "color: #cccccc; "
        "selection-background-color: #5566ff; "
        "selection-color: white; padding: 6px; }")
    layout.addWidget(view, 1)

    # --- bottom button bar -----------------------------------
    bb = QHBoxLayout()
    bb.setContentsMargins(0, 0, 0, 0)
    bb.setSpacing(6)

    btn_find = QPushButton("Find (Ctrl+F)")
    btn_find.clicked.connect(
        lambda: _toggle_find_bar(find_widget))
    bb.addWidget(btn_find)

    btn_save = QPushButton("Save copy as...")
    btn_save.clicked.connect(
        lambda: _save_copy(dlg, path, text, used_encoding))
    bb.addWidget(btn_save)

    bb.addStretch(1)

    btn_close = QPushButton("Close")
    btn_close.clicked.connect(dlg.accept)
    bb.addWidget(btn_close)

    layout.addLayout(bb)

    # --- wire up Find ----------------------------------------
    edit = find_widget.findChild(QLineEdit, "find_input")
    edit.returnPressed.connect(
        lambda: _find_next(view, edit.text()))
    next_btn = find_widget.findChild(QPushButton, "find_next")
    next_btn.clicked.connect(
        lambda: _find_next(view, edit.text()))

    # Keyboard shortcuts. QShortcut auto-cleans up with the
    # dialog so no leak concerns.
    QShortcut(QKeySequence("Ctrl+F"), dlg, activated=lambda:
              _toggle_find_bar(find_widget))
    QShortcut(QKeySequence("Escape"), dlg, activated=dlg.accept)
    QShortcut(QKeySequence("F3"), dlg, activated=lambda:
              _find_next(view, edit.text()))

    # Modal so the user can't accidentally lose it behind the
    # main window. exec() blocks until closed.
    dlg.exec()


# =====================================================================
# Helpers
# =====================================================================

def _read_with_fallback(path):
    """Try each encoding in ENCODINGS_TO_TRY until one decodes
    successfully. Returns (text, encoding_used) on success or
    (None, None) if everything failed (only possible if the
    file disappears mid-read - latin-1 itself can't fail on
    decode).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    for enc in ENCODINGS_TO_TRY:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    # Belt-and-suspenders: latin-1 should never reach here
    # because it can't fail to decode. But just in case.
    return raw.decode("latin-1", errors="replace"), "latin-1 (lossy)"


def _make_find_bar():
    """Build the inline Find bar (search input + Next button).
    Hidden by default; toggle via Ctrl+F or the Find button."""
    from PyQt6.QtWidgets import (
        QWidget, QHBoxLayout, QLineEdit, QPushButton, QLabel,
    )
    bar = QWidget()
    bl = QHBoxLayout(bar)
    bl.setContentsMargins(0, 0, 0, 0)
    bl.setSpacing(4)
    bl.addWidget(QLabel("Find:"))
    inp = QLineEdit()
    inp.setObjectName("find_input")
    inp.setStyleSheet(
        "QLineEdit { background-color: white; color: black; "
        "padding: 2px 4px; }")
    bl.addWidget(inp, 1)
    nb = QPushButton("Next (F3)")
    nb.setObjectName("find_next")
    bl.addWidget(nb)
    return bar


def _toggle_find_bar(widget):
    """Show/hide the find bar and focus the input when shown."""
    from PyQt6.QtWidgets import QLineEdit
    if widget.isHidden():
        widget.show()
        inp = widget.findChild(QLineEdit, "find_input")
        if inp:
            inp.setFocus()
            inp.selectAll()
    else:
        widget.hide()


def _find_next(view, needle):
    """Forward-search needle in `view`'s text from the current
    cursor position. Wraps to start on miss."""
    if not needle:
        return
    found = view.find(needle)
    if not found:
        # Wrap: move cursor to start and try once more
        from PyQt6.QtGui import QTextCursor
        cursor = view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        view.setTextCursor(cursor)
        view.find(needle)


def _save_copy(parent, source_path, text, used_encoding):
    """Save the loaded text under a user-picked filename. We
    write it back out in UTF-8 because that's the lossless
    superset - if the source was CP1252 the saved copy will
    still represent every original character correctly, and
    modern tools will read it without surprises."""
    from PyQt6.QtWidgets import QFileDialog, QMessageBox
    out_path, _ = QFileDialog.getSaveFileName(
        parent, "Save copy as...",
        f"{source_path.stem}_copy.txt",
        "Text files (*.txt);;All files (*)")
    if not out_path:
        return
    try:
        from pathlib import Path
        Path(out_path).write_text(text, encoding="utf-8")
    except OSError as e:
        QMessageBox.warning(parent, "Save failed",
                            f"Could not write file:\n{e}")
        return
    QMessageBox.information(
        parent, "Saved",
        f"Saved as UTF-8 copy:\n{out_path}\n\n"
        f"(Original was read as {used_encoding}.)")
