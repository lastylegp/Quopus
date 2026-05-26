"""Rclone browser dialog for Quopus Commander.

Interactive UI for browsing cloud-stored files via rclone. Two-
pane layout:

    Left  - Remotes list (one entry per configured rclone remote,
            with friendly type label)
    Right - Files in current remote/path (tree view, drill-down
            on double-click)

Toolbar above the files view: path breadcrumbs, up, refresh,
download-to-inactive-lister, upload-from-active-lister, delete,
new folder, rename.

We keep the dialog non-modal so the user can keep working in
the main Quopus window while transfers run. The transfer workers
emit progress signals that show up in the status bar at the
bottom of this dialog.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QSplitter, QMessageBox, QInputDialog, QFileDialog,
    QProgressDialog, QApplication, QMenu, QWidget,
)

from . import rclone_backend
from .rclone_backend import (
    RcloneError, RcloneNotFoundError, RcloneRemote, RcloneEntry,
)
from .config import scaled_font_px


# ---------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------


class _ListRemotesWorker(QThread):
    """Fetch the list of configured remotes in the background.
    `rclone listremotes` is fast but it does spawn a subprocess
    every time and we don't want to freeze the UI even for the
    ~200ms that takes on a cold cache."""
    done = pyqtSignal(bool, object, str)
    # ok, list[RcloneRemote] or None, error message

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager

    def run(self):
        try:
            remotes = self.manager.list_remotes()
            self.done.emit(True, remotes, "")
        except RcloneNotFoundError as e:
            self.done.emit(False, None, str(e))
        except RcloneError as e:
            self.done.emit(False, None, str(e))
        except Exception as e:
            self.done.emit(False, None, f"unexpected: {e}")


class _ListDirWorker(QThread):
    """Fetch the contents of one directory on a remote. lsjson
    can take a few seconds on large directories so we run it in
    a worker - otherwise navigating into a 'photos' folder with
    50k files would lock the UI."""
    done = pyqtSignal(bool, object, str)
    # ok, list[RcloneEntry] or None, error message

    def __init__(self, manager, remote, path, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.remote = remote
        self.path = path

    def run(self):
        try:
            entries = self.manager.list_dir(self.remote, self.path)
            self.done.emit(True, entries, "")
        except RcloneError as e:
            self.done.emit(False, None, str(e))
        except Exception as e:
            self.done.emit(False, None, f"unexpected: {e}")


class _TransferWorker(QThread):
    """Run a copy in either direction. Used for both upload and
    download. The `is_upload` flag picks which RcloneManager
    method to call.

    rclone's --progress output looks like:

        Transferred:    1.234 GiB / 5.678 GiB, 22%, 12.3 MiB/s, ETA 5m12s
        Transferred:            5 / 12, 41%
        Elapsed time:         3.4s
        Transferring:
         *                              foo.zip:  43% /1.234Gi, 12.3Mi/s, 5m12s

    We parse each line into structured fields (percent, bytes
    transferred, total, speed, ETA, current file) and emit those
    so the UI can drive a real QProgressBar + speed/ETA labels
    instead of just dumping the raw text.

    `progress` is now a dict: {
        'percent': int 0..100 or -1 if unknown,
        'transferred_str': '1.234 GiB',
        'total_str': '5.678 GiB',
        'speed_str': '12.3 MiB/s',
        'eta_str': '5m12s',
        'current_file': 'foo.zip',
        'raw': original line for debug
    }
    """
    progress = pyqtSignal(dict)
    done = pyqtSignal(bool, int, str)
    # ok (returncode==0), returncode, error message

    def __init__(self, manager, *, is_upload, local_path, remote,
                 remote_path, parent=None, is_sync=False):
        super().__init__(parent)
        self.manager = manager
        self.is_upload = is_upload
        self.is_sync = is_sync
        self.local_path = local_path
        self.remote = remote
        self.remote_path = remote_path
        self._cancelled = False
        # Popen handle, set from process_callback inside
        # _run_streaming once rclone has actually started.
        # We need this so cancel() can terminate() the process
        # cleanly from the GUI thread.
        self._proc = None
        # Sticky state: rclone prints separate lines for the
        # overall transfer and the current-file detail. We
        # accumulate the most-recent value of each so the UI
        # always has the latest data when the next line emits.
        self._cur_state = {
            'percent': -1, 'transferred_str': '',
            'total_str': '', 'speed_str': '',
            'eta_str': '', 'current_file': '', 'raw': '',
        }

    def cancel(self):
        """Best-effort cancel: terminate the rclone subprocess
        if we have a handle. The QThread's run() loop will then
        exit naturally when the subprocess closes its stdout.
        On Windows terminate() sends WM_CLOSE; on Unix it's
        SIGTERM. Either way rclone has a chance to clean up
        partially-uploaded chunks before exiting."""
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self):
        try:
            cb = self._on_progress_line
            # Capture the Popen handle so cancel() can terminate
            # the subprocess from the GUI thread.
            def on_started(proc):
                self._proc = proc
            if self.is_upload:
                if self.is_sync:
                    rc = self.manager.sync_to_remote(
                        self.local_path, self.remote,
                        self.remote_path,
                        progress_callback=cb,
                        process_callback=on_started)
                else:
                    rc = self.manager.copy_to_remote(
                        self.local_path, self.remote,
                        self.remote_path,
                        progress_callback=cb,
                        process_callback=on_started)
            else:
                if self.is_sync:
                    rc = self.manager.sync_from_remote(
                        self.remote, self.remote_path,
                        self.local_path,
                        progress_callback=cb,
                        process_callback=on_started)
                else:
                    rc = self.manager.copy_from_remote(
                        self.remote, self.remote_path,
                        self.local_path,
                        progress_callback=cb,
                        process_callback=on_started)
            self.done.emit(rc == 0, rc, "")
        except Exception as e:
            self.done.emit(False, -1, str(e))

    def _on_progress_line(self, line):
        """Parse one stdout line from `rclone copy --progress`
        and emit a structured update. We recognise two main
        line formats:

          1. "Transferred:   <bytes>/<total>, <pct>%, <speed>, ETA <eta>"
             (overall progress summary)
          2. " * <filename>: <pct>% /<size>, <speed>, <eta>"
             (current file detail line)

        Many lines (Elapsed time, Errors, blank, headers) carry
        no progress info - we skip those silently. The sticky
        _cur_state means a current-file line still gets emitted
        with the latest overall percent from a previous line.
        """
        line = line.strip()
        if not line:
            return
        import re
        # Overall summary line
        m = re.match(
            r"^Transferred:\s+"
            r"([\d.]+\s*[KMGTPEi]*B)\s*/\s*"
            r"([\d.]+\s*[KMGTPEi]*B)"
            r"(?:,\s*(\d+)%)?"
            r"(?:,\s*([\d.]+\s*[KMGTPEi]*B/s))?"
            r"(?:,\s*ETA\s*(\S+))?",
            line)
        if m:
            self._cur_state['transferred_str'] = m.group(1)
            self._cur_state['total_str'] = m.group(2)
            if m.group(3):
                self._cur_state['percent'] = int(m.group(3))
            if m.group(4):
                self._cur_state['speed_str'] = m.group(4)
            if m.group(5):
                self._cur_state['eta_str'] = m.group(5)
            self._cur_state['raw'] = line
            self.progress.emit(dict(self._cur_state))
            return
        # Current-file detail line (starts with " * filename:")
        m = re.match(
            r"^\*\s+(.+?):\s+"
            r"(\d+)%\s*"
            r"(?:/([\d.]+\s*[KMGTPEi]*))?"
            r"(?:,\s*([\d.]+\s*[KMGTPEi]*/s))?"
            r"(?:,\s*(\S+))?",
            line.lstrip())
        if m:
            self._cur_state['current_file'] = m.group(1).strip()
            # Don't override percent if we already have a higher
            # overall percent - the per-file value can be 0%
            # while the overall transfer is 80% done.
            self._cur_state['raw'] = line
            self.progress.emit(dict(self._cur_state))
            return
        # Unrecognised line - update raw for debug but don't
        # emit (avoids spamming the UI with parse-fail entries)
        self._cur_state['raw'] = line


# ---------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------


class RcloneBrowserDialog(QDialog):
    """The browser dialog. Constructed with a parent (typically
    the QuopusMainWindow) so we can read its config dict for
    rclone_path and reach its active-lister selection for
    uploads."""

    def __init__(self, parent=None, config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Rclone - Cloud Storage Browser")
        self.resize(1100, 700)
        # Non-modal so the user can keep working in Quopus while
        # a long transfer runs in the background. We just keep
        # ourselves on top relative to the main window.
        self.setModal(False)

        self._config = config or {}
        self._manager = rclone_backend.get_manager(self._config)
        self._main_window = parent

        # Current navigation state
        self._current_remote = ""
        self._current_path = ""

        # Active worker references (held so signals stay alive)
        self._list_remotes_worker = None
        self._list_dir_worker = None
        self._transfer_worker = None

        self._build_ui()
        # Kick off remote list load right away
        self._refresh_remotes()

    # ----- UI build ---------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(4)
        outer.setContentsMargins(6, 6, 6, 6)

        # Header: rclone version probe + reload button
        header = QHBoxLayout()
        self.lbl_version = QLabel("checking rclone...")
        self.lbl_version.setStyleSheet(
            f"color: #666; font-size: {scaled_font_px(11)}px;")
        header.addWidget(self.lbl_version, 1)
        self.btn_reload = QPushButton("Reload remotes")
        self.btn_reload.clicked.connect(self._refresh_remotes)
        header.addWidget(self.btn_reload)
        # Configure button: spawns 'rclone config' in a terminal
        # so the user can add cloud accounts without leaving
        # Quopus. Goes through the action dispatcher so the
        # platform-specific terminal-launching logic lives in
        # one place (act_rclone_setup).
        self.btn_configure = QPushButton("Configure...")
        self.btn_configure.setToolTip(
            "Open rclone's interactive config wizard in a new\n"
            "terminal window. Use this to add cloud accounts\n"
            "(Google Drive, Dropbox, OneDrive, S3, ...).")
        self.btn_configure.clicked.connect(self._on_configure)
        header.addWidget(self.btn_configure)
        # Quopus-side settings (binary path, bandwidth limit etc.)
        # Separate from rclone's own 'rclone config' which handles
        # cloud account credentials.
        self.btn_settings = QPushButton("Settings...")
        self.btn_settings.setToolTip(
            "Quopus rclone preferences: binary path, config file\n"
            "location, bandwidth limit, transfer concurrency.")
        self.btn_settings.clicked.connect(self._on_settings)
        header.addWidget(self.btn_settings)
        outer.addLayout(header)

        # Main split: remotes (left) + files (right)
        split = QSplitter(Qt.Orientation.Horizontal)

        # Remotes panel
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(2)
        left_lay.addWidget(QLabel("<b>Remotes:</b>"))
        self.lst_remotes = QListWidget()
        self.lst_remotes.itemClicked.connect(
            self._on_remote_selected)
        self.lst_remotes.itemDoubleClicked.connect(
            self._on_remote_double_clicked)
        left_lay.addWidget(self.lst_remotes, 1)
        # Hint shown at the bottom of the remotes list - users
        # who haven't configured anything yet need to know to
        # run `rclone config` from a terminal first. We can't do
        # OAuth flows inside Quopus without significant extra
        # work - rclone's own config wizard handles that already.
        self.lbl_remotes_hint = QLabel(
            "<i>No remotes? Run <code>rclone config</code><br>"
            "in a terminal to add cloud accounts.</i>")
        self.lbl_remotes_hint.setStyleSheet(
            f"color: #888; font-size: {scaled_font_px(10)}px;")
        self.lbl_remotes_hint.setWordWrap(True)
        left_lay.addWidget(self.lbl_remotes_hint)

        # Saved paths section - per-remote bookmarks of local
        # folders the user has linked to a cloud location. Lets
        # them re-sync a known pair with one click instead of
        # re-typing the paths each time. Shown for the currently
        # selected remote only.
        sp_hdr = QHBoxLayout()
        sp_hdr.addWidget(QLabel("<b>Saved paths:</b>"))
        sp_hdr.addStretch(1)
        self.btn_save_path = QPushButton("+ Save")
        self.btn_save_path.setToolTip(
            "Save the current local lister directory and remote\n"
            "folder as a quick-sync bookmark for this remote.")
        self.btn_save_path.clicked.connect(self._on_save_path)
        self.btn_save_path.setEnabled(False)
        sp_hdr.addWidget(self.btn_save_path)
        left_lay.addLayout(sp_hdr)

        self.lst_saved = QListWidget()
        self.lst_saved.setToolTip(
            "Double-click to load: changes both the local lister\n"
            "directory and the remote browser path to the saved\n"
            "pair, so you can immediately run Copy/Sync.\n\n"
            "Right-click for sync-now / remove options.")
        self.lst_saved.itemDoubleClicked.connect(
            self._on_saved_double_clicked)
        self.lst_saved.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst_saved.customContextMenuRequested.connect(
            self._on_saved_context_menu)
        left_lay.addWidget(self.lst_saved, 1)

        split.addWidget(left)

        # Files panel
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(2)

        # Toolbar
        tb = QHBoxLayout()
        tb.setSpacing(4)
        self.btn_up = QPushButton("⬆ Up")
        self.btn_up.clicked.connect(self._on_up)
        self.btn_up.setEnabled(False)
        tb.addWidget(self.btn_up)
        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.clicked.connect(self._refresh_files)
        self.btn_refresh.setEnabled(False)
        tb.addWidget(self.btn_refresh)
        self.btn_mkdir = QPushButton("New folder")
        self.btn_mkdir.clicked.connect(self._on_mkdir)
        self.btn_mkdir.setEnabled(False)
        tb.addWidget(self.btn_mkdir)
        tb.addStretch(1)
        self.btn_download = QPushButton("Download ⬇")
        self.btn_download.setToolTip(
            "Download selected file/folder to your local machine.\n"
            "Default target is the inactive lister's directory.")
        self.btn_download.clicked.connect(self._on_download)
        self.btn_download.setEnabled(False)
        tb.addWidget(self.btn_download)
        self.btn_upload = QPushButton("Upload ⬆")
        self.btn_upload.setToolTip(
            "Upload the active lister's selection into this\n"
            "remote folder.")
        self.btn_upload.clicked.connect(self._on_upload)
        self.btn_upload.setEnabled(False)
        tb.addWidget(self.btn_upload)
        # Sync up / down: destructive variants of upload/download
        # that delete extras on the destination to match source.
        # Coloured red-ish in the tooltip to flag the destructive
        # behaviour; a confirm dialog gates the actual sync call.
        self.btn_sync_up = QPushButton("Sync up ⇈")
        self.btn_sync_up.setToolTip(
            "Make the remote folder IDENTICAL to the active\n"
            "lister's directory. Files that exist on the remote\n"
            "but not locally are DELETED.\n\n"
            "Destructive - confirm dialog appears before running.")
        self.btn_sync_up.clicked.connect(self._on_sync_up)
        self.btn_sync_up.setEnabled(False)
        tb.addWidget(self.btn_sync_up)
        self.btn_sync_down = QPushButton("Sync down ⇊")
        self.btn_sync_down.setToolTip(
            "Make the inactive lister's directory IDENTICAL to\n"
            "this remote folder. Local files that don't exist\n"
            "on the remote are DELETED.\n\n"
            "Destructive - confirm dialog appears before running.")
        self.btn_sync_down.clicked.connect(self._on_sync_down)
        self.btn_sync_down.setEnabled(False)
        tb.addWidget(self.btn_sync_down)
        right_lay.addLayout(tb)

        # Path breadcrumbs
        self.lbl_path = QLabel("(no remote selected)")
        self.lbl_path.setStyleSheet(
            "background: #f0f0f0; padding: 4px 8px; "
            "border: 1px solid #ccc;")
        self.lbl_path.setWordWrap(False)
        # Don't let very long paths blow up the dialog width -
        # truncate visually but keep the full path retrievable
        # via _current_path.
        self.lbl_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        right_lay.addWidget(self.lbl_path)

        # Files tree
        self.tree_files = QTreeWidget()
        self.tree_files.setHeaderLabels(
            ["Name", "Size", "Modified", "Type"])
        self.tree_files.setColumnWidth(0, 380)
        self.tree_files.setColumnWidth(1, 100)
        self.tree_files.setColumnWidth(2, 160)
        self.tree_files.setColumnWidth(3, 80)
        self.tree_files.setRootIsDecorated(False)
        self.tree_files.setAlternatingRowColors(True)
        self.tree_files.setSortingEnabled(True)
        self.tree_files.itemDoubleClicked.connect(
            self._on_file_double_clicked)
        self.tree_files.itemSelectionChanged.connect(
            self._on_file_selection_changed)
        self.tree_files.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_files.customContextMenuRequested.connect(
            self._on_files_context_menu)
        right_lay.addWidget(self.tree_files, 1)

        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 800])
        outer.addWidget(split, 1)

        # Bottom: status bar + close
        bot = QHBoxLayout()
        self.lbl_status = QLabel("ready")
        self.lbl_status.setStyleSheet(
            "color: #555; padding: 2px 6px;")
        self.lbl_status.setWordWrap(True)
        bot.addWidget(self.lbl_status, 1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bot.addWidget(btn_close)
        outer.addLayout(bot)

    # ----- Remote handling --------------------------------------

    def _refresh_remotes(self):
        """Reload the remote list and update the version label.
        Called at startup and from the Reload button.

        If the rclone config file is encrypted and we don't have
        a password yet, prompt the user for it first - otherwise
        listremotes returns garbage and the UI looks broken.
        """
        self.lbl_version.setText("Probing rclone...")
        self.lst_remotes.clear()

        if not self._manager.is_available():
            self.lbl_version.setText(
                "<span style='color:#a00'>rclone not found.</span> "
                "Install from https://rclone.org/downloads/ then "
                "set the path in Quopus config if it's not on $PATH.")
            self._set_busy(False)
            return

        try:
            ver = self._manager.version()
            self.lbl_version.setText(
                f"<span style='color:#080'>{ver}</span>")
        except Exception as e:
            self.lbl_version.setText(
                f"<span style='color:#a00'>{e}</span>")
            return

        # Encrypted config detection. We only prompt if rclone
        # reports the config is encrypted AND we don't already
        # have the right password. Persist the password into the
        # IN-MEMORY config dict only - never into the on-disk
        # Quopus config file, since that would defeat the
        # encryption that protects the rclone credentials.
        if self._manager.config_is_encrypted():
            if not self._manager.config_password:
                pw = self._prompt_for_config_password()
                if pw is None:
                    self.lbl_status.setText(
                        "Password required to decrypt rclone "
                        "config - cancelled.")
                    return
                self._config["rclone_config_password"] = pw
                # Tear down the cached manager so the next call
                # picks up the new password.
                global_manager_reset()
                self._manager = rclone_backend.get_manager(
                    self._config)

        # Kick off the list-remotes worker
        self._list_remotes_worker = _ListRemotesWorker(
            self._manager, self)
        self._list_remotes_worker.done.connect(
            self._on_remotes_loaded)
        self._list_remotes_worker.start()
        self.lbl_status.setText("Loading remotes...")

    def _prompt_for_config_password(self):
        """Modal password prompt with hidden echo. Returns the
        entered string, or None if the user cancelled.

        We use the QInputDialog Password mode so OS-level
        password-manager integration can pick it up if the user
        has that configured."""
        from PyQt6.QtWidgets import QLineEdit
        pw, ok = QInputDialog.getText(
            self, "Rclone config password",
            "Your rclone.conf is encrypted.\n\n"
            "Enter the password to decrypt it.\n"
            "Quopus keeps this in memory only for the current "
            "session - it never gets written to disk.",
            QLineEdit.EchoMode.Password)
        if not ok:
            return None
        return pw

    def _on_remotes_loaded(self, ok, remotes, err):
        self.lst_remotes.clear()
        if not ok:
            self.lbl_status.setText(f"Error: {err}")
            return
        if not remotes:
            self.lbl_status.setText(
                "No remotes configured yet. "
                "Run 'rclone config' to add one.")
            return
        for r in remotes:
            item = QListWidgetItem(
                f"{r.name}\n  {r.friendly_type}")
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.lst_remotes.addItem(item)
        self.lbl_status.setText(
            f"{len(remotes)} remote(s) configured")

    def _on_remote_selected(self, item):
        # Single click = visual highlight only, no nav. Use
        # double-click to actually browse.
        pass

    def _on_remote_double_clicked(self, item):
        remote = item.data(Qt.ItemDataRole.UserRole)
        if not remote:
            return
        self._current_remote = remote.name
        self._current_path = ""
        self._update_path_label()
        self._refresh_files()
        self._refresh_saved_paths()
        self.btn_save_path.setEnabled(True)

    # ----- Saved paths ------------------------------------------

    def _refresh_saved_paths(self):
        """Reload the saved-paths list for the current remote.
        Each entry shows 'local -> remote' as a single line."""
        self.lst_saved.clear()
        if not self._current_remote:
            return
        entries = rclone_backend.get_saved_paths(
            self._current_remote)
        for e in entries:
            local = e.get("local", "")
            rmt = e.get("remote", "")
            item = QListWidgetItem(
                f"{local}\n  → :{rmt}")
            item.setData(Qt.ItemDataRole.UserRole, e)
            self.lst_saved.addItem(item)

    def _on_save_path(self):
        """Save the currently-active lister directory and the
        currently-browsed remote path as a saved pair for this
        remote."""
        if not self._current_remote:
            return
        mw = self._main_window
        local = None
        if mw is not None and hasattr(mw, "_active_lister"):
            try:
                active, _ = mw._active_lister()
                if active is not None:
                    local = str(active.current_path)
            except Exception:
                pass
        if local is None:
            local, ok = QInputDialog.getText(
                self, "Save path",
                "Local directory to save for this remote "
                "(no active Quopus lister, enter manually):")
            if not ok or not local.strip():
                return
            local = local.strip()
        rclone_backend.add_saved_path(
            self._current_remote, local, self._current_path)
        self.lbl_status.setText(
            f"Saved: {local}  →  {self._current_remote}:"
            f"{self._current_path}")
        self._refresh_saved_paths()

    def _on_saved_double_clicked(self, item):
        """Load a saved pair: navigate the remote browser to the
        saved remote path and ask the main window to navigate
        the active lister to the local path. Doesn't run any
        transfer - it just sets up both sides."""
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        rmt = entry.get("remote", "")
        local = entry.get("local", "")
        self._current_path = rmt
        self._update_path_label()
        self._refresh_files()
        # Try to navigate the active lister too. If we can't
        # (no main window, lister doesn't support setDirectory),
        # leave it to the user.
        mw = self._main_window
        if (mw is not None
                and hasattr(mw, "_active_lister")
                and local):
            try:
                active, _ = mw._active_lister()
                if active is not None and hasattr(
                        active, "navigate_to"):
                    active.navigate_to(local)
            except Exception:
                pass
        self.lbl_status.setText(
            f"Loaded: {local}  ↔  {self._current_remote}:{rmt}")

    def _on_saved_context_menu(self, pos):
        item = self.lst_saved.itemAt(pos)
        menu = QMenu(self)
        a_load = menu.addAction("Load (navigate both sides)")
        a_load.setEnabled(item is not None)
        a_load.triggered.connect(
            lambda: self._on_saved_double_clicked(item))
        menu.addSeparator()
        a_sync_up = menu.addAction("Sync local → remote")
        a_sync_up.setEnabled(item is not None)
        a_sync_up.triggered.connect(
            lambda: self._saved_sync(item, upload=True))
        a_sync_down = menu.addAction("Sync remote → local")
        a_sync_down.setEnabled(item is not None)
        a_sync_down.triggered.connect(
            lambda: self._saved_sync(item, upload=False))
        menu.addSeparator()
        a_copy_up = menu.addAction("Copy local → remote (non-destructive)")
        a_copy_up.setEnabled(item is not None)
        a_copy_up.triggered.connect(
            lambda: self._saved_copy(item, upload=True))
        a_copy_down = menu.addAction("Copy remote → local (non-destructive)")
        a_copy_down.setEnabled(item is not None)
        a_copy_down.triggered.connect(
            lambda: self._saved_copy(item, upload=False))
        menu.addSeparator()
        a_remove = menu.addAction("Remove from saved paths")
        a_remove.setEnabled(item is not None)
        a_remove.triggered.connect(
            lambda: self._saved_remove(item))
        menu.exec(self.lst_saved.mapToGlobal(pos))

    def _saved_sync(self, item, *, upload: bool):
        """Run a sync using a saved-paths entry. Confirm dialog
        always shown because sync is destructive."""
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        local = entry.get("local", "")
        rmt = entry.get("remote", "")
        if upload:
            target = f"{self._current_remote}:{rmt}"
            msg = (f"DESTRUCTIVE: make remote IDENTICAL to local\n\n"
                   f"  Local source:   {local}\n"
                   f"  Remote target:  {target}\n\n"
                   f"Files on the remote not present locally "
                   f"will be DELETED. Continue?")
        else:
            source = f"{self._current_remote}:{rmt}"
            msg = (f"DESTRUCTIVE: make local IDENTICAL to remote\n\n"
                   f"  Remote source:  {source}\n"
                   f"  Local target:   {local}\n\n"
                   f"Local files not on the remote will be DELETED. "
                   f"Continue?")
        if QMessageBox.warning(
                self, "Sync from saved path",
                msg,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_transfer(
            is_upload=upload, is_sync=True,
            local_path=local,
            remote=self._current_remote,
            remote_path=rmt,
            label=(f"Sync {'↑' if upload else '↓'} "
                   f"{local} ↔ "
                   f"{self._current_remote}:{rmt}"))

    def _saved_copy(self, item, *, upload: bool):
        """Run a copy using a saved-paths entry. No confirmation
        needed since copy is non-destructive."""
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        local = entry.get("local", "")
        rmt = entry.get("remote", "")
        self._start_transfer(
            is_upload=upload, is_sync=False,
            local_path=local,
            remote=self._current_remote,
            remote_path=rmt,
            label=(f"Copy {'↑' if upload else '↓'} "
                   f"{local} ↔ "
                   f"{self._current_remote}:{rmt}"))

    def _saved_remove(self, item):
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        local = entry.get("local", "")
        if QMessageBox.question(
                self, "Remove saved path",
                f"Remove this saved pair?\n\n  {local}\n  → "
                f"{self._current_remote}:{entry.get('remote', '')}",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        rclone_backend.remove_saved_path(
            self._current_remote, local)
        self._refresh_saved_paths()
        self.lbl_status.setText(f"Removed saved path: {local}")

    # ----- File listing -----------------------------------------

    def _refresh_files(self):
        if not self._current_remote:
            self.tree_files.clear()
            return
        self.tree_files.clear()
        self.btn_refresh.setEnabled(False)
        self.btn_up.setEnabled(False)
        self.lbl_status.setText(
            f"Listing {self._current_remote}:"
            f"{self._current_path}/ ...")
        self._list_dir_worker = _ListDirWorker(
            self._manager, self._current_remote,
            self._current_path, self)
        self._list_dir_worker.done.connect(self._on_dir_loaded)
        self._list_dir_worker.start()

    def _on_dir_loaded(self, ok, entries, err):
        self.btn_refresh.setEnabled(True)
        self.btn_up.setEnabled(bool(self._current_path))
        self.btn_mkdir.setEnabled(True)
        self.btn_upload.setEnabled(True)
        self.btn_sync_up.setEnabled(True)
        self.btn_sync_down.setEnabled(True)
        if not ok:
            self.lbl_status.setText(f"Listing error: {err}")
            return
        from datetime import datetime
        for e in entries:
            it = QTreeWidgetItem()
            it.setText(0, ("📁 " if e.is_dir else "  ") + e.name)
            if e.is_dir:
                it.setText(1, "")
            else:
                it.setText(1, _fmt_size(e.size))
            # Truncate mod-time to just the date+time bit
            mt = e.mod_time
            if "T" in mt:
                try:
                    dt = datetime.fromisoformat(
                        mt.replace("Z", "+00:00"))
                    mt = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            it.setText(2, mt)
            it.setText(3, "folder" if e.is_dir else "file")
            it.setData(0, Qt.ItemDataRole.UserRole, e)
            self.tree_files.addTopLevelItem(it)
        self.lbl_status.setText(f"{len(entries)} item(s)")

    def _on_file_double_clicked(self, item, _col):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return
        if entry.is_dir:
            # Navigate into it
            sub = entry.path  # rclone gives relative-to-parent
            if self._current_path:
                self._current_path = (
                    f"{self._current_path}/{sub}")
            else:
                self._current_path = sub
            self._update_path_label()
            self._refresh_files()
        else:
            # Files: prompt for download. Quick alternative to
            # right-click Download for users who don't know about
            # context menus.
            self._on_download()

    def _on_file_selection_changed(self):
        has_sel = bool(self.tree_files.selectedItems())
        self.btn_download.setEnabled(has_sel)

    # ----- Path navigation --------------------------------------

    def _on_up(self):
        if not self._current_path:
            return
        if "/" in self._current_path:
            self._current_path = self._current_path.rsplit(
                "/", 1)[0]
        else:
            self._current_path = ""
        self._update_path_label()
        self._refresh_files()

    def _update_path_label(self):
        if self._current_remote:
            self.lbl_path.setText(
                f"<b>{self._current_remote}:</b>"
                f"/{self._current_path}")
        else:
            self.lbl_path.setText("(no remote selected)")

    # ----- File operations --------------------------------------

    def _on_mkdir(self):
        if not self._current_remote:
            return
        name, ok = QInputDialog.getText(
            self, "New folder",
            f"Folder name to create in "
            f"{self._current_remote}:/{self._current_path}/")
        if not ok or not name.strip():
            return
        name = name.strip()
        target_path = (f"{self._current_path}/{name}"
                       if self._current_path else name)
        try:
            self._manager.mkdir(
                self._current_remote, target_path)
            self.lbl_status.setText(f"Created folder: {name}")
            self._refresh_files()
        except RcloneError as e:
            QMessageBox.warning(self, "Mkdir failed", str(e))

    def _on_download(self):
        """Download the currently-selected entry to the inactive
        lister's directory (if available) or prompt for a path."""
        items = self.tree_files.selectedItems()
        if not items:
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return
        # Pick target: inactive lister's current dir, else file
        # dialog. We get the inactive lister via the main window
        # if it exists - same pattern Quopus uses elsewhere.
        target_dir = self._get_inactive_lister_dir()
        if target_dir is None:
            target_dir = QFileDialog.getExistingDirectory(
                self, "Download to folder",
                str(Path.home()))
            if not target_dir:
                return
        # Build source / dest paths. For files we use copyto
        # with the final filename; for directories we use copy
        # which preserves the source name.
        if self._current_path:
            src_path = f"{self._current_path}/{entry.name}"
        else:
            src_path = entry.name
        if entry.is_dir:
            dst = str(Path(target_dir))
            # copy will create a subdir with the source name
        else:
            dst = str(Path(target_dir) / entry.name)
        self._start_transfer(
            is_upload=False, local_path=dst,
            remote=self._current_remote, remote_path=src_path,
            label=f"Downloading {entry.name}")

    def _on_upload(self):
        """Upload the active lister's selection into the current
        remote folder. If there's no Quopus active lister (e.g.
        the dialog was opened standalone), fall back to a file
        dialog."""
        sources = self._get_active_lister_selection()
        if not sources:
            f, _ = QFileDialog.getOpenFileName(
                self, "File to upload",
                str(Path.home()))
            if not f:
                return
            sources = [f]
        # Upload one item at a time. We could batch via
        # `rclone copy <multiple-srcs> <dst>` but rclone's CLI
        # only takes one source per invocation - we'd need to
        # spawn N processes or build a temp file with the list.
        # Sequential single-file uploads keeps the progress
        # display readable.
        for src in sources:
            name = Path(src).name
            dst = (f"{self._current_path}/{name}"
                   if self._current_path else name)
            self._start_transfer(
                is_upload=True, local_path=src,
                remote=self._current_remote, remote_path=dst,
                label=f"Uploading {name}")
            # Wait for this one before starting the next so
            # progress dialog isn't a confusing pile
            if (self._transfer_worker is not None
                    and self._transfer_worker.isRunning()):
                self._transfer_worker.wait()

    def _on_sync_up(self):
        """Sync local directory -> remote folder. DESTRUCTIVE:
        files on the remote that aren't local get DELETED. We
        always show a clear confirmation dialog with the actual
        paths involved before kicking off.

        The source is the active lister's current directory
        (a single directory, not a selection of files - rclone
        sync operates on dir<->dir pairs).
        """
        if not self._current_remote:
            return
        mw = self._main_window
        if mw is None or not hasattr(mw, "_active_lister"):
            QMessageBox.warning(
                self, "Sync up",
                "Sync requires a Quopus lister with a current "
                "directory.")
            return
        try:
            active, _ = mw._active_lister()
        except Exception:
            active = None
        if active is None:
            return
        local_dir = str(active.current_path)
        target = (f"{self._current_remote}:"
                  f"{self._current_path}")
        if QMessageBox.warning(
                self, "Sync up - confirm DESTRUCTIVE operation",
                f"This will make the remote folder IDENTICAL to "
                f"your local one.\n\n"
                f"  Local source:   {local_dir}\n"
                f"  Remote target:  {target}\n\n"
                f"Files on the remote that DON'T exist locally "
                f"will be DELETED.\n\n"
                f"Continue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_transfer(
            is_upload=True, is_sync=True,
            local_path=local_dir,
            remote=self._current_remote,
            remote_path=self._current_path,
            label=f"Syncing {local_dir} → {target}")

    def _on_sync_down(self):
        """Sync remote folder -> inactive lister directory.
        DESTRUCTIVE: local files that aren't on the remote get
        DELETED."""
        if not self._current_remote:
            return
        local_dir = self._get_inactive_lister_dir()
        if local_dir is None:
            QMessageBox.warning(
                self, "Sync down",
                "Sync down requires an inactive Quopus lister "
                "with a target directory.")
            return
        source = (f"{self._current_remote}:"
                  f"{self._current_path}")
        if QMessageBox.warning(
                self, "Sync down - confirm DESTRUCTIVE operation",
                f"This will make the LOCAL folder IDENTICAL to "
                f"the remote one.\n\n"
                f"  Remote source:  {source}\n"
                f"  Local target:   {local_dir}\n\n"
                f"LOCAL files that DON'T exist on the remote "
                f"will be DELETED.\n\n"
                f"Continue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_transfer(
            is_upload=False, is_sync=True,
            local_path=local_dir,
            remote=self._current_remote,
            remote_path=self._current_path,
            label=f"Syncing {source} → {local_dir}")

    def _start_transfer(self, *, is_upload, local_path, remote,
                        remote_path, label, is_sync=False):
        """Kick off a transfer worker. Shows a custom progress
        dialog with a real QProgressBar (0..100) plus separate
        labels for current file, bytes transferred, speed, and
        ETA - much more legible than just dumping rclone's raw
        text output.

        is_sync=True picks rclone sync instead of copy.
        """
        from PyQt6.QtWidgets import QProgressBar

        prog = QDialog(self)
        prog.setWindowTitle("Rclone transfer")
        prog.setModal(False)
        prog.resize(520, 200)

        plv = QVBoxLayout(prog)
        plv.setContentsMargins(12, 10, 12, 10)
        plv.setSpacing(6)

        # Header label - shows what the user kicked off
        lbl_op = QLabel(f"<b>{label}</b>")
        lbl_op.setWordWrap(True)
        plv.addWidget(lbl_op)

        # Current file being transferred (rclone reports per-file
        # for batch / directory copies). On single-file uploads
        # this will just echo the operation name.
        lbl_file = QLabel("starting...")
        lbl_file.setStyleSheet("color: #444;")
        lbl_file.setWordWrap(True)
        plv.addWidget(lbl_file)

        # The actual progress bar
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat("%p%")
        plv.addWidget(bar)

        # Stats row: transferred / total, speed, ETA
        stats = QHBoxLayout()
        lbl_bytes = QLabel("")
        lbl_bytes.setStyleSheet("color: #333;")
        stats.addWidget(lbl_bytes, 1)
        lbl_speed = QLabel("")
        lbl_speed.setStyleSheet("color: #333;")
        stats.addWidget(lbl_speed)
        lbl_eta = QLabel("")
        lbl_eta.setStyleSheet("color: #333;")
        stats.addWidget(lbl_eta)
        plv.addLayout(stats)

        plv.addStretch(1)

        # Cancel button row
        btnrow = QHBoxLayout()
        btnrow.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btnrow.addWidget(btn_cancel)
        plv.addLayout(btnrow)

        self._transfer_worker = _TransferWorker(
            self._manager,
            is_upload=is_upload,
            is_sync=is_sync,
            local_path=local_path,
            remote=remote,
            remote_path=remote_path,
            parent=self)

        def on_progress(state):
            # state is a dict from _TransferWorker._on_progress_line
            pct = state.get('percent', -1)
            if isinstance(pct, int) and 0 <= pct <= 100:
                # Determinate
                bar.setRange(0, 100)
                bar.setValue(pct)
            else:
                # Unknown overall percent - busy bar
                bar.setRange(0, 0)
            cur_file = state.get('current_file', '')
            if cur_file:
                lbl_file.setText(f"file: {cur_file}")
            trans = state.get('transferred_str', '')
            total = state.get('total_str', '')
            if trans and total:
                lbl_bytes.setText(f"{trans} / {total}")
            elif trans:
                lbl_bytes.setText(trans)
            speed = state.get('speed_str', '')
            if speed:
                lbl_speed.setText(speed)
            eta = state.get('eta_str', '')
            if eta:
                lbl_eta.setText(f"ETA {eta}")

        def on_done(ok, rc, err):
            prog.close()
            if ok:
                self.lbl_status.setText(
                    f"Transfer complete: {label}")
                self._refresh_files()
            else:
                msg = f"Transfer failed (rc={rc})"
                if err:
                    msg += f"\n\n{err[:500]}"
                QMessageBox.warning(self, "Transfer failed", msg)

        def on_cancel():
            self._transfer_worker.cancel()
            btn_cancel.setEnabled(False)
            btn_cancel.setText("Cancelling...")
            self.lbl_status.setText(
                "Cancel requested - rclone may continue running "
                "until current chunk completes")

        self._transfer_worker.progress.connect(on_progress)
        self._transfer_worker.done.connect(on_done)
        btn_cancel.clicked.connect(on_cancel)
        prog.closeEvent = lambda ev: on_cancel() if (
            self._transfer_worker is not None
            and self._transfer_worker.isRunning()) else None
        self._transfer_worker.start()
        prog.show()

    # ----- Context menu -----------------------------------------

    def _on_files_context_menu(self, pos):
        items = self.tree_files.selectedItems()
        menu = QMenu(self)
        a_download = menu.addAction("⬇ Download")
        a_download.setEnabled(bool(items))
        a_download.triggered.connect(self._on_download)
        menu.addSeparator()
        a_rename = menu.addAction("Rename...")
        a_rename.setEnabled(bool(items))
        a_rename.triggered.connect(self._on_rename)
        a_delete = menu.addAction("Delete")
        a_delete.setEnabled(bool(items))
        a_delete.triggered.connect(self._on_delete)
        menu.addSeparator()
        a_mkdir = menu.addAction("New folder...")
        a_mkdir.triggered.connect(self._on_mkdir)
        a_refresh = menu.addAction("Refresh")
        a_refresh.triggered.connect(self._refresh_files)
        menu.exec(self.tree_files.mapToGlobal(pos))

    def _on_rename(self):
        items = self.tree_files.selectedItems()
        if not items:
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename",
            f"New name for '{entry.name}':", text=entry.name)
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        if new_name == entry.name:
            return
        old_path = (f"{self._current_path}/{entry.name}"
                    if self._current_path else entry.name)
        new_path = (f"{self._current_path}/{new_name}"
                    if self._current_path else new_name)
        try:
            self._manager.rename(
                self._current_remote, old_path, new_path)
            self.lbl_status.setText(
                f"Renamed: {entry.name} -> {new_name}")
            self._refresh_files()
        except RcloneError as e:
            QMessageBox.warning(self, "Rename failed", str(e))

    def _on_delete(self):
        items = self.tree_files.selectedItems()
        if not items:
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return
        kind = "folder (and ALL its contents)" if entry.is_dir \
            else "file"
        if QMessageBox.question(
                self, "Delete",
                f"Delete this {kind} from the cloud?\n\n"
                f"{self._current_remote}:"
                f"{self._current_path}/{entry.name}",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        target = (f"{self._current_path}/{entry.name}"
                  if self._current_path else entry.name)
        try:
            self._manager.delete(
                self._current_remote, target,
                recursive=entry.is_dir)
            self.lbl_status.setText(f"Deleted: {entry.name}")
            self._refresh_files()
        except RcloneError as e:
            QMessageBox.warning(self, "Delete failed", str(e))

    # ----- Lister integration -----------------------------------

    def _get_active_lister_selection(self) -> list:
        """Return a list of local file paths the user has
        selected in the currently-active Quopus lister. Empty
        list if we can't reach the main window."""
        mw = self._main_window
        if mw is None or not hasattr(mw, "_active_lister"):
            return []
        try:
            active, _ = mw._active_lister()
        except Exception:
            return []
        if active is None:
            return []
        try:
            paths = active.selected_or_tagged() or []
            return [str(p) for p in paths]
        except Exception:
            return []

    def _get_inactive_lister_dir(self):
        """Return the inactive lister's current dir as a string,
        or None if not available. Used as the default download
        target so the user can download cloud->local with a
        single click."""
        mw = self._main_window
        if mw is None or not hasattr(mw, "_active_lister"):
            return None
        try:
            active, inactive = mw._active_lister()
        except Exception:
            return None
        if inactive is None:
            return None
        try:
            return str(inactive.current_path)
        except Exception:
            return None

    # ----- Misc helpers -----------------------------------------

    def _on_configure(self):
        """Spawn `rclone config` in a terminal. Routes through
        the same act_rclone_setup the action dispatcher uses so
        all the platform-specific terminal-launching logic
        stays in one place."""
        mw = self._main_window
        if mw is None or not hasattr(mw, "_actions"):
            # Standalone mode - no dispatcher available, do it
            # the lazy way: just run rclone config directly,
            # which works on a system where rclone is on PATH.
            import subprocess
            try:
                subprocess.Popen(
                    [self._manager.rclone_path, "config"])
            except Exception as e:
                QMessageBox.warning(
                    self, "Rclone setup",
                    f"Couldn't spawn rclone config:\n\n{e}")
            return
        try:
            mw._actions.act_rclone_setup(None, None, None)
        except Exception as e:
            QMessageBox.warning(
                self, "Rclone setup",
                f"Couldn't spawn rclone config:\n\n{e}")

    def _on_settings(self):
        """Open the Quopus rclone settings dialog. Lets the
        user override the rclone binary path, the config file
        location, bandwidth limit and transfer concurrency.
        Settings are persisted into the main Quopus config
        dict (so they survive across sessions)."""
        dlg = RcloneSettingsDialog(self, self._config)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Apply the new settings: rebuild the manager with
            # the new path/config, then reload remotes.
            new_settings = dlg.result_settings()
            self._config.update(new_settings)
            # Persist via main window if available
            mw = self._main_window
            if mw is not None and hasattr(mw, "save_config"):
                try:
                    mw.save_config()
                except Exception:
                    pass
            # Force a fresh manager so it picks up the new path
            global_manager_reset()
            self._manager = rclone_backend.get_manager(self._config)
            self._refresh_remotes()

    def _set_busy(self, busy: bool):
        self.btn_reload.setEnabled(not busy)


# ---------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------


class RcloneSettingsDialog(QDialog):
    """Quopus-side rclone preferences. NOT to be confused with
    `rclone config` which manages cloud-account credentials
    (that's done in a terminal via the Configure button).

    Settings exposed:

      rclone_path           - explicit path to the rclone binary
                              (overrides auto-detect)
      rclone_config_path    - explicit path to rclone.conf
                              (overrides rclone's default lookup)
      rclone_bwlimit        - --bwlimit value (e.g. "10M", "1G",
                              empty=no limit)
      rclone_transfers      - --transfers value (concurrency,
                              default 4 - higher uses more bw
                              but stresses the API)
      rclone_checkers       - --checkers (parallel hash checks
                              during sync, default 8)
      rclone_extra_args     - free-text extra rclone arguments
                              applied to every transfer
    """

    def __init__(self, parent=None, config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Rclone Settings")
        self.resize(640, 420)
        self._config = dict(config or {})

        from PyQt6.QtWidgets import (
            QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
            QGroupBox, QPlainTextEdit, QFileDialog as _QFD)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # --- Paths group --------------------------------------
        gb_paths = QGroupBox("Paths")
        form_paths = QFormLayout(gb_paths)

        # rclone binary path row: line edit + Browse button
        path_row = QHBoxLayout()
        self.ed_path = QLineEdit(
            self._config.get("rclone_path", ""))
        self.ed_path.setPlaceholderText(
            "(auto-detect: external/, $PATH, "
            "well-known install dirs)")
        self.ed_path.setToolTip(
            "Path to the rclone executable. Leave empty to "
            "auto-detect: Quopus looks in external/ first, "
            "then $PATH.")
        path_row.addWidget(self.ed_path, 1)
        btn_browse_path = QPushButton("Browse...")
        def _pick_binary():
            f, _ = _QFD.getOpenFileName(
                self, "Select rclone executable",
                self.ed_path.text() or str(Path.home()))
            if f:
                self.ed_path.setText(f)
        btn_browse_path.clicked.connect(_pick_binary)
        path_row.addWidget(btn_browse_path)
        form_paths.addRow("rclone binary:", path_row)

        # Config path row: line edit + Browse + Open-in-Editor
        cfg_row = QHBoxLayout()
        self.ed_cfg = QLineEdit(
            self._config.get("rclone_config_path", ""))
        self.ed_cfg.setPlaceholderText(
            "(default: rclone's standard location)")
        self.ed_cfg.setToolTip(
            "Path to the rclone.conf file. Leave empty to use "
            "rclone's default lookup\n"
            "(~/.config/rclone/rclone.conf on Linux/macOS,\n"
            " %APPDATA%\\rclone\\rclone.conf on Windows).")
        cfg_row.addWidget(self.ed_cfg, 1)
        btn_browse_cfg = QPushButton("Browse...")
        def _pick_cfg():
            f, _ = _QFD.getOpenFileName(
                self, "Select rclone config file",
                self.ed_cfg.text() or str(Path.home()),
                "Rclone config (*.conf);;All files (*)")
            if f:
                self.ed_cfg.setText(f)
        btn_browse_cfg.clicked.connect(_pick_cfg)
        cfg_row.addWidget(btn_browse_cfg)
        form_paths.addRow("rclone.conf:", cfg_row)

        outer.addWidget(gb_paths)

        # --- Transfer tuning group ----------------------------
        gb_tune = QGroupBox("Transfer tuning")
        form_tune = QFormLayout(gb_tune)

        self.ed_bwlimit = QLineEdit(
            self._config.get("rclone_bwlimit", ""))
        self.ed_bwlimit.setPlaceholderText(
            "e.g. 10M, 1G, 500K (empty = unlimited)")
        self.ed_bwlimit.setToolTip(
            "Bandwidth limit applied as --bwlimit.\n"
            "Values use rclone's size syntax:\n"
            "  500K   - 500 KByte/s\n"
            "  10M    - 10 MByte/s\n"
            "  1G     - 1 GByte/s\n"
            "  empty  - no limit\n\n"
            "You can also use rclone's timetable syntax for\n"
            "scheduled throttling, e.g. '08:00,512 12:00,off'.")
        form_tune.addRow("Bandwidth limit:", self.ed_bwlimit)

        self.sp_transfers = QSpinBox()
        self.sp_transfers.setRange(1, 64)
        self.sp_transfers.setValue(int(
            self._config.get("rclone_transfers", 4)))
        self.sp_transfers.setToolTip(
            "Number of file transfers to run in parallel\n"
            "(--transfers). Default 4. Higher uses more\n"
            "bandwidth and more API quota; some providers\n"
            "throttle if too high.")
        form_tune.addRow("Parallel transfers:", self.sp_transfers)

        self.sp_checkers = QSpinBox()
        self.sp_checkers.setRange(1, 64)
        self.sp_checkers.setValue(int(
            self._config.get("rclone_checkers", 8)))
        self.sp_checkers.setToolTip(
            "Number of parallel hash/metadata checks during\n"
            "sync (--checkers). Default 8. Mainly affects\n"
            "sync operations; lone copy uploads ignore it.")
        form_tune.addRow("Parallel checkers:", self.sp_checkers)

        outer.addWidget(gb_tune)

        # --- Extra args group ---------------------------------
        gb_extra = QGroupBox("Extra rclone arguments (advanced)")
        v_extra = QVBoxLayout(gb_extra)
        self.ed_extra = QPlainTextEdit(
            self._config.get("rclone_extra_args", ""))
        self.ed_extra.setPlaceholderText(
            "Additional rclone CLI flags applied to every "
            "transfer.\n"
            "One flag per line, or space-separated. Example:\n"
            "  --drive-shared-with-me\n"
            "  --s3-no-check-bucket\n"
            "  --ignore-existing")
        self.ed_extra.setToolTip(
            "Any extra rclone arguments. These are added to\n"
            "all transfer commands. Use with care - bad flags\n"
            "make rclone refuse to start.")
        self.ed_extra.setMaximumHeight(100)
        v_extra.addWidget(self.ed_extra)
        outer.addWidget(gb_extra)

        outer.addStretch(1)

        # OK / Cancel
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        outer.addWidget(btn_box)

    def result_settings(self) -> dict:
        """Return the new settings as a dict that can be merged
        into the main Quopus config. Empty strings are stored
        as empty strings (not None) so the config file remains
        self-describing - the user can see which knobs exist
        even if they're unset."""
        return {
            "rclone_path": self.ed_path.text().strip(),
            "rclone_config_path": self.ed_cfg.text().strip(),
            "rclone_bwlimit": self.ed_bwlimit.text().strip(),
            "rclone_transfers": self.sp_transfers.value(),
            "rclone_checkers": self.sp_checkers.value(),
            "rclone_extra_args":
                self.ed_extra.toPlainText().strip(),
        }


def global_manager_reset():
    """Drop the cached singleton so the next get_manager() call
    re-reads config and constructs a fresh RcloneManager. Used
    by the Settings dialog after the user changed paths."""
    rclone_backend._MANAGER_CACHE = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _fmt_size(n: int) -> str:
    """Format bytes as human-readable. Matches the style used
    elsewhere in Quopus (lister.py, db_browser.py)."""
    if n < 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{f:.1f} {units[i]}"


# ---------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------


def show_rclone_browser(parent=None, config: dict = None):
    """Open the Rclone browser dialog. Returns the dialog
    instance; caller is responsible for keeping a reference to
    it (the dialog is non-modal so Python's GC would otherwise
    collect it right after this function returns)."""
    dlg = RcloneBrowserDialog(parent=parent, config=config)
    dlg.show()
    return dlg
