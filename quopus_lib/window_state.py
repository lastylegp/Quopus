"""Window state persistence for module dialogs.

Stores window geometry (position + size) and table column widths
between sessions in <quopus>/config/window_state.json.

Each window/dialog identifies itself by a short stable key like
"asm64_browser" or "image_viewer". Two helpers do the heavy
lifting:

    install_window_state(widget, key)
        Restore + auto-save the window's geometry. Call once in the
        widget's __init__ after the layout is set up but before
        show(). The save happens automatically on close/resize.

    install_table_state(table, key, prefix=None)
        Restore + auto-save a QTableView/QTreeView/QTableWidget's
        column widths and sort order. Call once per table after
        the columns are populated. Auto-saves on column-resize and
        sort-change.

State is keyed by a string the caller controls so renaming a
class doesn't lose user customizations. We write state immediately
on every change rather than on close - some dialogs are killed
non-cleanly (WA_DeleteOnClose with abrupt window manager close)
and the closeEvent override doesn't always fire.

File format is plain JSON:

    {
      "windows": {
        "asm64_browser": {"x": 100, "y": 200, "w": 1024, "h": 768},
        "image_viewer":  {"x": 50,  "y": 50,  "w": 800,  "h": 600}
      },
      "tables": {
        "asm64_browser:results": {
          "cols": [40, 200, 300, 80],
          "sort_col": 1, "sort_order": "asc"
        }
      }
    }

The file is rewritten in full on every save - it's tiny (well under
100KB even with dozens of tracked windows), and a corrupted partial
write is easier to recover from than a corrupt SQLite database.
"""
import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QTimer, QEvent, QObject, QPoint, QSize,
)
from PyQt6.QtWidgets import (
    QWidget, QHeaderView, QTreeView, QTableView, QTableWidget,
    QTreeWidget,
)

from .config import CONFIG_DIR


_STATE_FILE = CONFIG_DIR / "window_state.json"

# In-memory cache of the on-disk state. Loaded lazily on first use,
# rewritten in full on every change. Single dict instead of two so
# we can share saves between window and table updates.
_state_cache: Optional[dict] = None


def _load() -> dict:
    """Read the state file. Returns a default empty dict if the
    file doesn't exist or is corrupted (we can't recover useful
    state from a bad file - silently start fresh)."""
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    if _STATE_FILE.is_file():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _state_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _state_cache = {}
    else:
        _state_cache = {}
    _state_cache.setdefault("windows", {})
    _state_cache.setdefault("tables", {})
    return _state_cache


def _save() -> None:
    """Write the in-memory cache to disk. Best-effort - failures
    are silently ignored since losing window positions isn't
    important enough to interrupt the user with an error."""
    if _state_cache is None:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state_cache, f, indent=2)
    except OSError:
        pass


# ============================================================
# Window geometry
# ============================================================


class _WindowStateFilter(QObject):
    """Event filter that watches a top-level widget for move/resize
    events and saves its geometry whenever it stabilizes.

    We debounce with a single-shot QTimer because drag-resizing a
    window fires hundreds of move/resize events per second; saving
    on every one is wasteful and can hit the disk hard. 500ms after
    the last event we write once."""

    def __init__(self, widget: QWidget, key: str):
        super().__init__(widget)
        self.widget = widget
        self.key = key
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._save_now)

    def eventFilter(self, obj, event):
        if obj is self.widget and event.type() in (
                QEvent.Type.Move, QEvent.Type.Resize):
            # Schedule debounced save
            self._timer.start()
        elif (obj is self.widget
              and event.type() == QEvent.Type.Close):
            # On close, save immediately - the widget may be
            # destroyed before the timer fires.
            self._save_now()
        return False  # don't consume - let the widget handle it

    def _save_now(self):
        # Don't store geometry while the window is maximized -
        # we'd save the maximized size which is useless next time
        # the user starts the app windowed. Save the underlying
        # "normal" geometry instead.
        if self.widget.isMaximized():
            geom = self.widget.normalGeometry()
        else:
            geom = self.widget.geometry()
        state = _load()
        state["windows"][self.key] = {
            "x": geom.x(),
            "y": geom.y(),
            "w": geom.width(),
            "h": geom.height(),
            "maximized": self.widget.isMaximized(),
        }
        _save()


def install_window_state(widget: QWidget, key: str) -> None:
    """Restore + persist a top-level widget's window geometry.

    Call from the widget's __init__ AFTER the layout is set up
    (so the default size is computed) but BEFORE show(). The
    saved geometry from a previous run (if any) overrides the
    default. From then on, every move and every resize is
    auto-saved to disk with 500ms debounce.

    Args:
        widget: The QDialog / QMainWindow / QWidget to track.
        key:    A short stable identifier - typically the module
                name like "asm64_browser" or "image_viewer".
    """
    state = _load()
    saved = state["windows"].get(key)
    if saved:
        try:
            # Clamp to a reasonable on-screen position - if the
            # user moved a window to a second monitor that's now
            # unplugged, restoring to (3000, 800) leaves it
            # off-screen and the user can't get it back. Pin to
            # the primary screen if the saved position is out of
            # bounds.
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.left(),
                    min(int(saved.get("x", 100)),
                        screen.right() - 200))
            y = max(screen.top(),
                    min(int(saved.get("y", 100)),
                        screen.bottom() - 100))
            w = max(200, min(int(saved.get("w", 800)),
                              screen.width()))
            h = max(150, min(int(saved.get("h", 600)),
                              screen.height()))
            widget.setGeometry(x, y, w, h)
            if saved.get("maximized"):
                widget.showMaximized()
        except (TypeError, ValueError, KeyError):
            # Corrupt entry - ignore and use defaults
            pass
    # Install the auto-save filter
    f = _WindowStateFilter(widget, key)
    widget.installEventFilter(f)
    # Hold a reference on the widget so the filter outlives the
    # local variable. Qt's parent system handles deletion when
    # the widget dies.
    widget._window_state_filter = f


# ============================================================
# Table column widths and sort order
# ============================================================


class _TableStateFilter(QObject):
    """Watches a header view for column resize / sort change and
    persists column widths + sort column. Same debounce strategy
    as the window state."""

    def __init__(self, table, key):
        super().__init__(table)
        self.table = table
        self.key = key
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._save_now)
        # Wire the header signals - we use sectionResized and
        # sortIndicatorChanged which fire for both QTableView/
        # QHeaderView and QTreeView/QHeaderView.
        header = self._header()
        if header is not None:
            header.sectionResized.connect(self._on_resize)
            header.sortIndicatorChanged.connect(self._on_resize)

    def _header(self):
        """Return the header view we should watch.

        QTableView/QTableWidget use horizontalHeader().
        QTreeView/QTreeWidget use header()."""
        if isinstance(self.table, (QTreeView, QTreeWidget)):
            return self.table.header()
        if isinstance(self.table, (QTableView, QTableWidget)):
            return self.table.horizontalHeader()
        return None

    def _on_resize(self, *args):
        self._timer.start()

    def _save_now(self):
        header = self._header()
        if header is None:
            return
        cols = []
        for i in range(header.count()):
            cols.append(header.sectionSize(i))
        sort_col = header.sortIndicatorSection()
        sort_order = (
            "asc"
            if header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
            else "desc")
        state = _load()
        state["tables"][self.key] = {
            "cols": cols,
            "sort_col": sort_col,
            "sort_order": sort_order,
        }
        _save()


def install_table_state(table, key: str) -> None:
    """Restore + persist a table's column widths and sort.

    Call once after the columns have been populated (e.g. after
    setHeaderLabels for QTreeWidget, or after setModel() for
    QTableView). If no saved state exists, defaults stay.

    Args:
        table: A QTableView/QTableWidget/QTreeView/QTreeWidget.
        key:   Stable identifier, usually 'window_name:table_name'
               like "asm64_browser:results".
    """
    state = _load()
    saved = state["tables"].get(key)
    f = _TableStateFilter(table, key)
    table.installEventFilter(f)   # keep reference alive
    table._table_state_filter = f
    if not saved:
        return
    header = f._header()
    if header is None:
        return
    # Restore widths - guard against count mismatch (column added
    # or removed since last save)
    cols = saved.get("cols", [])
    for i in range(min(len(cols), header.count())):
        try:
            header.resizeSection(i, int(cols[i]))
        except (TypeError, ValueError):
            continue
    # Restore sort order
    try:
        sort_col = int(saved.get("sort_col", 0))
        sort_order = (
            Qt.SortOrder.AscendingOrder
            if saved.get("sort_order", "asc") == "asc"
            else Qt.SortOrder.DescendingOrder)
        if 0 <= sort_col < header.count():
            # sortByColumn fires on the view, not the header
            if hasattr(table, 'sortByColumn'):
                table.sortByColumn(sort_col, sort_order)
            else:
                header.setSortIndicator(sort_col, sort_order)
    except (TypeError, ValueError):
        pass


def clear_state() -> None:
    """Wipe all stored window/table state. Useful for a 'Reset
    window positions' menu item or for the verify_features test
    harness."""
    global _state_cache
    _state_cache = {"windows": {}, "tables": {}}
    _save()
