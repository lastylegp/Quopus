"""Multi-rename tool (batch rename).

Inspired by Total Commander's multi-rename (Ctrl+M). Supports:

  Name template tokens:
    [N]       - full name (without extension)
    [N1]      - first char of name
    [N2-5]    - chars 2 through 5 of name
    [N2-]     - char 2 to end
    [E]       - extension (without dot)
    [C]       - counter (start/step/digits configurable)
    [YMD]     - current date (YYYY-MM-DD)
    [hms]     - current time (HH-MM-SS)
    [Yf]      - file modification year (4-digit)
    [Mf]      - file modification month (2-digit)
    [Df]      - file modification day
    [P]       - parent folder name

  Find/Replace:
    plain text or regex, case-sensitive toggle

  Case:
    unchanged / UPPER / lower / First cap / Each Word cap

  Extension:
    same options as Case, applied separately to the extension part
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QCheckBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QFormLayout, QWidget, QGroupBox, QSpinBox, QMessageBox,
)

from .palette import (
    C, button_qss, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS,
)
from .config import scaled_font_px


CASE_MODES = ["unchanged", "UPPER", "lower", "First cap", "Each Word"]


def _apply_case(s: str, mode: str) -> str:
    if mode == "UPPER":     return s.upper()
    if mode == "lower":     return s.lower()
    if mode == "First cap": return s[:1].upper() + s[1:].lower() if s else s
    if mode == "Each Word": return s.title()
    return s


def _expand_range_token(src: str, spec: str) -> str:
    """spec like '1', '2-5', '2-', '-5'."""
    if not spec: return src
    m = re.match(r'^(\d*)(?:-(\d*))?$', spec)
    if not m: return ''
    start_s, end_s = m.group(1), m.group(2)
    if m.group(0) == start_s:    # single index like '3'
        idx = int(start_s) - 1
        return src[idx:idx+1] if 0 <= idx < len(src) else ''
    start = int(start_s) - 1 if start_s else 0
    end   = int(end_s)       if end_s   else len(src)
    return src[start:end]


def expand_template(tpl: str, path: Path, counter: int) -> str:
    """Expand [N], [E], [C], [YMD], etc. in the template."""
    stem = path.stem
    ext  = path.suffix[1:] if path.suffix.startswith('.') else path.suffix
    parent = path.parent.name
    try:
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime)
    except Exception:
        mtime = datetime.now()
    now = datetime.now()

    out = []
    i = 0
    while i < len(tpl):
        c = tpl[i]
        if c == '[':
            end = tpl.find(']', i + 1)
            if end < 0:
                out.append(c); i += 1; continue
            tok = tpl[i+1:end]
            i = end + 1
            # Counter: [C], [C2], [C0], etc. just emits the counter
            if tok.startswith('C'):
                digits = 1
                m = re.match(r'C(\d+)$', tok)
                if m: digits = int(m.group(1))
                out.append(str(counter).zfill(digits))
            elif tok.startswith('N'):
                out.append(_expand_range_token(stem, tok[1:]))
            elif tok.startswith('E'):
                out.append(_expand_range_token(ext, tok[1:]))
            elif tok == 'P':
                out.append(parent)
            elif tok == 'YMD':
                out.append(now.strftime('%Y-%m-%d'))
            elif tok == 'hms':
                out.append(now.strftime('%H-%M-%S'))
            elif tok == 'Yf':
                out.append(mtime.strftime('%Y'))
            elif tok == 'Mf':
                out.append(mtime.strftime('%m'))
            elif tok == 'Df':
                out.append(mtime.strftime('%d'))
            elif tok == 'hf':
                out.append(mtime.strftime('%H'))
            elif tok == 'mf':
                out.append(mtime.strftime('%M'))
            elif tok == 'sf':
                out.append(mtime.strftime('%S'))
            else:
                # unknown token - pass through
                out.append('[' + tok + ']')
        else:
            out.append(c); i += 1
    return ''.join(out)


class MultiRenameDialog(QDialog):
    """Batch-rename dialog with live preview."""

    def __init__(self, paths: list[Path], parent=None):
        super().__init__(parent)
        self.paths = [Path(p) for p in paths if Path(p).exists()]
        self.setWindowTitle("Multi-rename tool")
        self.resize(920, 640)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "multi_rename")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(4)

        title = QLabel(f"  Multi-rename  ({len(self.paths)} file(s))  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        # --- Name / Extension templates ---
        grp_tpl = QGroupBox("Rename mask")
        grp_tpl.setStyleSheet(self._gb_qss())
        form = QFormLayout(grp_tpl); form.setSpacing(4)

        self.le_name = self._edit("[N]")
        form.addRow("Name mask:", self.le_name)
        self.le_ext  = self._edit("[E]")
        form.addRow("Ext mask:", self.le_ext)

        tok_hint = QLabel(
            "  Tokens: [N] name, [N2-5] slice, [E] ext, [C] counter, "
            "[YMD] today's date, [Yf]/[Mf]/[Df] file mtime y/m/d, "
            "[hms] now time, [P] parent folder  ")
        tok_hint.setStyleSheet(
            f"QLabel {{ color: #444; font-size: {scaled_font_px(10)}px; padding: 2px; }}")
        tok_hint.setWordWrap(True)
        form.addRow("", tok_hint)
        root.addWidget(grp_tpl)

        # --- Counter + Case ---
        row2 = QHBoxLayout(); row2.setSpacing(4)

        grp_c = QGroupBox("Counter [C]")
        grp_c.setStyleSheet(self._gb_qss())
        cf = QFormLayout(grp_c)
        self.sp_start = QSpinBox(); self.sp_start.setRange(0, 999999)
        self.sp_start.setValue(1)
        self.sp_start.setStyleSheet(self._spin_qss())
        cf.addRow("Start:", self.sp_start)
        self.sp_step = QSpinBox(); self.sp_step.setRange(1, 1000)
        self.sp_step.setValue(1)
        self.sp_step.setStyleSheet(self._spin_qss())
        cf.addRow("Step:", self.sp_step)
        self.sp_digits = QSpinBox(); self.sp_digits.setRange(1, 10)
        self.sp_digits.setValue(3)
        self.sp_digits.setStyleSheet(self._spin_qss())
        cf.addRow("Digits:", self.sp_digits)
        row2.addWidget(grp_c)

        grp_case = QGroupBox("Case")
        grp_case.setStyleSheet(self._gb_qss())
        casef = QFormLayout(grp_case)
        self.cb_case_name = QComboBox(); self.cb_case_name.addItems(CASE_MODES)
        self.cb_case_name.setStyleSheet(self._combo_qss())
        casef.addRow("Name:", self.cb_case_name)
        self.cb_case_ext  = QComboBox(); self.cb_case_ext.addItems(CASE_MODES)
        self.cb_case_ext.setStyleSheet(self._combo_qss())
        casef.addRow("Ext:", self.cb_case_ext)
        row2.addWidget(grp_case)

        # --- Search/Replace ---
        grp_sr = QGroupBox("Search && Replace")
        grp_sr.setStyleSheet(self._gb_qss())
        srf = QFormLayout(grp_sr); srf.setSpacing(4)
        self.le_find = self._edit("")
        srf.addRow("Search for:", self.le_find)
        self.le_replace = self._edit("")
        srf.addRow("Replace with:", self.le_replace)
        sr_opts = QHBoxLayout(); sr_opts.setSpacing(6)
        self.cb_regex = QCheckBox("Regex")
        self.cb_case_sens = QCheckBox("Case sensitive")
        self.cb_case_sens.setChecked(True)
        for cb in (self.cb_regex, self.cb_case_sens):
            cb.setStyleSheet(f"QCheckBox {{ background: transparent; }}")
            sr_opts.addWidget(cb)
        sr_opts.addStretch()
        sr_wrap = QWidget(); sr_wrap.setLayout(sr_opts)
        srf.addRow("", sr_wrap)
        row2.addWidget(grp_sr, 2)

        root.addLayout(row2)

        # --- Preview list ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Old name", "→  New name", "Status"])
        from quopus_lib.window_state import install_table_state
        install_table_state(self.tree, "multi_rename:tree")
        self.tree.setRootIsDecorated(False)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {C.LISTER_BG}; color: {C.LISTER_FG};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(12)}px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.WHITE};
            }}
            QHeaderView::section {{
                background-color: {C.WB_GREY}; color: {C.BLACK};
                font-family: 'Topaz-8',monospace; font-weight: bold;
                padding: 2px 8px; border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        root.addWidget(self.tree, 1)

        # --- Bottom ---
        btn_row = QHBoxLayout()
        self.lbl_info = QLabel(" 0 changes, 0 conflicts ")
        self.lbl_info.setStyleSheet(INFOBAR_QSS)
        btn_row.addWidget(self.lbl_info, 1)
        self.b_reset = QPushButton("Reset")
        self.b_reset.setStyleSheet(button_qss("mid"))
        self.b_reset.setFixedWidth(90)
        self.b_reset.clicked.connect(self._reset_fields)
        btn_row.addWidget(self.b_reset)
        self.b_apply = QPushButton("Rename")
        self.b_apply.setStyleSheet(button_qss("orange"))
        self.b_apply.setFixedWidth(110)
        self.b_apply.clicked.connect(self._apply)
        btn_row.addWidget(self.b_apply)
        self.b_close = QPushButton("Close")
        self.b_close.setStyleSheet(button_qss("red"))
        self.b_close.setFixedWidth(100)
        self.b_close.clicked.connect(self.reject)
        btn_row.addWidget(self.b_close)
        root.addLayout(btn_row)

        # --- Live preview ---
        self._debounce = QTimer(self); self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._update_preview)
        for le in (self.le_name, self.le_ext, self.le_find, self.le_replace):
            le.textChanged.connect(self._kick_preview)
        for cb in (self.cb_regex, self.cb_case_sens):
            cb.stateChanged.connect(self._kick_preview)
        for cb in (self.cb_case_name, self.cb_case_ext):
            cb.currentIndexChanged.connect(self._kick_preview)
        for sp in (self.sp_start, self.sp_step, self.sp_digits):
            sp.valueChanged.connect(self._kick_preview)

        self._update_preview()

    # ------------------------------------------------------------------

    def _gb_qss(self):
        return (f"QGroupBox {{ background-color: {C.WB_GREY}; color: {C.BLACK}; "
                f"font-family: 'Topaz-8',monospace; font-weight: bold; "
                f"border: 1px solid {C.BLACK}; margin-top: 8px; padding: 4px; }}"
                f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px; "
                f"padding: 0 4px; }}"
                f"QLabel {{ background: transparent; }}")

    def _edit(self, text=""):
        le = QLineEdit(text)
        le.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; "
            f"font-family: 'Topaz','Courier New',monospace; }}")
        return le

    def _combo_qss(self):
        return (f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
                f"padding: 2px; }}")

    def _spin_qss(self):
        return (f"QSpinBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
                f"padding: 2px; }}")

    def _kick_preview(self):
        self._debounce.start(120)

    # ------------------------------------------------------------------

    def _compute_new_name(self, p: Path, counter: int) -> str:
        """Compute the target filename for a single path."""
        # Expand name and ext templates
        new_stem = expand_template(self.le_name.text(), p, counter)
        new_ext  = expand_template(self.le_ext.text(),  p, counter)

        # Find/replace (applied to full name: stem.ext)
        find = self.le_find.text()
        repl = self.le_replace.text()
        # Expand template tokens in the replacement string too, so users
        # can insert [YMD], [C], etc. in their replacement.
        if repl:
            repl = expand_template(repl, p, counter)
        if find:
            combined = new_stem + ('.' + new_ext if new_ext else '')
            try:
                if self.cb_regex.isChecked():
                    flags = 0 if self.cb_case_sens.isChecked() else re.IGNORECASE
                    combined = re.sub(find, repl, combined, flags=flags)
                else:
                    if self.cb_case_sens.isChecked():
                        combined = combined.replace(find, repl)
                    else:
                        # Case-insensitive non-regex
                        combined = re.sub(re.escape(find), repl, combined,
                                          flags=re.IGNORECASE)
            except re.error:
                pass  # leave original on bad regex
            # split back into stem/ext
            if '.' in combined and not combined.startswith('.'):
                new_stem, _, new_ext = combined.rpartition('.')
            else:
                new_stem, new_ext = combined, ''

        # Case change
        new_stem = _apply_case(new_stem, self.cb_case_name.currentText())
        new_ext  = _apply_case(new_ext,  self.cb_case_ext.currentText())

        return new_stem + ('.' + new_ext if new_ext else '')

    def _update_preview(self):
        self.tree.clear()
        start = self.sp_start.value()
        step  = self.sp_step.value()

        new_names = []
        for i, p in enumerate(self.paths):
            counter = start + i * step
            try:
                new_name = self._compute_new_name(p, counter)
            except Exception as e:
                new_name = f"<error: {e}>"
            new_names.append((p, new_name))

        # Detect conflicts
        seen = {}
        conflict_idx = set()
        for idx, (p, new_name) in enumerate(new_names):
            key = (str(p.parent), new_name.lower())
            if key in seen:
                conflict_idx.add(idx); conflict_idx.add(seen[key])
            else:
                seen[key] = idx

        n_changed = 0
        for idx, (p, new_name) in enumerate(new_names):
            status = ""
            if not new_name or new_name.strip() in ("", "."):
                status = "EMPTY"
            elif idx in conflict_idx:
                status = "CONFLICT"
            elif new_name == p.name:
                status = "unchanged"
            else:
                status = "OK"
                n_changed += 1
                target = p.parent / new_name
                if target.exists() and target != p:
                    status = "EXISTS (will overwrite)"

            it = QTreeWidgetItem([p.name, new_name, status])
            it.setData(0, Qt.ItemDataRole.UserRole, (p, new_name))
            if status == "CONFLICT" or status == "EMPTY":
                # mark in red via foreground role - but delegate may override
                from PyQt6.QtGui import QBrush, QColor
                for col in range(3):
                    it.setForeground(col, QBrush(QColor("#c00000")))
            elif status.startswith("EXISTS"):
                from PyQt6.QtGui import QBrush, QColor
                for col in range(3):
                    it.setForeground(col, QBrush(QColor("#b06000")))
            self.tree.addTopLevelItem(it)

        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.lbl_info.setText(
            f" {n_changed} change(s), {len(conflict_idx)} conflict(s) ")

    # ------------------------------------------------------------------

    def _reset_fields(self):
        self.le_name.setText("[N]")
        self.le_ext.setText("[E]")
        self.le_find.clear(); self.le_replace.clear()
        self.cb_regex.setChecked(False); self.cb_case_sens.setChecked(True)
        self.cb_case_name.setCurrentIndex(0); self.cb_case_ext.setCurrentIndex(0)
        self.sp_start.setValue(1); self.sp_step.setValue(1); self.sp_digits.setValue(3)

    def _apply(self):
        # Gather pending renames from the preview tree
        renames = []
        conflicts = 0
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.text(2) == "CONFLICT" or it.text(2) == "EMPTY":
                conflicts += 1; continue
            if it.text(2) == "unchanged": continue
            p, new_name = it.data(0, Qt.ItemDataRole.UserRole)
            renames.append((p, new_name))

        if not renames:
            QMessageBox.information(self, "Rename", "Nothing to rename.")
            return
        if conflicts:
            if QMessageBox.question(
                self, "Rename",
                f"{conflicts} conflict(s) will be skipped. Continue with "
                f"the remaining {len(renames)} rename(s)?"
                ) != QMessageBox.StandardButton.Yes:
                return

        n_ok = 0; errors = []

        # Two-pass rename via temp names to avoid A→B overwriting each other
        # when a cyclic swap is requested. Phase 1: rename all to .tmpN
        temps = []
        for i, (p, new_name) in enumerate(renames):
            tmp = p.parent / f".__mr_{i}_{p.name}"
            try:
                p.rename(tmp)
                temps.append((tmp, p.parent / new_name))
            except Exception as e:
                errors.append(f"{p.name}: {e}")

        # Phase 2: rename temps to their final name
        for tmp, final in temps:
            try:
                if final.exists() and final != tmp:
                    if final.is_dir():
                        import shutil; shutil.rmtree(final)
                    else:
                        final.unlink()
                tmp.rename(final)
                n_ok += 1
            except Exception as e:
                errors.append(f"{final.name}: {e}")
                # try to restore the temp
                try: tmp.rename(final.parent / final.name.replace(".__mr_", ""))
                except Exception: pass

        msg = f"Renamed {n_ok} file(s)"
        if errors:
            msg += f"\n\nErrors:\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "Rename", msg)
        self.accept()
