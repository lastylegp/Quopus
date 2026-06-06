# date_time: 2026-06-06 23:14
"""Lister colors editor dialog.

Lets the user change the lister panel's background, default file
foreground, directory foreground, and a list of per-extension
color overrides (e.g. .prg = blue, .py = purple). Settings are
saved both to disk (quopus.cfg) and to the live main window
config dict, then applied to all open lister panels via
apply_lister_colors().
"""

LISTER_COLORS_AVAILABLE = False

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
        QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
        QColorDialog, QMessageBox, QGroupBox, QFormLayout,
        QAbstractItemView,
    )
    LISTER_COLORS_AVAILABLE = True
except Exception:
    pass


def _color_swatch_html(hex_color: str) -> str:
    """Tiny HTML preview block for displaying a color sample."""
    return (f'<div style="background:{hex_color};width:24px;'
            f'height:14px;border:1px solid #000;"></div>')


# ---------------------------------------------------------------
# Color presets for one-click theme switching. Each entry is a
# (bg, fg, dir_fg, ext_colors) tuple matching the look-and-feel
# of a well-known file manager's default color scheme. Selected
# via the Preset row at the top of the dialog - clicking a button
# pushes those values into the live form widgets but doesn't save
# until the user hits OK. That mirrors the way Reset-to-defaults
# already works and lets the user back out with Cancel.
# ---------------------------------------------------------------
LISTER_COLOR_PRESETS = {
    "total_commander": {
        "label": "Total Commander",
        "tip": ("Classic Total Commander look - white panel, "
                "black files, dark-blue directories, common "
                "categories tinted (archives brown, images "
                "magenta, audio teal, executables navy)."),
        "bg":     "#ffffff",
        "fg":     "#000000",
        "dir_fg": "#000080",
        "ext_colors": {
            # Executables - navy blue
            ".exe": "#000080", ".com": "#000080",
            ".bat": "#000080", ".cmd": "#000080",
            ".msi": "#000080",
            # Archives - brown
            ".zip": "#806000", ".rar": "#806000",
            ".7z":  "#806000", ".tar": "#806000",
            ".gz":  "#806000", ".bz2": "#806000",
            ".lha": "#806000", ".lzx": "#806000",
            # Images - magenta
            ".jpg": "#a000a0", ".jpeg": "#a000a0",
            ".png": "#a000a0", ".bmp": "#a000a0",
            ".gif": "#a000a0",
            # Audio - teal
            ".mp3": "#008080", ".wav": "#008080",
            ".ogg": "#008080", ".flac": "#008080",
            # Video - olive
            ".mp4": "#806020", ".avi": "#806020",
            ".mkv": "#806020", ".mov": "#806020",
            # Docs - dark red
            ".pdf": "#800000", ".doc": "#800000",
            ".docx": "#800000",
        },
    },
    "double_commander": {
        "label": "Double Commander",
        "tip": ("Default Double Commander palette - bright "
                "white panel, plain black files, blue "
                "directories. Archives lean red, scripts "
                "lean green."),
        "bg":     "#ffffff",
        "fg":     "#000000",
        "dir_fg": "#1a4a8a",
        "ext_colors": {
            # Archives - red
            ".zip": "#a02020", ".rar": "#a02020",
            ".7z":  "#a02020", ".tar": "#a02020",
            ".gz":  "#a02020", ".bz2": "#a02020",
            ".xz":  "#a02020",
            # Scripts / executables - green
            ".sh":  "#208020", ".py":  "#208020",
            ".pl":  "#208020", ".rb":  "#208020",
            ".exe": "#208020", ".bat": "#208020",
            ".com": "#208020", ".cmd": "#208020",
            # Source code - purple
            ".c":   "#6020a0", ".cpp": "#6020a0",
            ".h":   "#6020a0", ".hpp": "#6020a0",
            ".java": "#6020a0", ".cs": "#6020a0",
            ".rs":  "#6020a0", ".go":  "#6020a0",
            # Images - dark cyan
            ".jpg": "#207090", ".jpeg": "#207090",
            ".png": "#207090", ".gif": "#207090",
            ".bmp": "#207090", ".svg": "#207090",
            # Documents - brown
            ".pdf": "#806020", ".doc": "#806020",
            ".docx": "#806020", ".odt": "#806020",
            ".txt": "#505050", ".md": "#505050",
        },
    },
    "midnight_commander": {
        "label": "Midnight Commander",
        "tip": ("Classic MC/Norton terminal look - dark-blue "
                "panel, light files, bold-white directories, "
                "green executables, red archives, cyan media. "
                "Bright on dark, like running it in a console."),
        # The iconic MC dark-blue panel background
        "bg":     "#000080",
        "fg":     "#c0c0c0",   # files in light grey
        "dir_fg": "#ffffff",   # directories in bright white
        "ext_colors": {
            # Executables - bright green (the MC trademark)
            ".exe": "#00ff00", ".com": "#00ff00",
            ".bat": "#00ff00", ".cmd": "#00ff00",
            ".sh":  "#00ff00", ".py":  "#00ff00",
            ".pl":  "#00ff00", ".rb":  "#00ff00",
            # Archives - bright red
            ".zip": "#ff5050", ".rar": "#ff5050",
            ".7z":  "#ff5050", ".tar": "#ff5050",
            ".gz":  "#ff5050", ".bz2": "#ff5050",
            ".xz":  "#ff5050", ".lha": "#ff5050",
            ".lzx": "#ff5050",
            # Images - magenta
            ".jpg": "#ff80ff", ".jpeg": "#ff80ff",
            ".png": "#ff80ff", ".gif": "#ff80ff",
            ".bmp": "#ff80ff", ".svg": "#ff80ff",
            # Audio - bright cyan
            ".mp3": "#80ffff", ".wav": "#80ffff",
            ".ogg": "#80ffff", ".flac": "#80ffff",
            # Video - yellow
            ".mp4": "#ffff80", ".avi": "#ffff80",
            ".mkv": "#ffff80", ".mov": "#ffff80",
            # Source - bright yellow-green
            ".c":   "#c0ff80", ".cpp": "#c0ff80",
            ".h":   "#c0ff80", ".py":  "#c0ff80",
            # Documents - light tan
            ".pdf": "#ffc080", ".txt": "#ffc080",
            ".md":  "#ffc080", ".nfo": "#ffc080",
        },
    },
}


class _ColorButton(QPushButton):
    """Button that shows a color swatch as background and opens
    a QColorDialog on click. Holds the current color as a hex
    string in self.color_hex."""

    def __init__(self, initial: str, parent=None):
        super().__init__(parent)
        self.color_hex = initial or "#000000"
        self.setFixedWidth(110)
        self._apply_style()
        self.clicked.connect(self._pick)

    def _apply_style(self):
        # Pick a contrasting text color so the hex value stays
        # legible against any background.
        try:
            c = QColor(self.color_hex)
            lum = (0.299 * c.red() + 0.587 * c.green()
                   + 0.114 * c.blue())
            fg = "#000000" if lum > 140 else "#ffffff"
        except Exception:
            fg = "#000000"
        self.setStyleSheet(
            f"QPushButton {{ background:{self.color_hex}; "
            f"color:{fg}; border:1px solid #555; "
            f"padding:3px 6px; }}")
        self.setText(self.color_hex)

    def _pick(self):
        c = QColorDialog.getColor(
            QColor(self.color_hex), self, "Pick color")
        if c.isValid():
            self.color_hex = c.name()
            self._apply_style()

    def set_color(self, hex_color: str):
        self.color_hex = hex_color or "#000000"
        self._apply_style()


class ListerColorsDialog(QDialog):
    """Modal editor for the lister color settings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lister colors")
        self.setModal(True)
        self.resize(540, 540)
        self._mw = parent  # main window, may be None

        # Load current values from the live main-window config
        # if available, otherwise from disk.
        cfg = self._read_cfg()
        self._initial_bg = cfg.get("lister_bg", "#a0a0a0")
        self._initial_fg = cfg.get("lister_fg", "#000000")
        self._initial_dir = cfg.get("lister_dir_fg", "#0000cc")
        ext_map = cfg.get("lister_ext_colors") or {}
        if not isinstance(ext_map, dict):
            ext_map = {}
        self._initial_ext = {
            k.lower(): v for k, v in ext_map.items()
            if isinstance(k, str) and isinstance(v, str)}

        outer = QVBoxLayout(self)

        # --- Preset row ---
        # One-click theme switching. Each button writes its
        # preset's bg/fg/dir/ext into the live form widgets
        # without saving - the user still confirms with OK,
        # and Cancel undoes the picture. Tooltips describe
        # the look so the user knows what to expect before
        # clicking.
        g_preset = QGroupBox("Color presets")
        pr = QHBoxLayout(g_preset)
        pr.setSpacing(8)
        info_pr = QLabel("Apply a preset:")
        pr.addWidget(info_pr)
        for key, preset in LISTER_COLOR_PRESETS.items():
            btn = QPushButton(preset["label"])
            btn.setToolTip(preset["tip"])
            # Capture-by-default-arg so the loop variable isn't
            # leaking into the lambda.
            btn.clicked.connect(
                lambda _checked=False, k=key:
                    self._apply_preset(k))
            pr.addWidget(btn)
        pr.addStretch(1)
        outer.addWidget(g_preset)

        # --- Panel colors group ---
        g_panel = QGroupBox("Panel colors")
        form = QFormLayout(g_panel)
        self.btn_bg = _ColorButton(self._initial_bg, self)
        self.btn_fg = _ColorButton(self._initial_fg, self)
        self.btn_dir = _ColorButton(self._initial_dir, self)
        form.addRow("Background:", self.btn_bg)
        form.addRow("Files (default):", self.btn_fg)
        form.addRow("Directories:", self.btn_dir)
        outer.addWidget(g_panel)

        # --- Extension colors group ---
        g_ext = QGroupBox("Per-extension file colors")
        ev = QVBoxLayout(g_ext)
        info = QLabel(
            "Files whose extension matches an entry below use "
            "the listed color; everything else falls back to "
            "the default file color.")
        info.setWordWrap(True)
        ev.addWidget(info)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(
            ["Extension", "Color", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(
            self._on_cell_double_click)
        ev.addWidget(self.table)
        # Buttons under the table: Add / Remove / Edit
        bb = QHBoxLayout()
        b_add = QPushButton("Add...")
        b_add.clicked.connect(self._add_entry)
        b_edit = QPushButton("Edit color")
        b_edit.clicked.connect(self._edit_entry)
        b_del = QPushButton("Remove")
        b_del.clicked.connect(self._remove_entry)
        bb.addWidget(b_add)
        bb.addWidget(b_edit)
        bb.addWidget(b_del)
        bb.addStretch(1)
        ev.addLayout(bb)
        outer.addWidget(g_ext, 1)

        # --- Dialog buttons ---
        bot = QHBoxLayout()
        b_reset = QPushButton("Reset to defaults")
        b_reset.clicked.connect(self._reset_defaults)
        bot.addWidget(b_reset)
        bot.addStretch(1)
        b_ok = QPushButton("OK")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self._save_and_close)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        bot.addWidget(b_ok)
        bot.addWidget(b_cancel)
        outer.addLayout(bot)

        # Fill the table with current extensions
        self._populate_table(self._initial_ext)

    # ------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------
    def _read_cfg(self) -> dict:
        cfg = None
        if self._mw is not None:
            mw_cfg = getattr(self._mw, "config", None)
            if isinstance(mw_cfg, dict):
                cfg = mw_cfg
        if cfg is None:
            try:
                from .config import load_config
                cfg = load_config()
            except Exception:
                cfg = {}
        return cfg

    def _populate_table(self, ext_map: dict):
        self.table.setRowCount(0)
        for ext in sorted(ext_map):
            self._append_row(ext, ext_map[ext])

    def _append_row(self, ext: str, color_hex: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(ext))
        col_item = QTableWidgetItem(color_hex)
        self._paint_color_cell(col_item, color_hex)
        self.table.setItem(row, 1, col_item)
        # Embed a "Pick..." button in column 2 for one-click
        # editing of this row's color, per user request. The
        # double-click handler still works as before; this just
        # adds a more discoverable affordance.
        btn = QPushButton("Pick...")
        btn.setFixedHeight(22)
        btn.clicked.connect(
            lambda _checked, r=row: self._pick_color_for_row(r))
        self.table.setCellWidget(row, 2, btn)

    def _paint_color_cell(self, item, color_hex):
        """Set the swatch background + contrasting text on a
        color-column cell so the user sees the color at a glance."""
        try:
            c = QColor(color_hex)
            item.setBackground(c)
            lum = (0.299 * c.red() + 0.587 * c.green()
                   + 0.114 * c.blue())
            item.setForeground(
                QColor("#000000" if lum > 140 else "#ffffff"))
        except Exception:
            pass

    def _pick_color_for_row(self, row):
        """Open a color picker for the extension at given row.
        Used by the Pick... button in column 2."""
        if row < 0 or row >= self.table.rowCount():
            return
        ext_item = self.table.item(row, 0)
        col_item = self.table.item(row, 1)
        if not (ext_item and col_item):
            return
        ext = ext_item.text()
        cur_hex = col_item.text() or "#808080"
        c = QColorDialog.getColor(
            QColor(cur_hex), self, f"Color for {ext}")
        if c.isValid():
            col_item.setText(c.name())
            self._paint_color_cell(col_item, c.name())

    # ------------------------------------------------------------
    # Row actions
    # ------------------------------------------------------------
    def _add_entry(self):
        from PyQt6.QtWidgets import QInputDialog
        ext, ok = QInputDialog.getText(
            self, "Add extension",
            "Extension (with or without leading dot):")
        if not ok:
            return
        ext = (ext or "").strip().lower()
        if not ext:
            return
        if not ext.startswith("."):
            ext = "." + ext
        # Deduplicate
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.text().lower() == ext:
                self.table.selectRow(r)
                self._edit_entry()
                return
        # Ask color
        c = QColorDialog.getColor(
            QColor("#808080"), self, f"Color for {ext}")
        if not c.isValid():
            return
        self._append_row(ext, c.name())
        # Resort alphabetically by extension
        self._sort_table()

    def _edit_entry(self):
        self._pick_color_for_row(self.table.currentRow())

    def _on_cell_double_click(self, row, col):
        # Double-click anywhere on the row opens the color picker
        self.table.selectRow(row)
        self._pick_color_for_row(row)

    def _remove_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return
        ext_item = self.table.item(row, 0)
        ext = ext_item.text() if ext_item else "?"
        if QMessageBox.question(
                self, "Remove",
                f"Remove the color override for {ext}?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
            self.table.removeRow(row)

    def _sort_table(self):
        items = []
        for r in range(self.table.rowCount()):
            e = self.table.item(r, 0)
            c = self.table.item(r, 1)
            if e and c:
                items.append((e.text(), c.text()))
        items.sort(key=lambda x: x[0])
        self.table.setRowCount(0)
        for ext, col in items:
            self._append_row(ext, col)

    def _apply_preset(self, key: str):
        """Load the named preset into the live form widgets.
        Doesn't save - user still needs to click OK. Replaces
        the per-extension table contents entirely (so a
        preset switch doesn't leave stray colors behind from
        a previously-edited list)."""
        preset = LISTER_COLOR_PRESETS.get(key)
        if preset is None:
            return
        self.btn_bg.set_color(preset["bg"])
        self.btn_fg.set_color(preset["fg"])
        self.btn_dir.set_color(preset["dir_fg"])
        self._populate_table(preset["ext_colors"])

    def _reset_defaults(self):
        if QMessageBox.question(
                self, "Reset",
                "Reset all lister colors to the built-in "
                "defaults? This cannot be undone.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
            return
        from .config import DEFAULT_CONFIG
        self.btn_bg.set_color(
            DEFAULT_CONFIG.get("lister_bg", "#a0a0a0"))
        self.btn_fg.set_color(
            DEFAULT_CONFIG.get("lister_fg", "#000000"))
        self.btn_dir.set_color(
            DEFAULT_CONFIG.get("lister_dir_fg", "#0000cc"))
        ext = DEFAULT_CONFIG.get("lister_ext_colors") or {}
        self._populate_table(ext)

    # ------------------------------------------------------------
    # Save
    # ------------------------------------------------------------
    def _gather_ext_map(self) -> dict:
        ext = {}
        for r in range(self.table.rowCount()):
            e_it = self.table.item(r, 0)
            c_it = self.table.item(r, 1)
            if not (e_it and c_it):
                continue
            k = (e_it.text() or "").strip().lower()
            v = (c_it.text() or "").strip()
            if not k or not v:
                continue
            if not k.startswith("."):
                k = "." + k
            ext[k] = v
        return ext

    def _save_and_close(self):
        bg = self.btn_bg.color_hex
        fg = self.btn_fg.color_hex
        dir_fg = self.btn_dir.color_hex
        ext_map = self._gather_ext_map()
        # Save to disk (full round-trip so we don't lose
        # other keys that may have changed during this session)
        try:
            from .config import load_config, save_config
            cfg = load_config()
            cfg["lister_bg"] = bg
            cfg["lister_fg"] = fg
            cfg["lister_dir_fg"] = dir_fg
            cfg["lister_ext_colors"] = ext_map
            save_config(cfg)
        except Exception as e:
            QMessageBox.warning(
                self, "Save failed",
                f"Couldn't write colors to config: {e}")
            return
        # Mirror into the main window's live config so any
        # later main-window save doesn't clobber what we wrote.
        if self._mw is not None:
            mw_cfg = getattr(self._mw, "config", None)
            if isinstance(mw_cfg, dict):
                mw_cfg["lister_bg"] = bg
                mw_cfg["lister_fg"] = fg
                mw_cfg["lister_dir_fg"] = dir_fg
                mw_cfg["lister_ext_colors"] = ext_map
        self.accept()
