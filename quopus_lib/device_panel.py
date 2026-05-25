"""
Device column: vertical scrollable list of device buttons.

Lives in column 0 of the main button bank at the bottom of the window.
Each button (T:, DF1:, RAM:, C:, DEVS:, Conf1) is a clickable drive
shortcut. The column has a vertical scrollbar on its right so up to 40
buttons can be scrolled into view.

Click a device button -> navigate the active lister to its path.
Shift+click -> navigate BOTH listers to the path at once.
Right-click -> edit / remove / reorder.

Device entry types (in the `type` field of each device dict):
  - "local" (default): plain folder path
  - "ftp":  FTP bookmark; activates a saved FTP connection
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
    QInputDialog, QMessageBox, QMenu, QFileDialog, QDialog,
    QLineEdit, QDialogButtonBox, QFormLayout, QComboBox, QSpinBox,
    QLabel, QCheckBox,
)

from .palette import C, button_qss, SCROLLBAR_QSS


class DeviceButton(QPushButton):
    """Drive button with custom click handling.

    Plain click           -> emit clicked_with_mods(idx, 'normal')
    Shift+click           -> emit clicked_with_mods(idx, 'both')   (both panels)
    Middle-click          -> emit clicked_with_mods(idx, 'right')  (right panel only)
    """
    clicked_with_mods = pyqtSignal(int, str)

    def __init__(self, idx: int, parent=None):
        super().__init__(parent)
        self._idx = idx

    def mousePressEvent(self, ev: QMouseEvent):
        # Determine click target: middle = right panel, shift+left =
        # both panels, plain left = active panel
        button = ev.button()
        mods = ev.modifiers()
        if button == Qt.MouseButton.MiddleButton:
            self.clicked_with_mods.emit(self._idx, 'right')
            ev.accept()
            return
        if button == Qt.MouseButton.LeftButton:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.clicked_with_mods.emit(self._idx, 'both')
                ev.accept()
                return
            self.clicked_with_mods.emit(self._idx, 'normal')
            ev.accept()
            return
        super().mousePressEvent(ev)


class DeviceColumn(QWidget):
    """Scrollable vertical stack of device buttons, shows 6 at a time
    with a vertical scrollbar on the right to reach the rest (up to 40)."""
    # Signal carries (path_or_label, target) where target is one of:
    #   'normal' - the active lister
    #   'both'   - both listers (Shift+click)
    #   'right'  - the right lister only (middle-click)
    navigate_requested = pyqtSignal(dict, str)
    devices_changed = pyqtSignal()

    # Button height (must match _BUTTON_HEIGHT in _rebuild)
    _BUTTON_HEIGHT = 22
    _VISIBLE_BUTTONS = 6

    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.devices = list(devices)
        self.setStyleSheet(f"background-color: {C.WB_GREY};")

        # Fixed height: exactly 6 buttons + 5 gaps (1px each) + 2px border
        target_h = (self._VISIBLE_BUTTONS * self._BUTTON_HEIGHT
                    + (self._VISIBLE_BUTTONS - 1) * 1 + 2)
        self.setFixedHeight(target_h)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        # Scrollable button column - slider always visible so the user
        # can see they can scroll more devices in/out.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {C.WB_GREY}; border: none; }}"
            + SCROLLBAR_QSS
        )

        self.inner = QWidget()
        self.inner.setStyleSheet(f"background-color: {C.WB_GREY};")
        self.inner_layout = QVBoxLayout(self.inner)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner_layout.setSpacing(1)
        self.inner_layout.addStretch()
        self.scroll.setWidget(self.inner)

        outer.addWidget(self.scroll, 1)

        self._rebuild()

    def _rebuild(self):
        # Clear existing buttons (leave the stretch at end)
        while self.inner_layout.count() > 1:
            item = self.inner_layout.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()

        for i, dev in enumerate(self.devices):
            btn = DeviceButton(i, parent=self.inner)
            btn.setText(dev["label"])
            # Distinguish FTP bookmarks visually with a small leading
            # indicator. Plain folder buttons keep the standard style.
            kind = dev.get("type", "local")
            if kind == "ftp":
                # Slightly different palette for FTP entries so they
                # stand out from plain folder shortcuts.
                btn.setStyleSheet(button_qss("dev_ftp")
                                    if "dev_ftp" in str(button_qss)
                                    else button_qss("orange"))
                tip = (f"FTP: {dev.get('host', '?')}:{dev.get('port', 21)}"
                       f"  /  user: {dev.get('user', '')}\n"
                       f"Click: open in active panel\n"
                       f"Shift+Click: open in BOTH panels\n"
                       f"Middle-Click: open in RIGHT panel")
            else:
                btn.setStyleSheet(button_qss("dev"))
                tip = (f"{dev['path']}\n"
                       f"Click: navigate active panel\n"
                       f"Shift+Click: navigate BOTH panels\n"
                       f"Middle-Click: navigate RIGHT panel")
            btn.setFixedHeight(self._BUTTON_HEIGHT)
            btn.setToolTip(tip)
            # All routing goes through the unified mod-aware signal.
            btn.clicked_with_mods.connect(self._on_button_clicked)
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, idx=i: self._ctx_menu(idx, pos))
            self.inner_layout.insertWidget(i, btn)

    def _on_button_clicked(self, idx: int, target: str):
        """Centralised handler. Looks up the device dict for `idx`
        and emits navigate_requested with the dict + target string."""
        if not (0 <= idx < len(self.devices)):
            return
        dev = self.devices[idx]
        self.navigate_requested.emit(dev, target)

    def _ctx_menu(self, idx, pos):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {C.WB_GREY}; color: {C.BLACK};
                    border: 1px solid {C.BLACK}; }}
            QMenu::item:selected {{ background-color: {C.SELECTED}; color: {C.WHITE}; }}
        """)
        menu.addAction("Add folder bookmark...", self._add_device)
        menu.addAction("Add FTP bookmark...", self._add_ftp_device)
        menu.addSeparator()
        menu.addAction("Edit...", lambda: self._edit_device(idx))
        menu.addAction("Remove", lambda: self._remove_device(idx))
        menu.addSeparator()
        menu.addAction("Move up", lambda: self._move_device(idx, -1))
        menu.addAction("Move down", lambda: self._move_device(idx, 1))
        menu.addAction("Move to top", lambda: self._move_device_to(idx, 0))
        menu.addAction("Move to bottom",
                       lambda: self._move_device_to(idx, len(self.devices) - 1))
        menu.addSeparator()
        menu.addAction("Edit all...", self._edit_list)
        btn = self.inner_layout.itemAt(idx).widget()
        if btn:
            menu.exec(btn.mapToGlobal(pos))

    def _add_device(self):
        """Add a folder bookmark via the rich dialog (label + paths
        for left/right panel + open-target). The dialog supports
        configuring DIFFERENT paths for left and right - useful for
        e.g. 'open my source tree on the left and the build folder
        on the right at the same time'."""
        if len(self.devices) >= 40:
            QMessageBox.information(self, "Devices", "Max 40 devices")
            return
        dlg = _FolderBookmarkDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.result_dict()
        self.devices.append(entry)
        self._rebuild()
        self.devices_changed.emit()

    def _add_ftp_device(self):
        """Add an FTP bookmark via a dedicated dialog. The bookmark
        stores host/port/user/path; the password is requested at
        connect time so it isn't kept in the config file."""
        if len(self.devices) >= 40:
            QMessageBox.information(self, "Devices", "Max 40 devices")
            return
        dlg = _FtpBookmarkDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.result_dict()
        self.devices.append(entry)
        self._rebuild()
        self.devices_changed.emit()

    def _edit_device(self, idx):
        """Edit an existing device entry. Routes to the right dialog
        based on the entry's type (local folder vs FTP bookmark)."""
        if not (0 <= idx < len(self.devices)):
            return
        cur = self.devices[idx]
        kind = cur.get("type", "local")
        if kind == "ftp":
            dlg = _FtpBookmarkDialog(self, initial=cur)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self.devices[idx] = dlg.result_dict()
        else:
            dlg = _FolderBookmarkDialog(self, initial=cur)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self.devices[idx] = dlg.result_dict()
        self._rebuild()
        self.devices_changed.emit()

    def _remove_device(self, idx):
        if not (0 <= idx < len(self.devices)): return
        reply = QMessageBox.question(
            self, "Remove device",
            f"Remove '{self.devices[idx]['label']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.devices.pop(idx)
            self._rebuild()
            self.devices_changed.emit()

    def _move_device(self, idx, delta):
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.devices)): return
        self.devices[idx], self.devices[new_idx] = self.devices[new_idx], self.devices[idx]
        self._rebuild()
        self.devices_changed.emit()

    def _move_device_to(self, idx, target_idx):
        """Move device from idx to target_idx, shifting others."""
        if not (0 <= idx < len(self.devices)): return
        target_idx = max(0, min(len(self.devices) - 1, target_idx))
        if target_idx == idx: return
        item = self.devices.pop(idx)
        self.devices.insert(target_idx, item)
        self._rebuild()
        self.devices_changed.emit()

    def _edit_list(self):
        text = "\n".join(f"{d['label']}|{d['path']}" for d in self.devices)
        new_text, ok = QInputDialog.getMultiLineText(
            self, "Edit devices (up to 40)",
            "One per line, format:   LABEL|/path/to/dir",
            text)
        if not ok: return
        new_devices = []
        for ln in new_text.strip().splitlines():
            if "|" in ln:
                label, path = ln.split("|", 1)
                label = label.strip(); path = path.strip()
                if label and path:
                    new_devices.append({"label": label, "path": path})
        if new_devices:
            self.devices = new_devices[:40]
            self._rebuild()
            self.devices_changed.emit()


# =============================================================
# FTP bookmark dialog (used by Add FTP bookmark... in DeviceColumn)
# =============================================================
class _FtpBookmarkDialog(QDialog):
    """Modal dialog to gather FTP-bookmark fields. Password is NOT
    persisted - it gets prompted at connect time."""
    def __init__(self, parent=None, initial=None):
        super().__init__(parent)
        cur = dict(initial) if initial else {}
        self.setWindowTitle("Edit FTP bookmark" if initial
                              else "Add FTP bookmark")
        self.setModal(True)
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QFormLayout(self)
        self._label = QLineEdit(cur.get("label", "FTP1"))
        lay.addRow("Button label:", self._label)
        self._host = QLineEdit(cur.get("host", ""))
        self._host.setPlaceholderText("ftp.example.com")
        lay.addRow("Host:", self._host)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(int(cur.get("port", 21)))
        lay.addRow("Port:", self._port)
        self._user = QLineEdit(cur.get("user", "anonymous"))
        lay.addRow("Username:", self._user)
        self._pass = QLineEdit(cur.get("password", ""))
        self._pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass.setPlaceholderText("(leave empty - prompt at connect)")
        lay.addRow("Password:", self._pass)
        self._save_pass = QCheckBox(
            "Save password (in plain text) - NOT recommended")
        self._save_pass.setChecked("password" in cur)
        lay.addRow("", self._save_pass)
        self._initial = QLineEdit(cur.get("path", "/"))
        lay.addRow("Initial path:", self._initial)
        self._mode = QComboBox()
        self._mode.addItems(["passive", "active"])
        cur_mode = cur.get("mode", "passive")
        if cur_mode in ("passive", "active"):
            self._mode.setCurrentText(cur_mode)
        lay.addRow("Transfer mode:", self._mode)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def result_dict(self) -> dict:
        d = {
            "type":  "ftp",
            "label": self._label.text().strip() or "FTP",
            "host":  self._host.text().strip(),
            "port":  int(self._port.value()),
            "user":  self._user.text().strip(),
            "path":  self._initial.text().strip() or "/",
            "mode":  self._mode.currentText(),
        }
        if self._save_pass.isChecked() and self._pass.text():
            d["password"] = self._pass.text()
        return d


# =============================================================
# Folder bookmark dialog
# =============================================================
class _FolderBookmarkDialog(QDialog):
    """Rich dialog for adding/editing a local-folder drive button.

    Configurable fields:
      - Label                (button text)
      - Left-panel path      (where the LEFT lister navigates)
      - Right-panel path     (where the RIGHT lister navigates;
                              empty = same as left)
      - Open in              ('active panel' / 'both panels' /
                              'left only' / 'right only')

    The 'open_in' field decides what a plain click does. The user
    can still override per-click via Shift (= both) or middle-click
    (= right) - those modifier shortcuts always work regardless of
    the configured default. Choosing 'both panels' as the default
    just means a plain click already opens both at once - useful
    for project-style entries where the same workspace involves a
    src + build folder pair.
    """
    def __init__(self, parent=None, initial=None):
        super().__init__(parent)
        cur = dict(initial) if initial else {}
        self.setWindowTitle("Edit folder bookmark" if initial
                              else "Add folder bookmark")
        self.setModal(True)
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QFormLayout(self)

        self._label = QLineEdit(cur.get("label", ""))
        self._label.setPlaceholderText("e.g. WORK or PROJECT or HOME")
        lay.addRow("Button label:", self._label)

        # Left path with a Browse button beside it
        left_row = QHBoxLayout()
        self._path_left = QLineEdit(cur.get("path", ""))
        self._path_left.setPlaceholderText("/home/me/work")
        left_row.addWidget(self._path_left, 1)
        b_l = QPushButton("Browse...")
        b_l.clicked.connect(lambda: self._browse(self._path_left))
        left_row.addWidget(b_l)
        lay.addRow("Left-panel path:", left_row)

        # Right path - optional, defaults to left
        right_row = QHBoxLayout()
        self._path_right = QLineEdit(cur.get("path_right", ""))
        self._path_right.setPlaceholderText(
            "(leave empty - use left path for both)")
        right_row.addWidget(self._path_right, 1)
        b_r = QPushButton("Browse...")
        b_r.clicked.connect(lambda: self._browse(self._path_right))
        right_row.addWidget(b_r)
        lay.addRow("Right-panel path:", right_row)

        # Default click behaviour
        self._open_in = QComboBox()
        self._open_in.addItems([
            "active panel  (default - click navigates the focused panel)",
            "both panels  (click opens both listers at once)",
            "left only    (click always navigates the left lister)",
            "right only   (click always navigates the right lister)",
        ])
        # Map config strings to indices
        idx_for_value = {"active": 0, "both": 1, "left": 2, "right": 3}
        cur_open = cur.get("open_in", "active")
        self._open_in.setCurrentIndex(idx_for_value.get(cur_open, 0))
        lay.addRow("Default click target:", self._open_in)

        # Hint label so the user knows about the modifier shortcuts
        hint = QLabel(
            "  Modifier shortcuts always work too:\n"
            "  • Shift+Click  → open in BOTH panels\n"
            "  • Middle-Click → open in RIGHT panel only\n"
            "  • Plain Click  → uses the default above")
        hint.setStyleSheet(f"color: {C.BLACK}; padding: 4px;")
        lay.addRow("", hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def _browse(self, line_edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(
            self, "Pick directory",
            line_edit.text() or str(Path.home()))
        if d:
            line_edit.setText(d)

    def _on_accept(self):
        if not self._label.text().strip():
            QMessageBox.warning(self, "Folder bookmark",
                                  "Please enter a button label.")
            return
        if not self._path_left.text().strip():
            QMessageBox.warning(self, "Folder bookmark",
                                  "Please enter the left-panel path.")
            return
        self.accept()

    def result_dict(self) -> dict:
        d = {
            "type":  "local",
            "label": self._label.text().strip(),
            "path":  self._path_left.text().strip(),
        }
        right = self._path_right.text().strip()
        if right and right != d["path"]:
            d["path_right"] = right
        # Map index back to short string code
        idx = self._open_in.currentIndex()
        d["open_in"] = ("active", "both", "left", "right")[idx]
        return d
