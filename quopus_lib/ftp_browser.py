"""FTP connection dialog + remote file browser with Quopus styling."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QFileDialog, QMessageBox, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QFormLayout, QWidget, QProgressBar, QInputDialog,
)

from .palette import (
    C, button_qss, fmt_size, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, WB_TITLEBAR_ACTIVE_QSS, INFOBAR_QSS,
)
from .ftp_backend import make_backend, RemoteEntry
from .config import scaled_font_px


# Maximum number of FTP bookmarks kept in config
MAX_BOOKMARKS = 30


# Remember last connection between sessions
# (stored in main config under "ftp_bookmarks")
PROTOCOLS = [
    ("FTP (plain)",             "ftp",            21),
    ("FTPS (explicit TLS)",     "ftps",           21),
    ("FTPS (implicit, 990)",    "ftps-implicit",  990),
    ("SFTP (SSH)",              "sftp",           22),
]


class FtpBookmarkManagerDialog(QDialog):
    """Manage saved FTP bookmarks: list, add, edit, delete, reorder, connect."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.config.setdefault("ftp_bookmarks", [])
        self.connect_choice = None   # set when user clicks "Connect"

        self.setWindowTitle("FTP Bookmarks")
        self.resize(720, 520)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "ftp_bookmarks")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(2)

        title = QLabel(
            f"  FTP Bookmarks  "
            f"({len(self.config['ftp_bookmarks'])}/{MAX_BOOKMARKS})  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)
        self._title_label = title

        # Tree
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            ["Name", "Protocol", "Host", "Port", "User"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {C.LISTER_BG}; color: {C.LISTER_FG};
                font-family: "Topaz-8","Courier New",monospace;
                font-size: {scaled_font_px(12)}px;
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
        self.tree.itemDoubleClicked.connect(self._connect_selected)
        root.addWidget(self.tree, 1)

        # Toolbar
        tool = QHBoxLayout(); tool.setSpacing(2)
        b_add = QPushButton("Add...")
        b_add.setStyleSheet(button_qss("blue"))
        b_add.clicked.connect(self._add_bookmark)
        tool.addWidget(b_add)

        b_edit = QPushButton("Edit...")
        b_edit.setStyleSheet(button_qss("blue"))
        b_edit.clicked.connect(self._edit_bookmark)
        tool.addWidget(b_edit)

        b_del = QPushButton("Delete")
        b_del.setStyleSheet(button_qss("red"))
        b_del.clicked.connect(self._delete_bookmark)
        tool.addWidget(b_del)

        b_up = QPushButton("▲ Up")
        b_up.setStyleSheet(button_qss("mid"))
        b_up.setFixedWidth(70)
        b_up.clicked.connect(lambda: self._move(-1))
        tool.addWidget(b_up)

        b_down = QPushButton("▼ Down")
        b_down.setStyleSheet(button_qss("mid"))
        b_down.setFixedWidth(80)
        b_down.clicked.connect(lambda: self._move(+1))
        tool.addWidget(b_down)

        tool.addStretch()

        b_conn = QPushButton("Connect  (Enter)")
        b_conn.setStyleSheet(button_qss("orange"))
        b_conn.setFixedWidth(160)
        b_conn.setDefault(True)
        b_conn.clicked.connect(self._connect_selected)
        tool.addWidget(b_conn)

        b_close = QPushButton("Close")
        b_close.setStyleSheet(button_qss("red"))
        b_close.setFixedWidth(100)
        b_close.clicked.connect(self.reject)
        tool.addWidget(b_close)
        root.addLayout(tool)

        self._populate()

        # Hotkeys
        QShortcut(QKeySequence("Return"), self, self._connect_selected)
        QShortcut(QKeySequence("Escape"), self, self.reject)
        QShortcut(QKeySequence("Del"), self, self._delete_bookmark)
        QShortcut(QKeySequence("Ins"), self, self._add_bookmark)

    # ------------------------------------------------------------------

    def _populate(self):
        self.tree.clear()
        for bm in self.config["ftp_bookmarks"]:
            it = QTreeWidgetItem([
                bm.get('name', '?'),
                bm.get('protocol', 'ftp'),
                bm.get('host', ''),
                str(bm.get('port', '')),
                bm.get('user', ''),
            ])
            self.tree.addTopLevelItem(it)
        self._title_label.setText(
            f"  FTP Bookmarks  "
            f"({len(self.config['ftp_bookmarks'])}/{MAX_BOOKMARKS})  ")
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

    def _selected_index(self):
        it = self.tree.currentItem()
        if not it: return -1
        return self.tree.indexOfTopLevelItem(it)

    def _add_bookmark(self):
        if len(self.config["ftp_bookmarks"]) >= MAX_BOOKMARKS:
            QMessageBox.warning(self, "Bookmark",
                f"Maximum of {MAX_BOOKMARKS} bookmarks reached. "
                "Delete some before adding more.")
            return
        dlg = FtpConnectDialog([], self, edit_mode=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_bm = self._kw_to_bookmark(dlg.result_kwargs)
            if not new_bm.get('name'):
                QMessageBox.warning(self, "Bookmark",
                    "Please give the bookmark a name.")
                return
            # Check for dup name
            existing_names = {b.get('name') for b in self.config["ftp_bookmarks"]}
            if new_bm['name'] in existing_names:
                if QMessageBox.question(self, "Duplicate",
                    f"A bookmark '{new_bm['name']}' already exists. "
                    f"Replace it?") != QMessageBox.StandardButton.Yes:
                    return
                self.config["ftp_bookmarks"] = [
                    b for b in self.config["ftp_bookmarks"]
                    if b.get('name') != new_bm['name']
                ]
            self.config["ftp_bookmarks"].append(new_bm)
            self._save()
            self._populate()

    def _edit_bookmark(self):
        idx = self._selected_index()
        if idx < 0: return
        bm = self.config["ftp_bookmarks"][idx]
        dlg = FtpConnectDialog([], self, edit_mode=True, initial=bm)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_bm = self._kw_to_bookmark(dlg.result_kwargs)
            if not new_bm.get('name'):
                QMessageBox.warning(self, "Bookmark", "Name is required.")
                return
            self.config["ftp_bookmarks"][idx] = new_bm
            self._save()
            self._populate()
            # Re-select
            if idx < self.tree.topLevelItemCount():
                self.tree.setCurrentItem(self.tree.topLevelItem(idx))

    def _delete_bookmark(self):
        idx = self._selected_index()
        if idx < 0: return
        bm = self.config["ftp_bookmarks"][idx]
        if QMessageBox.question(self, "Delete bookmark",
            f"Delete bookmark '{bm.get('name', '?')}'?") \
            != QMessageBox.StandardButton.Yes:
            return
        self.config["ftp_bookmarks"].pop(idx)
        self._save()
        self._populate()

    def _move(self, direction):
        idx = self._selected_index()
        if idx < 0: return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.config["ftp_bookmarks"]):
            return
        bms = self.config["ftp_bookmarks"]
        bms[idx], bms[new_idx] = bms[new_idx], bms[idx]
        self._save()
        self._populate()
        self.tree.setCurrentItem(self.tree.topLevelItem(new_idx))

    def _connect_selected(self):
        idx = self._selected_index()
        if idx < 0: return
        self.connect_choice = self.config["ftp_bookmarks"][idx]
        self.accept()

    def _kw_to_bookmark(self, kw):
        """Reduce the full result_kwargs dict to a bookmark dict."""
        return {k: v for k, v in {
            'name':        kw.get('name'),
            'protocol':    kw.get('protocol'),
            'host':        kw.get('host'),
            'port':        kw.get('port'),
            'user':        kw.get('user'),
            'password':    kw.get('password'),
            'keyfile':     kw.get('keyfile'),
            'remote_path': kw.get('remote_path'),
        }.items() if v is not None}

    def _save(self):
        from .config import save_config
        save_config(self.config)



class FtpConnectDialog(QDialog):
    """Simple connection form. Returns connection kwargs on accept."""

    def __init__(self, bookmarks=None, parent=None,
                 edit_mode=False, initial=None):
        """
        bookmarks: list of saved bookmarks (for the dropdown)
        edit_mode: True when invoked from the bookmark manager for
                   adding/editing (hides the Connect-related buttons,
                   shows Save instead)
        initial:   dict of initial values (used for editing)
        """
        super().__init__(parent)
        self.setWindowTitle(
            "FTP Bookmark" if edit_mode else "FTP Connect")
        self.resize(560, 400)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "ftp_connect")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.bookmarks = bookmarks or []
        self.result_kwargs = None
        self.edit_mode = edit_mode
        self.open_manager_after = False

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(4)

        title = QLabel("  FTP / FTPS / SFTP Connection  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        # Bookmarks (only shown outside edit mode)
        if not edit_mode:
            bm_row = QHBoxLayout()
            bm_row.addWidget(QLabel("Bookmarks:"))
            self.cb_bm = QComboBox()
            self.cb_bm.addItem("-- select a bookmark --")
            for bm in bookmarks:
                self.cb_bm.addItem(
                    f"{bm.get('name','?')} ({bm.get('protocol','ftp')}://"
                    f"{bm.get('host','')})")
            self.cb_bm.setStyleSheet(
                f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
                f"padding: 2px; }}")
            self.cb_bm.currentIndexChanged.connect(self._load_bookmark)
            bm_row.addWidget(self.cb_bm, 1)

            b_mgr = QPushButton("Manage...")
            b_mgr.setStyleSheet(button_qss("blue"))
            b_mgr.setFixedWidth(100)
            b_mgr.clicked.connect(self._open_manager)
            bm_row.addWidget(b_mgr)
            root.addLayout(bm_row)

        form = QFormLayout(); form.setSpacing(4)

        self.cb_proto = QComboBox()
        for label, _, _ in PROTOCOLS:
            self.cb_proto.addItem(label)
        self.cb_proto.setStyleSheet(
            f"QComboBox {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"padding: 2px; }}")
        self.cb_proto.currentIndexChanged.connect(self._proto_changed)
        form.addRow("Protocol:", self.cb_proto)

        self.le_host = self._line_edit("ftp.example.com")
        form.addRow("Host:", self.le_host)

        self.le_port = self._line_edit("21")
        form.addRow("Port:", self.le_port)

        self.le_user = self._line_edit("anonymous")
        form.addRow("User:", self.le_user)

        self.le_pw = self._line_edit("")
        self.le_pw.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.le_pw)

        # Optional SFTP keyfile
        kf_row = QHBoxLayout()
        self.le_keyfile = self._line_edit("")
        self.le_keyfile.setPlaceholderText("Optional SSH private key")
        kf_row.addWidget(self.le_keyfile, 1)
        b_kf = QPushButton("...")
        b_kf.setStyleSheet(button_qss("mid")); b_kf.setFixedWidth(40)
        b_kf.clicked.connect(self._pick_keyfile)
        kf_row.addWidget(b_kf)
        kf_wrap = QWidget(); kf_wrap.setLayout(kf_row)
        form.addRow("SSH keyfile:", kf_wrap)

        # Optional initial remote directory: after the connection is
        # established, the backend is asked to cwd() into this path.
        # Stored in the bookmark, so saved sites jump straight into
        # e.g. /pub/incoming or /home/user/upload on connect.
        self.le_remote_path = self._line_edit("")
        self.le_remote_path.setPlaceholderText(
            "Optional - cwd here after connect (e.g. /pub/incoming)")
        form.addRow("Remote dir:", self.le_remote_path)

        self.le_name = self._line_edit("")
        if edit_mode:
            self.le_name.setPlaceholderText("Bookmark name (required)")
        else:
            self.le_name.setPlaceholderText("Optional - save as bookmark")
        form.addRow("Save as:" if not edit_mode else "Name:", self.le_name)

        # Drive-button anlegen-Option (nur im Connect-Mode, nicht beim
        # reinen Bookmark-Edit). Wenn der Haken gesetzt ist, wird die
        # Verbindung NACH dem Connect zusätzlich als Drive-Button im
        # linken Drive-Panel angelegt - so kann der User mit einem
        # einzigen Klick wieder hierher zurück.
        if not edit_mode:
            from PyQt6.QtWidgets import QCheckBox
            self.cb_drive_btn = QCheckBox(
                "  also add as drive button (left panel)")
            self.cb_drive_btn.setStyleSheet(
                f"QCheckBox {{ color: {C.BLACK}; }}")
            self.cb_drive_btn.setToolTip(
                "After connecting, also add this FTP location as a "
                "drive button in the left column. The button label "
                "comes from the 'Drive label:' field below; if empty, "
                "the host name is used.")
            form.addRow("", self.cb_drive_btn)
            self.le_drive_label = self._line_edit("")
            self.le_drive_label.setPlaceholderText(
                "(optional - defaults to host name in upper-case)")
            form.addRow("Drive label:", self.le_drive_label)

            # NEW: also offer to add the FTP connection as an action
            # button (the 6x6 grid at the bottom of Quopus, not the
            # left drive column). Requires the bookmark "Save as:"
            # name to be filled - the action stores the bookmark name
            # as its param so a click connects to that bookmark
            # directly via the ftp_site action.
            self.cb_action_btn = QCheckBox(
                "  also add as action button (6x6 grid)")
            self.cb_action_btn.setStyleSheet(
                f"QCheckBox {{ color: {C.BLACK}; }}")
            self.cb_action_btn.setToolTip(
                "Also add this FTP connection as an action button. "
                "When ticked + you click Save Bookmark or Connect, "
                "Quopus will ask which empty cell of the 6x6 button "
                "grid to put it in. The button uses the 'ftp_site' "
                "action with the bookmark name as its param, so a "
                "single click reconnects to this server.")
            form.addRow("", self.cb_action_btn)
            self.le_action_label = self._line_edit("")
            self.le_action_label.setPlaceholderText(
                "(optional - defaults to bookmark name)")
            form.addRow("Action label:", self.le_action_label)

            # NEW: separate checkbox for an UPLOAD-style action button
            # using the 'ftp_upload' action. A single click on that
            # button connects, cwds into the bookmark's remote_path,
            # then uploads everything currently selected/tagged in the
            # OTHER panel - one-shot drop-zone style. Both checkboxes
            # can be ticked at once to create two buttons.
            self.cb_upload_btn = QCheckBox(
                "  also add as upload action button (6x6 grid)")
            self.cb_upload_btn.setStyleSheet(
                f"QCheckBox {{ color: {C.BLACK}; }}")
            self.cb_upload_btn.setToolTip(
                "Also add this FTP connection as an UPLOAD action "
                "button. Pressing that button connects, cd's into "
                "the bookmark's 'Remote dir', then immediately uploads "
                "all files currently selected/tagged in the OTHER "
                "panel into that directory. Use this for one-click "
                "'send selection to host X' drop-zones. Configure the "
                "Remote dir field above so files land in the right "
                "place.")
            form.addRow("", self.cb_upload_btn)
            self.le_upload_label = self._line_edit("")
            self.le_upload_label.setPlaceholderText(
                "(optional - defaults to '<name> upload')")
            form.addRow("Upload label:", self.le_upload_label)
        else:
            self.cb_drive_btn = None
            self.le_drive_label = None
            self.cb_action_btn = None
            self.le_action_label = None
            self.cb_upload_btn = None
            self.le_upload_label = None

        form_wrap = QWidget(); form_wrap.setLayout(form)
        root.addWidget(form_wrap)
        root.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        if edit_mode:
            b_ok = QPushButton("Save")
            b_ok.setStyleSheet(button_qss("orange"))
            b_ok.setFixedWidth(110)
            b_ok.clicked.connect(self._on_save_only)
            btn_row.addWidget(b_ok)
        else:
            # Connect (= the main action) is the orange/highlighted
            # button. Save Bookmark is the secondary button: stores
            # the entry as a saved bookmark WITHOUT connecting, so
            # the user can build up a bookmark list before they're
            # ready to actually open a session.
            b_save_bm = QPushButton("Save Bookmark")
            b_save_bm.setStyleSheet(button_qss("blue"))
            b_save_bm.setFixedWidth(140)
            b_save_bm.setToolTip(
                "Save these connection details as an FTP bookmark "
                "without connecting now. Requires the 'Save as:' "
                "field above to be filled with a bookmark name.")
            b_save_bm.clicked.connect(self._on_save_bookmark_only)
            btn_row.addWidget(b_save_bm)
            b_connect = QPushButton("Connect")
            b_connect.setStyleSheet(button_qss("orange"))
            b_connect.setFixedWidth(110)
            b_connect.clicked.connect(self._on_connect)
            btn_row.addWidget(b_connect)
        b_cancel = QPushButton("Cancel")
        b_cancel.setStyleSheet(button_qss("red"))
        b_cancel.setFixedWidth(100)
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_cancel)
        root.addLayout(btn_row)

        # Apply initial values (for edit mode, or just the preferred setup)
        if initial:
            proto_key = initial.get('protocol', 'ftp')
            for i, (_, p, _) in enumerate(PROTOCOLS):
                if p == proto_key:
                    self.cb_proto.setCurrentIndex(i); break
            self.le_host.setText(initial.get('host', ''))
            self.le_port.setText(str(initial.get('port', 21)))
            self.le_user.setText(initial.get('user', 'anonymous'))
            self.le_pw.setText(initial.get('password', ''))
            self.le_keyfile.setText(initial.get('keyfile', '') or '')
            self.le_name.setText(initial.get('name', ''))
            self.le_remote_path.setText(initial.get('remote_path', '') or '')

    def _open_manager(self):
        """Open the manager - main code will re-open the connect dialog
        afterwards with fresh bookmark data."""
        # Closing-ourselves-with-a-flag so the calling code can loop back
        # to the manager and then re-open the connect dialog.
        self.open_manager_after = True
        self.reject()

    def _on_save_bookmark_only(self):
        """Connect-mode 'Save Bookmark' button: store the entry as a
        saved FTP bookmark WITHOUT connecting now. Name is required.
        Uses a separate flag (`save_bookmark_only`) so the caller in
        actions.py can persist + close without trying to connect."""
        if not self.le_name.text().strip():
            QMessageBox.warning(
                self, "Name required",
                "Please give the bookmark a name in 'Save as:' before "
                "saving.\n\nThe name is what shows up in the bookmark "
                "dropdown later.")
            return
        proto_idx = self.cb_proto.currentIndex()
        proto_key = PROTOCOLS[proto_idx][1]
        try:
            port = int(self.le_port.text().strip() or "21")
        except ValueError:
            QMessageBox.warning(self, "Port", "Invalid port.")
            return
        host = self.le_host.text().strip()
        if not host:
            QMessageBox.warning(self, "Host", "Host is required.")
            return
        self.result_kwargs = {
            'protocol': proto_key,
            'host': host,
            'port': port,
            'user': self.le_user.text().strip() or 'anonymous',
            'password': self.le_pw.text(),
            'keyfile': self.le_keyfile.text().strip() or None,
            'name': self.le_name.text().strip(),
            'remote_path': self.le_remote_path.text().strip() or None,
            # Tell the caller: don't try to connect, just save.
            'save_bookmark_only': True,
        }
        # Optional: also queue an add-as-drive-button request
        if self.cb_drive_btn is not None and self.cb_drive_btn.isChecked():
            label = (self.le_drive_label.text().strip()
                      or host.upper() or "FTP")
            self.result_kwargs['add_as_drive_button'] = True
            self.result_kwargs['drive_label'] = label
        # Optional: also queue an add-as-action-button request
        if self.cb_action_btn is not None \
                and self.cb_action_btn.isChecked():
            self.result_kwargs['add_as_action_button'] = True
            self.result_kwargs['action_label'] = (
                self.le_action_label.text().strip()
                or self.le_name.text().strip())
        # Optional: also queue an add-as-upload-button request
        # (separate from the regular action button so the user can
        # have both, or just one, on the 6x6 grid).
        if self.cb_upload_btn is not None \
                and self.cb_upload_btn.isChecked():
            self.result_kwargs['add_as_upload_button'] = True
            self.result_kwargs['upload_label'] = (
                self.le_upload_label.text().strip()
                or f"{self.le_name.text().strip()} upload")
        self.accept()

    def _on_save_only(self):
        """For edit_mode: save without connecting. Name is required."""
        if not self.le_name.text().strip():
            QMessageBox.warning(self, "Name required",
                "Please give the bookmark a name.")
            return
        # Use the same gathering as _on_connect but skip validation of host
        proto_idx = self.cb_proto.currentIndex()
        proto_key = PROTOCOLS[proto_idx][1]
        try:
            port = int(self.le_port.text().strip() or "21")
        except ValueError:
            QMessageBox.warning(self, "Port", "Invalid port.")
            return
        self.result_kwargs = {
            'protocol': proto_key,
            'host': self.le_host.text().strip(),
            'port': port,
            'user': self.le_user.text().strip() or 'anonymous',
            'password': self.le_pw.text(),
            'keyfile': self.le_keyfile.text().strip() or None,
            'name': self.le_name.text().strip(),
            'remote_path': self.le_remote_path.text().strip() or None,
        }
        self.accept()

    def _line_edit(self, text):
        le = QLineEdit(text)
        le.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; }}")
        return le

    def _proto_changed(self, idx):
        if 0 <= idx < len(PROTOCOLS):
            self.le_port.setText(str(PROTOCOLS[idx][2]))

    def _pick_keyfile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH private key")
        if path:
            self.le_keyfile.setText(path)

    def _load_bookmark(self, idx):
        if idx <= 0: return
        bm = self.bookmarks[idx - 1]
        # Match protocol
        proto_key = bm.get('protocol', 'ftp')
        for i, (_, p, _) in enumerate(PROTOCOLS):
            if p == proto_key:
                self.cb_proto.setCurrentIndex(i); break
        self.le_host.setText(bm.get('host', ''))
        self.le_port.setText(str(bm.get('port', 21)))
        self.le_user.setText(bm.get('user', 'anonymous'))
        self.le_pw.setText(bm.get('password', ''))
        self.le_keyfile.setText(bm.get('keyfile', '') or '')
        self.le_name.setText(bm.get('name', ''))
        self.le_remote_path.setText(bm.get('remote_path', '') or '')

    def _on_connect(self):
        proto_idx = self.cb_proto.currentIndex()
        proto_key = PROTOCOLS[proto_idx][1]
        try:
            port = int(self.le_port.text().strip() or "21")
        except ValueError:
            QMessageBox.warning(self, "Connect", "Invalid port.")
            return
        self.result_kwargs = {
            'protocol': proto_key,
            'host': self.le_host.text().strip(),
            'port': port,
            'user': self.le_user.text().strip() or 'anonymous',
            'password': self.le_pw.text(),
            'keyfile': self.le_keyfile.text().strip() or None,
            'name': self.le_name.text().strip(),
            'remote_path': self.le_remote_path.text().strip() or None,
        }
        # If the user ticked "also add as drive button", carry the
        # drive label too (defaults to host name in upper-case so the
        # caller has something usable even if left blank).
        if self.cb_drive_btn is not None and self.cb_drive_btn.isChecked():
            host = self.result_kwargs['host']
            label = (self.le_drive_label.text().strip()
                      or host.upper() or "FTP")
            self.result_kwargs['add_as_drive_button'] = True
            self.result_kwargs['drive_label'] = label
        # Same for the action-button checkbox
        if self.cb_action_btn is not None \
                and self.cb_action_btn.isChecked():
            self.result_kwargs['add_as_action_button'] = True
            self.result_kwargs['action_label'] = (
                self.le_action_label.text().strip()
                or self.le_name.text().strip()
                or self.result_kwargs['host'].upper())
        # Same for the upload-button checkbox
        if self.cb_upload_btn is not None \
                and self.cb_upload_btn.isChecked():
            self.result_kwargs['add_as_upload_button'] = True
            self.result_kwargs['upload_label'] = (
                self.le_upload_label.text().strip()
                or f"{self.le_name.text().strip()} upload"
                or f"{self.result_kwargs['host'].upper()} up")
        if not self.result_kwargs['host']:
            QMessageBox.warning(self, "Connect", "Host is required.")
            return
        self.accept()


# ============================================================
# File transfer worker thread
# ============================================================
class _TransferThread(QThread):
    progress = pyqtSignal(int, int)  # done, total
    finished_ok = pyqtSignal(str)
    finished_error = pyqtSignal(str)

    def __init__(self, backend, direction, remote_name, local_path):
        super().__init__()
        self.backend = backend
        self.direction = direction  # 'download' or 'upload'
        self.remote_name = remote_name
        self.local_path = local_path

    def run(self):
        try:
            if self.direction == 'download':
                self.backend.download(self.remote_name, self.local_path,
                                      progress=self.progress.emit)
            else:
                self.backend.upload(self.local_path, self.remote_name,
                                    progress=self.progress.emit)
            self.finished_ok.emit(self.remote_name)
        except Exception as e:
            self.finished_error.emit(f"{self.remote_name}: {e}")


# ============================================================
# FTP browser window
# ============================================================
class FtpBrowserDialog(QDialog):
    """Browse a remote FS and download/upload/rename/delete files."""

    def __init__(self, backend, connection_name, parent=None,
                 local_default_dir=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle(f"FTP: {connection_name}")
        self.resize(1000, 680)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "ftp_browser")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.local_default_dir = Path(local_default_dir or Path.home())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2); layout.setSpacing(2)

        title = QLabel(f"  FTP: {connection_name}  ")
        title.setStyleSheet(WB_TITLEBAR_ACTIVE_QSS)
        layout.addWidget(title)

        # Path bar
        path_row = QHBoxLayout(); path_row.setSpacing(2)
        self.btn_up = QPushButton("^"); self.btn_up.setStyleSheet(button_qss("mid"))
        self.btn_up.setFixedWidth(28); self.btn_up.setToolTip("Parent dir")
        self.btn_up.clicked.connect(self._go_parent)
        path_row.addWidget(self.btn_up)

        self.btn_home = QPushButton("/"); self.btn_home.setStyleSheet(button_qss("mid"))
        self.btn_home.setFixedWidth(28); self.btn_home.setToolTip("Go to /")
        self.btn_home.clicked.connect(lambda: self._cd("/"))
        path_row.addWidget(self.btn_home)

        self.le_path = QLineEdit()
        self.le_path.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"border: 1px solid {C.BLACK}; padding: 2px 4px; "
            f"font-family: 'Topaz-8','Courier New',monospace; }}")
        self.le_path.returnPressed.connect(
            lambda: self._cd(self.le_path.text().strip()))
        path_row.addWidget(self.le_path, 1)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setStyleSheet(button_qss("blue"))
        self.btn_refresh.setFixedWidth(80)
        self.btn_refresh.clicked.connect(self._refresh)
        path_row.addWidget(self.btn_refresh)
        layout.addLayout(path_row)

        # File list
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Size", "Date"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
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
                font-family: "Topaz-8","Topaz",monospace; font-weight: bold;
                padding: 2px 8px; border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        # Toolbar
        tool = QHBoxLayout(); tool.setSpacing(2)
        btns = [
            ("Download",  button_qss("orange"),  self._download),
            ("Upload...", button_qss("orange"),  self._upload),
            ("Delete",    button_qss("red"),     self._delete),
            ("Mkdir",     button_qss("purple"),  self._mkdir),
            ("Rename",    button_qss("purple"),  self._rename),
        ]
        for lbl, qss, fn in btns:
            b = QPushButton(lbl); b.setStyleSheet(qss); b.setFixedWidth(100)
            b.clicked.connect(fn)
            tool.addWidget(b)
        tool.addStretch()
        b_close = QPushButton("Disconnect")
        b_close.setStyleSheet(button_qss("red"))
        b_close.setFixedWidth(110)
        b_close.clicked.connect(self.accept)
        tool.addWidget(b_close)
        layout.addLayout(tool)

        # Status + progress
        self.progress = QProgressBar()
        self.progress.setStyleSheet(f"""
            QProgressBar {{ background-color: {C.WHITE}; color: {C.BLACK};
                            border: 1px solid {C.BLACK};
                            text-align: center; font-family: 'Topaz','monospace'; }}
            QProgressBar::chunk {{ background-color: {C.SELECTED}; }}
        """)
        self.progress.setFixedHeight(16)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Keyboard shortcuts
        QShortcut(QKeySequence("F5"), self, self._download)
        QShortcut(QKeySequence("F6"), self, self._upload)
        QShortcut(QKeySequence("F7"), self, self._mkdir)
        QShortcut(QKeySequence("F8"), self, self._delete)
        QShortcut(QKeySequence("Del"), self, self._delete)
        QShortcut(QKeySequence("F2"), self, self._rename)
        QShortcut(QKeySequence("Ctrl+R"), self, self._refresh)
        QShortcut(QKeySequence("Backspace"), self, self._go_parent)
        QShortcut(QKeySequence("Escape"), self, self.accept)

        self._refresh()

    # ------------------------------------------------------------------

    def _refresh(self):
        try:
            entries = self.backend.list_dir()
            pwd = self.backend.pwd()
        except Exception as e:
            QMessageBox.warning(self, "FTP", f"List failed: {e}")
            return
        self.le_path.setText(pwd)
        self.tree.clear()
        # Dirs first, then files
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        n_dirs = n_files = 0; total = 0
        for e in entries:
            name = e.name + ("/" if e.is_dir else "")
            size_s = "<DIR>" if e.is_dir else fmt_size(e.size)
            date_s = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else ""
            it = QTreeWidgetItem([name, size_s, date_s])
            it.setData(0, Qt.ItemDataRole.UserRole, e)
            if e.is_dir:
                it.setForeground(0, Qt.GlobalColor.blue)
                n_dirs += 1
            else:
                n_files += 1; total += e.size
            self.tree.addTopLevelItem(it)

        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.lbl_status.setText(
            f" {n_dirs} dirs, {n_files} files, {fmt_size(total)}  |  {pwd} ")

    def _selected_entries(self):
        out = []
        for it in self.tree.selectedItems():
            e = it.data(0, Qt.ItemDataRole.UserRole)
            if e: out.append(e)
        return out

    def _cd(self, path):
        try:
            self.backend.cwd(path)
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "FTP", f"Cannot cd: {e}")

    def _go_parent(self):
        self._cd("..")

    def _on_double_click(self, item, col):
        e = item.data(0, Qt.ItemDataRole.UserRole)
        if e is None: return
        if e.is_dir:
            self._cd(e.name)
        else:
            # Download + open via TextReader/ImageViewer auto
            self._download_and_view(e)

    # ------------------------------------------------------------------

    def _download(self):
        items = self._selected_entries()
        files = [e for e in items if not e.is_dir]
        if not files:
            self.lbl_status.setText(" Nothing selected to download ")
            return
        outdir = QFileDialog.getExistingDirectory(
            self, "Download to...", str(self.local_default_dir))
        if not outdir: return
        self.local_default_dir = Path(outdir)
        self._run_transfers(
            [(f.name, str(Path(outdir) / f.name), 'download') for f in files])

    def _upload(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Upload files...", str(self.local_default_dir))
        if not paths: return
        self.local_default_dir = Path(paths[0]).parent
        self._run_transfers(
            [(os.path.basename(p), p, 'upload') for p in paths])

    def _run_transfers(self, jobs):
        """Run a list of (remote_name, local_path, direction) serially."""
        self._jobs = list(jobs)
        self._job_count_total = len(jobs)
        self._job_index = 0
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._run_next_job()

    def _run_next_job(self):
        if self._job_index >= len(self._jobs):
            self.progress.setVisible(False)
            self.lbl_status.setText(
                f" {self._job_count_total} transfer(s) complete ")
            self._refresh()
            return
        remote, local, direction = self._jobs[self._job_index]
        self.lbl_status.setText(
            f" {direction.title()} [{self._job_index+1}/{self._job_count_total}]: "
            f"{remote} ")
        self._thread = _TransferThread(self.backend, direction, remote, local)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_ok.connect(self._on_job_done)
        self._thread.finished_error.connect(self._on_job_error)
        self._thread.start()

    def _on_progress(self, done, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{fmt_size(done)} / {fmt_size(total)}")
        else:
            self.progress.setRange(0, 0)

    def _on_job_done(self, _name):
        self._job_index += 1
        self._run_next_job()

    def _on_job_error(self, msg):
        QMessageBox.warning(self, "Transfer", msg)
        self._job_index += 1
        self._run_next_job()

    # ------------------------------------------------------------------

    def _download_and_view(self, entry):
        """Download to temp file and open with the internal viewer."""
        tmp = Path(tempfile.gettempdir()) / f"dopus_ftp_{os.getpid()}_{entry.name}"
        try:
            self.backend.download(entry.name, str(tmp))
        except Exception as e:
            QMessageBox.warning(self, "Download", f"{entry.name}: {e}")
            return
        # Dispatch via image/archive/text
        try:
            from .image_viewer import is_image, ImageViewer
            if is_image(tmp):
                ImageViewer(tmp, self).exec(); return
            from .archive_viewer import is_archive, ArchiveViewer
            if is_archive(tmp):
                ArchiveViewer(tmp, self).exec(); return
            from .readers import TextReader
            TextReader(tmp, self).exec()
        finally:
            try: tmp.unlink()
            except Exception: pass

    def _delete(self):
        items = self._selected_entries()
        if not items:
            self.lbl_status.setText(" Nothing selected "); return
        names = [e.name for e in items]
        if QMessageBox.question(
                self, "Delete",
                f"Delete {len(names)} item(s)?\n\n" + "\n".join(names[:10]) +
                ("\n..." if len(names) > 10 else "")
            ) != QMessageBox.StandardButton.Yes:
            return
        n_ok = 0
        for e in items:
            try:
                if e.is_dir: self.backend.rmdir(e.name)
                else:        self.backend.delete(e.name)
                n_ok += 1
            except Exception as ex:
                QMessageBox.warning(self, "Delete", f"{e.name}: {ex}")
        self.lbl_status.setText(f" Deleted {n_ok} item(s) ")
        self._refresh()

    def _mkdir(self):
        name, ok = QInputDialog.getText(self, "Mkdir", "New directory name:")
        if not ok or not name.strip(): return
        try:
            self.backend.mkdir(name.strip())
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "Mkdir", str(e))

    def _rename(self):
        items = self._selected_entries()
        if not items: return
        e = items[0]
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        if not ok or not new.strip() or new == e.name: return
        try:
            self.backend.rename(e.name, new.strip())
            self._refresh()
        except Exception as ex:
            QMessageBox.warning(self, "Rename", str(ex))

    def closeEvent(self, ev):
        try: self.backend.disconnect()
        except Exception: pass
        super().closeEvent(ev)


# ============================================================
# Entry point
# ============================================================
def open_ftp(parent, config):
    """Show connect dialog, then on success open the browser.
    config is the main Quopus config (for bookmarks).
    If the user clicks 'Manage...' in the connect dialog, the manager
    opens, and after closing we re-open the connect dialog with fresh
    bookmark data (loop)."""
    while True:
        bookmarks = config.get("ftp_bookmarks", [])
        dlg = FtpConnectDialog(bookmarks, parent)
        dlg.exec()
        if dlg.open_manager_after:
            # User clicked Manage... open manager, then loop back
            mgr = FtpBookmarkManagerDialog(config, parent)
            if mgr.exec() == QDialog.DialogCode.Accepted and mgr.connect_choice:
                kwargs = _bookmark_to_kwargs(mgr.connect_choice)
                _connect_and_browse(parent, config, kwargs)
                return
            # otherwise loop back to the connect dialog
            continue
        if dlg.result_kwargs is None:
            return  # cancelled
        kwargs = dlg.result_kwargs
        break

    # Save bookmark if a name was provided
    if kwargs.get('name'):
        _save_bookmark(config, kwargs, parent)

    _connect_and_browse(parent, config, kwargs)


def _bookmark_to_kwargs(bm):
    """Convert a stored bookmark dict to connect kwargs."""
    return {
        'protocol': bm.get('protocol', 'ftp'),
        'host':     bm.get('host', ''),
        'port':     bm.get('port', 21),
        'user':     bm.get('user', 'anonymous'),
        'password': bm.get('password', ''),
        'keyfile':  bm.get('keyfile'),
        'name':     bm.get('name', ''),
    }


def _save_bookmark(config, kwargs, parent):
    """Save a bookmark, enforce the 30-entry limit."""
    name = kwargs['name']
    config.setdefault("ftp_bookmarks", [])

    # Replace if same name
    existing = [b for b in config["ftp_bookmarks"] if b.get('name') != name]

    # Check limit (only counts after replacement)
    if len(existing) >= MAX_BOOKMARKS:
        QMessageBox.warning(parent, "Bookmark limit",
            f"Cannot add '{name}': maximum of {MAX_BOOKMARKS} bookmarks reached. "
            f"Use 'Manage...' to delete old ones.")
        return

    bm = {k: kwargs[k] for k in
          ('name', 'protocol', 'host', 'port', 'user', 'password',
           'keyfile', 'remote_path')
          if kwargs.get(k) is not None}
    config["ftp_bookmarks"] = existing + [bm]
    from .config import save_config
    save_config(config)


def _connect_and_browse(parent, config, kwargs):
    """Open backend connection and launch the FTP browser."""
    try:
        backend = make_backend(
            protocol=kwargs['protocol'],
            host=kwargs['host'],
            port=kwargs['port'],
            user=kwargs['user'],
            password=kwargs['password'],
            keyfile=kwargs.get('keyfile'),
        )
        backend.connect()
    except Exception as e:
        QMessageBox.warning(parent, "FTP", f"Connection failed:\n{e}")
        return

    label = kwargs.get('name') or f"{kwargs['protocol']}://{kwargs['host']}"
    browser = FtpBrowserDialog(backend, label, parent)
    browser.exec()
