"""Virtualized table model with tagged-items support (space key).
Four columns: Name, Ext, Size, Date. Sortable via header click."""
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PyQt6.QtGui import QBrush, QColor, QPen
from PyQt6.QtWidgets import QStyledItemDelegate, QStyle

from .palette import C, fmt_size, fmt_blocks


# Column indices. COL_FOLDER is only shown in search-results mode;
# regular listings use the first 4 columns and skip column 4.
COL_NAME   = 0
COL_EXT    = 1
COL_SIZE   = 2
COL_DATE   = 3
COL_FOLDER = 4
COL_COUNT  = 5


class TaggedItemDelegate(QStyledItemDelegate):
    """Custom delegate that paints tagged rows with the tag background
    color, across all columns."""
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self._model = model
        self._tagged_bg = QColor(C.TAGGED_BG)
        self._tagged_fg = QColor(C.TAGGED_FG)
        self._sel_bg = QColor(C.SELECTED)
        self._sel_fg = QColor(C.SELECTED_FG)
        self._dir_fg = QColor(C.LISTER_DIR)
        self._file_fg = QColor(C.LISTER_FG)

    def paint(self, painter, option, index):
        entry = self._model.entry_at(index.row())
        if entry is None:
            super().paint(painter, option, index)
            return

        is_tagged = entry.path in self._model.tagged
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if is_tagged:
            bg = self._tagged_bg
            fg = self._tagged_fg
        elif is_selected:
            bg = self._sel_bg
            fg = self._sel_fg
        else:
            bg = None
            fg = self._dir_fg if entry.is_dir else self._file_fg

        painter.save()
        if bg is not None:
            painter.fillRect(option.rect, bg)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.setPen(QPen(fg))
        painter.setFont(option.font)
        r = option.rect.adjusted(4, 0, -4, 0)
        # Right-align Size column; left-align others
        align = Qt.AlignmentFlag.AlignVCenter
        if index.column() == COL_SIZE:
            align |= Qt.AlignmentFlag.AlignRight
        else:
            align |= Qt.AlignmentFlag.AlignLeft
        painter.drawText(r, align, text)
        painter.restore()


class DirEntry:
    __slots__ = ("name", "path", "is_dir", "size", "mtime", "source_dir")

    def __init__(self, name, path, is_dir, size, mtime, source_dir=None):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime
        # Source directory annotation - only populated for entries
        # that came from a SearchResultsFs listing. None for regular
        # filesystem entries.
        self.source_dir = source_dir


class DirModel(QAbstractTableModel):
    SORT_NAME   = 0
    SORT_SIZE   = 1
    SORT_TIME   = 2
    SORT_EXT    = 3
    SORT_FOLDER = 4

    # Map column indices to sort keys for header clicks
    _COL_TO_SORT = {
        COL_NAME:   SORT_NAME,
        COL_EXT:    SORT_EXT,
        COL_SIZE:   SORT_SIZE,
        COL_DATE:   SORT_TIME,
        COL_FOLDER: SORT_FOLDER,
    }

    HEADERS = ("Name", "Ext", "Size", "Date", "Folder")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.order = []
        self.sort_key = self.SORT_NAME
        self.reverse = False
        self.tagged = set()
        # Whether to show the Folder column. Set to True only when
        # the lister is showing search results (entries have
        # source_dir populated).
        self.show_folder_column = False
        # Whether to format the Size column as C64 disk blocks
        # (256 bytes = 1 block) rather than human-readable bytes.
        # Set by FileLister.__init__ from the config.
        self.show_blocks = False

        self._dir_color = QBrush(QColor(C.LISTER_DIR))
        self._file_color = QBrush(QColor(C.LISTER_FG))
        self._tagged_bg = QBrush(QColor(C.TAGGED_BG))
        self._tagged_fg = QBrush(QColor(C.TAGGED_FG))

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self.order)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        # Hide the Folder column outside of search-results mode
        return COL_COUNT if self.show_folder_column else COL_COUNT - 1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < COL_COUNT:
                base = self.HEADERS[section]
                # Show sort indicator
                sort_col = None
                for c, s in self._COL_TO_SORT.items():
                    if s == self.sort_key:
                        sort_col = c; break
                if sort_col == section:
                    arrow = " ↓" if self.reverse else " ↑"
                    return base + arrow
                return base
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self.order):
            return None
        e = self.entries[self.order[row]]
        is_tagged = e.path in self.tagged

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_NAME:
                if e.is_dir:
                    return e.name
                # Strip extension - it's shown in the Ext column.
                # Files without an extension keep their full name.
                stem_idx = e.name.rfind('.')
                if stem_idx > 0:   # > 0 to keep ".hidden" files intact
                    return e.name[:stem_idx]
                return e.name
            elif col == COL_EXT:
                if e.is_dir:
                    return ""
                # Extension without leading dot, upper-case like TC
                suf = Path(e.name).suffix
                return suf[1:] if suf.startswith('.') else suf
            elif col == COL_SIZE:
                if e.is_dir:
                    return "<DIR>"
                # Size-display mode is read from the model's
                # show_blocks flag (set by the lister from config).
                # 256 bytes = 1 block in CBM DOS, rounded up.
                if getattr(self, 'show_blocks', False):
                    return fmt_blocks(e.size)
                return fmt_size(e.size)
            elif col == COL_DATE:
                try:
                    return datetime.fromtimestamp(e.mtime).strftime(
                        "%d-%b-%y %H:%M")
                except Exception:
                    return "?"
            elif col == COL_FOLDER:
                # Source-folder annotation, populated only on
                # search-results entries.
                return e.source_dir or ""

        if role == Qt.ItemDataRole.ForegroundRole:
            if is_tagged:
                return self._tagged_fg
            return self._dir_color if e.is_dir else self._file_color

        if role == Qt.ItemDataRole.BackgroundRole:
            if is_tagged:
                return self._tagged_bg
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            # Hover tooltip: show the full filesystem path. This
            # is most valuable for search results (where the
            # Folder column may be cut off by narrow column
            # width), but also useful in regular listings when
            # very long names get truncated. The same tip works
            # for every column so the user can hover anywhere
            # on the row.
            #
            # For search-result entries we have e.source_dir
            # set, and e.path is the absolute file path - we
            # show both so the user sees "what is this and
            # where does it live" at a glance. For normal
            # listings e.source_dir is None and we just show
            # the file's absolute path.
            if e.source_dir:
                # Search-results row - show search origin and
                # filename separately so they're easy to read.
                return (f"Name:   {e.name}\n"
                        f"Folder: {e.source_dir}")
            # Regular listing - the path IS the location.
            return e.path

        if role == Qt.ItemDataRole.UserRole:
            return e.path
        return None

    def flags(self, index):
        # Empty area in the view is the drop target - drops anywhere
        # in the lister go into the current directory. Individual rows
        # are NOT drop targets so Qt won't ask "drop into this folder?".
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return (Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled)

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = list(entries)
        live = {e.path for e in entries}
        self.tagged = {p for p in self.tagged if p in live}
        self._rebuild_order()
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.entries = []
        self.order = []
        self.tagged.clear()
        self.endResetModel()

    def entry_at(self, row):
        if 0 <= row < len(self.order):
            return self.entries[self.order[row]]
        return None

    def set_sort(self, sort_key, reverse=False):
        self.sort_key = sort_key
        self.reverse = reverse
        self.beginResetModel()
        self._rebuild_order()
        self.endResetModel()
        # Force header redraw to show the arrow
        self.headerDataChanged.emit(
            Qt.Orientation.Horizontal, 0, self.columnCount() - 1)

    def sort_by_column(self, column):
        """Called from header click. Toggle reverse when same col is clicked
        again, else switch to that column's sort key."""
        sort_key = self._COL_TO_SORT.get(column, self.SORT_NAME)
        if sort_key == self.sort_key:
            self.reverse = not self.reverse
        else:
            self.sort_key = sort_key
            self.reverse = False
        self.beginResetModel()
        self._rebuild_order()
        self.endResetModel()
        self.headerDataChanged.emit(
            Qt.Orientation.Horizontal, 0, self.columnCount() - 1)

    def toggle_reverse(self):
        self.reverse = not self.reverse
        self.beginResetModel()
        self._rebuild_order()
        self.endResetModel()
        self.headerDataChanged.emit(
            Qt.Orientation.Horizontal, 0, self.columnCount() - 1)

    def toggle_tag(self, row):
        e = self.entry_at(row)
        if not e:
            return
        if e.path in self.tagged:
            self.tagged.discard(e.path)
        else:
            self.tagged.add(e.path)
        # Repaint this row across all currently-visible columns. Note
        # we use columnCount() rather than COL_COUNT - the model can
        # be in 4-column or 5-column mode (Folder column hidden in
        # normal listings, shown in search-results mode).
        last_col = max(0, self.columnCount() - 1)
        left = self.index(row, 0)
        right = self.index(row, last_col)
        self.dataChanged.emit(left, right,
            [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole])

    def invert_tags(self):
        all_paths = {e.path for e in self.entries}
        self.tagged = all_paths - self.tagged
        last_col = max(0, self.columnCount() - 1)
        top = self.index(0, 0)
        bot = self.index(max(0, len(self.order) - 1), last_col)
        self.dataChanged.emit(top, bot,
            [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole])

    def clear_tags(self):
        if not self.tagged:
            return
        self.tagged.clear()
        last_col = max(0, self.columnCount() - 1)
        top = self.index(0, 0)
        bot = self.index(max(0, len(self.order) - 1), last_col)
        self.dataChanged.emit(top, bot,
            [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole])

    def tagged_paths(self):
        return [e.path for e in self.entries if e.path in self.tagged]

    def _rebuild_order(self):
        entries = self.entries
        if self.sort_key == self.SORT_SIZE:
            key = lambda i: entries[i].size
        elif self.sort_key == self.SORT_TIME:
            key = lambda i: entries[i].mtime
        elif self.sort_key == self.SORT_EXT:
            key = lambda i: (Path(entries[i].name).suffix.lower(),
                             entries[i].name.lower())
        elif self.sort_key == self.SORT_FOLDER:
            # Sort by source folder, then by filename within folder
            key = lambda i: ((entries[i].source_dir or "").lower(),
                              entries[i].name.lower())
        else:
            key = lambda i: entries[i].name.lower()
        dir_idx = [i for i, e in enumerate(entries) if e.is_dir]
        file_idx = [i for i, e in enumerate(entries) if not e.is_dir]
        dir_idx.sort(key=key, reverse=self.reverse)
        file_idx.sort(key=key, reverse=self.reverse)
        self.order = dir_idx + file_idx


# ---------------------------------------------------------------------
# 8.3 / DOS-style filename detection
# ---------------------------------------------------------------------
# Used by the Lister filter "Hide 8+3 names" - useful when preparing
# files for BBS upload (AmiExpress, classic DOS systems) where you
# only want to see the long-filename files that still need shortening.
#
# Rules for "fits 8.3":
#   - If no dot: name <= 8 characters
#   - If exactly one dot: name part <= 8 chars AND ext part <= 3 chars
#   - More than one dot (e.g. foo.tar.gz): does NOT fit 8.3
#   - Empty name or pure dot file (".bashrc"): doesn't fit 8.3 either
#   - We do NOT enforce DOS character restrictions (uppercase only,
#     no spaces, no Unicode) - too aggressive. Length is the
#     useful filter here.
# Directories work the same way (no extension expected) - a dir named
# "MyLongDir" is non-8.3 too, since DOS dirs were also capped at 8.

def fits_dos_83(name: str, is_dir: bool = False) -> bool:
    """Return True if `name` would fit the classic DOS 8.3 limit.

    is_dir is informational: directories follow the same rule (no dot
    expected, name <= 8 chars). Files with no extension are treated
    like dirs here - just the name length matters.
    """
    if not name:
        return False
    # No dot: pure name. Fits if <= 8 chars.
    if '.' not in name:
        return len(name) <= 8
    # Some dot. Split on LAST dot for ext.
    base, _, ext = name.rpartition('.')
    if not base:
        # Hidden-file style ".bashrc" - no name part, just ext.
        # Not 8.3 conform.
        return False
    if '.' in base:
        # Multiple dots like foo.tar.gz -> base="foo.tar" still has
        # a dot. DOS 8.3 allows only one dot total.
        return False
    return len(base) <= 8 and len(ext) <= 3
