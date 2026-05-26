"""File association configuration dialog."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QListWidgetItem, QFileDialog, QComboBox, QGroupBox,
    QInputDialog, QMessageBox, QWidget, QFormLayout,
)

from .palette import (
    C, button_qss, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS,
)
from .file_assoc import DEFAULT_ASSOC
from .config import scaled_font_px


INTERNAL_TYPES = ["auto", "text", "image", "archive", "hex",
                  "c64disasm", "c64emu", "crt_toolkit", "retrogfx",
                  "modplay", "sidplay",
                  "amigaguide"]


class FileAssocDialog(QDialog):
    """
    Left pane: list of extensions (and "*" as wildcard).
    Right pane: viewer and editor config for the selected extension.
    Each handler has:
      - mode: internal / external
      - if internal: type dropdown
      - if external: program path + args field
    """

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("File Associations")
        self.resize(880, 520)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "file_assoc")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(2)

        title = QLabel(
            "  File Associations   "
            "(internal viewers or external programs per extension)  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        body = QHBoxLayout(); body.setSpacing(4)
        root.addLayout(body, 1)

        # --- LEFT: extension list ---
        left = QVBoxLayout(); left.setSpacing(2)
        left.addWidget(QLabel(" Extensions "))
        self.list_ext = QListWidget()
        self.list_ext.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.WHITE}; color: {C.BLACK};
                font-family: "Topaz-8","Courier New",monospace;
                font-size: {scaled_font_px(12)}px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.WHITE};
            }}
            {SCROLLBAR_QSS}
        """)
        self.list_ext.itemSelectionChanged.connect(self._on_ext_selected)
        left.addWidget(self.list_ext, 1)

        lh = QHBoxLayout(); lh.setSpacing(2)
        b_add = QPushButton("Add...")
        b_add.setStyleSheet(button_qss("blue"))
        b_add.clicked.connect(self._add_ext)
        lh.addWidget(b_add)
        b_del = QPushButton("Remove")
        b_del.setStyleSheet(button_qss("red"))
        b_del.clicked.connect(self._remove_ext)
        lh.addWidget(b_del)
        left.addLayout(lh)

        left_wrap = QWidget(); left_wrap.setLayout(left)
        left_wrap.setFixedWidth(220)
        body.addWidget(left_wrap)

        # --- RIGHT: handler config for selected ---
        right = QVBoxLayout(); right.setSpacing(4)
        self.lbl_which = QLabel(" Select an extension ")
        self.lbl_which.setStyleSheet(
            f"QLabel {{ background-color: {C.ACTIVE_BG}; color: {C.ACTIVE_FG}; "
            f"font-weight: bold; padding: 4px 8px; }}")
        right.addWidget(self.lbl_which)

        self.grp_viewer = self._handler_groupbox("Viewer (F3 / Read)")
        self.grp_editor = self._handler_groupbox("Editor (F4 / Edit)")
        right.addWidget(self.grp_viewer["box"])
        right.addWidget(self.grp_editor["box"])
        right.addStretch()
        body.addLayout(right, 1)

        # --- BOTTOM: OK / Cancel ---
        bottom = QHBoxLayout(); bottom.addStretch()
        b_ok = QPushButton("OK")
        b_ok.setStyleSheet(button_qss("orange")); b_ok.setFixedWidth(100)
        b_ok.clicked.connect(self._save_and_close)
        bottom.addWidget(b_ok)
        b_cancel = QPushButton("Cancel")
        b_cancel.setStyleSheet(button_qss("red")); b_cancel.setFixedWidth(100)
        b_cancel.clicked.connect(self.reject)
        bottom.addWidget(b_cancel)
        root.addLayout(bottom)

        self._populate_list()
        self._current_ext = None
        # Select first item
        if self.list_ext.count() > 0:
            self.list_ext.setCurrentRow(0)

    # ------------------------------------------------------------------

    def _handler_groupbox(self, title):
        """Build a groupbox with mode/type/program/args widgets for one handler."""
        box = QGroupBox(title)
        box.setStyleSheet(f"""
            QGroupBox {{
                background-color: {C.WB_GREY}; color: {C.BLACK};
                font-family: "Topaz-8","Courier New",monospace;
                font-weight: bold;
                border: 1px solid {C.BLACK};
                margin-top: 10px; padding: 6px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 10px; padding: 0 4px;
            }}
            QLabel {{ background: transparent; }}
        """)
        form = QFormLayout(box)
        form.setSpacing(4)

        combo_mode = QComboBox()
        combo_mode.addItems(["internal", "external"])
        combo_mode.setStyleSheet(
            f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"padding: 2px; min-width: 120px; }}")
        form.addRow("Mode:", combo_mode)

        combo_type = QComboBox()
        combo_type.addItems(INTERNAL_TYPES)
        combo_type.setStyleSheet(
            f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"padding: 2px; min-width: 120px; }}")
        form.addRow("Internal type:", combo_type)

        prog_row = QHBoxLayout(); prog_row.setSpacing(2)
        edit_prog = QLineEdit()
        edit_prog.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; }}")
        edit_prog.setPlaceholderText("C:/Program Files/Notepad++/notepad++.exe")
        prog_row.addWidget(edit_prog, 1)
        btn_browse = QPushButton("...")
        btn_browse.setStyleSheet(button_qss("mid"))
        btn_browse.setFixedWidth(40)
        btn_browse.clicked.connect(lambda: self._browse_program(edit_prog))
        prog_row.addWidget(btn_browse)
        prog_wrap = QWidget(); prog_wrap.setLayout(prog_row)
        form.addRow("Program:", prog_wrap)

        edit_args = QLineEdit()
        edit_args.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; }}")
        edit_args.setPlaceholderText('-n "%f"   (use %f for the file path)')
        form.addRow("Args:", edit_args)

        hint = QLabel(
            "  Hint: use %f where the file path should go. "
            "If omitted, it's appended as the last argument.")
        hint.setStyleSheet(
            f"QLabel {{ color: #444; font-size: {scaled_font_px(10)}px; }}")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # Default Hint-Text - der String wird beim Wechsel zwischen
        # Internal-Types (c64emu vs alle anderen) ausgetauscht.
        _default_hint = (
            "  Hint: use %f where the file path should go. "
            "If omitted, it's appended as the last argument.")
        _c64emu_hint = (
            "  Args sind in 'C64 emulator config' (Quopus action "
            "'c64_emu_config') konfiguriert - das Args-Feld hier wird "
            "von 'c64emu' ignoriert. Dort kannst du z.B. "
            "'-binarymonitor -autostart {file}' setzen.")

        fields = {
            "box": box,
            "mode": combo_mode,
            "type": combo_type,
            "program": edit_prog,
            "args": edit_args,
            "hint": hint,
            "_default_hint": _default_hint,
            "_c64emu_hint": _c64emu_hint,
        }

        def on_mode_changed(txt):
            is_internal = (txt == "internal")
            combo_type.setEnabled(is_internal)
            edit_prog.setEnabled(not is_internal)
            edit_args.setEnabled(not is_internal)
            # Wenn internal type c64emu ist: Hint umstellen damit der
            # User weiss wo die Args wirklich liegen.
            _update_hint()
        def on_type_changed(_txt):
            _update_hint()
        def _update_hint():
            if (combo_mode.currentText() == "internal"
                    and combo_type.currentText() == "c64emu"):
                hint.setText(_c64emu_hint)
            else:
                hint.setText(_default_hint)
        combo_mode.currentTextChanged.connect(on_mode_changed)
        combo_type.currentTextChanged.connect(on_type_changed)
        on_mode_changed(combo_mode.currentText())

        return fields

    def _browse_program(self, line_edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select program",
            line_edit.text() or "",
            "Programs (*.exe *.bat *.cmd);;All files (*)" if Path("/").name != "/"
            else "All files (*)")
        if path:
            line_edit.setText(path)

    # ------------------------------------------------------------------

    def _populate_list(self):
        self.list_ext.clear()
        assoc = self.config.get("file_assoc", {})
        # Always show "*" first, then sorted extensions
        keys = sorted(assoc.keys(), key=lambda k: (k != "*", k))
        for k in keys:
            self.list_ext.addItem(k)

    def _on_ext_selected(self):
        # Save previous first
        self._save_current_fields()

        items = self.list_ext.selectedItems()
        if not items:
            self._current_ext = None
            self.lbl_which.setText(" (nothing selected) ")
            return

        ext = items[0].text()
        self._current_ext = ext
        self.lbl_which.setText(f"  Extension: {ext}  ")
        entry = self.config["file_assoc"].get(ext, {})
        self._load_handler(self.grp_viewer, entry.get("viewer") or DEFAULT_ASSOC["*"]["viewer"])
        self._load_handler(self.grp_editor, entry.get("editor") or DEFAULT_ASSOC["*"]["editor"])

    def _load_handler(self, fields, h):
        mode = h.get("mode", "internal")
        fields["mode"].setCurrentText(mode)
        if mode == "internal":
            fields["type"].setCurrentText(h.get("type", "auto"))
            fields["program"].setText("")
            fields["args"].setText("")
        else:
            fields["program"].setText(h.get("program", ""))
            args = h.get("args", [])
            if isinstance(args, list):
                args = " ".join(f'"{a}"' if " " in a else a for a in args)
            fields["args"].setText(args or "")

    def _save_current_fields(self):
        """Write the currently-displayed form back to config for _current_ext."""
        if not self._current_ext:
            return
        entry = self.config["file_assoc"].setdefault(self._current_ext, {})
        entry["viewer"] = self._dump_handler(self.grp_viewer)
        entry["editor"] = self._dump_handler(self.grp_editor)

    def _dump_handler(self, fields):
        mode = fields["mode"].currentText()
        if mode == "internal":
            return {"mode": "internal", "type": fields["type"].currentText()}
        args_str = fields["args"].text().strip()
        import shlex
        try:
            args = shlex.split(args_str, posix=False) if args_str else []
        except ValueError:
            args = [args_str] if args_str else []
        return {
            "mode": "external",
            "program": fields["program"].text().strip(),
            "args": args,
        }

    # ------------------------------------------------------------------

    def _add_ext(self):
        text, ok = QInputDialog.getText(
            self, "Add extension",
            "Extension (e.g. '.rs' - include the dot):")
        if not ok: return
        text = text.strip().lower()
        if not text:
            return
        if not text.startswith(".") and text != "*":
            text = "." + text
        if "file_assoc" not in self.config:
            self.config["file_assoc"] = {}
        if text not in self.config["file_assoc"]:
            self.config["file_assoc"][text] = {
                "viewer": dict(DEFAULT_ASSOC["*"]["viewer"]),
                "editor": dict(DEFAULT_ASSOC["*"]["editor"]),
            }
        self._populate_list()
        # Select the new one
        for i in range(self.list_ext.count()):
            if self.list_ext.item(i).text() == text:
                self.list_ext.setCurrentRow(i); break

    def _remove_ext(self):
        if not self._current_ext:
            return
        if self._current_ext == "*":
            QMessageBox.information(self, "Remove",
                "The wildcard '*' fallback cannot be removed.")
            return
        if QMessageBox.question(
                self, "Remove",
                f"Remove association for {self._current_ext}?"
            ) != QMessageBox.StandardButton.Yes:
            return
        self.config["file_assoc"].pop(self._current_ext, None)
        self._current_ext = None
        self._populate_list()
        if self.list_ext.count() > 0:
            self.list_ext.setCurrentRow(0)

    def _save_and_close(self):
        self._save_current_fields()
        self.accept()
