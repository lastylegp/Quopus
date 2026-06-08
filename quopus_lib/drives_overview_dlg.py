# date_time: 2026-06-07 15:50
"""Drives-overview window.

Two pages in one dialog:

  Page 0: a sortable table of every drive the OS reports with
          total/used/free space and a per-drive fill-bar.
  Page 1: a lightweight folder browser. Double-clicking a drive
          on page 0 jumps here pointing at the drive's root.
          Folders are clickable (double-click to enter, or
          select + back-up via the '..' entry); the back-button
          and the middle mouse button both return to page 0.

The point is to use the drives overview as a quick navigator
in its own right - you see at a glance which disk has space,
double-click into it, browse, and "Open in left/right lister"
when you find what you want.
"""

import os
import shutil
import platform
from pathlib import Path
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QProgressBar,
    QAbstractItemView, QStackedWidget, QWidget, QLineEdit,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _fmt(n):
    """Binary-prefixed size formatter. Matches the lister panel."""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            if u == "B":
                return f"{int(n)} {u}"
            return f"{n:.2f} {u}"
        n /= 1024.0
    return f"{n:.2f} EB"


def _win_volume_label(path):
    """GetVolumeInformationW wrapper - returns the user-set
    volume name on Windows, '' on failure."""
    try:
        import ctypes
        from ctypes import wintypes
        root = path
        if len(root) == 2 and root[1] == ":":
            root = root + "\\"
        elif not root.endswith(("\\", "/")):
            root = root + "\\"
        vol_name = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_comp = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            vol_name, 261,
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(flags),
            fs_name, 261)
        if not ok:
            return ""
        return vol_name.value or ""
    except Exception:
        return ""


def _drive_label_for(entry):
    """Friendly label for a drive button entry."""
    path = entry.get("path", "")
    base = entry.get("label", path)
    if platform.system() == "Windows":
        vol = _win_volume_label(path)
        if vol:
            return f"{base}  [{vol}]"
        return base
    if path and path != "/":
        leaf = Path(path).name
        if leaf and leaf != base:
            return f"{base}  [{leaf}]"
    return base


def _kind_pretty(kind):
    return {
        "home":      "Home",
        "root":      "Root",
        "fixed":     "Fixed",
        "removable": "Removable",
        "cdrom":     "CD/DVD",
        "remote":    "Network",
        "ramdisk":   "RAM disk",
        "unknown":   "Unknown",
    }.get(kind, kind or "Unknown")


def _fmt_mtime(ts):
    try:
        return datetime.fromtimestamp(ts).strftime(
            "%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


# ---------------------------------------------------------------
# Mouse-aware tree subclass. Catches middle-button clicks and
# bubbles them to the dialog via a callback. We can't use Qt's
# normal mouseReleaseEvent on the dialog itself because the
# tree eats clicks before they reach the parent.
# ---------------------------------------------------------------

class _MouseAwareTree(QTreeWidget):
    """QTreeWidget that fires a callback on middle-mouse press."""

    def __init__(self, parent=None, on_middle_click=None):
        super().__init__(parent)
        self._on_middle = on_middle_click

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.MiddleButton:
            if self._on_middle is not None:
                self._on_middle()
                return
        super().mousePressEvent(ev)


# ---------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------

class DrivesOverviewDialog(QDialog):
    """Drives overview + in-place folder browser. Page 0 lists
    all drives with usage; page 1 browses inside whichever
    drive was double-clicked. Middle-mouse-click anywhere on
    the tree returns to page 0."""

    AUTO_REFRESH_MS = 5000
    PAGE_DRIVES = 0
    PAGE_BROWSER = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mw = parent
        self.setWindowTitle("Drives")
        self.resize(900, 540)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinMaxButtonsHint)

        # Browser state. _browser_path is the directory currently
        # shown on page 1. None = nothing entered yet.
        self._browser_path = None
        # Remember the drive we came from so the breadcrumb can
        # show it and Back-To-Drives reads naturally.
        self._origin_drive_label = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_drives_page())
        self._stack.addWidget(self._build_browser_page())
        outer.addWidget(self._stack, 1)

        self._stack.setCurrentIndex(self.PAGE_DRIVES)

        # Auto-refresh ticker - only affects page 0 (drive list).
        # On the browser page the user wouldn't expect the
        # listing to randomly reshuffle, so we leave page 1
        # alone.
        self._timer = QTimer(self)
        self._timer.setInterval(self.AUTO_REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self.refresh_drives()

    # ----------------------------------------------------------
    # Page 0: drives list
    # ----------------------------------------------------------

    def _build_drives_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        hdr = QLabel(
            "Drives reported by the OS. Double-click to browse "
            "into a drive; middle-click to return here. Refreshes "
            "every 5 seconds.")
        hdr.setStyleSheet(
            "QLabel { color: #444; padding: 2px 4px; }")
        hdr.setWordWrap(True)
        lay.addWidget(hdr)

        self.tree_drives = _MouseAwareTree(
            on_middle_click=self._go_back_to_drives)
        self.tree_drives.setColumnCount(7)
        self.tree_drives.setHeaderLabels(
            ["Drive", "Type", "Path",
             "Total", "Used", "Free", "Usage"])
        self.tree_drives.setRootIsDecorated(False)
        self.tree_drives.setAlternatingRowColors(True)
        self.tree_drives.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_drives.setStyleSheet(
            "QTreeWidget { font-family: 'Courier New', "
            "monospace; font-size: 12px; }")
        hh = self.tree_drives.header()
        hh.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive)
        for i in (1, 3, 4, 5):
            hh.setSectionResizeMode(
                i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(
            6, QHeaderView.ResizeMode.Interactive)
        self.tree_drives.setColumnWidth(0, 180)
        self.tree_drives.setColumnWidth(6, 160)
        lay.addWidget(self.tree_drives, 1)

        self.tree_drives.itemSelectionChanged.connect(
            self._update_drives_buttons)
        self.tree_drives.itemDoubleClicked.connect(
            self._on_drive_doubleclicked)

        bot = QHBoxLayout()
        b_refresh = QPushButton("Refresh now")
        b_refresh.clicked.connect(self.refresh_drives)
        bot.addWidget(b_refresh)
        self.b_browse = QPushButton("Browse")
        self.b_browse.clicked.connect(
            self._enter_selected_drive)
        bot.addWidget(self.b_browse)
        self.b_left = QPushButton("Open in left lister")
        self.b_right = QPushButton("Open in right lister")
        self.b_left.clicked.connect(
            lambda: self._open_in_pane("left"))
        self.b_right.clicked.connect(
            lambda: self._open_in_pane("right"))
        bot.addWidget(self.b_left)
        bot.addWidget(self.b_right)
        bot.addStretch(1)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close)
        bot.addWidget(b_close)
        lay.addLayout(bot)

        self._update_drives_buttons()
        return page

    # ----------------------------------------------------------
    # Page 1: folder browser
    # ----------------------------------------------------------

    def _build_browser_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Top row: Back-to-drives + breadcrumb-style path display.
        # Middle-click anywhere on the tree also navigates back.
        top = QHBoxLayout()
        b_back = QPushButton("\u25C0 Back to drives")
        b_back.setToolTip(
            "Return to the drive list. Middle-click on the "
            "table does the same thing.")
        b_back.clicked.connect(self._go_back_to_drives)
        top.addWidget(b_back)
        self.lbl_path = QLineEdit()
        self.lbl_path.setReadOnly(True)
        self.lbl_path.setStyleSheet(
            "QLineEdit { background: transparent; "
            "font-family: 'Courier New', monospace; "
            "font-size: 13px; }")
        top.addWidget(self.lbl_path, 1)
        lay.addLayout(top)

        self.tree_browser = _MouseAwareTree(
            on_middle_click=self._go_back_to_drives)
        self.tree_browser.setColumnCount(4)
        self.tree_browser.setHeaderLabels(
            ["Name", "Size", "Modified", "Type"])
        self.tree_browser.setRootIsDecorated(False)
        self.tree_browser.setAlternatingRowColors(True)
        self.tree_browser.setSortingEnabled(False)
        self.tree_browser.setStyleSheet(
            "QTreeWidget { font-family: 'Courier New', "
            "monospace; font-size: 12px; }")
        hh = self.tree_browser.header()
        hh.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_browser.itemDoubleClicked.connect(
            self._on_browser_doubleclicked)
        lay.addWidget(self.tree_browser, 1)

        bot = QHBoxLayout()
        b_up = QPushButton("Up")
        b_up.setToolTip("Go up one directory level.")
        b_up.clicked.connect(self._browser_up)
        bot.addWidget(b_up)
        b_refresh = QPushButton("Refresh")
        b_refresh.clicked.connect(self._refresh_browser)
        bot.addWidget(b_refresh)
        bot.addStretch(1)
        b_left = QPushButton("Open here in left lister")
        b_left.clicked.connect(
            lambda: self._open_browser_in_pane("left"))
        bot.addWidget(b_left)
        b_right = QPushButton("Open here in right lister")
        b_right.clicked.connect(
            lambda: self._open_browser_in_pane("right"))
        bot.addWidget(b_right)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close)
        bot.addWidget(b_close)
        lay.addLayout(bot)

        return page

    # ----------------------------------------------------------
    # Page transitions
    # ----------------------------------------------------------

    def _go_back_to_drives(self):
        """Switch to the drives page. Triggered by the Back
        button OR by middle-mouse-click on either tree.
        Refreshes the drive table because something may have
        changed while we were browsing (size used etc.)."""
        self._stack.setCurrentIndex(self.PAGE_DRIVES)
        self.refresh_drives()
        self.setWindowTitle("Drives")

    def _enter_drive(self, path, label=""):
        """Switch to the browser page pointing at `path`."""
        if not path:
            return
        self._origin_drive_label = label or path
        self._browser_path = Path(path)
        self._stack.setCurrentIndex(self.PAGE_BROWSER)
        self._refresh_browser()
        self.setWindowTitle(
            f"Drives - {self._origin_drive_label}")

    def _enter_selected_drive(self):
        it = self.tree_drives.currentItem()
        if it is None:
            return
        path = it.data(0, Qt.ItemDataRole.UserRole)
        label = it.text(0)
        self._enter_drive(path, label)

    # ----------------------------------------------------------
    # Mouse events
    # ----------------------------------------------------------

    def _on_drive_doubleclicked(self, item, _col):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        self._enter_drive(path, item.text(0))

    def _on_browser_doubleclicked(self, item, _col):
        """Double-click in the browser: enter the subdirectory.
        Files do nothing on double-click - the dialog isn't
        a viewer. Use 'Open here in <pane>' to hand off to a
        real lister."""
        if self._browser_path is None:
            return
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind != "dir":
            return
        name = item.text(0)
        if name == "..":
            self._browser_up()
            return
        new_path = self._browser_path / name
        if new_path.is_dir():
            self._browser_path = new_path
            self._refresh_browser()

    def _browser_up(self):
        if self._browser_path is None:
            return
        parent = self._browser_path.parent
        if parent == self._browser_path:
            # Already at filesystem root - bounce back to drives.
            self._go_back_to_drives()
            return
        self._browser_path = parent
        self._refresh_browser()

    # ----------------------------------------------------------
    # Drive list refresh
    # ----------------------------------------------------------

    def _tick(self):
        # Only refresh the drive list page; the browser stays
        # stable so the user's selection isn't yanked away.
        if self._stack.currentIndex() == self.PAGE_DRIVES:
            self.refresh_drives()

    def showEvent(self, ev):
        self._timer.start()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)

    def refresh_drives(self):
        from .config import _system_default_drives
        prev = None
        cur = self.tree_drives.currentItem()
        if cur is not None:
            prev = cur.data(0, Qt.ItemDataRole.UserRole)
        self.tree_drives.clear()
        drives = _system_default_drives()
        for entry in drives:
            path = entry.get("path", "")
            label = _drive_label_for(entry)
            kind = _kind_pretty(entry.get("kind", ""))
            try:
                u = shutil.disk_usage(path)
                total, used, free = u.total, u.used, u.free
                pct = (used * 100 // total) if total else 0
                ok = True
            except (OSError, FileNotFoundError):
                total = used = free = 0
                pct = 0
                ok = False
            it = QTreeWidgetItem([
                label, kind, path,
                _fmt(total) if ok else "-",
                _fmt(used)  if ok else "-",
                _fmt(free)  if ok else "-",
                "",
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, path)
            for c in (3, 4, 5):
                it.setTextAlignment(
                    c, Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
            if ok and pct >= 90:
                for c in range(7):
                    it.setBackground(
                        c, QBrush(QColor("#ffe0e0")))
            elif ok and pct >= 75:
                for c in range(7):
                    it.setBackground(
                        c, QBrush(QColor("#fff4d0")))
            self.tree_drives.addTopLevelItem(it)
            if ok:
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(pct)
                bar.setFormat(f"{pct}%")
                bar.setTextVisible(True)
                bar.setAlignment(
                    Qt.AlignmentFlag.AlignCenter)
                if pct >= 90:
                    bar.setStyleSheet(
                        "QProgressBar::chunk "
                        "{ background-color: #d04040; }")
                elif pct >= 75:
                    bar.setStyleSheet(
                        "QProgressBar::chunk "
                        "{ background-color: #d09040; }")
                self.tree_drives.setItemWidget(it, 6, bar)
            else:
                lab = QLabel("(not ready)")
                lab.setStyleSheet(
                    "color: #888; padding-left: 6px;")
                self.tree_drives.setItemWidget(it, 6, lab)
            if prev is not None and path == prev:
                self.tree_drives.setCurrentItem(it)
        self._update_drives_buttons()

    def _update_drives_buttons(self):
        has_sel = self.tree_drives.currentItem() is not None
        has_mw = (
            self._mw is not None
            and hasattr(self._mw, "left_lister")
            and hasattr(self._mw, "right_lister"))
        self.b_browse.setEnabled(has_sel)
        self.b_left.setEnabled(has_sel and has_mw)
        self.b_right.setEnabled(has_sel and has_mw)

    # ----------------------------------------------------------
    # Browser refresh
    # ----------------------------------------------------------

    def _refresh_browser(self):
        """Rebuild the page-1 table from _browser_path. Folders
        first, then files; both sorted by name (case-folded)
        for stable ordering. Hidden entries starting with '.'
        are skipped to match Quopus' default behaviour."""
        if self._browser_path is None:
            return
        path = self._browser_path
        self.lbl_path.setText(str(path))
        self.tree_browser.clear()

        # Always offer a ".." row at the top - even at a
        # drive root it lets the user bounce back to the
        # drive list cleanly.
        up_it = QTreeWidgetItem(["..", "", "", "<UP>"])
        up_it.setData(
            0, Qt.ItemDataRole.UserRole + 1, "dir")
        self.tree_browser.addTopLevelItem(up_it)

        try:
            entries = list(path.iterdir())
        except (OSError, PermissionError) as e:
            err = QTreeWidgetItem([
                f"(error: {e})", "", "", ""])
            self.tree_browser.addTopLevelItem(err)
            return

        dirs, files = [], []
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e)
                else:
                    files.append(e)
            except OSError:
                continue
        dirs.sort(key=lambda p: p.name.casefold())
        files.sort(key=lambda p: p.name.casefold())

        for d in dirs:
            try:
                mt = _fmt_mtime(d.stat().st_mtime)
            except OSError:
                mt = "-"
            it = QTreeWidgetItem([d.name, "", mt, "<DIR>"])
            it.setData(
                0, Qt.ItemDataRole.UserRole + 1, "dir")
            self.tree_browser.addTopLevelItem(it)

        for f in files:
            try:
                st = f.stat()
                sz = _fmt(st.st_size)
                mt = _fmt_mtime(st.st_mtime)
            except OSError:
                sz = "-"
                mt = "-"
            # File "type" is the extension without leading dot.
            ext = f.suffix.lower().lstrip(".") or "file"
            it = QTreeWidgetItem([f.name, sz, mt, ext])
            it.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter)
            it.setData(
                0, Qt.ItemDataRole.UserRole + 1, "file")
            self.tree_browser.addTopLevelItem(it)

    # ----------------------------------------------------------
    # Open in actual Quopus lister panes
    # ----------------------------------------------------------

    def _open_in_pane(self, side):
        """From page 0: open the selected drive in the named
        Quopus lister and close the dialog."""
        if self._mw is None:
            return
        it = self.tree_drives.currentItem()
        if it is None:
            return
        path = it.data(0, Qt.ItemDataRole.UserRole)
        self._hand_off_to_lister(side, path)

    def _open_browser_in_pane(self, side):
        """From page 1: open the currently-browsed folder in
        the named Quopus lister and close the dialog."""
        if self._mw is None or self._browser_path is None:
            return
        self._hand_off_to_lister(
            side, str(self._browser_path))

    def _hand_off_to_lister(self, side, path):
        if not path or self._mw is None:
            return
        target = (self._mw.left_lister if side == "left"
                  else self._mw.right_lister)
        try:
            target.goto(path)
        except Exception:
            try:
                target.set_path(path)
            except Exception:
                pass
        self.close()
