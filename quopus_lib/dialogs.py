"""Dialogs: ButtonConfig, Buffers, DirReverse (with file-selection support)."""
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDialogButtonBox, QScrollArea, QWidget,
    QListWidget, QListWidgetItem, QMessageBox, QCheckBox, QFileDialog,
    QPlainTextEdit, QTabWidget,
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, SCREEN_TITLEBAR_QSS, INFOBAR_QSS,
    SCROLLBAR_QSS, BUTTON_STYLES, button_qss, get_topaz_font
)
from .config import scaled_font_px


# ============================================================
# BUTTON CONFIG DIALOG
# ============================================================
class ButtonConfigDialog(QDialog):
    # ACTIONS used to be a flat, hand-curated list of action keys
    # that drifted out of sync with the per-button right-click
    # editor's grouped catalogue. It's now derived from the single
    # source of truth in action_catalog.ACTION_GROUPS so both
    # editors show the same actions in the same order. Kept as a
    # class attribute (rather than removed entirely) for any
    # external script or plugin that imported it.
    try:
        from .action_catalog import flat_action_keys as _flat_actions
        ACTIONS = _flat_actions()
        del _flat_actions
    except Exception:
        # Fallback for the very unlikely import-order edge case;
        # keeps the class definition succeeding even if the catalog
        # module is somehow broken at import time.
        ACTIONS = []
    COLORS = list(BUTTON_STYLES.keys())

    # Mapping of Quopus's built-in hotkeys to (action, label_hint).
    # Used by the Hotkey column in this dialog: when the user picks
    # one of these from the dropdown, the corresponding action and
    # a sensible label suggestion are auto-filled into the row.
    # Pressing the same hotkey later anywhere in Quopus will trigger
    # exactly that action - so binding "Alt+U" to a button means the
    # button does what Alt+U already does (open U64 streamer).
    #
    # Two kinds of entries:
    #   - "action" set to a real action name (e.g. "u64view"): the
    #     button just dispatches that action when clicked. These
    #     come from the bind(...) calls that use actions.dispatch.
    #   - "action" set to "hotkey" with the combo as param: the
    #     button simulates pressing that key combo, which routes
    #     through MainWindow._fire_builtin_hotkey to whichever
    #     bound handler is there. Used for hotkeys whose handler
    #     isn't a simple action dispatch (e.g. Ctrl+B = branch view,
    #     Ctrl+Q = quick view, F1 = README).
    #
    # Keep these in sync with the bind(...) calls in
    # main_window._setup_hotkeys.
    BUILTIN_HOTKEYS = {
        # Function keys F1-F10 (main commander layer)
        "F1":         ("hotkey",       "Help"),
        "F2":         ("hotkey",       "Refresh"),
        "F3":         ("read",         "Read"),
        "F4":         ("edit",         "Edit"),
        "F5":         ("copy",         "Copy"),
        "F6":         ("move",         "Move"),
        "F7":         ("makedir",      "Makedir"),
        "F8":         ("delete",       "Delete"),
        "F9":         ("hexread",      "Hex Read"),
        "F10":        ("config",       "Config"),
        "Del":        ("delete",       "Delete"),
        # Shift
        "Shift+F4":   ("hotkey",       "New Text"),
        "Shift+F5":   ("hotkey",       "Copy Same"),
        "Shift+F6":   ("rename",       "Rename"),
        "Shift+F10":  ("hotkey",       "Ctx Menu"),
        "Shift+Del":  ("hotkey",       "Wipe"),
        # Alt
        "Alt+F1":     ("hotkey",       "L Drives"),
        "Alt+F2":     ("hotkey",       "R Drives"),
        "Alt+F3":     ("hotkey",       "AltView"),
        "Alt+F4":     ("quit",         "Exit"),
        "Alt+F5":     ("archive",      "Pack"),
        "Alt+F7":     ("search",       "Find"),
        "Alt+F9":     ("extract",      "Extract"),
        "Alt+F10":    ("archive",      "Archive"),
        "Alt+F11":    ("compare",      "Compare"),
        "Alt+F":      ("hotkey",       "FILE_ID"),
        "Alt+U":      ("u64view",      "U64 View"),
        "Alt+Return": ("info",         "Info"),
        # Ctrl letter combos
        "Ctrl+A":     ("select_all",   "All"),
        "Ctrl+B":     ("hotkey",       "Branch"),
        "Ctrl+C":     ("hotkey",       "Clip Copy"),
        "Ctrl+D":     ("hotkey",       "Hotlist"),
        "Ctrl+F":     ("ftp",          "FTP"),
        "Ctrl+H":     ("find",         "Hunt"),
        "Ctrl+I":     ("hotkey",       "InvTags"),
        "Ctrl+L":     ("getsizes",     "Sizes"),
        "Ctrl+M":     ("multi_rename", "MultiRen"),
        "Ctrl+N":     ("ftp",          "New FTP"),
        "Ctrl+Q":     ("hotkey",       "QuickV"),
        "Ctrl+R":     ("hotkey",       "Refresh"),
        "Ctrl+S":     ("hotkey",       "Filter"),
        "Ctrl+T":     ("hotkey",       "Layer"),
        "Ctrl+U":     ("swap",         "Swap"),
        "Ctrl+V":     ("hotkey",       "Paste"),
        "Ctrl+X":     ("hotkey",       "Cut"),
        "Ctrl+Z":     ("comment",      "Comment"),
        # Ctrl + special
        "Ctrl+Space":     ("hotkey",   "Tag"),
        "Ctrl+PgUp":      ("parent",   "Up"),
        "Ctrl+\\":        ("root",     "Root"),
        "Ctrl+Return":    ("hotkey",   "CopyName"),
        "Ctrl+Shift+Return": ("hotkey", "CopyPath"),
        "Ctrl+Shift+F":   ("hotkey",   "FTP Disc"),
        "Backspace":      ("hotkey",   "Back"),
        # Ctrl+F-keys (view modes / sorting)
        "Ctrl+F1":    ("hotkey",       "Brief"),
        "Ctrl+F2":    ("hotkey",       "Details"),
        "Ctrl+F3":    ("hotkey",       "SortName"),
        "Ctrl+F4":    ("hotkey",       "SortExt"),
        "Ctrl+F5":    ("hotkey",       "SortTime"),
        "Ctrl+F6":    ("hotkey",       "SortSize"),
        "Ctrl+Left":  ("hotkey",       "Send L"),
        "Ctrl+Right": ("hotkey",       "Send R"),
    }

    def __init__(self, buttons_cfg, parent=None, buttons_shift_cfg=None,
                 buttons_shift_alt_cfg=None):
        """Edit dialog for the action-button grid.

        buttons_cfg:           the 6x6 main layer (always present).
        buttons_shift_cfg:     the 6x6 Shift-layer; pass None to fall
                               back to an empty grid.
        buttons_shift_alt_cfg: the 6x6 Shift+Alt-layer; pass None to
                               fall back to an empty grid.

        The dialog edits all three layers in tabs and returns them
        via result_config() / result_shift_config() /
        result_shift_alt_config()."""
        super().__init__(parent)
        self.setWindowTitle("Quopus Button Configuration")
        self.setMinimumSize(960, 660)
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        # Deep-copy all three layers so cancel really cancels.
        self.buttons_cfg = [[dict(b) if b else None for b in row]
                            for row in buttons_cfg]
        if buttons_shift_cfg is None:
            buttons_shift_cfg = [[None]*6 for _ in range(6)]
        self.buttons_shift_cfg = [
            [dict(b) if b else None for b in row]
            for row in buttons_shift_cfg]
        if buttons_shift_alt_cfg is None:
            buttons_shift_alt_cfg = [[None]*6 for _ in range(6)]
        self.buttons_shift_alt_cfg = [
            [dict(b) if b else None for b in row]
            for row in buttons_shift_alt_cfg]

        layout = QVBoxLayout(self)
        header = QLabel(" Edit action buttons. Empty label removes button. ")
        header.setStyleSheet(SCREEN_TITLEBAR_QSS)
        layout.addWidget(header)

        hint = QLabel(
            "  Three layers: 'Main' is shown by default, 'Shift-layer' "
            "while the user holds Shift, 'Shift+Alt-layer' while both "
            "are held. Ctrl+T cycles through them persistently. For "
            "'external_script' or 'execute_command' actions, put the "
            "program + args in the Param column. Tokens: %f=first file, "
            "%F=all selected (quoted), %n=basename, %p=current dir, "
            "%d=other-side dir  ")
        hint.setStyleSheet(
            f"QLabel {{ background-color: {C.WB_GREY}; color: #555; "
            f"font-size: {scaled_font_px(10)}px; padding: 2px 6px; }}")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Three tabs: Main, Shift, Shift+Alt. Each tab gets its own
        # grid of edit widgets (label/action/color/param/clear).
        tabs = QTabWidget()
        tabs.setStyleSheet(
            f"QTabWidget::pane {{ background-color: {C.WB_GREY}; }} "
            f"QTabBar::tab {{ background-color: #b0b0b0; color: #000; "
            f"padding: 4px 12px; }} "
            f"QTabBar::tab:selected {{ background-color: #d0d0d0; }} ")
        # Edits arrays - one per layer
        self.edits = []            # main layer edits
        self.shift_edits = []      # shift layer edits
        self.shift_alt_edits = []  # shift+alt layer edits
        tabs.addTab(self._build_grid(self.buttons_cfg, self.edits),
                     "Main layer")
        tabs.addTab(self._build_grid(self.buttons_shift_cfg, self.shift_edits),
                     "Shift-layer  (held while Shift is pressed)")
        tabs.addTab(self._build_grid(self.buttons_shift_alt_cfg,
                                     self.shift_alt_edits),
                     "Shift+Alt-layer  (held while Shift+Alt are pressed)")
        layout.addWidget(tabs, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        # Help button on the LEFT side (ActionRole keeps it out of
        # the OK/Cancel cluster on the right). Opens a non-modal
        # cheat-sheet window with all tokens + example commands;
        # clicking a row copies its text to the clipboard.
        b_help = QPushButton("Help: Tokens && Examples")
        b_help.setStyleSheet(button_qss("blue"))
        b_help.setToolTip(
            "Show a non-modal reference window with the available "
            "%-tokens and ready-to-copy example commands.")
        b_help.clicked.connect(self._show_token_help)
        bb.addButton(b_help, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(bb)

    def _show_token_help(self):
        """Open the token-help cheat sheet. Non-modal so the user
        can keep it visible while editing button params, and
        reuse the same window if they click Help twice."""
        existing = getattr(self, '_token_help_dlg', None)
        if existing is not None and existing.isVisible():
            existing.raise_(); existing.activateWindow()
            return
        self._token_help_dlg = _TokenHelpDialog(parent=self)
        self._token_help_dlg.show()

    def _build_grid(self, cfg, edits_list) -> QWidget:
        """Build one tab's worth of editor rows for the given 6x6 cfg.
        Appends each row's widget tuple to `edits_list` so result_*
        methods know where to read from later."""
        from PyQt6.QtWidgets import QCheckBox
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid = QGridLayout(inner); grid.setSpacing(2)
        for c, h in enumerate(["R", "C", "Label", "Action", "Color",
                                  "Param", "Show", "Refresh",
                                  "Hotkey", "Clear"]):
            grid.addWidget(QLabel(f"<b>{h}</b>"), 0, c)
        ri = 1
        for r, row in enumerate(cfg):
            for c, btn in enumerate(row):
                grid.addWidget(QLabel(str(r + 1)), ri, 0)
                grid.addWidget(QLabel(str(c + 1)), ri, 1)
                le_label = QLineEdit(btn["label"] if btn else "")
                grid.addWidget(le_label, ri, 2)
                # Action picker via the central catalog. Same
                # hierarchical group menu used by the per-button
                # right-click editor, so both editors show the same
                # actions in the same order. The picker exposes
                # .currentText() / .setCurrentText() shims so the
                # collect/clear code below (originally written for
                # a QComboBox) doesn't need to know it's now a
                # menu button.
                from .action_catalog import build_action_picker_button
                initial_action = (
                    btn.get("action") if btn and btn.get("action") else "")
                cb_action = build_action_picker_button(
                    self, initial_action, lambda _k: None,
                    include_empty=True, width_hint=180)
                grid.addWidget(cb_action, ri, 3)

                # Color picker button - shows swatch + name, popup with
                # all 40 color presets as a grid
                current_color = (btn.get("color") if btn else None) or "blue"
                if current_color not in self.COLORS:
                    current_color = "blue"
                color_btn = self._make_color_picker(current_color)
                grid.addWidget(color_btn, ri, 4)

                le_param = QLineEdit(btn.get("param", "") if btn else "")
                grid.addWidget(le_param, ri, 5)

                # 'Show' checkbox: when checked, external_script /
                # execute_command actions show their stdout/stderr
                # in a Quopus output dialog instead of running silently
                # detached. Useful for tools like 'unp64 %f' that
                # produce diagnostic info you actually want to read.
                cb_show = QCheckBox()
                cb_show.setChecked(bool(btn and btn.get("show_output")))
                cb_show.setToolTip(
                    "Show command output in a Quopus dialog (instead of "
                    "running detached). Only affects external_script "
                    "and execute_command actions.")
                grid.addWidget(cb_show, ri, 6)

                # 'Refresh' checkbox: when checked, both panels are
                # re-read after the command finishes. Combine with
                # 'Show' for tools that drop new files into the
                # current dir - the fresh files pop up automatically.
                cb_refresh = QCheckBox()
                cb_refresh.setChecked(bool(btn and btn.get("refresh_after")))
                cb_refresh.setToolTip(
                    "Re-read both panels after the command finishes. "
                    "Only affects external_script and execute_command "
                    "actions.")
                grid.addWidget(cb_refresh, ri, 7)

                # Hotkey field. Editable QComboBox: the dropdown
                # offers Quopus's built-in hotkeys (Alt+U, F5, ...)
                # so the user can pick one and have the action +
                # label auto-fill. Or they can type their own combo
                # like "Ctrl+Shift+P" to bind a custom shortcut.
                #
                # Picking a built-in hotkey is the common case and
                # the whole reason this column exists - the user
                # essentially asks "make this button do what F5
                # already does" without having to know the action
                # name.
                cb_hotkey = QComboBox()
                cb_hotkey.setEditable(True)
                cb_hotkey.addItem("")    # "no hotkey" option
                for hk, (act, lbl) in self.BUILTIN_HOTKEYS.items():
                    cb_hotkey.addItem(f"{hk}  ({act})", hk)
                cb_hotkey.setFixedWidth(180)
                # Pre-fill from the existing button entry. The
                # display text shows '<combo>  (<action>)' format
                # for known hotkeys, plain text for custom ones.
                if btn and btn.get("hotkey"):
                    hk = btn["hotkey"]
                    if hk in self.BUILTIN_HOTKEYS:
                        cb_hotkey.setCurrentText(
                            f"{hk}  ({self.BUILTIN_HOTKEYS[hk][0]})")
                    else:
                        cb_hotkey.setCurrentText(hk)
                cb_hotkey.setToolTip(
                    "Pick a built-in Quopus hotkey from the dropdown "
                    "to make this button behave exactly like that "
                    "key combo (action + label auto-fill).\n\n"
                    "Or type a custom combo like 'Ctrl+Shift+P' to "
                    "bind a new shortcut.\n\n"
                    "Empty = no hotkey on this button.")
                # When the user picks a built-in entry from the
                # dropdown, fill in the action ComboBox + suggest a
                # label. We capture the dependent widgets via the
                # default-arg trick so the lambda doesn't bind to
                # the LAST iteration's vars.
                def _on_hk_picked(idx, _cb_hk=cb_hotkey,
                                    _cb_act=cb_action, _le_lbl=le_label,
                                    _le_par=le_param):
                    hk_data = _cb_hk.itemData(idx)
                    if not hk_data or hk_data not in self.BUILTIN_HOTKEYS:
                        return
                    act, lbl = self.BUILTIN_HOTKEYS[hk_data]
                    # Only overwrite the action if it doesn't match
                    # already - keeps the user's manual choice if
                    # they happened to pick the same hotkey for it.
                    if _cb_act.currentText().strip() != act:
                        _cb_act.setCurrentText(act)
                    # For the special "hotkey" action, the param
                    # has to be the combo string itself - that's how
                    # act_hotkey knows which key to fire.
                    if act == "hotkey":
                        _le_par.setText(hk_data)
                    # Only fill label if currently empty, so we
                    # don't clobber the user's nicer name.
                    if not _le_lbl.text().strip():
                        _le_lbl.setText(lbl)
                cb_hotkey.activated.connect(_on_hk_picked)
                grid.addWidget(cb_hotkey, ri, 8)

                btn_clear = QPushButton("X"); btn_clear.setFixedWidth(30)
                btn_clear.clicked.connect(
                    lambda chk, L=le_label, A=cb_action, P=le_param,
                            S=cb_show, R=cb_refresh, K=cb_hotkey:
                    (L.setText(""), A.setCurrentText(""), P.setText(""),
                     S.setChecked(False), R.setChecked(False),
                     K.setCurrentText("")))
                grid.addWidget(btn_clear, ri, 9)
                edits_list.append(
                    (r, c, le_label, cb_action, color_btn, le_param,
                      cb_show, cb_refresh, cb_hotkey))
                ri += 1
        scroll.setWidget(inner)
        return scroll

    def result_config(self):
        """Read out the main-layer edits and return the 6x6 list."""
        return self._collect(self.edits, self.buttons_cfg)

    def result_shift_config(self):
        """Read out the Shift-layer edits and return the 6x6 list."""
        return self._collect(self.shift_edits, self.buttons_shift_cfg)

    def result_shift_alt_config(self):
        """Read out the Shift+Alt-layer edits and return the 6x6 list."""
        return self._collect(self.shift_alt_edits,
                              self.buttons_shift_alt_cfg)

    @staticmethod
    def _parse_hotkey_field(cb_hotkey):
        """Extract the actual hotkey string from the Hotkey
        ComboBox. The dropdown shows '<combo>  (<action>)' for
        built-in hotkeys but the persisted value should be just
        '<combo>' so the QShortcut binding works. For custom
        free-typed text we just return it stripped.
        """
        # Prefer the itemData (set on dropdown items) if the user
        # picked something from the list - that gives us the clean
        # combo string without the action suffix.
        idx = cb_hotkey.currentIndex()
        if idx >= 0:
            data = cb_hotkey.itemData(idx)
            if data and cb_hotkey.currentText().endswith(
                    f"  ({ButtonConfigDialog.BUILTIN_HOTKEYS.get(data, ('',))[0]})"):
                return data
        # User typed something custom or stale display text.
        # Strip "  (xxx)" suffix if present.
        text = cb_hotkey.currentText().strip()
        if "  (" in text:
            text = text.split("  (")[0].strip()
        return text

    def _collect(self, edits_list, target_cfg):
        # Defensive: a button with a label but no action means the
        # ComboBox didn't initialise cleanly (rare Qt theme issue
        # where setCurrentText fails silently). In that case we
        # keep the EXISTING button entry from target_cfg instead
        # of nuking it. The user can fix the action manually later
        # without losing labels/colors/params/hotkeys.
        for r, c, le_l, cb_a, color_btn, le_p, cb_show, cb_refresh, \
                cb_hotkey in edits_list:
            label = le_l.text().strip()
            action = cb_a.currentText().strip()
            hk = self._parse_hotkey_field(cb_hotkey)
            if not label and not action:
                # Both empty -> button cleared
                target_cfg[r][c] = None
                continue
            if not action:
                # Label set but action lost - this happens if Qt
                # failed to populate the combobox at dialog-open
                # time (some themes), or if the action value got
                # lost on roundtrip. Rather than discard the button,
                # fall back to whatever was there before. New
                # values from the OTHER fields override the old.
                existing = target_cfg[r][c]
                if existing:
                    b = dict(existing)    # preserve action+others
                    b["label"] = label
                    b["color"] = getattr(
                        color_btn, 'current_color', b.get("color", "blue"))
                    p = le_p.text().strip()
                    if p:
                        b["param"] = p
                    elif "param" in b:
                        del b["param"]
                    if cb_show.isChecked():
                        b["show_output"] = True
                    elif "show_output" in b:
                        del b["show_output"]
                    if cb_refresh.isChecked():
                        b["refresh_after"] = True
                    elif "refresh_after" in b:
                        del b["refresh_after"]
                    if hk:
                        b["hotkey"] = hk
                    elif "hotkey" in b:
                        del b["hotkey"]
                    target_cfg[r][c] = b
                else:
                    # No prior entry to preserve and no action -
                    # the button can't fire anything sensible.
                    # Keep it as None.
                    target_cfg[r][c] = None
                continue
            # Normal case: both label and action present
            b = {"label": label, "action": action,
                 "color": getattr(color_btn, 'current_color', 'blue')}
            p = le_p.text().strip()
            if p: b["param"] = p
            # Persist the optional flags only when set so existing
            # button configs that don't use them stay clean and
            # forward-compatible.
            if cb_show.isChecked():
                b["show_output"] = True
            if cb_refresh.isChecked():
                b["refresh_after"] = True
            if hk:
                b["hotkey"] = hk
            target_cfg[r][c] = b
        return target_cfg

    def _make_color_picker(self, initial_color):
        """A button showing the color as a swatch; clicking opens a grid of
        all available colors to pick from."""
        from PyQt6.QtWidgets import QPushButton
        btn = QPushButton()
        btn.current_color = initial_color
        btn.setFixedWidth(110)

        def refresh():
            bg, fg = BUTTON_STYLES.get(btn.current_color, BUTTON_STYLES["blue"])
            btn.setText(btn.current_color)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: {fg}; "
                f"border: 1px solid #000000; padding: 2px 4px; "
                f"font-family: 'Topaz','Courier New',monospace; "
                f"font-size: {scaled_font_px(11)}px; text-align: center; }}"
            )
        refresh()
        btn._refresh = refresh

        btn.clicked.connect(lambda: self._pick_color(btn))
        return btn

    def _pick_color(self, color_btn):
        """Popup color picker showing swatches for all BUTTON_STYLES."""
        from PyQt6.QtWidgets import (
            QDialog, QGridLayout, QPushButton, QLabel, QVBoxLayout
        )
        from PyQt6.QtCore import Qt
        dlg = QDialog(self)
        dlg.setWindowTitle("Pick a color")
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(6, 6, 6, 6); layout.setSpacing(4)
        layout.addWidget(QLabel("Click a color:"))

        grid = QGridLayout(); grid.setSpacing(2)
        layout.addLayout(grid)

        names = list(BUTTON_STYLES.keys())
        cols = 5
        for i, name in enumerate(names):
            bg, fg = BUTTON_STYLES[name]
            r, c = divmod(i, cols)
            b = QPushButton(name)
            b.setFixedSize(130, 32)
            b.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: {fg}; "
                f"border: 1px solid #000000; "
                f"font-family: 'Topaz','Courier New',monospace; "
                f"font-weight: bold; font-size: {scaled_font_px(11)}px; }}"
                f"QPushButton:hover {{ border: 2px solid #ffff00; }}"
            )
            def pick(_=None, n=name):
                color_btn.current_color = n
                color_btn._refresh()
                dlg.accept()
            b.clicked.connect(pick)
            grid.addWidget(b, r, c)

        dlg.exec()


# ============================================================
# TOKEN HELP DIALOG
# ============================================================
# Non-modal cheat sheet that the Button Config dialog opens via
# its Help button. Lists all %-tokens and a pile of ready-to-use
# example commands; clicking any row copies the text to the
# clipboard so the user can paste it straight into a Param field.
# ============================================================
class _TokenHelpDialog(QDialog):
    """Token reference + example-command cheat sheet.

    The window has two sections stacked vertically:
      * Token table: %f, %F, %n, %p, %d, %i, %% with descriptions
      * Examples list: pre-baked command strings, double-click to
        copy the command (or single-click the Copy button)

    Both areas use the Workbench-styled QListWidget so the look
    matches the rest of the app. A status line at the bottom
    confirms what was copied so the user knows the click did
    something.
    """

    # Token reference. Each entry is (token, short, long_explanation).
    # Kept here rather than parsed from actions.py at runtime so
    # the wording is curated, not raw doc-strings.
    TOKENS = [
        ("%f",
         "First selected/tagged file (full path)",
         "Expands to the absolute path of the first selected or "
         "tagged file in the active panel. Shell-quoted so spaces "
         "in folder names like 'combat school' don't break the "
         "command."),
        ("%F",
         "ALL selected/tagged files (space-separated, each quoted)",
         "Expands to every selected/tagged file in the active panel, "
         "each individually shell-quoted, joined by spaces. Useful "
         "for batch tools that take a list of arguments: "
         "`md5sum %F`, `tar czf out.tgz %F`, `scp %F user@host:`."),
        ("%n",
         "First selected file - basename only (no path)",
         "Just the filename without the directory part, shell-quoted. "
         "Useful when the tool wants only the leaf name: "
         "`echo Renaming %n`, or when constructing a destination "
         "path from a name: `cp %f /backup/%n`."),
        ("%p",
         "Current source directory (active panel)",
         "Absolute path of the active panel's current folder, "
         "shell-quoted. Common use: pass the working directory to "
         "a script that operates on the whole folder rather than "
         "individual files. Example: `python pack.py %p`."),
        ("%d",
         "Other-side / destination directory",
         "Absolute path of the inactive panel - the OTHER side. "
         "Common use: redirect output to the other panel so the "
         "result shows up next to your sources. Examples: "
         "`dir %p > %d/listing.txt`, "
         "`md5sum %F > %d/checksums.txt`."),
        ("%i",
         "Prompted user input (filename / string)",
         "Pops an 'Enter filename' dialog before the command runs. "
         "Default suggestion is the current date+time as "
         "YYYYMMDD-HHMMSS so multiple captures don't overwrite. "
         "Empty input => the auto-name is used. Cancel => command "
         "doesn't run. Result is shell-quoted. "
         "Examples: `ef3usb r %i.d64`, `nibtools -r %i.nib`, "
         "`tar czf %d/%i.tar.gz %F`."),
        ("%%",
         "Literal % character",
         "Use this when you actually want a percent sign in the "
         "command, e.g. for sprintf-style format strings inside "
         "a tool: `printf 'progress: %%d\\n' 50`. Without the "
         "doubling, the single % would try to start a token."),
    ]

    # Pre-baked example commands. Each is (action_kind, command,
    # short_description). Action_kind is shown for context but
    # only the command goes to the clipboard on copy.
    EXAMPLES = [
        # Editor / viewer
        ("external_script",
         '"C:\\Program Files\\Notepad++\\notepad++.exe" -n %f',
         "Open selected file in Notepad++ on Windows"),
        ("external_script",
         "code %f",
         "Open selected file in VS Code"),
        ("external_script",
         "subl %f",
         "Open selected file in Sublime Text"),
        # Emulators
        ("external_script",
         "x64sc %f",
         "Open C64 disk/tape in VICE"),
        ("external_script",
         "x128 %f",
         "Open C128 image in VICE"),
        ("external_script",
         "fs-uae %f",
         "Open Amiga disk in FS-UAE"),
        # Hardware tools (the %i ones)
        ("external_script",
         "ef3usb r %i.d64",
         "Read a disk from real Floppy via EF3-USB; prompts for "
         "filename, default date-time"),
        ("external_script",
         "ef3usb b %f",
         "Burn selected PRG to EasyFlash3 cart via USB"),
        ("external_script",
         "nibtools -r %i.nib",
         "Read a disk to .nib via nibtools; prompts for filename"),
        ("external_script",
         "cbmctrl detect",
         "Detect IEC bus devices via OpenCBM"),
        # Packaging / archival
        ("execute_command",
         "tar czf %d/%i.tar.gz %F",
         "Pack tagged files into a .tar.gz on the other side; "
         "prompts for archive name"),
        ("execute_command",
         "zip -r %d/%i.zip %F",
         "Pack tagged files into a .zip on the other side"),
        ("external_script",
         "lha a %d/%i.lha %F",
         "LHA-pack tagged files (Amiga style)"),
        # Hashes / inspection
        ("execute_command",
         "md5sum %F > %d/checksums.txt",
         "MD5 every tagged file, write list to other side"),
        ("execute_command",
         "sha256sum %F > %d/sha256.txt",
         "SHA-256 hashes to other side"),
        ("execute_command",
         "file %F",
         "Show file types of every tagged file (POSIX `file`)"),
        # Transfer
        ("execute_command",
         "scp %F user@bbs.example.com:/incoming/",
         "Copy tagged files via SCP to a remote BBS"),
        ("execute_command",
         "rsync -av %F user@host:/dst/",
         "Sync tagged files via rsync"),
        # Custom scripts
        ("external_script",
         "python C:\\scripts\\packrelease.py %p",
         "Run packrelease.py on the active directory"),
        ("external_script",
         "python ~/scripts/dannounce.py %f",
         "Run dannounce on the selected log file"),
        # Dir listings
        ("execute_command",
         "dir %p > %d\\listing.txt",
         "Save Windows dir listing to other side"),
        ("execute_command",
         "ls -la %p > %d/listing.txt",
         "Save POSIX ls listing to other side"),
        # Conversions
        ("external_script",
         "petcat -2 -o %d/%n.bas %f",
         "Detokenize C64 BASIC PRG to .bas via petcat"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tokens && Examples")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(720, 600)
        # Non-modal so user can keep it visible while editing.
        self.setWindowFlag(Qt.WindowType.Tool, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # --- Title strip ---
        title = QLabel(" Param-string substitution tokens ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        intro = QLabel(
            "  Click any row to copy its token (or example "
            "command) to the clipboard.  ")
        intro.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(intro)

        # --- Token list ---
        self.lst_tokens = QListWidget()
        self.lst_tokens.setStyleSheet(SCROLLBAR_QSS)
        for tok, short, _long in self.TOKENS:
            # Visual layout: [token]  short-description
            item = QListWidgetItem(f"  {tok:<5}  {short}")
            # Stash both the raw token and the long explanation
            # in user-data so click handlers can use them.
            item.setData(Qt.ItemDataRole.UserRole, tok)
            item.setData(Qt.ItemDataRole.UserRole + 1, _long)
            item.setToolTip(_long)
            self.lst_tokens.addItem(item)
        self.lst_tokens.setMaximumHeight(180)
        self.lst_tokens.itemClicked.connect(self._on_token_clicked)
        layout.addWidget(self.lst_tokens)

        # --- Examples header + filter ---
        ex_title = QLabel(" Example commands ")
        ex_title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(ex_title)

        # Quick filter so the list of 20+ examples stays usable.
        filter_row = QHBoxLayout()
        filter_row.setSpacing(2)
        filter_row.addWidget(QLabel(" Filter: "))
        self.le_filter = QLineEdit()
        self.le_filter.setPlaceholderText(
            "type to filter: 'ef3', 'tar', 'rsync', ...")
        self.le_filter.textChanged.connect(self._refresh_examples)
        filter_row.addWidget(self.le_filter, 1)
        f_wrap = QWidget(); f_wrap.setLayout(filter_row)
        layout.addWidget(f_wrap)

        # --- Examples list ---
        self.lst_examples = QListWidget()
        self.lst_examples.setStyleSheet(SCROLLBAR_QSS)
        # Use a monospace font for the command column so all the
        # %f / %F / etc. line up vertically.
        from PyQt6.QtGui import QFont
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self.lst_examples.setFont(mono)
        self.lst_examples.itemClicked.connect(self._on_example_clicked)
        self.lst_examples.itemDoubleClicked.connect(self._on_example_clicked)
        layout.addWidget(self.lst_examples, 1)
        self._refresh_examples()    # populate

        # --- Buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(2)
        self.lbl_status = QLabel("  ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        btn_row.addWidget(self.lbl_status, 1)
        b_close = QPushButton("Close")
        b_close.setStyleSheet(button_qss("red"))
        b_close.setFixedWidth(80)
        b_close.clicked.connect(self.close)
        btn_row.addWidget(b_close)
        b_wrap = QWidget(); b_wrap.setLayout(btn_row)
        layout.addWidget(b_wrap)

    def _refresh_examples(self):
        """Repopulate the examples list, filtered by the search
        box. Filter matches command + description case-insensitive."""
        flt = self.le_filter.text().strip().lower()
        self.lst_examples.clear()
        for kind, cmd, desc in self.EXAMPLES:
            hay = (kind + " " + cmd + " " + desc).lower()
            if flt and flt not in hay:
                continue
            # Two lines per item: the command (mono) on top, the
            # description (slightly indented) below. Implemented
            # via a single multi-line item for simplicity - looks
            # fine in the QListWidget.
            label = f"  {cmd}\n      [{kind}] {desc}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            item.setToolTip(
                f"{desc}\n\nClick to copy:\n  {cmd}")
            self.lst_examples.addItem(item)

    def _on_token_clicked(self, item):
        """Copy the bare token (e.g. '%i') to the clipboard."""
        tok = item.data(Qt.ItemDataRole.UserRole)
        if not tok:
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(tok)
        self.lbl_status.setText(f"  Copied to clipboard: {tok}  ")

    def _on_example_clicked(self, item):
        """Copy the example command to the clipboard."""
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if not cmd:
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(cmd)
        # Truncate the status echo for very long commands.
        echo = cmd if len(cmd) <= 60 else cmd[:57] + "..."
        self.lbl_status.setText(f"  Copied: {echo}  ")


# ============================================================
# BUFFERS DIALOG
# ============================================================
class BuffersDialog(QDialog):
    def __init__(self, left_lister, right_lister, saved_buffers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Buffers")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(600, 400)

        self.left = left_lister; self.right = right_lister; self.saved = saved_buffers
        self.selected_path = None; self.target = None

        lay = QVBoxLayout(self)
        title = QLabel(" Buffers ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS); lay.addWidget(title)

        candidates = []; seen = set()
        def add(path, source):
            p = str(path)
            if p not in seen:
                seen.add(p); candidates.append((source, p))
        add(self.left.current_path, "LEFT now")
        add(self.right.current_path, "RIGHT now")
        for p in reversed(self.left.history): add(p, "LEFT hist")
        for p in reversed(self.right.history): add(p, "RIGHT hist")
        for p in self.saved: add(p, "saved")

        self.lw = QListWidget()
        self.lw.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.LISTER_BG}; color: {C.LISTER_FG};
                font-family: 'Topaz','Courier New',monospace;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.SELECTED_FG};
            }}
            {SCROLLBAR_QSS}
        """)
        for src, p in candidates:
            item = QListWidgetItem(f"[{src:10s}] {p}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.lw.addItem(item)
        lay.addWidget(self.lw, 1)

        row = QHBoxLayout()
        b_save = QPushButton("Save current"); b_save.setStyleSheet(button_qss("orange"))
        b_save.clicked.connect(self._save_current); row.addWidget(b_save)
        b_left = QPushButton("Open in LEFT"); b_left.setStyleSheet(button_qss("blue"))
        b_left.clicked.connect(lambda: self._open('left')); row.addWidget(b_left)
        b_right = QPushButton("Open in RIGHT"); b_right.setStyleSheet(button_qss("blue"))
        b_right.clicked.connect(lambda: self._open('right')); row.addWidget(b_right)
        b_close = QPushButton("Close"); b_close.setStyleSheet(button_qss("red"))
        b_close.clicked.connect(self.reject); row.addWidget(b_close)
        lay.addLayout(row)

    def _save_current(self):
        for p in (self.left.current_path, self.right.current_path):
            if p not in self.saved:
                self.saved.append(p)
        QMessageBox.information(self, "Buffers", "Saved.")

    def _open(self, which):
        it = self.lw.currentItem()
        if not it: return
        self.selected_path = it.data(Qt.ItemDataRole.UserRole)
        self.target = which
        self.accept()


# ============================================================
# /X DIR REVERSE DIALOG (with single-file / selection support)
# ============================================================
class DirReverseDialog(QDialog):
    """
    /X dir listing generator.

    Args:
        initial_dir: base directory
        selected_files: optional list of Path objects (selected or tagged files).
                        If non-empty, dialog defaults to 'Selected only' mode.
    """
    def __init__(self, initial_dir, selected_files=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("/X Dir Reverser")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(720, 560)
        self.initial_dir = Path(initial_dir)
        self.selected_files = [Path(p) for p in (selected_files or [])
                               if Path(p).is_file()]

        lay = QVBoxLayout(self)
        title = QLabel(" /X Directory Reverser ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        lay.addWidget(title)

        # Source
        row1 = QHBoxLayout(); row1.addWidget(QLabel("Source:"))
        self.src_edit = QLineEdit(str(initial_dir))
        self.src_edit.setStyleSheet(
            f"background-color:{C.WHITE};color:{C.BLACK};"
            f"font-family:'Topaz','Courier New',monospace;padding:2px;")
        row1.addWidget(self.src_edit, 1)
        b_br = QPushButton("Browse..."); b_br.setStyleSheet(button_qss("blue"))
        b_br.clicked.connect(self._browse_src); row1.addWidget(b_br)
        lay.addLayout(row1)

        # Output
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Output:"))
        self.out_edit = QLineEdit(str(Path(initial_dir) / "DIR.LST"))
        self.out_edit.setStyleSheet(
            f"background-color:{C.WHITE};color:{C.BLACK};"
            f"font-family:'Topaz','Courier New',monospace;padding:2px;")
        row2.addWidget(self.out_edit, 1)
        b_so = QPushButton("Save as..."); b_so.setStyleSheet(button_qss("blue"))
        b_so.clicked.connect(self._browse_out); row2.addWidget(b_so)
        lay.addLayout(row2)

        # Options
        opts = QHBoxLayout()
        self.cb_reverse = QCheckBox("Reverse (newest first)"); self.cb_reverse.setChecked(True)
        opts.addWidget(self.cb_reverse)

        # Mode: full / selected
        self.cb_selected = QCheckBox(
            f"Selected files only ({len(self.selected_files)} file(s))")
        if self.selected_files:
            self.cb_selected.setChecked(True)
        else:
            self.cb_selected.setEnabled(False)
        opts.addWidget(self.cb_selected)

        self.cb_recurse = QCheckBox("Include subdirs")
        opts.addWidget(self.cb_recurse)
        opts.addWidget(QLabel("Uploader:"))
        self.uploader_edit = QLineEdit("SYSOP"); self.uploader_edit.setFixedWidth(120)
        self.uploader_edit.setStyleSheet(
            f"background-color:{C.WHITE};color:{C.BLACK};"
            f"font-family:'Topaz','Courier New',monospace;padding:2px;")
        opts.addWidget(self.uploader_edit)
        opts.addStretch()
        lay.addLayout(opts)

        # Actions
        act = QHBoxLayout()
        b_prev = QPushButton("Preview"); b_prev.setStyleSheet(button_qss("orange"))
        b_prev.clicked.connect(self._preview); act.addWidget(b_prev)
        b_save = QPushButton("Save to file"); b_save.setStyleSheet(button_qss("purple"))
        b_save.clicked.connect(self._save); act.addWidget(b_save)
        act.addStretch()
        b_close = QPushButton("Close"); b_close.setStyleSheet(button_qss("red"))
        b_close.clicked.connect(self.reject); act.addWidget(b_close)
        lay.addLayout(act)

        # Preview
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        self.preview.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {C.BLACK}; color: {C.WHITE};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(12)}px; border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.preview.setFont(get_topaz_font(10))
        lay.addWidget(self.preview, 1)

    def _browse_src(self):
        d = QFileDialog.getExistingDirectory(self, "Source directory", self.src_edit.text())
        if d: self.src_edit.setText(d)

    def _browse_out(self):
        f, _ = QFileDialog.getSaveFileName(self, "Output file", self.out_edit.text(),
                                           "Text (*.lst *.txt);;All files (*)")
        if f: self.out_edit.setText(f)

    def _gen(self):
        from .dir_reverse import make_dir_listing
        file_paths = self.selected_files if self.cb_selected.isChecked() else None
        suffix = ""
        if file_paths:
            suffix = f"{len(file_paths)} selected file(s) from lister"
        return make_dir_listing(
            self.src_edit.text(),
            reverse=self.cb_reverse.isChecked(),
            include_subdirs=self.cb_recurse.isChecked(),
            uploader=self.uploader_edit.text().strip() or "SYSOP",
            file_paths=file_paths,
            title_suffix=suffix,
        )

    def _preview(self):
        try:
            self.preview.setPlainText(self._gen())
        except Exception as e:
            QMessageBox.critical(self, "/X", str(e))

    def _save(self):
        try:
            text = self._gen()
            out = Path(self.out_edit.text())
            out.write_text(text, encoding="utf-8")
            self.preview.setPlainText(text)
            QMessageBox.information(self, "/X",
                f"Saved:\n{out}\n\n{len(text.splitlines())} lines")
        except Exception as e:
            QMessageBox.critical(self, "/X", str(e))
