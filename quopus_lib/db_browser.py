"""Database Browser UI - search the indexed archive.

Three tabs:
  - Files: search by filename across the whole DB
  - Disks: search by disk header name or ID
  - Stats: counts, top-level scan management

Searches run as the user types (debounced 300ms). Results render
in a tree with file size, location, MD5. Double-click jumps to
the parent folder in Quopus, or for archived items, shows the
container in the location pane.
"""
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QTreeWidget, QTreeWidgetItem, QTabWidget, QWidget,
    QFileDialog, QMessageBox, QProgressBar, QSplitter, QTextEdit,
    QApplication, QCheckBox, QMenu,
)

from . import database
from . import db_scanner
from .window_state import install_window_state, install_table_state


def _fmt_size(n) -> str:
    """Human-friendly file size in bytes."""
    if n is None:
        return ""
    if n < 1024:
        return f"{n}"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} K"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f} M"
    return f"{n/1024/1024/1024:.2f} G"


def _fmt_blocks(n) -> str:
    """Size in CBM blocks. One block = 256 bytes on disk.

    On a real CBM disk the user-visible "blocks" count refers to
    254-byte data blocks (the first 2 bytes of each block hold
    the chain link to the next block). But the disk image
    physically allocates 256 bytes per block. For host-side
    files (plain PRG on the filesystem) we don't have chained
    blocks, so 256-byte counting is the consistent measure.

    Returns ceil(size / 256) so a 100-byte PRG shows as 1 block
    rather than 0."""
    if n is None:
        return ""
    blocks = (n + 255) // 256
    if blocks < 1000:
        return f"{blocks}"
    if blocks < 1000000:
        return f"{blocks/1000:.1f}k"
    return f"{blocks/1000000:.1f}M"


def _fmt_size_or_blocks(n, mode: str) -> str:
    """Dispatch helper. mode is 'bytes' or 'blocks'."""
    if mode == "blocks":
        return _fmt_blocks(n)
    return _fmt_size(n)


# ============================================================
# Scan worker (runs in a separate thread)
# ============================================================


class _ScanWorker(QThread):
    """Runs the AsyncScanner in a background thread so the UI
    stays responsive. The actual heavy lifting happens on the
    process-wide IngestQueue worker pool (default 2 threads);
    this QThread just drives the directory walk and waits for
    the pool to drain. We use a QThread rather than running
    AsyncScanner directly on the UI thread so progress_cb
    callbacks marshal cleanly via signal-emit."""

    progress = pyqtSignal(str, int, int)
    # ^ current path, completed_so_far, total_walked_so_far
    finished_with = pyqtSignal(int, int, int)
    # ^ scan_id, files_added, error_count

    def __init__(self, root: Path, incremental: bool = True):
        super().__init__()
        self.root = Path(root)
        self.incremental = incremental
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        scanner = db_scanner.AsyncScanner(
            self.root,
            progress_cb=self._on_progress,
            cancel_cb=lambda: self._cancel,
            incremental=self.incremental)
        scan_id = scanner.run()
        # Read final counts from the DB for the summary signal
        try:
            with database.connection() as conn:
                row = conn.execute(
                    "SELECT file_count, error_count FROM scans "
                    "WHERE id = ?", (scan_id,)).fetchone()
            files = row["file_count"] if row else 0
            errors = row["error_count"] if row else 0
        except Exception:
            files = scanner.files_enqueued
            errors = 0
        self.finished_with.emit(scan_id, files, errors)

    def _on_progress(self, path: str, completed: int,
                     total_walked):
        # total_walked can be None from the legacy Scanner
        # interface; normalize to 0.
        self.progress.emit(path, completed,
                           total_walked if total_walked else 0)


# ============================================================
# Main dialog
# ============================================================


class DatabaseBrowserDialog(QDialog):
    """Browse and search the Quopus archive database."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # The parent main window. We use this for "Copy to Lister"
        # context menu actions - we need to call into the lister
        # API to navigate / refresh / etc. If parent is None (e.g.
        # called from a script with no GUI parent), copy-to-lister
        # menu items are hidden gracefully.
        self.main_window = parent
        self.setWindowTitle("Quopus Database")
        self.resize(1000, 700)
        # Make this a top-level window with its own taskbar entry
        # so it feels like a separate tool, not a modal popup.
        # WindowType.Window is the right flag for a free-floating
        # dialog that doesn't block the parent.
        self.setWindowFlag(Qt.WindowType.Window, True)
        # Explicit non-modal - Qt's default is non-modal for QDialog
        # constructed without a parent, but with a parent it can
        # vary. Set explicitly so behavior is consistent on every
        # platform.
        self.setModal(False)
        # Restore window geometry from last session
        install_window_state(self, "database_browser")
        # Ensure DB exists on first open
        database.init_db()

        # Size display mode: "bytes" (human-readable K/M/G) or
        # "blocks" (CBM 256-byte blocks). Stored persistently so
        # the user's last choice survives a restart.
        self.size_mode = self._load_size_mode()

        self.scan_worker: Optional[_ScanWorker] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Top toolbar: database file management. Lets the user
        # save their current catalog to a portable file, load a
        # catalog shared by another sysop, and switch back to
        # their own. The active DB filename shows in a label
        # between the buttons so it's always clear which DB
        # you're looking at.
        toolbar = QHBoxLayout()
        self.btn_db_open = QPushButton("Open DB...")
        self.btn_db_open.setToolTip(
            "Load a database file shared by another user. "
            "Opens read-only - the file won't be modified.")
        self.btn_db_open.clicked.connect(self._on_open_db)
        toolbar.addWidget(self.btn_db_open)

        self.btn_db_save_as = QPushButton("Save As...")
        self.btn_db_save_as.setToolTip(
            "Copy the current database to a different file - "
            "useful for sharing your catalog with friends or "
            "backing it up before a risky operation.")
        self.btn_db_save_as.clicked.connect(self._on_save_db_as)
        toolbar.addWidget(self.btn_db_save_as)

        self.btn_db_default = QPushButton("Open Own DB")
        self.btn_db_default.setToolTip(
            "Switch back to your personal database in the "
            "Quopus config folder.")
        self.btn_db_default.clicked.connect(self._on_open_default_db)
        toolbar.addWidget(self.btn_db_default)

        toolbar.addSpacing(20)
        self.lbl_db_file = QLabel()
        self.lbl_db_file.setToolTip(
            "The DB file currently being viewed. Read-only "
            "databases are marked with [RO] - you can search "
            "and browse but not scan.")
        toolbar.addWidget(self.lbl_db_file, 1)
        lay.addLayout(toolbar)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)

        self._build_files_tab()
        self._build_disks_tab()
        self._build_watch_tab()
        self._build_folders_tab()
        self._build_issues_tab()
        self._build_stats_tab()

        # After the files tree exists, sync the header label
        # with our saved size_mode preference.
        self._update_size_mode_button()

        # Footer: status line
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("color: #666;")
        lay.addWidget(self.lbl_status)

        # Bottom buttons
        bottom = QHBoxLayout()
        self.btn_scan = QPushButton("Scan Folder...")
        self.btn_scan.clicked.connect(self._on_scan_folder)
        bottom.addWidget(self.btn_scan)
        self.btn_cancel = QPushButton("Cancel Scan")
        self.btn_cancel.clicked.connect(self._on_cancel_scan)
        self.btn_cancel.setEnabled(False)
        bottom.addWidget(self.btn_cancel)
        # Size display toggle - applies to Files tab and the
        # disk-content sub-list in the Disks tab.
        self.btn_size_mode = QPushButton()
        self.btn_size_mode.clicked.connect(self._on_toggle_size_mode)
        bottom.addWidget(self.btn_size_mode)
        self._update_size_mode_button()
        bottom.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_close)
        lay.addLayout(bottom)

        # Refresh stats on open
        self._refresh_stats()

        # Sync DB-file label and write-enabled state of buttons.
        # We do this last so all the buttons it references already
        # exist (it touches btn_scan and btn_db_default).
        self._update_db_file_label()

    # --------------------------------------------------------
    # Size display mode (bytes vs CBM 256-byte blocks)
    # --------------------------------------------------------

    def _size_mode_file(self):
        """Where the size_mode preference is persisted."""
        from .config import CONFIG_DIR
        return CONFIG_DIR / "db_browser_size_mode.txt"

    def _load_size_mode(self) -> str:
        """Read the last-used mode from disk. Defaults to bytes
        for first-time users since that's the most familiar
        unit."""
        try:
            txt = self._size_mode_file().read_text().strip()
            if txt in ("bytes", "blocks"):
                return txt
        except (OSError, FileNotFoundError):
            pass
        return "bytes"

    def _save_size_mode(self):
        """Write the current mode back to disk so it survives
        the next launch. Silent on errors - if we can't write,
        the user just sees the default again next time."""
        try:
            from .config import CONFIG_DIR
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._size_mode_file().write_text(self.size_mode)
        except OSError:
            pass

    def _on_toggle_size_mode(self):
        """Flip between bytes and blocks. Re-renders all visible
        trees so the user sees the change immediately - no need
        to re-run a search."""
        self.size_mode = (
            "blocks" if self.size_mode == "bytes" else "bytes")
        self._save_size_mode()
        self._update_size_mode_button()
        # Re-render whatever's currently displayed
        if self.tabs.currentIndex() == 0:
            self._do_files_search()
        elif self.tabs.currentIndex() == 1:
            # Refresh the lower disk-files pane
            self._on_disk_selected()

    def _update_size_mode_button(self):
        """Sync the toggle button's label with the current mode.
        Called whenever the mode changes."""
        if hasattr(self, "btn_size_mode"):
            label = ("Show: Bytes" if self.size_mode == "blocks"
                     else "Show: Blocks")
            self.btn_size_mode.setText(label)
            self.btn_size_mode.setToolTip(
                f"Currently showing sizes as "
                f"{'CBM 256-byte blocks' if self.size_mode == 'blocks' else 'bytes'}. "
                f"Click to switch.")
        # Reflect the unit in the column header too so the user
        # knows what they're looking at without hovering the
        # toggle button.
        header_label = ("Blocks" if self.size_mode == "blocks"
                        else "Size")
        if hasattr(self, "tree_files"):
            self.tree_files.headerItem().setText(2, header_label)

    # --------------------------------------------------------
    # Database file management - open shared catalogs, save
    # a copy for sharing, switch back to own DB.
    # --------------------------------------------------------

    def _update_db_file_label(self):
        """Refresh the 'currently viewing X.sqlite' label and
        the disabled state of buttons that need write access.
        Called after every DB switch."""
        from pathlib import Path as _Path
        cur = database.DB_PATH
        is_default = database.is_default_db()
        is_ro = database.is_readonly()

        # Trim long paths for display - show the filename plus
        # the immediate parent for context. Full path is in the
        # tooltip if the user needs to see it.
        try:
            short = f"{cur.parent.name}/{cur.name}"
        except Exception:
            short = str(cur)
        marker = ""
        if not is_default:
            marker = " [shared]"
        if is_ro:
            marker += " [RO]"

        prefix = "<b>DB:</b> "
        if is_ro:
            color = "#a60"  # orange for read-only
        elif not is_default:
            color = "#06a"  # blue for non-default
        else:
            color = "#080"  # green for personal DB
        self.lbl_db_file.setText(
            f"{prefix}<span style='color:{color}'>"
            f"{short}{marker}</span>")
        self.lbl_db_file.setToolTip(
            f"Full path: {cur}\n"
            f"Status: "
            f"{'Read-only shared database' if is_ro else 'Read-write personal database'}")

        # Disable Scan and other write-requiring buttons when
        # the DB is read-only. We don't disable the buttons
        # outright when just on a non-default-but-writable DB
        # since the user might legitimately want to scan into
        # a fresh DB file they just saved.
        if hasattr(self, "btn_scan"):
            self.btn_scan.setEnabled(not is_ro)
        if hasattr(self, "btn_db_default"):
            # Already on default? Grey out the switch button.
            self.btn_db_default.setEnabled(not is_default)
        # Stats-tab write buttons (cleanup, vacuum, reset) only
        # make sense on a writable DB.
        for attr in ("btn_cleanup", "btn_vacuum", "btn_reset"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setEnabled(not is_ro)
        # Also the Issues-tab clear button writes to the DB
        if hasattr(self, "btn_issues_clear"):
            self.btn_issues_clear.setEnabled(not is_ro)

    def _on_open_db(self):
        """Load a database file from another user. Opens in
        read-only mode so the friend's catalog can't be
        modified.

        Stops the watcher if it was running, since the watcher
        would otherwise try to ingest into the friend's DB.
        The watcher's folder list is preserved - the user can
        restart it after switching back to their own DB."""
        from pathlib import Path as _Path
        start_dir = str(_Path.home())
        # Last-used "open" path is remembered for convenience
        try:
            from .config import CONFIG_DIR
            last_file = CONFIG_DIR / "db_browser_last_open.txt"
            if last_file.is_file():
                last_dir = last_file.read_text().strip()
                if _Path(last_dir).is_dir():
                    start_dir = last_dir
        except Exception:
            pass

        path, _filter = QFileDialog.getOpenFileName(
            self, "Open shared database",
            start_dir,
            "SQLite databases (*.sqlite *.db *.qdb);;All files (*)")
        if not path:
            return
        p = _Path(path)
        if not p.is_file():
            QMessageBox.warning(
                self, "Open DB", f"Not a file:\n{p}")
            return

        # Stop the watcher so it can't try to write to the new
        # (read-only) DB. The watched-folders config persists so
        # the user can resume watching their own DB later.
        try:
            from . import db_watcher
            if db_watcher.is_running():
                db_watcher.stop_watcher()
        except Exception:
            pass

        # Switch! From now on every database.connection() opens
        # the new file in read-only mode.
        database.set_db_path(p, readonly=True)

        # Probe the new DB to make sure it's actually a Quopus
        # catalog before showing the user "0 files" and confusing
        # them. We check for the files table - the most basic
        # marker that this is one of our DBs.
        try:
            with database.connection() as conn:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='files'")
                if cur.fetchone() is None:
                    raise ValueError(
                        "No 'files' table found - this doesn't "
                        "look like a Quopus database.")
        except Exception as e:
            QMessageBox.critical(
                self, "Open DB",
                f"Couldn't open {p.name}:\n{e}\n\n"
                f"Reverting to your own database.")
            database.switch_to_default()
            self._update_db_file_label()
            self._refresh_stats()
            return

        # Persist the directory for next time
        try:
            from .config import CONFIG_DIR
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / "db_browser_last_open.txt").write_text(
                str(p.parent))
        except Exception:
            pass

        # Refresh all the tabs to reflect the new data
        self._update_db_file_label()
        self._refresh_stats()
        self._refresh_issues()
        # Clear any current search results - they reference the
        # OLD DB's row IDs and would be invalid
        if hasattr(self, "tree_files"):
            self.tree_files.clear()
        if hasattr(self, "tree_disks"):
            self.tree_disks.clear()
        if hasattr(self, "tree_disk_files"):
            self.tree_disk_files.clear()
        self.lbl_status.setText(
            f"Loaded read-only DB: {p.name}")
        # Window title shows which DB is active
        self.setWindowTitle(f"Quopus Database — {p.name} [Read-Only]")

    def _on_save_db_as(self):
        """Save the current DB to a new file. This makes a
        portable copy - just the .sqlite file - that can be
        shared with another user, who opens it via Open DB
        in their Quopus.

        Uses sqlite's backup API rather than a plain file copy
        because the source DB might have in-flight WAL entries
        that haven't been committed back to the main file yet.
        The backup API correctly checkpoints and produces a
        complete standalone file.
        """
        from pathlib import Path as _Path
        # Default filename: own name with a timestamp suffix so
        # multiple exports don't overwrite each other
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M%S")
        default_name = f"quopus_db_{ts}.sqlite"
        start_dir = str(_Path.home() / default_name)

        path, _filter = QFileDialog.getSaveFileName(
            self, "Save database copy as",
            start_dir,
            "SQLite databases (*.sqlite);;All files (*)")
        if not path:
            return
        out = _Path(path)
        if not out.suffix:
            out = out.with_suffix(".sqlite")

        # Use sqlite3's backup API - works on WAL-mode DBs and
        # produces a clean standalone file even if writers are
        # active. We have to open BOTH connections without our
        # _connect() helper since that respects _db_readonly and
        # we want the source DB to be readable, target DB to be
        # writable, regardless of current state.
        import sqlite3 as _sql
        self.lbl_status.setText("Saving...")
        QApplication.processEvents()
        try:
            src = _sql.connect(str(database.DB_PATH))
            try:
                dst = _sql.connect(str(out))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except Exception as e:
            QMessageBox.critical(
                self, "Save As", f"Couldn't save:\n{e}")
            self.lbl_status.setText("Save failed")
            return

        self.lbl_status.setText(
            f"Saved to {out}  ({out.stat().st_size:,} bytes)")
        # Offer to immediately switch to the new file so user can
        # verify it loaded right. Useful sanity check before
        # sharing.
        if QMessageBox.question(
                self, "Save complete",
                f"Saved {out.stat().st_size:,} bytes to:\n{out}\n\n"
                f"Open the new file now to verify? "
                f"(opens read-only)",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            database.set_db_path(out, readonly=True)
            self._update_db_file_label()
            self._refresh_stats()
            self.setWindowTitle(
                f"Quopus Database — {out.name} [Read-Only]")

    def _on_open_default_db(self):
        """Switch back to the user's own DB. Doesn't auto-restart
        the watcher (the user can do that explicitly from the
        Watch tab) so they don't get surprise indexing kicking
        off when they just wanted to see their own catalog
        again."""
        if database.is_default_db():
            self.lbl_status.setText("Already on your own DB")
            return
        database.switch_to_default()
        database.init_db()  # ensure default DB still has schema
        self._update_db_file_label()
        self._refresh_stats()
        self._refresh_issues()
        # Clear search results - they're from the previous DB
        if hasattr(self, "tree_files"):
            self.tree_files.clear()
        if hasattr(self, "tree_disks"):
            self.tree_disks.clear()
        if hasattr(self, "tree_disk_files"):
            self.tree_disk_files.clear()
        self.setWindowTitle("Quopus Database")
        self.lbl_status.setText("Switched to your own DB")

    # --------------------------------------------------------
    # Files tab
    # --------------------------------------------------------

    def _build_files_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel("Search filename:"))
        self.ed_files = QLineEdit()
        self.ed_files.setPlaceholderText(
            "Type to search... (substring match, e.g. 'turrican')")
        self.ed_files.textChanged.connect(self._on_files_query_changed)
        row.addWidget(self.ed_files, 1)
        # Dedupe-by-MD5 toggle - collapses identical files (same
        # binary content) into a single row, with a [+N] badge on
        # the Name column showing how many copies exist. Useful
        # for Scenebase-style trees where the same release has been
        # mirrored across many directories. Persisted in config
        # so the user's preference survives restarts.
        self.btn_files_dedupe = QPushButton("Dedup: OFF")
        self.btn_files_dedupe.setCheckable(True)
        self.btn_files_dedupe.setToolTip(
            "Collapse files with identical MD5 into a single row.\n"
            "Hit count shown as [+N] badge after the filename.\n"
            "Right-click a deduplicated row -> 'Show all copies'\n"
            "to see every location, ignoring the toggle.")
        self.btn_files_dedupe.toggled.connect(
            self._on_dedupe_toggled)
        row.addWidget(self.btn_files_dedupe)
        v.addLayout(row)

        # Restore the toggle state from config so the user's
        # preferred mode survives restarts. Has to come before
        # the first search.
        self._load_dedupe_mode()

        self.tree_files = QTreeWidget()
        self.tree_files.setHeaderLabels([
            "Name", "Type", "Size", "Location", "MD5"])
        self.tree_files.setColumnWidth(0, 220)
        self.tree_files.setColumnWidth(1, 60)
        self.tree_files.setColumnWidth(2, 80)
        self.tree_files.setColumnWidth(3, 380)
        self.tree_files.setColumnWidth(4, 250)
        self.tree_files.setRootIsDecorated(False)
        self.tree_files.setUniformRowHeights(True)
        # ExtendedSelection lets the user shift-click / ctrl-click
        # to pick multiple rows. We use this for batch actions
        # like "Run all selected files in emulator one after the
        # other" - launching the next one each time the previous
        # emulator instance exits.
        self.tree_files.setSelectionMode(
            QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree_files.itemDoubleClicked.connect(self._on_open_item)
        # Right-click context menu with reveal/copy-to-lister
        # actions. The menu is built per-item so it knows whether
        # this is a file vs a disk-entry and shows different
        # options accordingly.
        self.tree_files.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_files.customContextMenuRequested.connect(
            self._on_files_context_menu)
        install_table_state(self.tree_files,
                            "database_browser:files")
        v.addWidget(self.tree_files, 1)

        # Debounce timer - search runs 300ms after the user stops
        # typing. Without this, every keystroke would re-query and
        # the trigram FTS matches can be slow on huge DBs.
        self._files_timer = QTimer(self)
        self._files_timer.setSingleShot(True)
        self._files_timer.setInterval(300)
        self._files_timer.timeout.connect(self._do_files_search)

        self.tabs.addTab(w, "Files")

    def _on_files_query_changed(self):
        self._files_timer.start()

    def _dedupe_mode_file(self):
        """Where the dedupe-toggle preference is persisted.
        Lives in config/db_browser_dedupe.txt - separate from
        size_mode and other preferences so each is independently
        upgradeable."""
        from .config import CONFIG_DIR
        return CONFIG_DIR / "db_browser_dedupe.txt"

    def _load_dedupe_mode(self):
        """Read the saved dedupe toggle state. Default off so a
        new install behaves like the old code (all hits shown)."""
        try:
            p = self._dedupe_mode_file()
            if p.is_file():
                txt = p.read_text().strip().lower()
                self.btn_files_dedupe.setChecked(txt == "on")
        except Exception:
            pass
        self._sync_dedupe_button_label()

    def _save_dedupe_mode(self):
        """Persist the current toggle state for next launch."""
        try:
            from .config import CONFIG_DIR
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._dedupe_mode_file().write_text(
                "on" if self.btn_files_dedupe.isChecked() else "off")
        except Exception:
            pass

    def _sync_dedupe_button_label(self):
        """Update the button label so it always reflects state."""
        on = self.btn_files_dedupe.isChecked()
        self.btn_files_dedupe.setText(
            "Dedup: ON" if on else "Dedup: OFF")

    def _on_dedupe_toggled(self, checked: bool):
        """User clicked the dedupe button - save, re-render
        currently-displayed search results with the new mode."""
        self._sync_dedupe_button_label()
        self._save_dedupe_mode()
        # Re-run the current search through the new mode. No
        # debounce timer kick - the user's intent is clear.
        if self.ed_files.text().strip():
            self._do_files_search()

    def _do_files_search(self):
        # Defensive: called by the debounce timer 300ms after
        # the user stops typing. If the dialog closed in those
        # 300ms the timer should be stopped, but a tick already
        # in-flight needs to fail safely rather than crash.
        try:
            self._do_files_search_impl()
        except RuntimeError as e:
            if "deleted" in str(e):
                return
            raise

    def _do_files_search_impl(self):
        q = self.ed_files.text().strip()
        self.tree_files.clear()
        if not q:
            self.lbl_status.setText("Type something to search...")
            return
        dedupe = self.btn_files_dedupe.isChecked()
        try:
            results = database.search_filenames(
                q, limit=2000, dedupe_md5=dedupe)
        except Exception as e:
            self.lbl_status.setText(f"Search error: {e}")
            return
        # Count how many MD5-duplicate groups got collapsed - we
        # surface this in the status bar so the user notices.
        collapsed_groups = 0
        collapsed_rows = 0
        for r in results:
            it = QTreeWidgetItem()
            # Name column shows "[+N more]" badge for collapsed
            # MD5 groups so the user knows there are other copies
            # they could view via the context menu.
            dup_count = r.get("dup_count", 1)
            name_text = r["name"]
            if dup_count > 1:
                name_text = f"{name_text}   [+{dup_count - 1} more]"
                collapsed_groups += 1
                collapsed_rows += dup_count - 1
            it.setText(0, name_text)
            if r["kind"] == "file":
                it.setText(1, "file")
                it.setText(2, _fmt_size_or_blocks(
                    r["size"], self.size_mode))
                # Show container path if from archive
                loc = r["container"] if r["container"] else r["path"]
                it.setText(3, loc)
            else:
                it.setText(1, (r.get("file_type") or "").upper())
                it.setText(2, _fmt_size_or_blocks(
                    r["size_bytes"], self.size_mode))
                it.setText(3, r["path"])
            it.setText(4, r.get("md5") or "")
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            self.tree_files.addTopLevelItem(it)
        # Tell the user which search mode ran. Below 3 chars we
        # fall back to LIKE which is slower on huge DBs - useful
        # to know so they understand any noticeable lag.
        mode = "slow scan" if len(q) < 3 else "indexed"
        if dedupe and collapsed_groups:
            self.lbl_status.setText(
                f"{len(results)} unique match(es) for {q!r} "
                f"({mode}) - {collapsed_groups} group(s) collapsed, "
                f"{collapsed_rows} duplicate(s) hidden")
        else:
            self.lbl_status.setText(
                f"{len(results)} match(es) for {q!r} ({mode})")

    # --------------------------------------------------------
    # Disks tab
    # --------------------------------------------------------

    def _build_disks_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel("Search disk header / ID:"))
        self.ed_disks = QLineEdit()
        self.ed_disks.setPlaceholderText(
            "Type disk name (e.g. 'SIDOLOGY 12') or ID (e.g. '2A')")
        self.ed_disks.textChanged.connect(self._on_disks_query_changed)
        row.addWidget(self.ed_disks, 1)
        v.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)

        self.tree_disks = QTreeWidget()
        self.tree_disks.setHeaderLabels([
            "Disk Name", "ID", "Type", "Files", "Image Path"])
        self.tree_disks.setColumnWidth(0, 200)
        self.tree_disks.setColumnWidth(1, 60)
        self.tree_disks.setColumnWidth(2, 50)
        self.tree_disks.setColumnWidth(3, 60)
        self.tree_disks.setColumnWidth(4, 500)
        self.tree_disks.setRootIsDecorated(False)
        self.tree_disks.itemSelectionChanged.connect(
            self._on_disk_selected)
        self.tree_disks.itemDoubleClicked.connect(self._on_open_item)
        # Right-click: copy whole disk image to lister, reveal etc
        self.tree_disks.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_disks.customContextMenuRequested.connect(
            self._on_disks_context_menu)
        install_table_state(self.tree_disks,
                            "database_browser:disks")
        split.addWidget(self.tree_disks)

        # Lower pane: file listing of the selected disk
        self.tree_disk_files = QTreeWidget()
        self.tree_disk_files.setHeaderLabels([
            "Name", "Type", "Blocks", "Bytes", "MD5"])
        self.tree_disk_files.setColumnWidth(0, 220)
        self.tree_disk_files.setColumnWidth(1, 60)
        self.tree_disk_files.setColumnWidth(2, 60)
        self.tree_disk_files.setColumnWidth(3, 80)
        self.tree_disk_files.setColumnWidth(4, 250)
        self.tree_disk_files.setRootIsDecorated(False)
        # Right-click: extract single PRG from disk to lister
        self.tree_disk_files.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_disk_files.customContextMenuRequested.connect(
            self._on_disk_files_context_menu)
        install_table_state(self.tree_disk_files,
                            "database_browser:disk_files")
        split.addWidget(self.tree_disk_files)

        split.setSizes([400, 250])
        v.addWidget(split, 1)

        self._disks_timer = QTimer(self)
        self._disks_timer.setSingleShot(True)
        self._disks_timer.setInterval(300)
        self._disks_timer.timeout.connect(self._do_disks_search)

        self.tabs.addTab(w, "Disks")

    def _on_disks_query_changed(self):
        self._disks_timer.start()

    def _do_disks_search(self):
        # Defensive: see _do_files_search docstring.
        try:
            self._do_disks_search_impl()
        except RuntimeError as e:
            if "deleted" in str(e):
                return
            raise

    def _do_disks_search_impl(self):
        q = self.ed_disks.text().strip()
        self.tree_disks.clear()
        self.tree_disk_files.clear()
        if not q:
            self.lbl_status.setText("Type something to search...")
            return
        try:
            results = database.search_disk_headers(q, limit=500)
        except Exception as e:
            self.lbl_status.setText(f"Search error: {e}")
            return
        for r in results:
            it = QTreeWidgetItem()
            it.setText(0, r["disk_name"] or "")
            it.setText(1, r["disk_id"] or "")
            it.setText(2, (r["image_type"] or "").upper())
            it.setText(3, str(r["file_count"]))
            it.setText(4, r["path"])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            self.tree_disks.addTopLevelItem(it)
        self.lbl_status.setText(
            f"{len(results)} disk(s) match {q!r}")

    def _on_disk_selected(self):
        """When user clicks a disk, show its file listing in the
        lower pane. Each entry stores its full info as UserRole
        data so the context-menu code can extract the file later
        without re-querying.

        We need the disk image's host path AND the entry's
        track/sector so extraction (via CbmDiskReader) can
        actually reach into the .d64 file on disk."""
        self.tree_disk_files.clear()
        items = self.tree_disks.selectedItems()
        if not items:
            return
        disk_data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not disk_data:
            return
        disk_id = disk_data["id"]
        # Look up the disk image's path on disk and image type
        # so the context-menu code can extract files later
        with database.connection() as conn:
            disk_info = conn.execute("""
                SELECT d.image_type, d.disk_name, f.path
                FROM disk_images d
                JOIN files f ON f.id = d.file_id
                WHERE d.id = ?
            """, (disk_id,)).fetchone()
            if not disk_info:
                return
            cur = conn.execute("""
                SELECT name, file_type, size_blocks, size_bytes,
                       md5, track, sector
                FROM disk_entries
                WHERE disk_image_id = ?
                ORDER BY name
            """, (disk_id,))
            for row in cur.fetchall():
                it = QTreeWidgetItem()
                it.setText(0, row["name"] or "")
                it.setText(1, (row["file_type"] or "").upper())
                it.setText(2, str(row["size_blocks"] or 0))
                it.setText(3, _fmt_size(row["size_bytes"]))
                it.setText(4, row["md5"] or "")
                # Stash everything the context menu needs to
                # extract this entry: which disk file it lives
                # in, what type, where the file starts.
                it.setData(0, Qt.ItemDataRole.UserRole, {
                    "name": row["name"],
                    "file_type": row["file_type"],
                    "size_blocks": row["size_blocks"],
                    "track": row["track"],
                    "sector": row["sector"],
                    "disk_path": disk_info["path"],
                    "image_type": disk_info["image_type"],
                    "disk_name": disk_info["disk_name"],
                })
                self.tree_disk_files.addTopLevelItem(it)

    # --------------------------------------------------------
    # Context menus - "Copy to Lister", "Reveal", etc
    # --------------------------------------------------------

    def _on_files_context_menu(self, pos):
        """Right-click on the Files tab. Builds a menu based on
        whether the selected row is a real file (path on disk),
        a member of an archive (virtual path with '!'), or a
        disk entry."""
        item = self.tree_files.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        # Collect all selected rows so we can offer bulk actions.
        # The right-click happens on `item` even if it's not in
        # the selection - Qt's default behaviour. If the user
        # right-clicked outside the current selection we use just
        # that one item; otherwise we use the full selection.
        selected_items = self.tree_files.selectedItems()
        if item not in selected_items or len(selected_items) <= 1:
            selected_data = [data]
        else:
            selected_data = [
                it.data(0, Qt.ItemDataRole.UserRole)
                for it in selected_items
                if it.data(0, Qt.ItemDataRole.UserRole)]
        if len(selected_data) > 1:
            menu = self._build_multi_file_context_menu(selected_data)
        else:
            menu = self._build_file_context_menu(data)
        menu.exec(self.tree_files.viewport().mapToGlobal(pos))

    def _on_disks_context_menu(self, pos):
        """Right-click on the upper Disks tab pane. Each row is
        a whole disk image - we offer to copy the .d64 file to
        the inactive lister, or reveal its containing folder."""
        item = self.tree_disks.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or not data.get("path"):
            return
        menu = QMenu(self)
        path = data["path"]
        from pathlib import Path as _Path
        p = _Path(path)
        self._add_lister_actions(
            menu, source_path=p,
            label=f"disk image '{data.get('disk_name') or p.name}'")
        menu.addSeparator()
        a_copy_path = menu.addAction("Copy Path to Clipboard")
        a_copy_path.triggered.connect(
            lambda: self._copy_to_clipboard(path))
        menu.exec(self.tree_disks.viewport().mapToGlobal(pos))

    def _on_disk_files_context_menu(self, pos):
        """Right-click on a single file inside a disk image.
        We can either extract just this PRG to the lister, or
        copy the whole containing .d64."""
        item = self.tree_disk_files.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        menu = QMenu(self)
        from pathlib import Path as _Path
        disk_path = _Path(data["disk_path"])
        entry_name = data["name"]

        # Action 1: Extract this single file from the disk to
        # the inactive lister
        if self._has_lister():
            _, inactive = self.main_window._active_lister()
            dest_label = self._lister_label(inactive)
            a_extract = menu.addAction(
                f"Extract '{entry_name}' to {dest_label}")
            a_extract.triggered.connect(
                lambda: self._extract_disk_entry(data))

            # Action 2: Copy the whole containing disk image
            a_copy_disk = menu.addAction(
                f"Copy whole disk '{disk_path.name}' to "
                f"{dest_label}")
            a_copy_disk.triggered.connect(
                lambda: self._copy_path_to_inactive(disk_path))
            menu.addSeparator()

        # Reveal the .d64 file in lister
        a_reveal = menu.addAction(
            f"Reveal disk '{disk_path.name}' in active Lister")
        a_reveal.setEnabled(self._has_lister())
        a_reveal.triggered.connect(
            lambda: self._reveal_in_active_lister(disk_path))
        menu.addSeparator()

        a_copy_disk_path = menu.addAction(
            "Copy Disk Path to Clipboard")
        a_copy_disk_path.triggered.connect(
            lambda: self._copy_to_clipboard(str(disk_path)))
        a_copy_name = menu.addAction(
            f"Copy Filename '{entry_name}' to Clipboard")
        a_copy_name.triggered.connect(
            lambda: self._copy_to_clipboard(entry_name))

        menu.exec(self.tree_disk_files.viewport().mapToGlobal(pos))

    def _build_file_context_menu(self, data) -> "QMenu":
        """Compose a menu for a Files-tab row. Different options
        appear depending on what the row represents:
          - File on disk (kind='file', has a real path): copy
            or reveal it
          - Archive member (kind='file', path contains '!'):
            can't reveal directly - offer to extract to lister
            or copy the container path
          - Disk entry (kind='entry'): extract from disk
        """
        from pathlib import Path as _Path
        menu = QMenu(self)
        kind = data.get("kind")
        if kind == "file":
            path_str = data.get("path", "")
            container = data.get("container")  # parent archive

            if container:
                # This is an archive member. The 'path' is
                # virtual (foo.zip!member.prg). For runnable
                # types (PRG, D64, CRT, ...) we can extract the
                # member on demand and launch it - the resolver
                # writes it to %TEMP%/quopus_run/<hash>/ and
                # hands the extracted path to the emulator.
                cp = _Path(container)
                self._add_lister_actions(
                    menu, source_path=cp,
                    label=f"archive '{cp.name}' (containing "
                          f"'{data.get('name')}')")
                # Run-in-emulator for runnable archive members.
                # We sniff the extension off the member name (not
                # the archive's), because the archive is .zip but
                # the member could be .prg or .d64.
                member_ext = _Path(
                    data.get("name", "")).suffix.lower()
                if member_ext in self._RUNNABLE_EXTS:
                    a_run = menu.addAction(
                        f"Run '{data.get('name')}' in emulator")
                    a_run.setToolTip(
                        "Extract this file from the archive into\n"
                        "a temp location and launch it in the\n"
                        "configured C64 emulator.")
                    a_run.setEnabled(cp.is_file())
                    file_ref_id = data.get("ref_id")
                    a_run.triggered.connect(
                        lambda checked=False, pid=path_str,
                               rid=file_ref_id:
                            self._launch_resolved(pid, rid))
                menu.addSeparator()
                a_copy = menu.addAction(
                    "Copy Archive Path to Clipboard")
                a_copy.triggered.connect(
                    lambda: self._copy_to_clipboard(container))
            else:
                # Regular file on disk
                p = _Path(path_str)
                self._add_lister_actions(
                    menu, source_path=p,
                    label=f"file '{p.name}'")
                # Run-in-emulator for C64-executable file types.
                # The set is the class constant _RUNNABLE_EXTS
                # so the single-item and bulk paths agree on
                # which extensions count as runnable.
                if p.suffix.lower() in self._RUNNABLE_EXTS:
                    a_run = menu.addAction(
                        f"Run '{p.name}' in emulator")
                    # Enabled if either the file exists OR it's
                    # a virtual archive-member path (those don't
                    # exist on disk but we'll extract them on
                    # demand via _resolve_to_real_file).
                    is_virtual = "!" in path_str
                    a_run.setEnabled(p.is_file() or is_virtual)
                    file_ref_id = data.get("ref_id")
                    a_run.triggered.connect(
                        lambda checked=False, pid=path_str,
                               rid=file_ref_id:
                            self._launch_resolved(pid, rid))
                menu.addSeparator()
                a_copy = menu.addAction(
                    "Copy Path to Clipboard")
                a_copy.triggered.connect(
                    lambda: self._copy_to_clipboard(path_str))
        else:  # kind == 'entry' (a PRG/SEQ/USR/REL inside a disk)
            # In v3 we tried to split the synthetic display path
            # "disk_path:disk_name/entry_name" on ':' to recover
            # the host path. That broke on two things:
            #   1) Windows drive letters - "C:\Games\f.d64" has
            #      a ':' so split(':')[0] returns just "C".
            #   2) Nested containers - if a D64 is inside a ZIP,
            #      the synthetic path has more layers and split
            #      gives nonsense.
            # The fix is to read disk_path directly from the data
            # dict; database.search_filenames() already stores it
            # as a proper string. Same for ref_id (the disk_entries
            # row id) which lets us do an O(1) lookup later.
            disk_path_str = data.get("disk_path") or ""
            disk_path = _Path(disk_path_str)
            entry_name = data.get("name", "")
            entry_id = data.get("ref_id")
            # Virtual paths (containing '!') refer to disk images
            # nested inside archive containers. Those don't exist
            # as real files but we can extract them on demand via
            # _resolve_to_real_file.
            is_virtual_disk = "!" in disk_path_str

            if self._has_lister():
                _, inactive = self.main_window._active_lister()
                dest_label = self._lister_label(inactive)
                # Need to assemble a fake disk_entry record for
                # the extractor since we only have the row from
                # the search result, not the full entry data
                # with track/sector. Look up by id (fast) instead
                # of (host_path + entry_name) which was both slow
                # AND broken for archive-nested disks.
                a_extract = menu.addAction(
                    f"Extract '{entry_name}' to {dest_label}")
                a_extract.triggered.connect(
                    lambda: self._extract_disk_entry_by_search(
                        data))

                a_copy_disk = menu.addAction(
                    f"Copy whole disk '{disk_path.name}' to "
                    f"{dest_label}")
                if is_virtual_disk:
                    # Need to extract from container first, then
                    # copy the extracted file to the inactive
                    # lister. Wrap that in a helper lambda.
                    disk_file_id = data.get("disk_file_id")
                    a_copy_disk.triggered.connect(
                        lambda checked=False,
                               dps=disk_path_str,
                               dfid=disk_file_id:
                            self._copy_resolved_to_inactive(
                                dps, dfid))
                else:
                    a_copy_disk.triggered.connect(
                        lambda: self._copy_path_to_inactive(
                            disk_path))
                menu.addSeparator()

            # "Run in emulator" - extracts the PRG/disk and
            # launches the configured C64 emulator (VICE etc.)
            # with the file as argument. For PRG-style entries
            # we extract to a temp dir; for non-extractable
            # entries we fall back to running the whole disk.
            a_run = menu.addAction(
                f"Run '{entry_name}' in emulator")
            a_run.setToolTip(
                "Extract this entry and launch it in the "
                "configured C64 emulator. Requires emulator "
                "to be set in the C64 emulator config dialog.")
            a_run.triggered.connect(
                lambda: self._run_disk_entry_in_emulator(data))

            a_run_disk = menu.addAction(
                f"Mount disk '{disk_path.name}' in emulator")
            a_run_disk.setToolTip(
                "Launch the C64 emulator with the whole disk "
                "image mounted on Drive 8 (LOAD\"*\",8,1).\n"
                "If the disk is inside an archive, it gets "
                "extracted to a temp file first.")
            a_run_disk.setEnabled(
                disk_path.is_file() or is_virtual_disk)
            disk_file_id = data.get("disk_file_id")
            a_run_disk.triggered.connect(
                lambda checked=False,
                       dps=disk_path_str,
                       dfid=disk_file_id:
                    self._run_disk_in_emulator(dps, dfid))

            menu.addSeparator()

            # For virtual disks (D64 inside ZIP etc), reveal the
            # outer container instead - the D64 doesn't exist on
            # disk so 'reveal' would land on nothing.
            if is_virtual_disk:
                # parts[0] is the real on-disk container path
                container_path = _Path(
                    disk_path_str.split("!")[0])
                a_reveal = menu.addAction(
                    f"Reveal container '{container_path.name}' "
                    f"in active Lister")
                a_reveal.setEnabled(self._has_lister()
                                    and container_path.is_file())
                a_reveal.triggered.connect(
                    lambda: self._reveal_in_active_lister(
                        container_path))
            else:
                a_reveal = menu.addAction(
                    f"Reveal disk '{disk_path.name}' in active "
                    f"Lister")
                a_reveal.setEnabled(self._has_lister()
                                    and disk_path.is_file())
                a_reveal.triggered.connect(
                    lambda: self._reveal_in_active_lister(
                        disk_path))

        # If the row is the head of a deduplicated MD5 group,
        # add an action to expand all copies. Only shown when
        # there's actually something to expand (dup_count > 1)
        # AND we have an MD5 to query by - MD5-less rows can't
        # be looked up.
        dup_count = data.get("dup_count", 1)
        md5 = data.get("md5")
        if dup_count > 1 and md5:
            menu.addSeparator()
            a_show_dups = menu.addAction(
                f"Show all {dup_count} copies (same MD5)")
            a_show_dups.setToolTip(
                "Open a dialog listing every file and disk "
                "entry that shares this MD5.")
            a_show_dups.triggered.connect(
                lambda: self._show_md5_duplicates(md5,
                                                  data.get("name", "")))
        return menu

    # ---- Multi-selection context menu -----------------------

    _RUNNABLE_EXTS = {
        ".prg", ".p00", ".d64", ".d71", ".d81",
        ".g64", ".g71", ".d80", ".d82", ".crt",
        ".tap", ".t64"}

    def _build_multi_file_context_menu(self, items: list) -> "QMenu":
        """Build a context menu for when the user has multiple
        rows selected in the Files tab.

        We can't sensibly offer every per-item action (extract,
        reveal, copy path) on a heterogeneous selection, so we
        focus on the things that ARE useful in batch:

          - Run all in emulator (sequentially) - launches the
            first item, waits for the emulator to exit, then
            launches the next. Lets the user queue up an
            evening of demos without going back to the dialog
            for each one.
          - Copy all to inactive lister - bulk copy.
          - Copy all paths to clipboard - one per line.

        The menu shows a header line with the selection count
        so the user knows the actions apply to all selected
        items, not just the right-clicked one.
        """
        from pathlib import Path as _Path
        menu = QMenu(self)
        header = menu.addAction(f"{len(items)} item(s) selected")
        header.setEnabled(False)
        menu.addSeparator()

        # Build the list of resolvable host paths once and
        # remember which items are runnable / copyable.
        runnable = []   # list of (item_data, kind, hint)
        host_paths = []
        for it in items:
            if it.get("kind") == "file":
                p = _Path(it.get("path", ""))
                if p.suffix.lower() in self._RUNNABLE_EXTS:
                    runnable.append(it)
                if p.is_file():
                    host_paths.append(p)
            elif it.get("kind") == "entry":
                # Disk entries always run via extract-then-launch.
                # No file-ext check - the entry type tells us.
                runnable.append(it)

        # Bulk-Run sequentially
        if runnable:
            a_run = menu.addAction(
                f"Run all {len(runnable)} item(s) in emulator "
                f"(sequential)")
            a_run.setToolTip(
                "Launch the first item in the C64 emulator.\n"
                "When you close the emulator window, the next\n"
                "item launches automatically. Cancel by closing\n"
                "the progress dialog.")
            a_run.triggered.connect(
                lambda: self._run_multiple_in_emulator_sequential(
                    runnable))
            menu.addSeparator()

        # Bulk-copy to inactive lister
        if host_paths and self._has_lister():
            _, inactive = self.main_window._active_lister()
            dest_label = self._lister_label(inactive)
            a_copy = menu.addAction(
                f"Copy {len(host_paths)} file(s) to {dest_label}")
            a_copy.triggered.connect(
                lambda: self._bulk_copy_to_inactive(host_paths))

        # Copy all paths to clipboard, one per line
        if items:
            paths_for_clipboard = []
            for it in items:
                if it.get("kind") == "file":
                    paths_for_clipboard.append(
                        it.get("path", ""))
                else:
                    # Disk entries: use the synthetic display
                    # path which includes both disk and entry
                    paths_for_clipboard.append(
                        it.get("path", ""))
            if paths_for_clipboard:
                a_copy_paths = menu.addAction(
                    f"Copy all {len(paths_for_clipboard)} path(s) "
                    f"to clipboard")
                a_copy_paths.triggered.connect(
                    lambda: self._copy_to_clipboard(
                        "\n".join(paths_for_clipboard)))

        return menu

    def _bulk_copy_to_inactive(self, paths: list):
        """Copy multiple files to the inactive lister, in
        sequence. Just delegates to the existing single-file
        copier so we get the same overwrite-confirm + refresh
        behaviour."""
        if not self._has_lister():
            return
        copied = 0
        for p in paths:
            try:
                self._copy_path_to_inactive(p)
                copied += 1
            except Exception as e:
                self.lbl_status.setText(
                    f"Copy failed at {p.name}: {e}")
                break
        self.lbl_status.setText(
            f"Copied {copied} of {len(paths)} file(s)")

    def _run_multiple_in_emulator_sequential(self, items: list):
        """Launch items in the emulator one after the other.

        Each emulator invocation is monitored by a QThread
        which calls .wait() on the subprocess; when the user
        closes the emulator window, the thread emits a signal
        and we launch the next item.

        A small non-modal progress dialog shows current item
        + queue position and gives the user a Cancel button to
        abort the chain without having to wait for the current
        emulator to exit.

        Disk entries get extracted to a temp dir first (same as
        the single-item run path); regular files are launched
        directly. Items that fail to launch are logged and
        skipped rather than aborting the whole queue.
        """
        from PyQt6.QtCore import QThread, pyqtSignal

        # State for the sequential runner. Held on self so the
        # objects don't get GC'd while the chain is running, and
        # so a second invocation can detect and reject overlap.
        if getattr(self, "_seq_runner_active", False):
            QMessageBox.information(
                self, "Sequential runner busy",
                "A sequential emulator run is already in "
                "progress. Close that one first.")
            return

        # Pre-resolve everything we can up-front so we don't
        # do expensive DB lookups while the user's waiting.
        # Each entry becomes (display_name, resolved_path).
        from pathlib import Path as _Path
        import tempfile
        from .cbmfiles import (CbmDiskReader,
                                _petscii_filename_to_ascii)

        resolved = []  # list of dicts: {name, path, temp_path?}
        errors = []
        for it in items:
            try:
                if it.get("kind") == "file":
                    p = _Path(it.get("path", ""))
                    if not p.is_file():
                        errors.append(
                            f"Not found: {p}")
                        continue
                    resolved.append({
                        "name": p.name,
                        "path": p,
                        "kind": "file",
                    })
                elif it.get("kind") == "entry":
                    # Look up + extract to temp now so we don't
                    # block between launches.
                    entry_id = it.get("ref_id")
                    if entry_id is None:
                        errors.append(
                            f"Missing ref_id for entry "
                            f"{it.get('name')}")
                        continue
                    with database.connection() as conn:
                        row = conn.execute("""
                            SELECT e.name, e.file_type, e.track,
                                   e.sector, d.image_type,
                                   d.disk_name,
                                   f.id as disk_file_id, f.path
                            FROM disk_entries e
                            JOIN disk_images d
                                ON d.id = e.disk_image_id
                            JOIN files f ON f.id = d.file_id
                            WHERE e.id = ?
                        """, (entry_id,)).fetchone()
                    if not row:
                        errors.append(
                            f"DB row gone: entry "
                            f"id={entry_id}")
                        continue
                    disk_path_str = row["path"]
                    # Resolve nested archives - this may extract
                    # a D64 from a ZIP to a temp file. For plain
                    # paths it's just a file existence check.
                    # Resolver returns None and shows a warning
                    # dialog if it can't resolve; we still want
                    # to continue with other items so we treat
                    # that the same as a missing file.
                    try:
                        disk_path = self._resolve_to_real_file(
                            disk_path_str, quiet=True,
                            ref_id=row["disk_file_id"])
                    except Exception as resolve_err:
                        errors.append(
                            f"Resolve failed for {row['name']}: "
                            f"{resolve_err}")
                        continue
                    if disk_path is None:
                        errors.append(
                            f"Disk not accessible: "
                            f"{disk_path_str}")
                        continue
                    # Extract
                    reader = CbmDiskReader(str(disk_path))
                    reader.open()
                    target_entry = None
                    if (row["track"] is not None
                            and row["sector"] is not None):
                        for e in reader.entries:
                            if (e.start_track == row["track"]
                                    and e.start_sector
                                    == row["sector"]):
                                target_entry = e
                                break
                    if target_entry is None:
                        for e in reader.entries:
                            if _petscii_filename_to_ascii(
                                    e.name_petscii) == row["name"]:
                                target_entry = e
                                break
                    if target_entry is None:
                        errors.append(
                            f"Entry vanished: {row['name']}")
                        continue
                    data_bytes = reader.extract(target_entry)
                    tmp_dir = (_Path(tempfile.gettempdir())
                               / "quopus_run")
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = "".join(
                        c if c.isalnum() or c in "._- "
                        else "_"
                        for c in row["name"]).strip() or "entry"
                    ext = (row["file_type"] or "prg").lower()
                    out_path = tmp_dir / f"{safe_name}.{ext}"
                    out_path.write_bytes(data_bytes)
                    resolved.append({
                        "name": f"{row['name']} (from "
                                f"{disk_path.name})",
                        "path": out_path,
                        "kind": "entry",
                    })
            except Exception as e:
                errors.append(
                    f"{it.get('name', '?')}: {e}")

        if not resolved:
            QMessageBox.warning(
                self, "Sequential run",
                "Nothing to run after pre-flight resolution.\n\n"
                + ("\n".join(errors[:10]) if errors
                   else "(no items were resolvable)"))
            return

        if errors:
            print(f"[quopus] Sequential run: skipped "
                  f"{len(errors)} item(s):")
            for e in errors[:20]:
                print(f"  - {e}")

        # Set up progress dialog. Non-modal so the user can
        # still interact with Quopus while emulator is running.
        from PyQt6.QtWidgets import QProgressDialog
        prog = QProgressDialog(
            f"Starting sequential run ({len(resolved)} items)...",
            "Cancel queue", 0, len(resolved), self)
        prog.setWindowTitle("Run in emulator (sequential)")
        prog.setWindowModality(Qt.WindowModality.NonModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.show()

        # Worker thread: waits for emulator subprocess to exit
        # and emits a signal so the main thread can launch the
        # next item. We can't do proc.wait() on the main thread
        # because it would block Qt's event loop, freezing the
        # progress dialog.
        class _WaitWorker(QThread):
            done = pyqtSignal()

            def __init__(self, proc):
                super().__init__()
                self.proc = proc

            def run(self):
                try:
                    self.proc.wait()
                except Exception:
                    pass
                self.done.emit()

        state = {
            "index": 0,
            "cancelled": False,
            "current_worker": None,
            "current_proc": None,
        }
        self._seq_runner_state = state
        self._seq_runner_active = True

        def cleanup():
            self._seq_runner_active = False
            self._seq_runner_state = None
            try:
                prog.close()
            except Exception:
                pass

        def launch_next():
            if state["cancelled"] or state["index"] >= len(resolved):
                self.lbl_status.setText(
                    f"Sequential run done: "
                    f"{state['index']} item(s) launched")
                cleanup()
                return
            item = resolved[state["index"]]
            prog.setLabelText(
                f"Item {state['index']+1}/{len(resolved)}:\n"
                f"{item['name']}\n\n"
                "Close the emulator window to advance to "
                "the next item.")
            prog.setValue(state["index"])
            QApplication.processEvents()
            proc = self._launch_c64_emulator(
                item["path"], return_process=True)
            if proc is None:
                # Launch failed - skip and continue
                self.lbl_status.setText(
                    f"Skipped (launch failed): {item['name']}")
                state["index"] += 1
                QTimer.singleShot(0, launch_next)
                return
            state["current_proc"] = proc
            worker = _WaitWorker(proc)

            def on_worker_done():
                # Emulator exited. Move on - unless user
                # cancelled while we were waiting.
                state["current_worker"] = None
                state["current_proc"] = None
                state["index"] += 1
                if state["cancelled"]:
                    cleanup()
                    return
                launch_next()

            worker.done.connect(on_worker_done)
            state["current_worker"] = worker
            worker.start()

        def on_cancel():
            # User clicked Cancel in the progress dialog. We
            # don't kill the running emulator - that would be
            # rude. We just stop launching new items once the
            # current one exits.
            state["cancelled"] = True
            prog.setLabelText(
                "Cancelled - will stop after current "
                "emulator exits.")
            self.lbl_status.setText(
                "Sequential run cancelled (current item "
                "will finish)")

        prog.canceled.connect(on_cancel)
        launch_next()

    # ---- single-row helpers continue below ------------------

    # -- Helpers ------------------------------------------

    def _show_md5_duplicates(self, md5: str, hint_name: str = ""):
        """Open a dialog listing every file/entry with this MD5.

        Used by the 'Show all N copies' context-menu action that
        appears on deduplicated rows. The dialog re-uses the
        same column layout as the Files tab so the user can
        right-click a copy and trigger the normal copy-to-lister
        / extract / reveal actions on it.

        Non-modal so the user can browse copies and the main
        results list at the same time. Held in self._dup_dialogs
        so the GC doesn't collect them while they're open.
        """
        try:
            rows = database.find_by_md5(md5)
        except Exception as e:
            QMessageBox.warning(
                self, "Show copies",
                f"Couldn't look up MD5 {md5}:\n{e}")
            return
        if not rows:
            QMessageBox.information(
                self, "Show copies",
                f"No copies found for MD5 {md5}.\n\n"
                "The database may have been modified since the "
                "search ran.")
            return

        # Build a non-modal browser-like dialog. Re-uses the
        # row-rendering code so the columns match the main tab.
        dlg = QDialog(self)
        dlg.setWindowTitle(
            f"Copies of {hint_name or 'file'} "
            f"({len(rows)} found, MD5={md5[:16]}...)")
        dlg.setModal(False)
        dlg.resize(900, 400)
        # WA_DeleteOnClose so we don't accumulate dialogs in
        # the parent's child list across many open/close cycles.
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        lay = QVBoxLayout(dlg)

        info = QLabel(
            f"<b>{len(rows)} location(s) share the same MD5:</b> "
            f"{md5}<br>"
            "Right-click a row to copy/reveal/extract that "
            "specific copy.")
        info.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(info)

        tree = QTreeWidget()
        tree.setHeaderLabels([
            "Name", "Type", "Size", "Location", "MD5"])
        tree.setColumnWidth(0, 200)
        tree.setColumnWidth(1, 60)
        tree.setColumnWidth(2, 80)
        tree.setColumnWidth(3, 420)
        tree.setColumnWidth(4, 100)
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(True)
        # Wire up the same context menu so users can act on a
        # specific copy.
        tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)

        def on_ctx(pos):
            item = tree.itemAt(pos)
            if not item:
                return
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if not data:
                return
            menu = self._build_file_context_menu(data)
            menu.exec(tree.viewport().mapToGlobal(pos))

        tree.customContextMenuRequested.connect(on_ctx)
        # Double-click delegates to the main browser's open
        # handler so behaviour matches the Files tab.
        tree.itemDoubleClicked.connect(self._on_open_item)

        for r in rows:
            it = QTreeWidgetItem()
            it.setText(0, r["name"])
            if r["kind"] == "file":
                it.setText(1, "file")
                it.setText(2, _fmt_size_or_blocks(
                    r["size"], self.size_mode))
                loc = r["container"] if r["container"] else r["path"]
                it.setText(3, loc)
            else:
                it.setText(1, (r.get("file_type") or "").upper())
                it.setText(2, _fmt_size_or_blocks(
                    r["size_bytes"], self.size_mode))
                it.setText(3, r["path"])
            it.setText(4, r.get("md5") or "")
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            tree.addTopLevelItem(it)
        lay.addWidget(tree, 1)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        lay.addWidget(btn_close)

        # Track open dialogs so they don't get garbage-collected
        # the moment this method returns.
        if not hasattr(self, "_dup_dialogs"):
            self._dup_dialogs = []
        self._dup_dialogs.append(dlg)
        dlg.destroyed.connect(
            lambda *_: self._dup_dialogs.remove(dlg)
            if dlg in self._dup_dialogs else None)
        dlg.show()
        dlg.raise_()

    def _has_lister(self) -> bool:
        """True if we have a usable Quopus main window. The
        copy-to-lister actions need the lister API to navigate
        and refresh; without a main window (e.g. tests) we hide
        those menu items."""
        return (self.main_window is not None
                and hasattr(self.main_window, "_active_lister"))

    def _lister_label(self, lister) -> str:
        """Friendly label for a lister, e.g. 'right Lister
        (C:/Downloads)'. Used in menu item text so the user
        knows where things will land."""
        side = (
            "right" if lister is self.main_window.right_lister
            else "left")
        cur = getattr(lister, "current_path", None)
        if cur is None:
            return f"{side} Lister"
        s = str(cur)
        if len(s) > 40:
            s = "..." + s[-37:]
        return f"{side} Lister ({s})"

    def _add_lister_actions(self, menu, source_path,
                            label: str):
        """Add the standard set of lister actions to a menu:
          - Copy to inactive lister
          - Reveal in active lister
        Both grey out if there's no main window."""
        if not self._has_lister():
            a = menu.addAction(f"(no main window)")
            a.setEnabled(False)
            return
        _, inactive = self.main_window._active_lister()
        dest_label = self._lister_label(inactive)
        a_copy = menu.addAction(f"Copy {label} to {dest_label}")
        a_copy.triggered.connect(
            lambda: self._copy_path_to_inactive(source_path))
        a_reveal = menu.addAction(
            f"Reveal {label} in active Lister")
        a_reveal.triggered.connect(
            lambda: self._reveal_in_active_lister(source_path))

    def _copy_to_clipboard(self, text: str):
        QApplication.clipboard().setText(text)
        self.lbl_status.setText(f"Copied: {text[:80]}")

    def _reveal_in_active_lister(self, path):
        """Navigate the active lister to the file's containing
        folder and select the file itself."""
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.exists():
            QMessageBox.warning(
                self, "Quopus Database",
                f"File not found:\n{p}\n\n"
                f"It may have been moved or deleted since the "
                f"last scan. Re-scan to refresh the database.")
            return
        target_dir = p.parent if p.is_file() else p
        active, _ = self.main_window._active_lister()
        active.goto(str(target_dir))
        # Try to select the file in the new directory listing.
        # The lister refreshes asynchronously, so we use a
        # short timer to give it a tick to repopulate before
        # we look for the row.
        if p.is_file():
            from PyQt6.QtCore import QTimer as _QT
            _QT.singleShot(50, lambda: self._select_in_lister(
                active, p.name))
        self.lbl_status.setText(
            f"Revealed in active Lister: {p.name}")

    def _select_in_lister(self, lister, name: str):
        """Best-effort: find the row matching `name` in the
        lister and select it. The lister might have already
        moved on or its model might not expose row-by-name
        lookup; if the call fails we just silently give up."""
        try:
            # FileLister exposes select_name() in some versions;
            # check before calling.
            if hasattr(lister, "select_name"):
                lister.select_name(name)
        except Exception:
            pass

    def _copy_path_to_inactive(self, source_path):
        """Copy a file (or directory) to the inactive lister's
        current path. Uses Python's shutil.copy2 to preserve
        timestamps. Runs synchronously - for huge files this
        will block briefly, but interactive copy of a single
        D64/ZIP is fast enough that a background thread isn't
        worth the complexity.

        After the copy succeeds we refresh the destination
        lister so the new file shows up immediately."""
        import shutil
        from pathlib import Path as _Path
        source_path = _Path(source_path)
        if not source_path.exists():
            QMessageBox.warning(
                self, "Quopus Database",
                f"Source file not found:\n{source_path}\n\n"
                f"It may have been moved or deleted since the "
                f"last scan. Re-scan to refresh the database.")
            return
        _, inactive = self.main_window._active_lister()
        dest_dir = getattr(inactive, "current_path", None)
        if not dest_dir:
            QMessageBox.warning(
                self, "Quopus Database",
                "Inactive Lister has no local path "
                "(remote/special filesystem?).")
            return
        dest_dir = _Path(dest_dir)
        if not dest_dir.is_dir():
            QMessageBox.warning(
                self, "Quopus Database",
                f"Destination is not a directory:\n{dest_dir}")
            return
        dest_path = dest_dir / source_path.name
        # Prevent silent overwrite - prompt if the target exists
        if dest_path.exists():
            if QMessageBox.question(
                    self, "File exists",
                    f"{dest_path.name} already exists in\n"
                    f"{dest_dir}\n\n"
                    f"Overwrite?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            shutil.copy2(source_path, dest_path)
        except OSError as e:
            QMessageBox.critical(
                self, "Copy failed", f"{e}")
            return
        # Refresh so the file appears in the lister's view
        try:
            inactive.refresh()
        except Exception:
            pass
        self.lbl_status.setText(
            f"Copied to {dest_dir.name}: {source_path.name}")

    def _extract_disk_entry(self, entry_data):
        """Extract a single PRG/SEQ/USR/REL from a disk image
        and write it as a host file in the inactive lister.

        entry_data has the keys we stashed in _on_disk_selected:
        name, file_type, track, sector, disk_path, image_type.
        We re-open the disk via CbmDiskReader, find the matching
        directory entry, and use its extract() method.

        This uses the SAME code path as the existing D64 editor's
        "save file" action, so output bytes are guaranteed to
        match what the disk editor would produce."""
        from pathlib import Path as _Path
        from .cbmfiles import CbmDiskReader
        disk_path = _Path(entry_data["disk_path"])
        if not disk_path.is_file():
            QMessageBox.warning(
                self, "Quopus Database",
                f"Disk image not found:\n{disk_path}\n\n"
                f"The .d64 may have been moved or deleted since "
                f"the last scan.")
            return
        _, inactive = self.main_window._active_lister()
        dest_dir = getattr(inactive, "current_path", None)
        if not dest_dir or not _Path(dest_dir).is_dir():
            QMessageBox.warning(
                self, "Quopus Database",
                "Inactive Lister has no usable local path.")
            return

        # Build the output filename. Track/sector in DB are 1-based
        # CBM disk addresses; the reader matches them up with the
        # dir entries it discovers itself.
        base = entry_data["name"] or "extracted"
        ext = (entry_data.get("file_type") or "prg").lower()
        out_name = f"{base}.{ext}"
        out_path = _Path(dest_dir) / out_name
        if out_path.exists():
            if QMessageBox.question(
                    self, "File exists",
                    f"{out_name} already exists in\n{dest_dir}\n\n"
                    f"Overwrite?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            reader = CbmDiskReader(str(disk_path))
            reader.open()
            # Find the entry by track+sector (more reliable than
            # name, since two entries can have the same name and
            # the DB stores track/sector explicitly)
            target_track = entry_data.get("track")
            target_sector = entry_data.get("sector")
            target_name = entry_data["name"]
            target_entry = None
            for e in reader.entries:
                # Prefer track/sector match if we have both
                if (target_track is not None
                        and target_sector is not None
                        and e.start_track == target_track
                        and e.start_sector == target_sector):
                    target_entry = e
                    break
            # Fallback: match by decoded name
            if target_entry is None:
                from .cbmfiles import _petscii_filename_to_ascii
                for e in reader.entries:
                    if _petscii_filename_to_ascii(
                            e.name_petscii) == target_name:
                        target_entry = e
                        break
            if target_entry is None:
                QMessageBox.warning(
                    self, "Extract failed",
                    f"Couldn't locate {target_name!r} in "
                    f"{disk_path.name} - the disk's directory "
                    f"may have changed since indexing.")
                return
            data = reader.extract(target_entry)
            out_path.write_bytes(data)
        except Exception as e:
            QMessageBox.critical(
                self, "Extract failed", f"{e}")
            return
        # Refresh destination lister so the file appears
        try:
            inactive.refresh()
        except Exception:
            pass
        self.lbl_status.setText(
            f"Extracted to {dest_dir}: {out_name}")

    def _extract_disk_entry_by_search(self, search_data):
        """Same as _extract_disk_entry but takes the dict that
        comes out of database.search_filenames() instead of
        the richer dict we build in _on_disk_selected.

        Looks up the disk-entry row by its ref_id (the
        disk_entries.id stored as part of the search result),
        which is O(1) and works for any path - including disks
        nested inside archives, where the synthetic display
        path can't be parsed reliably.
        """
        entry_id = search_data.get("ref_id")
        if entry_id is None:
            # Old-style result without ref_id - fall back to the
            # name+path lookup (still broken on Windows, but at
            # least won't crash).
            QMessageBox.warning(
                self, "Quopus Database",
                "Search result is missing the entry ID. "
                "Try clearing the search and re-running it.")
            return
        with database.connection() as conn:
            row = conn.execute("""
                SELECT e.name, e.file_type, e.track, e.sector,
                       d.image_type, d.disk_name, f.path
                FROM disk_entries e
                JOIN disk_images d ON d.id = e.disk_image_id
                JOIN files f ON f.id = d.file_id
                WHERE e.id = ?
            """, (entry_id,)).fetchone()
        if not row:
            QMessageBox.warning(
                self, "Quopus Database",
                f"Disk entry id={entry_id} no longer exists.\n\n"
                "It was probably removed by a database "
                "cleanup, re-scan, or container update. "
                "Re-run the search to pick up the latest data.")
            return
        # Reuse the regular extractor
        self._extract_disk_entry({
            "name": row["name"],
            "file_type": row["file_type"],
            "track": row["track"],
            "sector": row["sector"],
            "disk_path": row["path"],
            "image_type": row["image_type"],
            "disk_name": row["disk_name"],
        })

    def _launch_c64_emulator(self, file_path,
                              return_process: bool = False):
        """Common helper to launch the configured C64 emulator
        with the given file as argument. Reads the emulator
        path and args template from main_window.config keys
        c64_emulator and c64_emulator_args.

        The args template supports tokens {file}, {name}, {dir}.
        Quoted tokens like "{file}" survive shlex splitting so
        paths with spaces work without the user having to escape
        anything.

        If no emulator is configured, offers to open the config
        dialog.

        Returns True on launch success, False if the user bailed
        or it failed. If return_process=True, returns the
        subprocess.Popen object on success instead - the caller
        can then .wait() on it to chain a follow-up launch (used
        by the bulk "Run all in emulator sequentially" action).
        """
        from pathlib import Path as _Path
        main = self.main_window
        if not main or not hasattr(main, "config"):
            QMessageBox.warning(
                self, "Run in emulator",
                "Main window not available - cannot launch.")
            return None if return_process else False
        emu_path = main.config.get("c64_emulator", "").strip()
        emu_args = main.config.get(
            "c64_emulator_args", "{file}").strip()

        if not emu_path or not _Path(emu_path).is_file():
            ans = QMessageBox.question(
                self, "C64 Emulator",
                "No C64 emulator configured yet.\n\n"
                "Open the configuration dialog now?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return None if return_process else False
            try:
                from .actions import ActionDispatcher
                disp = ActionDispatcher(main)
                disp.dispatch("c64_emu_config", None, None, "")
            except Exception as e:
                QMessageBox.warning(
                    self, "Config",
                    f"Could not open emulator config: {e}")
                return None if return_process else False
            emu_path = main.config.get("c64_emulator", "").strip()
            emu_args = main.config.get(
                "c64_emulator_args", "{file}").strip()
            if not emu_path or not _Path(emu_path).is_file():
                return None if return_process else False

        import shlex
        try:
            template_args = shlex.split(emu_args, posix=False)
        except Exception:
            template_args = emu_args.split()

        def _expand(s):
            return (s.replace("{file}", str(file_path))
                     .replace("{name}", _Path(file_path).name)
                     .replace("{dir}",  str(_Path(file_path).parent)))

        arg_list = [_expand(a) for a in template_args]
        cleaned = []
        for a in arg_list:
            if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
                a = a[1:-1]
            cleaned.append(a)
        arg_list = cleaned
        full_cmd = [emu_path] + arg_list

        import subprocess, sys as _sys
        try:
            if _sys.platform == "win32":
                # For sequential-launch we DON'T want DETACHED_PROCESS
                # because that breaks .wait() - we'd lose the child
                # handle. CREATE_NEW_PROCESS_GROUP alone keeps the
                # emulator independent of Quopus's Ctrl+C handling
                # while still letting us join on exit.
                if return_process:
                    proc = subprocess.Popen(
                        full_cmd,
                        creationflags=(
                            subprocess.CREATE_NEW_PROCESS_GROUP))
                else:
                    # Fire-and-forget mode: fully detach so the
                    # emulator survives Quopus closing.
                    proc = subprocess.Popen(
                        full_cmd,
                        creationflags=(
                            subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.DETACHED_PROCESS))
            else:
                proc = subprocess.Popen(
                    full_cmd,
                    start_new_session=True)
            self.lbl_status.setText(
                f"Launched emulator: "
                f"{_Path(file_path).name}")
            return proc if return_process else True
        except Exception as e:
            QMessageBox.warning(
                self, "Run in emulator",
                f"Could not launch emulator:\n\n"
                f"command: {emu_path}\n"
                f"args: {arg_list}\n\n"
                f"error: {e}")
            return None if return_process else False

    def _run_disk_entry_in_emulator(self, search_data):
        """Extract a disk entry to a temp dir, then launch it
        in the configured C64 emulator.

        Used by the Files-tab context-menu 'Run in emulator'
        action on disk-entry rows. The extracted file lives
        in a per-session temp dir under /tmp (or %TEMP% on
        Windows); Quopus doesn't clean it up - the OS does on
        reboot, and an emulator that's still holding it open
        won't be happy about file deletion mid-run anyway.
        """
        entry_id = search_data.get("ref_id")
        if entry_id is None:
            QMessageBox.warning(
                self, "Run in emulator",
                "Search result is missing the entry ID. "
                "Re-run the search to refresh.")
            return
        # Look up full entry info (need track/sector for
        # extraction, plus the disk's host path and its file_id
        # so the resolver can walk the container chain via DB).
        with database.connection() as conn:
            row = conn.execute("""
                SELECT e.name, e.file_type, e.track, e.sector,
                       d.image_type, d.disk_name,
                       f.id as disk_file_id, f.path
                FROM disk_entries e
                JOIN disk_images d ON d.id = e.disk_image_id
                JOIN files f ON f.id = d.file_id
                WHERE e.id = ?
            """, (entry_id,)).fetchone()
        if not row:
            QMessageBox.warning(
                self, "Run in emulator",
                f"Disk entry id={entry_id} no longer exists.")
            return

        from pathlib import Path as _Path
        import tempfile
        from .cbmfiles import (CbmDiskReader,
                                _petscii_filename_to_ascii)
        # Resolve the disk path - this handles nested archives
        # by extracting the D64 from its container to a temp file
        # first. For plain paths it just checks the file exists.
        disk_path_str = row["path"]
        resolved_disk = self._resolve_to_real_file(
            disk_path_str, ref_id=row["disk_file_id"])
        if resolved_disk is None:
            # Resolver already showed the warning
            return
        disk_path = resolved_disk

        # Use the same extraction path as _extract_disk_entry,
        # writing to a temp location instead of the inactive
        # lister. This guarantees byte-identical results.
        try:
            reader = CbmDiskReader(str(disk_path))
            reader.open()
            target_track = row["track"]
            target_sector = row["sector"]
            target_name = row["name"]
            target_entry = None
            # Prefer track/sector match (reliable even if two
            # files have the same name)
            if target_track is not None and target_sector is not None:
                for e in reader.entries:
                    if (e.start_track == target_track
                            and e.start_sector == target_sector):
                        target_entry = e
                        break
            # Fallback: match by decoded name
            if target_entry is None:
                for e in reader.entries:
                    if _petscii_filename_to_ascii(
                            e.name_petscii) == target_name:
                        target_entry = e
                        break
            if target_entry is None:
                QMessageBox.warning(
                    self, "Run in emulator",
                    f"Couldn't locate '{target_name}' in "
                    f"{disk_path.name}. The image may have "
                    f"changed since the last scan.")
                return
            data_bytes = reader.extract(target_entry)
        except Exception as e:
            QMessageBox.warning(
                self, "Run in emulator",
                f"Extraction failed:\n\n{e}")
            return

        # Write to temp dir with a sanitized filename. PETSCII
        # filenames can contain chars that aren't valid Windows
        # filenames, so strip them out.
        tmp_dir = _Path(tempfile.gettempdir()) / "quopus_run"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(
            c if c.isalnum() or c in "._- " else "_"
            for c in target_name).strip() or "entry"
        ext = (row["file_type"] or "prg").lower()
        out_path = tmp_dir / f"{safe_name}.{ext}"
        try:
            out_path.write_bytes(data_bytes)
        except Exception as e:
            QMessageBox.warning(
                self, "Run in emulator",
                f"Couldn't write temp file:\n{out_path}\n\n{e}")
            return

        # Launch
        self._launch_c64_emulator(out_path)

    def _launch_resolved(self, path_str, ref_id=None):
        """Resolve a possibly-virtual path to a real on-disk
        file (extracting from archive containers if needed),
        then launch it in the configured C64 emulator. Used by
        the Files-tab 'Run in emulator' action so paths like
        'foo.zip!bar.prg' work without the user having to
        extract the archive first.

        ref_id, if given, lets the resolver walk the container
        chain via the DB instead of parsing the synthetic '!'
        path - more robust when filenames contain '!'.
        """
        p = self._resolve_to_real_file(path_str, ref_id=ref_id)
        if p is not None:
            self._launch_c64_emulator(p)

    def _copy_resolved_to_inactive(self, path_str, ref_id=None):
        """Extract a virtual path's content (e.g. a D64 inside
        a ZIP) to a temp file, then copy that temp file to the
        inactive lister. Used by the disk-entry context menu's
        'Copy whole disk' action when the disk image is itself
        nested inside an archive."""
        p = self._resolve_to_real_file(path_str, ref_id=ref_id)
        if p is not None:
            self._copy_path_to_inactive(p)

    def _run_disk_in_emulator(self, disk_path, ref_id=None):
        """Launch the configured C64 emulator with the whole
        disk image mounted. The emulator typically auto-runs
        LOAD\"*\",8,1 to start the first program on the disk.

        Handles virtual paths like 'foo.zip!disk.d64' by
        extracting the disk from the container first. The
        optional ref_id (the files.id of the disk in the DB)
        lets the resolver use the container-chain walk instead
        of '!' splitting, which is robust against names that
        contain '!'.
        """
        from pathlib import Path as _Path
        resolved = self._resolve_to_real_file(str(disk_path),
                                              ref_id=ref_id)
        if resolved is None:
            # Resolver already showed a warning, just bail
            return
        self._launch_c64_emulator(resolved)

    def _resolve_to_real_file(self, path_str: str, quiet: bool = False,
                              ref_id=None):
        """Return a real, on-disk Path that VICE can open.

        For plain paths this is a no-op - we just check the
        file exists. For virtual archive-member paths (those
        containing '!' separators like
        'C:\\foo\\Music_Collection.zip!Music_Collection.d64'
        or 'foo.zip!sub.zip!disk.d64') we extract the inner
        file to a temp dir and return that path. VICE then sees
        a regular .d64 / .prg / etc file.

        Returns None on failure. By default shows a warning
        dialog for the user; pass quiet=True to suppress the
        dialog (caller will surface the error in some other way).
        The quiet path is used by the sequential runner that
        pre-flights many items at once - we don't want 20 popups
        for an unplugged drive.

        If ref_id is given, we look up the container hierarchy
        via the database (`files.container_id`) instead of
        parsing the synthetic '!' display path. This is more
        reliable when filenames themselves contain '!', which
        happens often in scene packs (`!Read.me`,
        `Group!Title.d64` etc) - naive splitting on '!' would
        produce garbage chain entries in that case.

        Extracted temps live in ${TEMP}/quopus_run/<hash>/ keyed
        on the full virtual path so re-launches reuse the same
        extract. Not deleted by us - the OS cleans /tmp on
        reboot, and the extracted file might still be open in
        VICE.
        """
        from pathlib import Path as _Path
        import tempfile

        def _warn(msg):
            if not quiet:
                QMessageBox.warning(
                    self, "Run in emulator", msg)

        # Fast path: plain on-disk file
        if "!" not in path_str:
            p = _Path(path_str)
            if p.is_file():
                return p
            _warn(f"File not accessible:\n{p}\n\n"
                  "Plug in the drive or rescan.")
            return None

        # Nested path. If we got a ref_id (the files.id), use
        # it to walk the container chain via the DB - that's
        # robust against filenames containing '!'. Otherwise
        # we have to fall back to splitting the display path
        # on '!' and hope no filename contains one.
        if ref_id is not None:
            try:
                chain = database.get_file_container_chain(ref_id)
            except Exception:
                chain = None
            if chain:
                # chain[0] is the outermost real file, chain[-1]
                # is the file we want. Build parts as the
                # member-name-only list from chain[1:] onward,
                # with the outer real path first.
                parts = [chain[0]["path"]]
                for entry in chain[1:]:
                    parts.append(entry["name"])
                return self._do_extract_chain(parts, path_str,
                                              quiet)

        # Legacy/fallback: split on '!'. Works for paths where
        # no filename contains '!'.
        parts = path_str.split("!")
        return self._do_extract_chain(parts, path_str, quiet)

    def _do_extract_chain(self, parts, path_str_for_hash,
                          quiet=False):
        """Actually do the extraction given a resolved chain
        of [outer_path, member1, member2, ...]. Split out
        from _resolve_to_real_file so both the DB-based and
        the legacy split-based paths reach it.
        """
        from pathlib import Path as _Path
        import tempfile

        def _warn(msg):
            if not quiet:
                QMessageBox.warning(
                    self, "Run in emulator", msg)

        if len(parts) < 2:
            _warn(f"Path doesn't look nested: {path_str_for_hash}")
            return None

        real_root = _Path(parts[0])
        if not real_root.is_file():
            _warn(f"Outer container not accessible:\n{real_root}"
                  f"\n\nPlug in the drive or rescan.")
            return None

        import hashlib
        chain_hash = hashlib.md5(
            path_str_for_hash.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        tmp_root = (_Path(tempfile.gettempdir())
                    / "quopus_run" / chain_hash)
        # Final filename: last member's name, sanitised for the
        # host filesystem (PETSCII / Amiga names may have chars
        # Windows hates).
        raw_final = parts[-1]
        final_name = "".join(
            c if c.isalnum() or c in "._- " else "_"
            for c in raw_final).strip() or "extracted"
        # If sanitization stripped the extension preserve it
        # explicitly so VICE picks the right autostart logic
        original_ext = _Path(raw_final).suffix
        if original_ext and not final_name.endswith(original_ext):
            sanitised_ext = "".join(
                c if c.isalnum() or c == "." else ""
                for c in original_ext)
            if sanitised_ext:
                final_name = (_Path(final_name).stem
                              + sanitised_ext)
        out_path = tmp_root / final_name
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        tmp_root.mkdir(parents=True, exist_ok=True)
        try:
            data = self._extract_nested_member(parts)
        except Exception as e:
            _warn(f"Couldn't extract nested file:\n\n"
                  f"{path_str_for_hash}\n\nError: {e}")
            return None
        if data is None:
            _warn(f"Inner member not found in container:\n\n"
                  f"{path_str_for_hash}")
            return None
        try:
            out_path.write_bytes(data)
        except Exception as e:
            _warn(f"Couldn't write temp file:\n{out_path}\n\n{e}")
            return None
        self.lbl_status.setText(
            f"Extracted {final_name} ({len(data):,} bytes) "
            f"for emulator launch")
        return out_path

    def _extract_nested_member(self, parts: list):
        """Walk a parts list like
        ['/foo/outer.zip', 'inner.lha', 'disk.d64']
        and return the bytes of the innermost member.

        Supports the same container formats the scanner does:
        .zip (built-in), .lha/.lzh (lhafile), .lzx (unlzx),
        .dms (xdms). Other formats raise NotImplementedError.

        Returns None if the named member doesn't exist in its
        container.
        """
        from pathlib import Path as _Path
        # Level 0 = the actual file on disk. Read its contents
        # by treating it as a container and pulling out parts[1].
        # If there are more parts beyond that, repeat with the
        # extracted bytes acting as the next container.
        current_data = None
        current_path = _Path(parts[0])  # only used for ext sniff
        for i, member_name in enumerate(parts[1:], start=1):
            # Decide which container reader to use based on the
            # CURRENT container's extension. After the first
            # iteration current_path was updated to the member
            # name from the previous step (so ext sniffing works
            # for nested containers).
            ext = current_path.suffix.lower()

            if ext == ".zip":
                # Read either from disk (level 0) or from the
                # bytes we already extracted (level > 0).
                import io, zipfile
                if current_data is None:
                    zf = zipfile.ZipFile(str(current_path), "r")
                else:
                    zf = zipfile.ZipFile(io.BytesIO(current_data), "r")
                try:
                    # Match by name. ZIP entries may use / as
                    # separator regardless of platform.
                    member_lookup = member_name.replace("\\", "/")
                    try:
                        current_data = zf.read(member_lookup)
                    except KeyError:
                        # Some ZIP scanners record names slightly
                        # differently - try without leading
                        # separators, fall back to scan.
                        found = None
                        for n in zf.namelist():
                            if (n == member_lookup
                                    or n.endswith("/" + member_lookup)
                                    or _Path(n).name == member_lookup):
                                found = n
                                break
                        if found is None:
                            return None
                        current_data = zf.read(found)
                finally:
                    zf.close()
            elif ext in (".lha", ".lzh"):
                try:
                    import lhafile
                except ImportError:
                    raise NotImplementedError(
                        "lhafile module not installed - "
                        "can't extract from .lha containers")
                # lhafile only takes a real path. If we're at
                # a nested level, write the bytes to a temp file
                # first.
                src_path = current_path
                temp_for_lha = None
                try:
                    if current_data is not None:
                        import tempfile
                        f = tempfile.NamedTemporaryFile(
                            suffix=".lha", delete=False)
                        f.write(current_data)
                        f.close()
                        temp_for_lha = _Path(f.name)
                        src_path = temp_for_lha
                    lh = lhafile.LhaFile(str(src_path))
                    member_lookup = member_name.replace("\\", "/")
                    try:
                        current_data = lh.read(member_lookup)
                    except KeyError:
                        found = None
                        for info in lh.infolist():
                            if (info.filename == member_lookup
                                    or _Path(info.filename).name
                                        == member_lookup):
                                found = info.filename
                                break
                        if found is None:
                            return None
                        current_data = lh.read(found)
                finally:
                    if temp_for_lha is not None:
                        try:
                            temp_for_lha.unlink()
                        except Exception:
                            pass
            elif ext == ".lzx":
                # LZX support requires the external `unlzx` tool
                # and we'd have to set up a temp dir, run it,
                # collect output etc. Rare enough as a nested
                # container that we punt - users can manually
                # extract these to disk and re-scan.
                raise NotImplementedError(
                    "Nested .lzx extraction is not supported. "
                    "Extract the archive manually and re-scan.")
            elif ext == ".dms":
                # DMS converts to ADF. For Quopus we typically
                # treat the ADF as the extractable result, not
                # individual files. Just convert and return.
                # But this only makes sense if member is the
                # ADF name itself.
                raise NotImplementedError(
                    ".dms nested extraction is not supported - "
                    "manually convert with xdms first")
            else:
                raise NotImplementedError(
                    f"Don't know how to extract members from "
                    f"a .{ext} container ({current_path.name})")

            # After this level we treat the member's extension
            # as the new container type for the next iteration
            current_path = _Path(member_name)

        return current_data

    # --------------------------------------------------------
    # Watch tab - live FS monitoring
    # --------------------------------------------------------

    def _build_watch_tab(self):
        from . import db_watcher
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)

        # Status banner at the top - shows whether watching is
        # currently active and which mechanism is in use
        self.lbl_watch_status = QLabel()
        self.lbl_watch_status.setFont(QFont("Courier", 10))
        self.lbl_watch_status.setWordWrap(True)
        v.addWidget(self.lbl_watch_status)

        # Folder list
        v.addWidget(QLabel("Watched folders:"))
        self.tree_watch = QTreeWidget()
        self.tree_watch.setHeaderLabels([
            "Folder", "Status"])
        self.tree_watch.setColumnWidth(0, 600)
        self.tree_watch.setRootIsDecorated(False)
        install_table_state(self.tree_watch,
                            "database_browser:watch")
        v.addWidget(self.tree_watch, 1)

        # Buttons
        row = QHBoxLayout()
        self.btn_watch_add = QPushButton("Add Folder...")
        self.btn_watch_add.clicked.connect(self._on_watch_add)
        row.addWidget(self.btn_watch_add)

        self.btn_watch_remove = QPushButton("Remove Selected")
        self.btn_watch_remove.clicked.connect(self._on_watch_remove)
        row.addWidget(self.btn_watch_remove)

        row.addSpacing(20)

        self.btn_watch_start = QPushButton("Start Watcher")
        self.btn_watch_start.clicked.connect(self._on_watch_start)
        row.addWidget(self.btn_watch_start)

        self.btn_watch_stop = QPushButton("Stop Watcher")
        self.btn_watch_stop.clicked.connect(self._on_watch_stop)
        row.addWidget(self.btn_watch_stop)

        row.addStretch(1)
        v.addLayout(row)

        # Help text below the buttons
        help_text = QLabel(
            "<i>When the watcher is running, files added or changed "
            "in any watched folder are automatically indexed - no "
            "need to re-scan manually. Quopus remembers your watched "
            "folders across restarts.</i>")
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #666;")
        v.addWidget(help_text)

        self.tabs.addTab(w, "Watch")
        # Periodic refresh of the status display while the tab
        # is open. Cheap - just toggles button enable states.
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(2000)
        self._watch_timer.timeout.connect(self._refresh_watch_status)
        self._watch_timer.start()
        self._refresh_watch_status()

    def _refresh_watch_status(self):
        # Defensive: this slot is called by self._watch_timer
        # every 2 seconds. If the dialog is closing the timer
        # is supposed to be stopped first - but a tick can
        # already be in-flight when stop() is called, racing
        # against deletion. Catch the "wrapped C/C++ object
        # has been deleted" RuntimeError that surfaces when
        # we touch a destroyed widget after that, and bail.
        try:
            self._refresh_watch_status_impl()
        except RuntimeError as e:
            # Widget tree already torn down. Stop our timer to
            # prevent more wasted ticks. We don't crash.
            if "deleted" in str(e):
                try:
                    self._watch_timer.stop()
                except Exception:
                    pass
                return
            raise

    def _refresh_watch_status_impl(self):
        from . import db_watcher
        from . import ingest_queue

        # Header status
        running = db_watcher.is_running()
        native = db_watcher.has_native_support()
        if running and native:
            status = ("<b>Status:</b> "
                      "<span style='color:#080'>"
                      "Watching (native FS notifications)</span>")
        elif running and not native:
            status = ("<b>Status:</b> "
                      "<span style='color:#a80'>"
                      "Watching (polling every 60s - install "
                      "'watchdog' for live notifications)</span>")
        else:
            status = ("<b>Status:</b> "
                      "<span style='color:#888'>Not watching</span>")
        if not native:
            status += ("<br><i>For live notifications:</i> "
                       "<code>pip install watchdog</code>")

        # Ingest queue status - useful so the user can see when
        # files dropped in are actually being processed. The
        # queue might be busy from a parallel bulk scan too.
        q = ingest_queue.get_queue()
        qs = q.stats()
        in_flight = qs["in_flight"]
        pending = qs["queued"] - qs["completed"] - qs["errored"]
        pending = max(0, pending - in_flight)
        if in_flight or pending:
            status += (
                f"<br><b>Ingest queue:</b> "
                f"{in_flight} processing, {pending} queued, "
                f"{qs['completed']} done")
            if qs["errored"]:
                status += f" ({qs['errored']} errors)"
        elif qs["completed"]:
            status += (
                f"<br><b>Ingest queue:</b> idle "
                f"({qs['completed']} files processed since launch)")
        self.lbl_watch_status.setText(status)

        # Folder list
        self.tree_watch.clear()
        folders = db_watcher.list_watched_folders()
        for folder in folders:
            it = QTreeWidgetItem()
            it.setText(0, folder)
            from pathlib import Path
            if not Path(folder).is_dir():
                it.setText(1, "MISSING")
                it.setForeground(1, Qt.GlobalColor.red)
            else:
                it.setText(1, "watching" if running else "added")
            self.tree_watch.addTopLevelItem(it)

        # Button state
        readonly = database.is_readonly()
        self.btn_watch_start.setEnabled(
            not running and bool(folders) and not readonly)
        self.btn_watch_stop.setEnabled(running)
        self.btn_watch_add.setEnabled(not readonly)
        self.btn_watch_remove.setEnabled(
            bool(self.tree_watch.selectedItems()) and not readonly)

        # If we're viewing a read-only shared DB, tell the user
        # why these buttons are greyed out.
        if readonly:
            self.lbl_watch_status.setText(
                self.lbl_watch_status.text()
                + "<br><i>Watching disabled - you're viewing a "
                "read-only shared database. Switch to your own "
                "DB to start watching.</i>")

    def _on_watch_add(self):
        from . import db_watcher
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to watch", str(Path.home()))
        if not folder:
            return
        added = db_watcher.add_watched_folder(folder)
        if added:
            self.lbl_status.setText(f"Watching: {folder}")
            # Offer to do the initial full scan now - the watcher
            # only picks up CHANGES after this point, so without
            # a baseline scan we miss everything that's already
            # there. Ask the user politely rather than just
            # blasting through GB of archives unprompted.
            if QMessageBox.question(
                    self, "Initial Scan?",
                    f"Run an initial full scan of\n{folder}\n"
                    f"now so the DB knows what's already there?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
            ) == QMessageBox.StandardButton.Yes:
                # Reuse the scan worker
                self.scan_worker = _ScanWorker(
                    Path(folder), incremental=True)
                self.scan_worker.progress.connect(
                    self._on_scan_progress)
                self.scan_worker.finished_with.connect(
                    self._on_scan_done)
                self.scan_worker.start()
                self.btn_scan.setEnabled(False)
                self.btn_cancel.setEnabled(True)
        else:
            self.lbl_status.setText("Already watching this folder")
        self._refresh_watch_status()

    def _on_watch_remove(self):
        from . import db_watcher
        items = self.tree_watch.selectedItems()
        if not items:
            return
        folder = items[0].text(0)
        if QMessageBox.question(
                self, "Stop Watching",
                f"Stop watching {folder}?\n\n"
                f"(Already-indexed files stay in the database.)",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        db_watcher.remove_watched_folder(folder)
        self._refresh_watch_status()

    def _on_watch_start(self):
        from . import db_watcher
        if db_watcher.start_watcher():
            self.lbl_status.setText("Watcher started")
        else:
            self.lbl_status.setText("Watcher was already running")
        self._refresh_watch_status()

    def _on_watch_stop(self):
        from . import db_watcher
        if db_watcher.stop_watcher():
            self.lbl_status.setText("Watcher stopped")
        else:
            self.lbl_status.setText("Watcher was not running")
        self._refresh_watch_status()

    # --------------------------------------------------------
    # Folders tab - persistent list of folders that have been
    # scanned into the DB. Lets the user add a new folder,
    # re-scan an existing one (incremental, picks up new
    # files), or re-scan all at once.
    #
    # Distinct from the Watch tab: Watch is for live FS
    # notifications (catches changes as they happen). Folders
    # is for one-shot bulk scans where you don't want a watcher
    # running permanently - e.g. a folder on a removable drive
    # you only plug in occasionally.
    #
    # Persistence: config/scanned_folders.json. Each entry has:
    #   path: absolute folder path
    #   last_scan_id: id of the most recent scan run on it
    #   last_scan_time: epoch timestamp of last scan
    #   last_file_count: how many files the last scan indexed
    # --------------------------------------------------------

    def _folders_config_file(self):
        """Where the persistent scanned-folders list lives."""
        from .config import CONFIG_DIR
        return CONFIG_DIR / "scanned_folders.json"

    def _load_scanned_folders(self) -> list[dict]:
        """Read the saved folder list. Returns empty list if
        no file or corrupt JSON."""
        import json
        try:
            p = self._folders_config_file()
            if not p.is_file():
                return []
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_scanned_folders(self, folders: list[dict]):
        """Persist the folder list to disk."""
        import json
        try:
            from .config import CONFIG_DIR
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._folders_config_file().write_text(
                json.dumps(folders, indent=2))
        except Exception as e:
            self.lbl_status.setText(
                f"Couldn't save folders list: {e}")

    def _add_folder_to_list(self, folder: str,
                             scan_id: int = None,
                             file_count: int = 0):
        """Add (or update) a folder in the scanned-folders list.
        Idempotent - if the folder is already in the list, just
        updates the last_scan fields."""
        import time
        folder = str(Path(folder).expanduser().resolve())
        folders = self._load_scanned_folders()
        # Find existing entry by path (case-insensitive on
        # Windows - safer match)
        existing = None
        for entry in folders:
            if entry.get("path", "").lower() == folder.lower():
                existing = entry
                break
        if existing is not None:
            existing["last_scan_id"] = scan_id
            existing["last_scan_time"] = time.time()
            existing["last_file_count"] = file_count
        else:
            folders.append({
                "path": folder,
                "last_scan_id": scan_id,
                "last_scan_time": time.time(),
                "last_file_count": file_count,
            })
        self._save_scanned_folders(folders)

    def _build_folders_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)

        info = QLabel(
            "<b>Folders indexed in this database.</b><br>"
            "Each folder you scan is remembered here. You can "
            "re-scan a single folder to pick up new files, "
            "re-scan all of them at once, or remove an entry "
            "from the list (the indexed files stay in the DB - "
            "removing only stops the folder from showing here)."
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        v.addWidget(info)

        self.tree_folders = QTreeWidget()
        self.tree_folders.setHeaderLabels([
            "Folder", "Files indexed", "Last scan",
            "Last scan ID"])
        self.tree_folders.setColumnWidth(0, 450)
        self.tree_folders.setColumnWidth(1, 100)
        self.tree_folders.setColumnWidth(2, 180)
        self.tree_folders.setRootIsDecorated(False)
        self.tree_folders.setSelectionMode(
            QTreeWidget.SelectionMode.ExtendedSelection)
        # Double-click a row to re-scan that single folder
        self.tree_folders.itemDoubleClicked.connect(
            self._on_rescan_selected_folder)
        v.addWidget(self.tree_folders, 1)

        # Action buttons. Add is the primary action; Re-scan
        # variants underneath. Remove is destructive so styled
        # in muted red.
        row = QHBoxLayout()
        self.btn_folder_add = QPushButton("Add Folder...")
        self.btn_folder_add.setToolTip(
            "Pick a folder to scan into the database. "
            "If it's already on the list, runs an "
            "incremental re-scan.")
        self.btn_folder_add.clicked.connect(
            self._on_add_folder_to_db)
        row.addWidget(self.btn_folder_add)

        self.btn_folder_rescan_one = QPushButton(
            "Re-scan Selected (incremental)")
        self.btn_folder_rescan_one.setToolTip(
            "Re-scan the selected folder, skipping files "
            "whose mtime hasn't changed. Picks up new files "
            "without re-hashing the whole collection.")
        self.btn_folder_rescan_one.clicked.connect(
            self._on_rescan_selected_folder)
        row.addWidget(self.btn_folder_rescan_one)

        self.btn_folder_rescan_all = QPushButton(
            "Re-scan All (incremental)")
        self.btn_folder_rescan_all.setToolTip(
            "Walk every folder in the list and incrementally "
            "re-scan. Runs sequentially - if you have many "
            "folders this may take a while.")
        self.btn_folder_rescan_all.clicked.connect(
            self._on_rescan_all_folders)
        row.addWidget(self.btn_folder_rescan_all)

        self.btn_folder_remove = QPushButton("Remove from list")
        self.btn_folder_remove.setToolTip(
            "Remove the selected folder(s) from this list. "
            "The files already indexed STAY in the database - "
            "removing only takes the folder off this view. "
            "Use 'Reset DB' on the Stats tab if you want to "
            "fully wipe.")
        self.btn_folder_remove.setStyleSheet("color: #a00;")
        self.btn_folder_remove.clicked.connect(
            self._on_remove_folder_from_list)
        row.addWidget(self.btn_folder_remove)

        row.addStretch(1)
        v.addLayout(row)

        self.tabs.addTab(w, "Folders")
        self._refresh_folders_list()

    def _refresh_folders_list(self):
        """Reload the folders tree from disk."""
        import time
        self.tree_folders.clear()
        folders = self._load_scanned_folders()
        # Sort by last_scan_time descending so the freshest is
        # at top - most relevant when the list grows long.
        folders.sort(key=lambda e: e.get("last_scan_time") or 0,
                     reverse=True)
        for entry in folders:
            it = QTreeWidgetItem()
            it.setText(0, entry.get("path", "?"))
            it.setText(1, str(entry.get("last_file_count") or 0))
            ts = entry.get("last_scan_time")
            if ts:
                # Friendly format: "2026-05-18 19:42"
                t = time.localtime(ts)
                it.setText(2, time.strftime(
                    "%Y-%m-%d %H:%M", t))
            else:
                it.setText(2, "(never)")
            sid = entry.get("last_scan_id")
            it.setText(3, str(sid) if sid else "")
            it.setData(0, Qt.ItemDataRole.UserRole, entry)
            # Color-code: missing folders (e.g. unplugged USB
            # drive) get a grey tint so they're visually
            # distinct from currently-accessible ones.
            try:
                if not Path(entry["path"]).is_dir():
                    from PyQt6.QtGui import QBrush, QColor
                    grey = QBrush(QColor("#888"))
                    for col in range(4):
                        it.setForeground(col, grey)
                    it.setToolTip(
                        0, "Folder is not currently accessible "
                        "(maybe a removable drive that's unplugged)")
            except Exception:
                pass
            self.tree_folders.addTopLevelItem(it)
        # Status hint at the bottom of the tab so the user
        # knows how big the list is
        n = len(folders)
        if hasattr(self, "btn_folder_rescan_all"):
            self.btn_folder_rescan_all.setEnabled(n > 0)
            self.btn_folder_rescan_all.setText(
                f"Re-scan All ({n}) (incremental)"
                if n else "Re-scan All (incremental)")

    def _on_add_folder_to_db(self):
        """Pick a folder and start scanning it. If it's already
        in our list, an incremental scan picks up just the new
        files."""
        if self.scan_worker is not None:
            QMessageBox.information(
                self, "Scan in progress",
                "Wait for the current scan to finish or "
                "cancel it first.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to add", str(Path.home()))
        if not folder:
            return
        # Always use incremental for the Folders-tab workflow.
        # The "force full re-hash" option stays available via
        # the bottom-toolbar Scan Folder button which has the
        # Yes/No dialog.
        self._launch_scan(folder, incremental=True,
                          on_finish_track_folder=True)

    def _on_rescan_selected_folder(self, *args):
        """Re-scan the currently-selected folder(s). Activated
        by either the button or double-click. If multiple
        folders are selected, runs the first; the user can
        run the others via Re-scan All or one by one."""
        if self.scan_worker is not None:
            QMessageBox.information(
                self, "Scan in progress",
                "Wait for the current scan to finish or "
                "cancel it first.")
            return
        items = self.tree_folders.selectedItems()
        if not items:
            QMessageBox.information(
                self, "No selection",
                "Pick a folder from the list first.")
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return
        folder = entry.get("path")
        if not folder:
            return
        if not Path(folder).is_dir():
            QMessageBox.warning(
                self, "Folder not accessible",
                f"The folder is not currently reachable:\n\n"
                f"{folder}\n\n"
                "If it's a removable drive, plug it in and "
                "try again. Otherwise use Remove from List "
                "to take it off the list.")
            return
        self._launch_scan(folder, incremental=True,
                          on_finish_track_folder=True)

    def _on_rescan_all_folders(self):
        """Sequentially re-scan every folder on the list. We
        chain them: when one finishes, the next starts. This
        keeps memory bounded (only one walker active at a
        time) and lets the user cancel cleanly between
        folders.

        Skips folders that aren't currently accessible (e.g.
        unplugged USB drives) with a warning at the end."""
        if self.scan_worker is not None:
            QMessageBox.information(
                self, "Scan in progress",
                "Wait for the current scan to finish or "
                "cancel it first.")
            return
        folders = self._load_scanned_folders()
        if not folders:
            return
        # Filter to accessible ones; remember skipped for
        # post-run report
        accessible = []
        skipped = []
        for entry in folders:
            p = entry.get("path")
            if p and Path(p).is_dir():
                accessible.append(p)
            else:
                skipped.append(p or "?")
        if not accessible:
            QMessageBox.warning(
                self, "No accessible folders",
                "None of the folders in the list are "
                "currently accessible. Plug in removable "
                "drives or remove entries from the list.")
            return
        # Confirm with the user - this could take hours on
        # a multi-TB collection
        note = ""
        if skipped:
            note = (f"\n\nNOTE: {len(skipped)} folder(s) are "
                    "not currently accessible and will be skipped.")
        if QMessageBox.question(
                self, "Re-scan all",
                f"Re-scan {len(accessible)} folder(s) "
                f"incrementally?\n\n"
                "This will skip files whose mtime hasn't "
                f"changed since their last scan.{note}",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
        ) != QMessageBox.StandardButton.Yes:
            return
        # Set up the chain
        self._rescan_queue = list(accessible)
        self._rescan_skipped = skipped
        self._do_next_rescan()

    def _do_next_rescan(self):
        """Internal: dequeue the next pending re-scan and start
        it. Called recursively-via-signal as each finishes."""
        if not getattr(self, "_rescan_queue", None):
            # All done!
            done_msg = "Re-scan all complete"
            if getattr(self, "_rescan_skipped", None):
                done_msg += (
                    f" ({len(self._rescan_skipped)} "
                    f"inaccessible folder(s) skipped)")
            self.lbl_status.setText(done_msg)
            self._rescan_queue = None
            self._rescan_skipped = None
            return
        folder = self._rescan_queue.pop(0)
        remaining = len(self._rescan_queue)
        self.lbl_status.setText(
            f"Re-scanning {folder} (+ {remaining} more)...")
        self._launch_scan(
            folder, incremental=True,
            on_finish_track_folder=True,
            on_finish_chain_next=True)

    def _on_remove_folder_from_list(self):
        """Take the selected folder(s) off the persisted list.
        The indexed files STAY in the database - this is just
        a UI cleanup. The actual files can be removed via the
        Stats tab's Reset DB (nukes everything) or by manually
        deleting them from disk and running a re-scan."""
        items = self.tree_folders.selectedItems()
        if not items:
            return
        paths_to_remove = []
        for it in items:
            entry = it.data(0, Qt.ItemDataRole.UserRole)
            if entry and entry.get("path"):
                paths_to_remove.append(entry["path"])
        if not paths_to_remove:
            return
        if QMessageBox.question(
                self, "Remove from list",
                f"Remove {len(paths_to_remove)} folder(s) "
                f"from the list?\n\n"
                "The indexed files stay in the database. "
                "Removing only takes the folder off this view.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
        ) != QMessageBox.StandardButton.Yes:
            return
        folders = self._load_scanned_folders()
        remaining = [e for e in folders
                     if e.get("path") not in paths_to_remove]
        self._save_scanned_folders(remaining)
        self._refresh_folders_list()
        self.lbl_status.setText(
            f"Removed {len(paths_to_remove)} folder(s) "
            "from the list")

    def _launch_scan(self, folder: str, incremental: bool = True,
                     on_finish_track_folder: bool = False,
                     on_finish_chain_next: bool = False):
        """Common scan-launching helper. Used by the bottom-
        toolbar Scan Folder button AND by the Folders-tab
        workflows. The on_finish_* flags control what happens
        when the scan completes:

          on_finish_track_folder:
              update the scanned-folders list with the
              just-completed scan's id and file count.

          on_finish_chain_next:
              after the scan finishes, automatically launch
              the next folder in self._rescan_queue. Used by
              Re-scan All to walk through the list one at a
              time.
        """
        # Trial-tier check: warn if the user is at or past the
        # disk-image cap before starting. We only show the
        # dialog once per session - they can dismiss it and
        # keep scanning (loose files still get catalogued, just
        # no new disk content).
        try:
            from . import license as _lic
            if (not _lic.has_feature(_lic.FEATURE_DB_UNLIMITED)
                    and not getattr(
                        self, "_trial_warn_shown", False)):
                cur_disks = database.disk_count()
                cap = _lic.TRIAL_DB_DISK_LIMIT
                if cur_disks >= cap:
                    QMessageBox.information(
                        self, "Trial limit reached",
                        f"You've reached the trial limit of "
                        f"{cap:,} disk images.\n\n"
                        f"The scan will still index loose files "
                        f"(PRGs on disk, archive members), but "
                        f"disk-image contents (D64/D71/D81/...) "
                        f"won't be catalogued beyond this point.\n\n"
                        f"Register Quopus to remove the cap.")
                    self._trial_warn_shown = True
                elif cur_disks >= int(cap * 0.9):
                    # 90%+ - friendly heads-up so the user knows
                    # they're approaching the cap
                    remaining = cap - cur_disks
                    self.lbl_status.setText(
                        f"Heads up: trial has {remaining:,} disk "
                        f"slots free of {cap:,}")
        except Exception:
            pass

        self._pending_track_folder = (
            folder if on_finish_track_folder else None)
        self._pending_chain_next = on_finish_chain_next
        self.scan_worker = _ScanWorker(Path(folder), incremental)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished_with.connect(self._on_scan_done)
        self.scan_worker.start()
        self.btn_scan.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        if hasattr(self, "btn_folder_add"):
            self.btn_folder_add.setEnabled(False)
            self.btn_folder_rescan_one.setEnabled(False)
            self.btn_folder_rescan_all.setEnabled(False)
        self.lbl_status.setText(f"Scanning {folder}...")

    # --------------------------------------------------------
    # Issues tab - files we couldn't index normally
    # --------------------------------------------------------

    def _build_issues_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)

        # Filter row
        row = QHBoxLayout()
        row.addWidget(QLabel("Filter:"))
        from PyQt6.QtWidgets import QComboBox
        self.cmb_issue_filter = QComboBox()
        self.cmb_issue_filter.addItem("All issues", None)
        # Friendly labels for each issue type. The internal keys
        # are the database.ISSUE_* constants.
        self.cmb_issue_filter.addItem(
            "Password-protected archives", "password")
        self.cmb_issue_filter.addItem(
            "Corrupt disk images", "corrupt_disk")
        self.cmb_issue_filter.addItem(
            "Disks without directory (trackloader / NIB)",
            "no_directory")
        self.cmb_issue_filter.addItem(
            "Extract failures", "extract_failed")
        self.cmb_issue_filter.addItem(
            "Unknown formats", "unknown_format")
        self.cmb_issue_filter.currentIndexChanged.connect(
            self._refresh_issues)
        row.addWidget(self.cmb_issue_filter)
        row.addStretch(1)
        self.btn_issues_refresh = QPushButton("Refresh")
        self.btn_issues_refresh.clicked.connect(self._refresh_issues)
        row.addWidget(self.btn_issues_refresh)
        self.btn_issues_clear = QPushButton("Clear All Issues")
        self.btn_issues_clear.setToolTip(
            "Remove the issue log. Useful after you've reviewed "
            "and handled them, or after re-scanning fixed files.")
        self.btn_issues_clear.clicked.connect(self._on_clear_issues)
        row.addWidget(self.btn_issues_clear)
        v.addLayout(row)

        # Issues list
        self.tree_issues = QTreeWidget()
        self.tree_issues.setHeaderLabels([
            "When", "Type", "File", "Detail"])
        self.tree_issues.setColumnWidth(0, 140)
        self.tree_issues.setColumnWidth(1, 140)
        self.tree_issues.setColumnWidth(2, 380)
        self.tree_issues.setColumnWidth(3, 280)
        self.tree_issues.setRootIsDecorated(False)
        self.tree_issues.itemDoubleClicked.connect(
            self._on_issue_double_click)
        install_table_state(self.tree_issues,
                            "database_browser:issues")
        v.addWidget(self.tree_issues, 1)

        # Help text
        help_text = QLabel(
            "<i>Files that couldn't be fully indexed. "
            "Password-protected archives need to be unpacked "
            "manually first. Disks without a directory are "
            "still indexed by MD5 so you can find them, but "
            "their contents aren't searchable. Double-click an "
            "entry to copy its path.</i>")
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #666;")
        v.addWidget(help_text)

        self.tabs.addTab(w, "Issues")
        self._refresh_issues()

    def _refresh_issues(self):
        self.tree_issues.clear()
        try:
            issue_type = self.cmb_issue_filter.currentData()
            issues = database.list_scan_issues(
                scan_id=None,
                issue_type=issue_type,
                limit=5000)
        except Exception as e:
            self.lbl_status.setText(f"Issues load failed: {e}")
            return
        import time as _time
        # Friendly type labels for the column
        type_labels = {
            "password": "Password",
            "corrupt_disk": "Corrupt disk",
            "no_directory": "No directory",
            "extract_failed": "Extract failed",
            "unknown_format": "Unknown format",
        }
        # Color tinting per type so they're visually
        # distinguishable in a long list
        type_colors = {
            "password": Qt.GlobalColor.darkBlue,
            "corrupt_disk": Qt.GlobalColor.darkRed,
            "no_directory": Qt.GlobalColor.darkYellow,
            "extract_failed": Qt.GlobalColor.darkMagenta,
            "unknown_format": Qt.GlobalColor.darkGray,
        }
        for issue in issues:
            it = QTreeWidgetItem()
            when = issue.get("occurred_at") or 0
            it.setText(0, _time.strftime(
                "%Y-%m-%d %H:%M", _time.localtime(when)))
            t = issue.get("issue_type", "")
            it.setText(1, type_labels.get(t, t))
            it.setText(2, issue.get("path", ""))
            it.setText(3, issue.get("detail", ""))
            it.setData(0, Qt.ItemDataRole.UserRole, issue)
            color = type_colors.get(t)
            if color:
                it.setForeground(1, color)
            self.tree_issues.addTopLevelItem(it)
        # Update tab title with count so user can spot new
        # issues without switching tabs
        idx = self.tabs.indexOf(self.tree_issues.parent())
        if idx >= 0:
            label = "Issues"
            if issues:
                label = f"Issues ({len(issues)})"
            self.tabs.setTabText(idx, label)

    def _on_issue_double_click(self, item, _col):
        """Copy the offending file's path to the clipboard so the
        user can paste it into a terminal / file manager and deal
        with the issue."""
        issue = item.data(0, Qt.ItemDataRole.UserRole)
        if not issue:
            return
        path = issue.get("path", "")
        if path:
            # Strip the !member.foo suffix if it's an in-archive
            # path - the user wants the container, not the
            # virtual entry
            container = path.split("!")[0]
            QApplication.clipboard().setText(container)
            self.lbl_status.setText(
                f"Path copied: {container}")

    def _on_clear_issues(self):
        if QMessageBox.question(
                self, "Clear Issues",
                "Remove all logged issues?\n\n"
                "This doesn't undo anything - it just clears the "
                "list. Issues will reappear on the next scan if "
                "the underlying problems are still there.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        n = database.clear_scan_issues()
        self.lbl_status.setText(f"Cleared {n} issue(s)")
        self._refresh_issues()

    # --------------------------------------------------------
    # Stats tab
    # --------------------------------------------------------

    def _build_stats_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)

        self.lbl_stats = QLabel()
        self.lbl_stats.setFont(QFont("Courier", 11))
        v.addWidget(self.lbl_stats)

        # Scan history
        v.addWidget(QLabel("Recent scans:"))
        self.tree_scans = QTreeWidget()
        self.tree_scans.setHeaderLabels([
            "When", "Root", "Files", "Disks", "Errors", "Status"])
        self.tree_scans.setColumnWidth(0, 160)
        self.tree_scans.setColumnWidth(1, 380)
        self.tree_scans.setColumnWidth(2, 70)
        self.tree_scans.setColumnWidth(3, 70)
        self.tree_scans.setColumnWidth(4, 70)
        self.tree_scans.setColumnWidth(5, 90)
        self.tree_scans.setRootIsDecorated(False)
        install_table_state(self.tree_scans,
                            "database_browser:scans")
        v.addWidget(self.tree_scans, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_stats)
        row.addWidget(self.btn_refresh)
        self.btn_cleanup = QPushButton("Clean Up Non-C64 Files")
        self.btn_cleanup.setToolTip(
            "Remove indexed files that aren't PRG/SEQ/USR/REL, "
            "disk images, or archives. Useful after changing "
            "the filter rules.")
        self.btn_cleanup.clicked.connect(self._on_cleanup_non_c64)
        row.addWidget(self.btn_cleanup)
        self.btn_vacuum = QPushButton("Vacuum (compact DB)")
        self.btn_vacuum.clicked.connect(self._on_vacuum)
        row.addWidget(self.btn_vacuum)
        self.btn_reset = QPushButton("Reset Database...")
        self.btn_reset.setStyleSheet("color: #a00;")
        self.btn_reset.clicked.connect(self._on_reset_db)
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        v.addLayout(row)

        self.tabs.addTab(w, "Stats")

    def _refresh_stats(self):
        try:
            s = database.stats()
        except Exception as e:
            self.lbl_stats.setText(f"Error: {e}")
            return
        # Format the headline numbers
        bytes_str = _fmt_size(s["bytes"])
        # For trial users include "X / 1000" so they can see
        # how close they are to the cap. Pro users (or anyone
        # with FEATURE_DB_UNLIMITED) just see the raw number.
        disk_display = f"{s['disks']:,}"
        try:
            from . import license as _lic
            if not _lic.has_feature(_lic.FEATURE_DB_UNLIMITED):
                cap = _lic.TRIAL_DB_DISK_LIMIT
                remaining = max(0, cap - s['disks'])
                if s['disks'] >= cap:
                    disk_display = (
                        f"{s['disks']:,} / {cap:,}  "
                        f"[TRIAL LIMIT REACHED - "
                        f"register to keep cataloging]")
                else:
                    disk_display = (
                        f"{s['disks']:,} / {cap:,}  "
                        f"(trial, {remaining:,} disks free)")
        except Exception:
            pass
        text = (
            f"Files indexed:    {s['files']:,}\n"
            f"Total bytes:      {bytes_str}\n"
            f"Disk images:      {disk_display}\n"
            f"Disk entries:     {s['entries']:,}\n"
            f"Scans performed:  {s['scans']:,}\n"
            f"DB file:          {database.DB_PATH}")
        # Pending = files mid-process when Quopus last died.
        # Failed = files we tried and gave up on. Both call out
        # for user attention so we show them with warnings.
        pending = s.get("pending", 0)
        failed = s.get("failed", 0)
        if pending:
            text += (f"\n\n[!] {pending} file(s) in 'pending' "
                     f"state - left over from an interrupted scan.\n"
                     f"    Crash recovery on next Quopus launch "
                     f"will re-enqueue them.")
        if failed:
            text += (f"\n\n[!] {failed} file(s) marked 'failed' "
                     f"(scanner gave up).\n"
                     f"    Re-scan that folder to retry, or use "
                     f"the Issues tab to investigate.")
        # Issue counts give the user a quick "anything I need to
        # look at?" without switching tabs. Listed with their
        # friendly labels so the numbers are immediately
        # actionable.
        try:
            issue_counts = database.count_issues_by_type()
        except Exception:
            issue_counts = {}
        if issue_counts:
            text += "\n\nIssues:\n"
            label_map = {
                "password": "Password-protected",
                "corrupt_disk": "Corrupt disks",
                "no_directory": "No-directory disks",
                "extract_failed": "Extract failures",
                "unknown_format": "Unknown formats",
                "trial_disk_limit": "Trial disk-count limit",
            }
            for itype, n in sorted(issue_counts.items()):
                lbl = label_map.get(itype, itype)
                text += f"  {lbl:24} {n:,}\n"
        self.lbl_stats.setText(text)

        # Refresh scan history
        self.tree_scans.clear()
        import time
        with database.connection() as conn:
            cur = conn.execute("""
                SELECT id, started_at, root_path, file_count,
                       disk_count, error_count, status
                FROM scans
                ORDER BY started_at DESC
                LIMIT 50
            """)
            for row in cur.fetchall():
                it = QTreeWidgetItem()
                started = row["started_at"] or 0
                it.setText(0, time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(started)))
                it.setText(1, row["root_path"] or "")
                it.setText(2, str(row["file_count"] or 0))
                it.setText(3, str(row["disk_count"] or 0))
                it.setText(4, str(row["error_count"] or 0))
                it.setText(5, row["status"] or "?")
                if row["error_count"] and row["error_count"] > 0:
                    it.setForeground(4,
                                     Qt.GlobalColor.darkYellow)
                if row["status"] == "error":
                    it.setForeground(5, Qt.GlobalColor.red)
                self.tree_scans.addTopLevelItem(it)

    def _on_vacuum(self):
        """Full DB maintenance: VACUUM + FTS5 optimize + PRAGMA
        optimize. Reclaims space from deleted rows, compacts the
        two FTS trigram indexes, and lets SQLite gather fresh
        statistics for its query planner.

        On a big catalog this can take a few minutes - we run
        each step with a status message so the user knows it's
        not frozen.
        """
        try:
            with database.connection() as conn:
                self.lbl_status.setText(
                    "Optimizing FTS index (files)...")
                QApplication.processEvents()
                # FTS5 'optimize' command merges all the b-tree
                # segments into one, which makes searches faster
                # and shrinks the index. Documented as a slow op
                # for big indexes but only needs to run when the
                # index has had lots of insert/delete churn.
                conn.execute(
                    "INSERT INTO fts_names(fts_names) "
                    "VALUES('optimize')")

                self.lbl_status.setText(
                    "Optimizing FTS index (disk entries)...")
                QApplication.processEvents()
                conn.execute(
                    "INSERT INTO fts_entries(fts_entries) "
                    "VALUES('optimize')")

                self.lbl_status.setText(
                    "Analyzing for query planner...")
                QApplication.processEvents()
                # PRAGMA optimize uses sqlite_stat tables to
                # decide which indexes the planner should prefer.
                # Cheap; only updates stats that have gone stale.
                conn.execute("PRAGMA optimize")

                self.lbl_status.setText("Vacuuming (compacting)...")
                QApplication.processEvents()
                # VACUUM has to be outside any transaction.
                # The connection() context already ran COMMIT-on-
                # exit, so we exec it here and then run COMMIT
                # explicitly to flush.
                conn.execute("VACUUM")
                conn.commit()
            self.lbl_status.setText("Vacuum + optimize complete")
        except Exception as e:
            self.lbl_status.setText(
                f"Vacuum failed: {e}")
        self._refresh_stats()

    def _on_cleanup_non_c64(self):
        """Delete file rows whose extension isn't in our C64
        filter list. Useful after upgrading from a version that
        indexed everything - or just to trim out cruft if the
        user ever turned the filter off."""
        from .db_scanner import (
            C64_FILE_EXTS, DISK_EXTS, ARCHIVE_EXTS)
        allowed = (C64_FILE_EXTS | DISK_EXTS | ARCHIVE_EXTS)
        # SQL: DELETE FROM files WHERE extension NOT IN (...)
        # We build a placeholder list for parameterized binding.
        placeholders = ",".join("?" * len(allowed))
        query = (f"DELETE FROM files WHERE extension NOT IN "
                 f"({placeholders}) "
                 f"OR extension IS NULL")
        if QMessageBox.question(
                self, "Clean Up Non-C64 Files",
                "Remove all indexed files whose extension is not "
                "PRG/SEQ/USR/REL, a disk image, or an archive?\n\n"
                "Disk entries (PRG/SEQ/USR/REL inside disk images) "
                "are kept regardless.\n\n"
                "This is permanent. Re-scanning will only re-index "
                "files matching the current filter.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self.lbl_status.setText("Cleaning up...")
        QApplication.processEvents()
        try:
            with database.connection() as conn:
                cur = conn.execute(query, tuple(allowed))
                deleted = cur.rowcount
                conn.commit()
            self.lbl_status.setText(
                f"Removed {deleted} non-C64 files. "
                f"Run Vacuum to reclaim disk space.")
        except Exception as e:
            self.lbl_status.setText(f"Cleanup failed: {e}")
        self._refresh_stats()
        # Re-run any active searches
        if self.tabs.currentIndex() == 0:
            self._do_files_search()
        elif self.tabs.currentIndex() == 1:
            self._do_disks_search()

    def _on_reset_db(self):
        if QMessageBox.question(
                self, "Reset Database",
                "This deletes every indexed file and disk entry.\n"
                "You'll need to re-scan your archives.\n\n"
                "Are you sure?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            if database.DB_PATH.exists():
                database.DB_PATH.unlink()
            # Also remove WAL and SHM files
            for suffix in ("-wal", "-shm"):
                p = database.DB_PATH.parent / (
                    database.DB_PATH.name + suffix)
                if p.exists():
                    p.unlink()
            database.init_db()
            self.lbl_status.setText("Database reset")
        except Exception as e:
            self.lbl_status.setText(f"Reset failed: {e}")
        self._refresh_stats()
        self.tree_files.clear()
        self.tree_disks.clear()
        self.tree_disk_files.clear()

    # --------------------------------------------------------
    # Scan controls
    # --------------------------------------------------------

    def _on_scan_folder(self):
        if self.scan_worker is not None:
            QMessageBox.information(
                self, "Scan in progress",
                "Wait for the current scan to finish or cancel "
                "it first.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to scan", str(Path.home()))
        if not folder:
            return
        # Ask about incremental mode
        choice = QMessageBox.question(
            self, "Scan Mode",
            f"Scan {folder}?\n\n"
            "Click YES for incremental (skip files we already\n"
            "indexed with same mtime - fast)\n\n"
            "Click NO for full re-scan (hash every file again -\n"
            "use if you suspect the DB is out of sync)",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes)
        if choice == QMessageBox.StandardButton.Cancel:
            return
        incremental = (choice == QMessageBox.StandardButton.Yes)
        # Use the same _launch_scan helper as the Folders tab,
        # tracking the folder afterwards so it appears in the
        # persistent list.
        self._launch_scan(folder, incremental=incremental,
                          on_finish_track_folder=True)

    def _on_scan_progress(self, path: str, completed: int,
                          walked: int):
        # Trim the path for readability
        short = path
        if len(short) > 80:
            short = "..." + short[-77:]
        # The async scanner shows BOTH counts so the user can see
        # the queue draining even after the walker is done.
        if walked > 0 and walked > completed:
            self.lbl_status.setText(
                f"Scanning: {completed}/{walked} files done "
                f"- {short}")
        else:
            self.lbl_status.setText(
                f"Scanning: {completed} files done - {short}")

    def _on_scan_done(self, scan_id, files, errors):
        self.scan_worker = None
        self.btn_scan.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        # Re-enable folders-tab buttons too
        if hasattr(self, "btn_folder_add"):
            self.btn_folder_add.setEnabled(True)
            self.btn_folder_rescan_one.setEnabled(True)
            self.btn_folder_rescan_all.setEnabled(
                len(self._load_scanned_folders()) > 0)
        # If this scan was launched with track_folder, persist
        # the result so it shows up in the Folders tab.
        if getattr(self, "_pending_track_folder", None):
            self._add_folder_to_list(
                self._pending_track_folder,
                scan_id=scan_id, file_count=files)
            self._pending_track_folder = None
            self._refresh_folders_list()
        msg = f"Scan {scan_id} complete: {files} files"
        if errors:
            msg += f", {errors} errors"
        # Mention issues so the user knows to check the Issues
        # tab. We count what got logged THIS scan specifically.
        try:
            scan_issues = database.list_scan_issues(
                scan_id=scan_id, limit=10000)
            if scan_issues:
                msg += f", {len(scan_issues)} issue(s) - see Issues tab"
        except Exception:
            pass
        self.lbl_status.setText(msg)
        self._refresh_stats()
        self._refresh_issues()
        # Re-run the current search to pick up new results
        if self.tabs.currentIndex() == 0:
            self._do_files_search()
        elif self.tabs.currentIndex() == 1:
            self._do_disks_search()
        # If we're in the middle of a Re-scan All chain, kick
        # off the next folder. This has to come AFTER all the
        # state-resetting above (scan_worker = None etc) since
        # _do_next_rescan ends up calling _launch_scan which
        # needs a clean slate.
        if getattr(self, "_pending_chain_next", False):
            self._pending_chain_next = False
            # Defer the next start to the event loop so the UI
            # gets a chance to repaint between folders. Without
            # this delay rapidly-completing scans (e.g. all
            # incremental no-op folders) would lock the UI.
            QTimer.singleShot(50, self._do_next_rescan)

    def _on_cancel_scan(self):
        if self.scan_worker:
            self.scan_worker.cancel()
            self.lbl_status.setText("Cancelling...")

    # --------------------------------------------------------
    # Open / navigate
    # --------------------------------------------------------

    def _on_open_item(self, item, _col):
        """Double-click: copy path to clipboard for now. Future:
        navigate the main Quopus lister to the parent folder."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        path = data.get("path") or data.get("disk_path") or ""
        if "!" in path:
            # Inside an archive - strip the suffix to get container
            path = path.split("!")[0]
        if path:
            QApplication.clipboard().setText(path)
            self.lbl_status.setText(
                f"Path copied to clipboard: {path}")

    def closeEvent(self, ev):
        """Tear down all background machinery before the dialog
        gets deleted. Without this, timers and worker threads
        keep firing their done/timeout signals into slots that
        reference `self`, which is about to become a 'wrapped
        C/C++ object has been deleted' crash on the next Qt
        event loop tick.

        The order matters:

          1. Confirm with the user if a scan is in progress -
             they might not actually want to close.
          2. Stop the periodic Watch-tab refresh timer.
          3. Stop the search debounce timers.
          4. Cancel the sequential emulator runner (set its
             cancel flag, then disconnect signals so the worker
             can't reach back into our destroyed slots when it
             eventually exits).
          5. Cancel any in-progress scan worker.

        We deliberately don't .wait() on the sequential runner's
        QThread - it might be blocked on subprocess.Popen.wait()
        for a VICE window the user is still using. Setting the
        cancel flag and disconnecting signals is enough to make
        the thread harmless when it eventually completes.
        """
        # Step 1: scan worker confirmation
        if self.scan_worker is not None:
            if QMessageBox.question(
                    self, "Scan in progress",
                    "A scan is still running. Cancel it and close?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.scan_worker.cancel()
            self.scan_worker.wait(3000)

        # Step 2: kill periodic timers. These were the main
        # crash culprit - the 2-second watch refresh keeps
        # firing _refresh_watch_status which touches
        # self.tree_watch even after the C++ widget is gone.
        for attr in ("_watch_timer", "_files_timer",
                     "_disks_timer"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                    # Also disconnect so any in-flight signal
                    # we missed doesn't reach the slot
                    try:
                        t.timeout.disconnect()
                    except (TypeError, RuntimeError):
                        # No slots connected, or already
                        # disconnected
                        pass
                except RuntimeError:
                    # Timer already gone (unusual but safe)
                    pass

        # Step 3: clean up the sequential emulator runner if
        # it's mid-flight. We don't kill the running VICE
        # process - the user can keep playing - but we disable
        # the chain so no more items launch, and disconnect
        # the worker's done signal so its eventual exit can't
        # call back into our deleted widgets.
        state = getattr(self, "_seq_runner_state", None)
        if state is not None:
            state["cancelled"] = True
            worker = state.get("current_worker")
            if worker is not None:
                try:
                    worker.done.disconnect()
                except (TypeError, RuntimeError):
                    pass
        # Mark inactive so a second close attempt doesn't try
        # to clean up the same state again
        self._seq_runner_active = False
        self._seq_runner_state = None

        super().closeEvent(ev)


def show_database_browser(parent=None):
    """Entry point used by actions.py.

    Non-modal: returns immediately, Quopus stays usable while
    the database browser is open. This is essential because
    indexing huge archive folders can take many minutes -
    blocking the lister UI for that long would be unacceptable.

    The dialog has WA_DeleteOnClose set so closing it cleans up
    automatically; we don't need to track instances. Multiple
    DB browsers can be open at once if the user wants (though
    they all share the same underlying SQLite file and ingest
    queue, so the second window mostly just gives a second
    search view)."""
    dlg = DatabaseBrowserDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg
