"""Conversion dialog for ASCII ↔ PETSCII batch file converter."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QFileDialog, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QCheckBox, QWidget, QRadioButton, QButtonGroup, QGroupBox,
    QFormLayout,
)

from .palette import (
    C, button_qss, fmt_size, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS,
)
from .petscii_convert import (
    ascii_to_petscii, petscii_to_ascii, detect_encoding, detect_charset_mode
)


def _petscii_bytes_visual(data: bytes) -> str:
    """Render PETSCII bytes as a readable preview:
    - Printable ASCII-range bytes are shown as text
    - CR ($0D) → newline
    - Control + graphics bytes shown as hex placeholders in <brackets>
    """
    out = []
    for b in data:
        if b == 0x0D:
            out.append('\n')
        elif 0x20 <= b <= 0x7E:
            out.append(chr(b))
        else:
            out.append(f'<{b:02X}>')
    return ''.join(out)


class PetsciiConvertDialog(QDialog):
    """
    Batch-convert selected files between ASCII and PETSCII.
    Shows a file list with detected encoding, lets the user pick
    direction (explicit) or auto-detect, and output extension.
    """

    def __init__(self, paths: list[Path], parent=None):
        super().__init__(parent)
        self.paths = [Path(p) for p in paths if Path(p).is_file()]
        self.setWindowTitle("ASCII ↔ PETSCII converter")
        self.resize(900, 780)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "petscii_convert")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(4)

        title = QLabel(f"  ASCII ↔ PETSCII converter  ({len(self.paths)} file(s))  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        # Direction
        dir_box = QGroupBox("Direction")
        dir_box.setStyleSheet(self._groupbox_qss())
        dir_h = QHBoxLayout(dir_box)
        self.rb_auto  = QRadioButton("Auto-detect per file")
        self.rb_a2p   = QRadioButton("ASCII → PETSCII")
        self.rb_p2a   = QRadioButton("PETSCII → ASCII")
        self.rb_auto.setChecked(True)
        bg = QButtonGroup(self)
        for rb in (self.rb_auto, self.rb_a2p, self.rb_p2a):
            bg.addButton(rb); dir_h.addWidget(rb)
        dir_h.addStretch()
        root.addWidget(dir_box)

        # Mode (charset)
        mode_box = QGroupBox("Charset mode")
        mode_box.setStyleSheet(self._groupbox_qss())
        mode_h = QHBoxLayout(mode_box)
        self.rb_auto_mode    = QRadioButton("Auto-detect")
        self.rb_mixed        = QRadioButton("Mixed")
        self.rb_upper        = QRadioButton("Upper (all UC)")
        self.rb_hybrid       = QRadioButton("Hybrid (UC + LC)")
        self.rb_hybrid_smart = QRadioButton("Smart (sUBS → Subs)")
        self.rb_auto_mode.setChecked(True)
        bg2 = QButtonGroup(self)
        for rb in (self.rb_auto_mode, self.rb_mixed, self.rb_upper,
                   self.rb_hybrid, self.rb_hybrid_smart):
            bg2.addButton(rb); mode_h.addWidget(rb)
        mode_h.addStretch()
        root.addWidget(mode_box)

        # Output filename options
        out_box = QGroupBox("Output")
        out_box.setStyleSheet(self._groupbox_qss())
        out_form = QFormLayout(out_box)

        out_row1 = QHBoxLayout()
        self.rb_ext = QRadioButton("Append extension:")
        self.rb_ext.setChecked(True)
        self.le_ext = self._line_edit(".asc")
        self.le_ext.setFixedWidth(80)
        out_row1.addWidget(self.rb_ext); out_row1.addWidget(self.le_ext)
        out_row1.addStretch()
        w1 = QWidget(); w1.setLayout(out_row1)
        out_form.addRow("", w1)

        out_row2 = QHBoxLayout()
        self.rb_replace = QRadioButton("Replace extension with:")
        self.le_replace_ext = self._line_edit(".asc")
        self.le_replace_ext.setFixedWidth(80)
        out_row2.addWidget(self.rb_replace); out_row2.addWidget(self.le_replace_ext)
        out_row2.addStretch()
        w2 = QWidget(); w2.setLayout(out_row2)
        out_form.addRow("", w2)

        out_row3 = QHBoxLayout()
        self.rb_outdir = QRadioButton("Write to folder:")
        self.le_outdir = self._line_edit("")
        self.le_outdir.setPlaceholderText("(same as source)")
        b_brw = QPushButton("...")
        b_brw.setStyleSheet(button_qss("mid")); b_brw.setFixedWidth(40)
        b_brw.clicked.connect(self._pick_outdir)
        out_row3.addWidget(self.rb_outdir)
        out_row3.addWidget(self.le_outdir, 1)
        out_row3.addWidget(b_brw)
        w3 = QWidget(); w3.setLayout(out_row3)
        out_form.addRow("", w3)

        bg3 = QButtonGroup(self)
        for rb in (self.rb_ext, self.rb_replace, self.rb_outdir):
            bg3.addButton(rb)

        self.cb_overwrite = QCheckBox("Overwrite existing output files")
        out_form.addRow("", self.cb_overwrite)
        root.addWidget(out_box)

        # File list with detected encoding + preview pane
        from PyQt6.QtWidgets import QSplitter, QTextEdit
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setStyleSheet(f"""
            QSplitter::handle {{ background-color: {C.BLACK}; }}
            QSplitter::handle:vertical {{ height: 3px; }}
        """)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["File", "Size", "Detected", "Will become"])
        from quopus_lib.window_state import install_table_state
        install_table_state(self.tree, "petscii_convert:tree")
        self.tree.setRootIsDecorated(False)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {C.LISTER_BG}; color: {C.LISTER_FG};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: 12px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.WHITE};
            }}
            QHeaderView::section {{
                background-color: {C.WB_GREY}; color: {C.BLACK};
                font-family: "Topaz-8",monospace; font-weight: bold;
                padding: 2px 8px; border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.tree.itemSelectionChanged.connect(self._update_preview_pane)
        splitter.addWidget(self.tree)

        # Preview pane with header + text area
        preview_box = QWidget()
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(0)

        self.lbl_preview = QLabel(" Preview - select a file above ")
        self.lbl_preview.setStyleSheet(
            f"QLabel {{ background-color: {C.WB_GREY}; color: {C.BLACK}; "
            f"font-family: 'Topaz-8',monospace; font-weight: bold; "
            f"padding: 2px 8px; border: 1px solid {C.BLACK}; }}")
        pv.addWidget(self.lbl_preview)

        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.txt_preview.setStyleSheet(f"""
            QTextEdit {{
                background-color: #000000; color: #ffffff;
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: 12px; border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        pv.addWidget(self.txt_preview, 1)
        splitter.addWidget(preview_box)

        # Top (tree) gets roughly 35%, bottom (preview) gets 65%
        splitter.setSizes([200, 350])
        root.addWidget(splitter, 1)
        self._populate()
        # Auto-select first file to show initial preview
        if self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        # Update preview column on option change
        for rb in (self.rb_auto, self.rb_a2p, self.rb_p2a):
            rb.toggled.connect(self._update_preview)
            rb.toggled.connect(self._update_preview_pane)
        for rb in (self.rb_ext, self.rb_replace, self.rb_outdir):
            rb.toggled.connect(self._update_preview)
        for rb in (self.rb_auto_mode, self.rb_mixed, self.rb_upper,
                   self.rb_hybrid, self.rb_hybrid_smart):
            rb.toggled.connect(self._update_preview_pane)
        self.le_ext.textChanged.connect(self._update_preview)
        self.le_replace_ext.textChanged.connect(self._update_preview)
        self.le_outdir.textChanged.connect(self._update_preview)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.b_convert = QPushButton("Convert")
        self.b_convert.setStyleSheet(button_qss("orange"))
        self.b_convert.setFixedWidth(110)
        self.b_convert.clicked.connect(self._do_convert)
        btn_row.addWidget(self.b_convert)
        b_cancel = QPushButton("Cancel")
        b_cancel.setStyleSheet(button_qss("red"))
        b_cancel.setFixedWidth(100)
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_cancel)
        root.addLayout(btn_row)

        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        root.addWidget(self.lbl_status)

    def _line_edit(self, text):
        le = QLineEdit(text)
        le.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; }}")
        return le

    def _groupbox_qss(self):
        return (f"QGroupBox {{ background-color: {C.WB_GREY}; color: {C.BLACK}; "
                f"font-family: 'Topaz-8',monospace; font-weight: bold; "
                f"border: 1px solid {C.BLACK}; margin-top: 8px; padding: 4px; }}"
                f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px; "
                f"padding: 0 4px; }}"
                f"QLabel {{ background: transparent; }}"
                f"QRadioButton {{ background: transparent; padding: 2px; }}"
                f"QCheckBox  {{ background: transparent; padding: 2px; }}")

    def _pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder",
                                              self.le_outdir.text() or "")
        if d:
            self.le_outdir.setText(d)
            self.rb_outdir.setChecked(True)

    # ------------------------------------------------------------------

    def _populate(self):
        """Detect encoding for each file and display."""
        self.tree.clear()
        self._detected = {}
        self._detected_mode = {}
        for p in self.paths:
            try:
                data = p.read_bytes()[:8192]
                enc = detect_encoding(data)
                mode = detect_charset_mode(data) if enc == 'petscii' else 'mixed'
                size = p.stat().st_size
            except Exception as e:
                enc = f"error: {e}"; mode = 'mixed'
                size = 0
            self._detected[str(p)] = enc
            self._detected_mode[str(p)] = mode
            detect_str = enc.upper()
            if enc == 'petscii':
                detect_str += f" ({mode})"
            it = QTreeWidgetItem([p.name, fmt_size(size), detect_str, ""])
            it.setData(0, Qt.ItemDataRole.UserRole, p)
            self.tree.addTopLevelItem(it)

        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._update_preview()

    def _direction_for(self, path):
        """Resolve the direction for a given source path based on radio buttons."""
        if self.rb_a2p.isChecked(): return 'a2p'
        if self.rb_p2a.isChecked(): return 'p2a'
        # auto
        enc = self._detected.get(str(path), 'ascii')
        return 'p2a' if enc == 'petscii' else 'a2p'

    def _output_path(self, path: Path, direction):
        """Compute the target filename for a given source."""
        # folder
        if self.rb_outdir.isChecked() and self.le_outdir.text().strip():
            out_dir = Path(self.le_outdir.text().strip())
        else:
            out_dir = path.parent

        # default extension if user left it empty
        default_ext = '.pet' if direction == 'a2p' else '.asc'

        if self.rb_replace.isChecked():
            new_ext = (self.le_replace_ext.text().strip() or default_ext)
            if not new_ext.startswith('.'): new_ext = '.' + new_ext
            stem = path.stem
            return out_dir / (stem + new_ext)
        else:
            # Append mode (or outdir-only = appends too, with sensible ext)
            add_ext = (self.le_ext.text().strip() or default_ext)
            if not add_ext.startswith('.'): add_ext = '.' + add_ext
            return out_dir / (path.name + add_ext)

    def _update_preview(self):
        """Refresh the 'Will become' column for each row."""
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            p = it.data(0, Qt.ItemDataRole.UserRole)
            direction = self._direction_for(p)
            out = self._output_path(p, direction)
            arrow = "→ PET" if direction == 'a2p' else "→ ASC"
            it.setText(3, f"{arrow}  {out.name}")

    # ------------------------------------------------------------------

    def _mode_for(self, path):
        """Pick the charset mode for a given source."""
        if self.rb_upper.isChecked():        return 'upper'
        if self.rb_mixed.isChecked():        return 'mixed'
        if self.rb_hybrid.isChecked():       return 'hybrid'
        if self.rb_hybrid_smart.isChecked(): return 'hybrid-smart'
        # auto: per-file
        return self._detected_mode.get(str(path), 'mixed')

    def _update_preview_pane(self):
        """Show a live preview of the selected file converted with
        the current settings."""
        items = self.tree.selectedItems()
        if not items:
            # Auto-select first file if nothing picked
            if self.tree.topLevelItemCount() > 0:
                self.tree.setCurrentItem(self.tree.topLevelItem(0))
                return
            self.txt_preview.setPlainText("")
            self.lbl_preview.setText(" Preview - no file selected ")
            return
        it = items[0]
        p = it.data(0, Qt.ItemDataRole.UserRole)
        if not p:
            return
        direction = self._direction_for(p)
        mode = self._mode_for(p)
        try:
            data = p.read_bytes()
        except Exception as e:
            self.txt_preview.setPlainText(f"Read error: {e}")
            return
        # Limit preview to first 16 KB
        if len(data) > 16 * 1024:
            data = data[:16 * 1024]
            truncated = True
        else:
            truncated = False
        try:
            if direction == 'a2p':
                converted = ascii_to_petscii(data, mode=mode)
                preview_text = _petscii_bytes_visual(converted)
            else:
                converted = petscii_to_ascii(data, mode=mode)
                preview_text = converted.decode('latin-1', errors='replace')
        except Exception as e:
            preview_text = f"Conversion error: {e}"
            converted = b''

        if truncated:
            preview_text += "\n\n... (preview truncated to 16KB) ..."

        self.txt_preview.setPlainText(preview_text)

        arrow = "ASCII → PETSCII" if direction == 'a2p' else "PETSCII → ASCII"
        self.lbl_preview.setText(
            f" Preview: {p.name}   |   {arrow}   |   mode={mode}   "
            f"|   {len(data)}B → {len(converted)}B ")

    def _do_convert(self):
        n_ok = 0; n_skipped = 0; errors = []

        for p in self.paths:
            direction = self._direction_for(p)
            mode = self._mode_for(p)
            out = self._output_path(p, direction)
            if out.exists() and not self.cb_overwrite.isChecked():
                n_skipped += 1
                errors.append(f"skipped (exists): {out.name}")
                continue
            if out.resolve() == p.resolve():
                errors.append(f"skipped (same file): {p.name}")
                n_skipped += 1
                continue
            try:
                data = p.read_bytes()
                if direction == 'a2p':
                    converted = ascii_to_petscii(data, mode=mode)
                else:
                    converted = petscii_to_ascii(data, mode=mode)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(converted)
                n_ok += 1
            except Exception as e:
                errors.append(f"{p.name}: {e}")

        msg = f"Converted: {n_ok}"
        if n_skipped: msg += f", skipped: {n_skipped}"
        if errors:
            msg += f"\n\n" + "\n".join(errors[:10])
            if len(errors) > 10: msg += f"\n... and {len(errors)-10} more"
        QMessageBox.information(self, "Done", msg)
        self.accept()
