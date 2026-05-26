"""
FileLister widget - pure file list, no drive buttons inside.

Drive buttons live in column 0 of the main button bank.

Keys (handled by eventFilter on the QListView):
  TAB       -> emit tab_pressed (main switches active side)
  SPACE     -> tag/untag current row, move cursor down
  *         -> invert all tags
  BACKSPACE -> parent directory
  ENTER     -> open / enter
"""
import os
import platform
import shutil
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListView, QTreeView, QHeaderView, QAbstractItemView,
    QMenu, QInputDialog, QMessageBox
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, WB_TITLEBAR_ACTIVE_QSS,
    INFOBAR_QSS, PATH_EDIT_QSS, LISTER_QSS, SCROLLBAR_QSS,
    button_qss, get_topaz_font, fmt_size
)
from .dirmodel import DirModel, DirEntry, TaggedItemDelegate
from .readers import TextReader, HexReader
from .config import scaled_font_px


class _DropSourceShim:
    """Quacks like a FileLister enough for ActionDispatcher._do_copy_or_move
    to use it as the source side when files are dropped from outside Quopus
    (e.g. Windows Explorer). It just exposes the dropped paths."""
    class _FakeFs:
        kind = 'local'
        def display_path(self): return ""
    def __init__(self, paths):
        self.fs = self._FakeFs()
        self._paths = [Path(p) for p in paths]
        # If all dropped files share a parent, use it as current_path so
        # the "same dir" guard in _do_copy_or_move works correctly.
        parents = {p.parent for p in self._paths}
        self.current_path = (parents.pop() if len(parents) == 1
                              else self._paths[0].parent)
    def selected_entries(self):
        from .fs_backend import FsEntry
        from datetime import datetime as _dt
        out = []
        for p in self._paths:
            try: st = p.stat()
            except Exception: continue
            try: mt = _dt.fromtimestamp(st.st_mtime)
            except Exception: mt = None
            out.append(FsEntry(
                name=p.name, path=str(p),
                is_dir=p.is_dir(),
                size=st.st_size if not p.is_dir() else 0,
                mtime=mt))
        return out
    def selected_or_tagged(self):
        return list(self._paths)
    def refresh(self):  # nothing to refresh on a shim
        pass


class _DnDTreeView(QTreeView):
    """QTreeView subclass that supports drag & drop of files between
    listers (and to/from external apps like Explorer).
    Drag: builds a MIME object with file URLs from the selected/tagged rows
    Drop: hands the URLs to the owning lister, which copies (default) or
    moves (Shift held) into its current_path.
    """
    def __init__(self, lister):
        super().__init__()
        self.lister = lister
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        # Also enable on the viewport, which is where Qt actually
        # dispatches drag events first.
        self.viewport().setAcceptDrops(True)
        # Don't show the row-highlight drop indicator - drops go to the
        # whole lister regardless of which row the cursor is over.
        self.setDropIndicatorShown(False)
        # Always copy by default; we read modifier state at drop time
        # to switch to move when the user holds Shift.
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._lmb_press_pos = None
        self._lmb_press_on_selection = False

    def mousePressEvent(self, event):
        # Middle-button click anywhere in the file view -> jump to
        # parent directory. Equivalent to Backspace or the Parent
        # button. Behaves the same regardless of where exactly in the
        # view you clicked - much faster than reaching for the keyboard.
        if event.button() == Qt.MouseButton.MiddleButton:
            # Walk up to the FileLister parent and call parent_dir.
            # The view is contained in a FileLister (see lister widget
            # tree); walk up until we find one.
            w = self.parent()
            while w is not None and not hasattr(w, 'parent_dir'):
                w = w.parent() if hasattr(w, 'parent') else None
            if w is not None and hasattr(w, 'parent_dir'):
                w.parent_dir()
            event.accept()
            return
        # Remember left-button press position so mouseMove can detect
        # when the user has dragged far enough to start a drag operation.
        if event.button() == Qt.MouseButton.LeftButton:
            self._lmb_press_pos = event.pos()
            idx = self.indexAt(event.pos())
            self._lmb_press_on_selection = (
                idx.isValid()
                and self.selectionModel().isSelected(idx))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Shift+Double-Click on a file: open its `.comment` sidecar
        if one exists. This is meant for browsing files that have
        been annotated via the Comment action (act_comment). Plain
        double-click still works as before (open via dispatch).

        Behaviour:
          - Shift held + double-click on a FILE that has a sidecar
            file at <fullname>.comment -> open the comment in a
            simple text viewer.
          - Shift held + no comment file present -> show a small
            info popup. The plain dispatch is NOT triggered in this
            case to avoid surprising the user.
          - No Shift held -> default Qt behaviour (which fires
            doubleClicked, handled by FileLister._on_double_click).
        """
        if event.button() == Qt.MouseButton.LeftButton \
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            idx = self.indexAt(event.pos())
            if idx.isValid():
                # Walk up to the owning FileLister and let it handle
                # the comment-display. The view doesn't know about the
                # model entries' Path representation, so the lister is
                # the right level for filesystem ops.
                w = self.parent()
                while w is not None and not hasattr(w, '_show_comment_for'):
                    w = w.parent() if hasattr(w, 'parent') else None
                if w is not None and hasattr(w, '_show_comment_for'):
                    w._show_comment_for(idx)
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        # Only initiate a drag if:
        #   - left button is held
        #   - we have a remembered press position
        #   - the press happened ON A SELECTED row (so plain rubber-band
        #     selection on empty area still works)
        #   - the user has moved far enough to count as a drag
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._lmb_press_pos is not None
                and getattr(self, '_lmb_press_on_selection', False)):
            from PyQt6.QtWidgets import QApplication
            dist = (event.pos() - self._lmb_press_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._lmb_press_pos = None
                self._do_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._lmb_press_pos = None
        super().mouseReleaseEvent(event)

    def _do_drag(self):
        """Build a QDrag and execute it. Called from mouseMoveEvent when
        the user has dragged a selected row far enough."""
        paths = self.lister.selected_or_tagged()
        if self.lister.fs.kind != 'local' or not paths:
            return
        from PyQt6.QtCore import QMimeData, QUrl
        from PyQt6.QtGui import QDrag, QPixmap, QPainter, QColor, QFont
        mime = QMimeData()
        urls = []
        for p in paths:
            try:
                ap = p.resolve(strict=False)
            except Exception:
                ap = p
            urls.append(QUrl.fromLocalFile(str(ap)))
        mime.setUrls(urls)
        text_payload = "\n".join(str(p.resolve(strict=False)) for p in paths)
        mime.setText(text_payload)

        drag = QDrag(self)
        drag.setMimeData(mime)

        n = len(paths)
        if n == 1:
            label = paths[0].name
        else:
            label = f"{n} items"
        if len(label) > 40:
            label = label[:37] + "..."
        font = QFont("Topaz-8, Courier New, monospace", 9)
        pix = QPixmap(max(120, len(label) * 8), 22)
        pix.fill(QColor(C.WB_BLUE))
        p = QPainter(pix)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, label)
        p.end()
        drag.setPixmap(pix)

        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                   Qt.DropAction.CopyAction)

    def startDrag(self, supportedActions):
        # Fallback path - shouldn't be hit normally because we drive the
        # drag from mouseMoveEvent.
        self._do_drag()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore(); return
        urls = event.mimeData().urls()
        # Convert to local paths; ignore non-file urls
        srcs = []
        for u in urls:
            if u.isLocalFile():
                srcs.append(Path(u.toLocalFile()))
        if not srcs:
            event.ignore(); return

        # Modifier state decides Copy (default) vs Move (Shift).
        from PyQt6.QtWidgets import QApplication
        mods = QApplication.keyboardModifiers()
        is_move = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # Don't drop into the same dir we came from (meaningless copy)
        target_dir = self.lister.current_path
        if all(s.parent == target_dir for s in srcs) and not is_move:
            event.ignore(); return

        self.lister.handle_dropped_paths(srcs, is_move)
        event.acceptProposedAction()


class FileLister(QWidget):
    path_changed = pyqtSignal(str)
    makedir_requested = pyqtSignal(object)
    got_focus = pyqtSignal(object)
    tab_pressed = pyqtSignal(object)
    add_drive_requested = pyqtSignal(str, str)  # (label, path)
    # Emitted when the user wants to bookmark a current FTP location
    # as a drive button. Carries a partial dict (host/port/user/...)
    # which the main window passes to the FTP-bookmark dialog so the
    # user can confirm + add a label before saving.
    add_ftp_bookmark_requested = pyqtSignal(dict)

    def __init__(self, initial_path, side_label="QUOPUS.1"):
        super().__init__()
        self.current_path = Path(initial_path).expanduser().resolve()
        self.side_label = side_label
        # Filesystem backend - local by default, can be swapped to remote
        from .fs_backend import LocalFs
        self.fs = LocalFs(self.current_path)
        self.history = deque(maxlen=64)
        self.forward_stack = []
        self.model = DirModel(self)
        # Apply the size-display setting from the global config so
        # the Size column uses C64 disk-blocks (256B = 1 block) when
        # the user has switched to that view, instead of bytes.
        try:
            mw = self.window()
            if mw and hasattr(mw, 'config'):
                self.model.show_blocks = (
                    mw.config.get('size_display', 'bytes') == 'blocks')
        except Exception:
            pass
        self.is_active = False

        # Filter: wenn True, blendet das Listing alle Files aus die
        # in das DOS-8+3-Schema passen (Name <= 8 + Ext <= 3 mit
        # max 1 Punkt). Praktisch um vor BBS-Upload nur die noch zu
        # benennenden Long-Filename-Files zu sehen. Pro-Lister-State,
        # nicht persistiert.
        self._filter_non_dos83 = False

        # Refresh info bar whenever tagging changes
        self.model.dataChanged.connect(
            lambda *a: self._update_info_bar())

        self._build()
        QTimer.singleShot(0, self.refresh)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        # Title bar
        self.title = QLabel(f" {self.side_label} ")
        self.title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        self.title.setFixedHeight(20)
        outer.addWidget(self.title)

        # Path / nav row
        nav_row = QHBoxLayout()
        nav_row.setSpacing(1); nav_row.setContentsMargins(0, 0, 0, 0)

        self.btn_back = QPushButton("<"); self.btn_back.setStyleSheet(button_qss("mid"))
        self.btn_back.setFixedWidth(22); self.btn_back.setToolTip("Previous folder")
        self.btn_back.clicked.connect(self.go_back)
        nav_row.addWidget(self.btn_back)

        self.btn_fwd = QPushButton(">"); self.btn_fwd.setStyleSheet(button_qss("mid"))
        self.btn_fwd.setFixedWidth(22); self.btn_fwd.setToolTip("Next folder")
        self.btn_fwd.clicked.connect(self.go_forward)
        nav_row.addWidget(self.btn_fwd)

        self.btn_up = QPushButton("^"); self.btn_up.setStyleSheet(button_qss("mid"))
        self.btn_up.setFixedWidth(22); self.btn_up.setToolTip("Parent")
        self.btn_up.clicked.connect(self.parent_dir)
        nav_row.addWidget(self.btn_up)

        self.path_edit = QLineEdit(str(self.current_path))
        self.path_edit.setStyleSheet(PATH_EDIT_QSS)
        self.path_edit.returnPressed.connect(self._on_path_entered)
        self.path_edit.installEventFilter(self)
        nav_row.addWidget(self.path_edit, 1)
        # Disconnect button: only visible when the lister is in
        # remote mode (FTP or Quopus Drive). One-click way to
        # drop back to the local filesystem without diving into
        # the right-click menu. Hidden by default; toggled by
        # refresh() / set_remote_fs() / disconnect_remote().
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setStyleSheet(button_qss("red"))
        self.btn_disconnect.setFixedWidth(82)
        self.btn_disconnect.setToolTip(
            "Disconnect from the remote filesystem and return "
            "to the local view")
        self.btn_disconnect.clicked.connect(
            lambda: self.disconnect_remote(confirm=True))
        self.btn_disconnect.hide()
        nav_row.addWidget(self.btn_disconnect)
        wrap_nav = QWidget(); wrap_nav.setLayout(nav_row)
        outer.addWidget(wrap_nav)

        # Tree view - multi-column table with resizable headers
        from PyQt6.QtWidgets import QHeaderView
        self.view = _DnDTreeView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(TaggedItemDelegate(self.model, self.view))
        self.view.setStyleSheet(LISTER_QSS + SCROLLBAR_QSS + f"""
            QHeaderView::section {{
                background-color: {C.WB_GREY};
                color: {C.BLACK};
                padding: 2px 6px;
                border: 1px solid {C.BLACK};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(11)}px;
                font-weight: bold;
            }}
            QTreeView {{ show-decoration-selected: 1; }}
            QTreeView::item {{ border: 0; padding: 0 2px; }}
            QTreeView::branch {{ background: transparent; }}
        """)
        self.view.setFont(get_topaz_font(11))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setUniformRowHeights(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setRootIsDecorated(False)
        self.view.setIndentation(0)
        self.view.setAlternatingRowColors(False)
        self.view.setSortingEnabled(False)   # we handle it manually
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setTabKeyNavigation(False)
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._ctx_menu)
        self.view.installEventFilter(self)

        # Header: column widths + click-to-sort
        hdr = self.view.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)   # we render arrows in header text
        hdr.sectionClicked.connect(self._on_header_clicked)
        # Header right-click: column-specific menu. On the Size
        # column it lets the user switch between bytes and C64 blocks.
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._on_header_ctx)
        # Default column widths
        self.view.setColumnWidth(0, 260)   # Name
        self.view.setColumnWidth(1, 54)    # Ext
        self.view.setColumnWidth(2, 80)    # Size
        self.view.setColumnWidth(3, 130)   # Date
        self.view.setColumnWidth(4, 320)   # Folder (search-mode only)
        hdr.setMinimumSectionSize(30)
        # Save widths on resize
        hdr.sectionResized.connect(self._on_section_resized)

        outer.addWidget(self.view, 1)

        # Update info bar when the user changes the mouse selection
        sel_model = self.view.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(
                lambda *a: self._update_info_bar())

        self.info_bar = QLabel("")
        self.info_bar.setStyleSheet(INFOBAR_QSS)
        self.info_bar.setFixedHeight(18)
        outer.addWidget(self.info_bar)

    def _on_header_clicked(self, logical_index):
        self.model.sort_by_column(logical_index)
        self._save_sort_state()

    def _on_header_ctx(self, pos):
        """Right-click on a column header - context menu with options
        relevant to the clicked column. The Size column gets the
        bytes/blocks toggle; all columns get a quick sort menu."""
        from .dirmodel import COL_SIZE, COL_NAME, COL_DATE, COL_EXT
        from PyQt6.QtWidgets import QMenu
        # The view is a QTreeView - its header is `header()`, not
        # `horizontalHeader()` (that's for QTableView).
        hdr = self.view.header()
        col = hdr.logicalIndexAt(pos)
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {C.WB_GREY}; color: {C.BLACK};
                     border: 1px solid {C.BLACK}; }}
            QMenu::item:selected {{ background-color: {C.SELECTED};
                                     color: {C.WHITE}; }}
        """)
        # Size column specifics first - this is what Mario actually
        # wanted: a way to switch to C64 disk-blocks.
        if col == COL_SIZE:
            cur = "blocks" if self.model.show_blocks else "bytes"
            a_bytes = menu.addAction(
                ("✓ " if cur == "bytes" else "    ")
                + "Size in bytes (4K, 1.2M, ...)")
            a_blocks = menu.addAction(
                ("✓ " if cur == "blocks" else "    ")
                + "Size in C64 blocks (256 B = 1 block)")
            menu.addSeparator()
        else:
            a_bytes = a_blocks = None
        # Sort options - apply to whichever column was clicked
        a_sort = menu.addAction(f"Sort by this column")
        a_reverse = menu.addAction("Toggle reverse sort order")
        menu.addSeparator()
        # Filter "Hide 8+3 names": gilt unabhaengig welche Spalte
        # angeklickt wurde, ist aber im Namen-Kontext am naheliegend-
        # sten. Checkbox-Optik via Haekchen-Prefix.
        a_filter83 = menu.addAction(
            ("\u2713 " if self._filter_non_dos83 else "    ")
            + "Hide 8+3 filenames (DOS conform)")
        menu.addSeparator()
        # Always offer the Size toggle from any column header so the
        # user finds the option even if they right-click on Name or
        # Date by mistake.
        if col != COL_SIZE:
            cur = "blocks" if self.model.show_blocks else "bytes"
            a_size_menu = menu.addMenu("Size column display")
            a_bytes2 = a_size_menu.addAction(
                ("✓ " if cur == "bytes" else "    ")
                + "Bytes (4K, 1.2M, ...)")
            a_blocks2 = a_size_menu.addAction(
                ("✓ " if cur == "blocks" else "    ")
                + "C64 blocks (256 B)")
        else:
            a_bytes2 = a_blocks2 = None
        chosen = menu.exec(hdr.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is a_bytes or chosen is a_bytes2:
            self._set_size_display("bytes")
        elif chosen is a_blocks or chosen is a_blocks2:
            self._set_size_display("blocks")
        elif chosen is a_sort:
            self.model.sort_by_column(col)
            self._save_sort_state()
        elif chosen is a_reverse:
            self.model.toggle_reverse()
            self._save_sort_state()
        elif chosen is a_filter83:
            self.toggle_non_dos83()

    def _set_size_display(self, mode: str):
        """Switch the Size column between 'bytes' and 'blocks'.
        Persists to global config and applies to BOTH listers via
        the main-window broadcast helper, so they stay consistent."""
        if mode not in ("bytes", "blocks"):
            return
        try:
            w = self.window()
            if hasattr(w, '_apply_size_display'):
                w._apply_size_display(mode)
            else:
                # Fallback for unparented test usage
                self.model.show_blocks = (mode == "blocks")
                self.model.layoutChanged.emit()
        except Exception:
            pass

    def toggle_non_dos83(self):
        """Toggle den 'Hide 8+3 filenames' Filter.

        Beim ersten Aktivieren werden alle Files mit klassischem DOS-
        Schema (Name <= 8 + Ext <= 3) versteckt - sichtbar bleiben nur
        Files die noch eine "lange" Form haben. Praktisch vor BBS-
        Upload, wo die langnamigen erst gekuerzt werden muessen.

        Beim Deaktivieren zeigt das Listing wieder alles.

        Wir refreshen das Listing damit der Filter sofort greift -
        sonst wuerde der Wechsel erst beim naechsten Directory-Wechsel
        sichtbar.
        """
        self._filter_non_dos83 = not self._filter_non_dos83
        try:
            self.refresh()
        except Exception:
            pass

    def _save_sort_state(self):
        """Persist current sort key + reverse to the window's config.
        Stored per side so left and right can sort independently."""
        try:
            w = self.window()
            if not hasattr(w, 'config'): return
            cfg = w.config.setdefault('sort_state', {})
            cfg[self.side_label] = {
                'key': int(self.model.sort_key),
                'reverse': bool(self.model.reverse),
            }
            from .config import save_config
            save_config(w.config)
        except Exception:
            pass

    def apply_sort_state(self, state):
        """Restore sort key + reverse direction from config."""
        if not state: return
        try:
            key = int(state.get('key', self.model.SORT_NAME))
            rev = bool(state.get('reverse', False))
            self.model.sort_key = key
            self.model.reverse = rev
            self.model.beginResetModel()
            self.model._rebuild_order()
            self.model.endResetModel()
            self.model.headerDataChanged.emit(
                Qt.Orientation.Horizontal, 0, self.model.columnCount() - 1)
        except Exception:
            pass

    def _on_section_resized(self, logical_index, old_size, new_size):
        """Persist column widths to the window's config when the user drags.
        Stored per side so left and right can have independent widths."""
        try:
            w = self.window()
            if not hasattr(w, 'config'): return
            all_widths = w.config.setdefault('column_widths', {})
            # Backwards compat: old configs stored a flat {col: width}.
            # New format is {side_label: {col: width}}. Convert if needed.
            if all_widths and not isinstance(
                    next(iter(all_widths.values())), dict):
                # Old flat dict - migrate to per-side
                old = dict(all_widths)
                all_widths.clear()
                all_widths['QUOPUS.1'] = dict(old)
                all_widths['QUOPUS.2'] = dict(old)
            side = all_widths.setdefault(self.side_label, {})
            side[str(logical_index)] = new_size
            from .config import save_config
            save_config(w.config)
        except Exception:
            pass

    def apply_column_widths(self, widths):
        """Restore column widths from a dict.
        Accepts both old flat format {col: width} and new
        per-side format {side_label: {col: width}}.
        For backwards compatibility with the pre-rename builds,
        we also accept legacy DOPUS.1 / DOPUS.2 keys and map them
        to the current QUOPUS.* labels."""
        if not widths: return
        # Pick the right sub-dict if per-side
        if widths and isinstance(next(iter(widths.values())), dict):
            sub = widths.get(self.side_label)
            if sub is None:
                # Legacy fallback: try DOPUS.N for QUOPUS.N
                legacy_key = self.side_label.replace("QUOPUS", "DOPUS")
                sub = widths.get(legacy_key, {})
            widths = sub
        if not widths: return
        for k, v in widths.items():
            try:
                col = int(k)
                if 0 <= col < self.model.columnCount() and v > 10:
                    self.view.setColumnWidth(col, v)
            except Exception:
                pass

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.got_focus.emit(self)

        view = getattr(self, 'view', None)
        if view is not None and obj is view and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()

            if key == Qt.Key.Key_Tab or key == Qt.Key.Key_Backtab:
                self.tab_pressed.emit(self)
                return True

            if key == Qt.Key.Key_Space and not mods:
                idx = view.currentIndex()
                if idx.isValid():
                    self.model.toggle_tag(idx.row())
                    view.viewport().update()  # force repaint with delegate
                    next_row = idx.row() + 1
                    if next_row < self.model.rowCount():
                        next_idx = self.model.index(next_row, 0)
                        view.setCurrentIndex(next_idx)
                        view.scrollTo(next_idx)
                return True

            if key == Qt.Key.Key_Asterisk:
                self.model.invert_tags()
                view.viewport().update()
                return True

            if key == Qt.Key.Key_Backspace:
                self.parent_dir()
                return True

            # Escape disconnects an active FTP session
            if key == Qt.Key.Key_Escape and not mods:
                if self.fs.kind == 'remote':
                    self.disconnect_remote()
                    return True

            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                idx = view.currentIndex()
                if idx.isValid():
                    self._on_double_click(idx)
                return True

        return super().eventFilter(obj, event)

    def set_active(self, active: bool):
        """Toggle the active visual state. Tags remain visible either way;
        only the titlebar color changes to indicate source/target."""
        if self.is_active == active:
            return
        self.is_active = active
        self.title.setStyleSheet(
            WB_TITLEBAR_ACTIVE_QSS if active else WB_TITLEBAR_INACTIVE_QSS
        )
        # Clear Qt-level selection on the lister losing focus so the
        # blue selection highlight doesn't obscure tag highlighting.
        # Tags themselves remain in self.model.tagged - they stay visible.
        if not active:
            self.view.clearSelection()

    def focus_list(self):
        self.view.setFocus(Qt.FocusReason.TabFocusReason)
        self.got_focus.emit(self)

    def _on_path_entered(self):
        np = self.path_edit.text().strip()
        if np == "~":
            np = str(Path.home())
        # Guard: in local mode, ignore FTP-looking paths (left over from a
        # previous remote session) - bounce to home so user isn't stuck.
        if self.fs.kind == 'local' and (
            np.startswith(("ftp://", "ftps://", "sftp://"))
            or "://" in np):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Quopus",
                "That looks like an FTP path but no FTP connection is "
                "active. Use Ctrl+F to connect.")
            self.path_edit.setText(str(self.current_path))
            return
        self.goto(np)

    # Errors that indicate a lost/broken connection - trigger auto-disconnect
    _CONN_DEAD_MARKERS = (
        "10054",        # Windows connection reset by peer
        "10053",        # Windows connection aborted
        "10060",        # Windows timeout
        "10061",        # connection refused
        "10038",        # socket not a socket (already closed)
        "EOF",
        "Connection reset",
        "Connection closed",
        "Connection aborted",
        "Broken pipe",
        "timed out",
        "not connected",
        "421 ",         # FTP "service not available" - sent before disconnect
        "Socket is closed",
    )

    def _is_connection_lost_error(self, exc):
        """Check if an exception indicates the FTP connection is dead."""
        import socket
        if isinstance(exc, (EOFError, ConnectionError,
                             socket.timeout, OSError)):
            # OSError covers WinError 10054 etc.
            return True
        msg = str(exc)
        return any(m in msg for m in self._CONN_DEAD_MARKERS)

    def _handle_remote_error(self, exc, context=""):
        """Central handler for errors during remote operations.
        If the connection is dead, offer to drop back to local mode.
        Returns True if we handled it (caller should bail out),
        False if it was a normal error the caller should display."""
        if self.fs.kind != 'remote':
            return False
        if not self._is_connection_lost_error(exc):
            return False
        # Re-entrancy guard: if we're already handling a disconnect,
        # just bail out without another dialog.
        if getattr(self, '_handling_disconnect', False):
            return True
        self._handling_disconnect = True
        try:
            reply = QMessageBox.warning(
                self,
                "FTP connection lost",
                f"{context}\n\nThe FTP connection has been lost:\n{exc}\n\n"
                "Return to local filesystem?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.disconnect_remote(confirm=False)
        finally:
            self._handling_disconnect = False
        return True

    def goto(self, new_path, push_history=True):
        if self.fs.kind == 'remote':
            # Remote navigation
            try:
                old_cwd = self.fs.pwd()
                self.fs.cd(str(new_path))
                if push_history and old_cwd != self.fs.pwd():
                    self.history.append(old_cwd)
                    self.forward_stack.clear()
                self.refresh()
                self.path_changed.emit(self.fs.pwd())
            except Exception as e:
                if not self._handle_remote_error(e, "Cannot cd"):
                    QMessageBox.warning(self, "Quopus (remote)",
                                         f"Cannot cd: {e}")
            return
        try:
            p = Path(new_path).expanduser().resolve()
            if p.is_dir():
                if push_history and p != self.current_path:
                    self.history.append(self.current_path)
                    self.forward_stack.clear()
                self.current_path = p
                # Keep LocalFs in sync
                from .fs_backend import LocalFs
                self.fs = LocalFs(p)
                self.refresh()
                self.path_changed.emit(str(p))
            else:
                QMessageBox.warning(self, "Quopus", f"Not a directory:\n{p}")
        except Exception as e:
            QMessageBox.critical(self, "Quopus", f"Error: {e}")

    def go_back(self):
        if not self.history: return
        prev = self.history.pop()
        if self.fs.kind == 'remote':
            self.forward_stack.append(self.fs.pwd())
            try: self.fs.cd(prev)
            except Exception: return
            self.refresh(); self.path_changed.emit(prev)
        else:
            self.forward_stack.append(self.current_path)
            self.current_path = prev
            from .fs_backend import LocalFs
            self.fs = LocalFs(prev)
            self.refresh()
            self.path_changed.emit(str(prev))

    def go_forward(self):
        if not self.forward_stack: return
        nxt = self.forward_stack.pop()
        if self.fs.kind == 'remote':
            self.history.append(self.fs.pwd())
            try: self.fs.cd(nxt)
            except Exception: return
            self.refresh(); self.path_changed.emit(nxt)
        else:
            self.history.append(self.current_path)
            self.current_path = nxt
            from .fs_backend import LocalFs
            self.fs = LocalFs(nxt)
            self.refresh()
            self.path_changed.emit(str(nxt))

    def parent_dir(self):
        if self.fs.kind == 'remote':
            cur = self.fs.pwd()
            if cur not in ('/', ''):
                self.goto('..')
            return
        p = self.current_path.parent
        if p != self.current_path:
            self.goto(str(p))
        else:
            # Already at root - show drive picker
            self._show_drive_picker()

    def _show_drive_picker(self):
        """Popup menu with available drives / mount points."""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {C.WB_GREY}; color: {C.BLACK};
                    border: 1px solid {C.BLACK};
                    font-family: "Topaz","Courier New",monospace; }}
            QMenu::item {{ padding: 3px 20px; }}
            QMenu::item:selected {{ background-color: {C.SELECTED}; color: {C.WHITE}; }}
        """)

        if platform.system() == "Windows":
            # Windows: enumerate drive letters A:-Z: that actually exist
            import string
            for letter in string.ascii_uppercase:
                drive = f"{letter}:/"
                if Path(drive).exists():
                    # Try to get volume label for nicer display
                    label = drive
                    try:
                        import ctypes
                        buf = ctypes.create_unicode_buffer(261)
                        fs_buf = ctypes.create_unicode_buffer(261)
                        ret = ctypes.windll.kernel32.GetVolumeInformationW(
                            ctypes.c_wchar_p(drive), buf, 260,
                            None, None, None, fs_buf, 260)
                        if ret and buf.value:
                            label = f"{letter}: ({buf.value})"
                        else:
                            label = f"{letter}:\\"
                    except Exception:
                        label = f"{letter}:\\"
                    menu.addAction(label,
                        lambda d=drive: self.goto(d, push_history=True))
        else:
            # Unix / macOS: offer common mount points + psutil partitions
            mounts = ["/"]
            for extra in ("/home", "/mnt", "/media", "/Volumes", "/tmp"):
                if Path(extra).is_dir():
                    mounts.append(extra)
            try:
                import psutil
                for p in psutil.disk_partitions(all=False):
                    if p.mountpoint not in mounts:
                        mounts.append(p.mountpoint)
            except Exception:
                pass
            for m in mounts:
                menu.addAction(m, lambda d=m: self.goto(d, push_history=True))

        menu.addSeparator()
        menu.addAction("Home", lambda: self.goto(str(Path.home())))
        menu.addAction("Cancel", lambda: None)

        # Position menu under the path bar
        pos = self.btn_up.mapToGlobal(self.btn_up.rect().bottomLeft())
        menu.exec(pos)

    def root_dir(self):
        if platform.system() == "Windows":
            drive = self.current_path.drive or "C:"
            self.goto(drive + "/")
        else:
            self.goto("/")

    def refresh(self):
        self.path_edit.setText(self.fs.display_path())
        if self.fs.kind == 'remote':
            self.title.setText(f" {self.side_label}:  [REMOTE]  {self.fs.display_path()} ")
            # Show disconnect button + adapt tooltip to remote kind
            label_hint = getattr(self.fs, 'label', '') or ''
            if label_hint.startswith('qdrive://'):
                self.btn_disconnect.setToolTip(
                    f"Disconnect Quopus Drive\n({label_hint})")
            else:
                self.btn_disconnect.setToolTip(
                    f"Disconnect FTP\n({label_hint or 'remote'})")
            self.btn_disconnect.show()
        else:
            self.title.setText(f" {self.side_label}: {self.current_path} ")
            self.btn_disconnect.hide()

        entries = []
        total_size = 0
        n_dirs = 0
        n_files = 0
        try:
            fs_entries = self.fs.list()
        except PermissionError:
            QMessageBox.warning(self, "Quopus", "Permission denied")
            self.model.clear(); return
        except Exception as ex:
            if self._handle_remote_error(ex, "Directory listing failed"):
                return
            QMessageBox.warning(self, "Quopus", str(ex))
            self.model.clear(); return

        for e in fs_entries:
            entries.append(DirEntry(e.name, e.path, e.is_dir,
                                     e.size, e.mtime or 0,
                                     getattr(e, 'source_dir', None)))

        # Filter "Hide 8+3 filenames" - blendet alle Files (und Dirs)
        # aus deren Name ins klassische DOS-Schema passt. Wir countn
        # NACH dem Filter damit Info-Bar und Listing konsistent sind.
        if self._filter_non_dos83:
            from .dirmodel import fits_dos_83
            entries = [e for e in entries
                         if not fits_dos_83(e.name, e.is_dir)]

        # Recount nach Filter
        for e in entries:
            if e.is_dir: n_dirs += 1
            else: n_files += 1; total_size += e.size

        self.model.set_entries(entries)
        # Cache totals for the info-bar (used by _update_info_bar when tags
        # change, without re-walking the filesystem)
        self._stat_n_dirs = n_dirs
        self._stat_n_files = n_files
        self._stat_total_size = total_size
        self._update_info_bar()
        self.btn_back.setEnabled(bool(self.history))
        self.btn_fwd.setEnabled(bool(self.forward_stack))

    def _update_info_bar(self):
        """Refresh the info bar based on cached totals + current tags AND
        mouse selection. Format mirrors classic Quopus 4:
            "X of Y dirs, A of B files, Z of T bytes"
        when something is tagged or selected; falls back to plain totals
        otherwise."""
        n_dirs  = getattr(self, '_stat_n_dirs', 0)
        n_files = getattr(self, '_stat_n_files', 0)
        total_size = getattr(self, '_stat_total_size', 0)

        # Build the set of "active" entries: union of tagged + mouse-selected
        active_paths = set(self.model.tagged_paths())
        try:
            sel_model = self.view.selectionModel()
            if sel_model is not None:
                for idx in sel_model.selectedRows():
                    e = self.model.entry_at(idx.row())
                    if e is not None:
                        active_paths.add(e.path)
        except Exception:
            pass

        t_dirs = t_files = t_size = 0
        if active_paths:
            for e in self.model.entries:
                if e.path not in active_paths:
                    continue
                if e.is_dir:
                    t_dirs += 1
                else:
                    t_files += 1
                    t_size += e.size

        suffix = ""
        if self.fs.kind == 'remote':
            suffix = "  |  [FTP]  Esc to disconnect"

        if active_paths:
            text = (f" {t_dirs} of {n_dirs} dirs, "
                    f"{t_files} of {n_files} files, "
                    f"{fmt_size(t_size)} of {fmt_size(total_size)}"
                    f"{suffix} ")
        else:
            text = (f" {n_dirs} dirs, {n_files} files, "
                    f"{fmt_size(total_size)}{suffix} ")
        self.info_bar.setText(text)

    def _set_sort(self, key):
        """Used by Ctrl+F3..F6 hotkeys."""
        self.model.set_sort(key, self.model.reverse)

    def _toggle_reverse(self):
        self.model.toggle_reverse()

    def _on_double_click(self, index):
        e = self.model.entry_at(index.row())
        if not e: return
        if self.fs.kind == 'remote':
            if e.is_dir:
                # Use just the name - backend will handle relative cd
                self.goto(e.name)
            else:
                # Download to temp and open
                self._view_remote_file(e)
            return
        p = Path(e.path)
        if e.is_dir:
            self.goto(str(p))
            return
        # Note: multi-SID parallel mode is NOT triggered by
        # double-click - it would be too ambiguous (if you tag a
        # bunch of SIDs to copy them somewhere, you don't want a
        # double-click to suddenly start them all in parallel).
        # Use right-click → "Play as multi-SID" instead, where the
        # intent is explicit.
        self._dispatch_view(p)

    def _show_comment_for(self, index):
        """Shift+Double-Click handler. Open the .comment sidecar of
        the file at `index` in a small text viewer. If no comment
        exists, show a tiny info popup and offer to create one."""
        e = self.model.entry_at(index.row())
        if not e:
            return
        if e.is_dir:
            # Comments only make sense for files, not directories
            return
        if self.fs.kind == 'remote':
            # Remote files don't have local sidecar comments. We could
            # download them on-the-fly but that's a separate feature -
            # for now just tell the user it's local-only.
            QMessageBox.information(
                self, "Comment",
                "Comment files are stored locally as <name>.comment.\n"
                "This feature works for local files only.")
            return
        p = Path(e.path)
        comment_path = p.with_suffix(p.suffix + ".comment")
        if not comment_path.exists():
            # No comment yet - offer to create one. Doing nothing
            # would leave the user wondering whether the shortcut
            # worked at all.
            reply = QMessageBox.question(
                self, "Comment",
                f"No comment file exists for:\n  {p.name}\n\n"
                f"Create one now? (saves to {comment_path.name})",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                text, ok = QInputDialog.getMultiLineText(
                    self, "New comment",
                    f"Comment for {p.name}:", "")
                if ok and text.strip():
                    try:
                        comment_path.write_text(text, encoding="utf-8")
                    except Exception as ex:
                        QMessageBox.warning(
                            self, "Comment", f"Could not save: {ex}")
            return
        # Comment exists - read and display in a non-modal viewer.
        try:
            content = comment_path.read_text(encoding="utf-8",
                                              errors="replace")
        except Exception as ex:
            QMessageBox.warning(self, "Comment",
                                  f"Could not read comment: {ex}")
            return
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QPlainTextEdit, QDialogButtonBox)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Comment: {p.name}")
        dlg.resize(600, 360)
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(6, 6, 6, 6)
        # Header showing which file's comment this is
        hdr = QLabel(f"  {p.name}.comment  "
                       f"({comment_path.stat().st_size} bytes)  ")
        hdr.setStyleSheet(
            f"QLabel {{ background-color: {C.SELECTED}; "
            f"color: {C.WHITE}; padding: 4px; "
            f"font-family: 'Topaz','Courier New',monospace; }}")
        lay.addWidget(hdr)
        edit = QPlainTextEdit()
        edit.setPlainText(content)
        edit.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {C.BLACK}; "
            f"color: {C.WHITE}; "
            f"font-family: 'Topaz','Courier New',monospace; "
            f"font-size: {scaled_font_px(11)}px; padding: 4px; }}")
        lay.addWidget(edit, 1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close)
        def _save():
            try:
                comment_path.write_text(edit.toPlainText(),
                                         encoding="utf-8")
                dlg.accept()
            except Exception as ex:
                QMessageBox.warning(dlg, "Save",
                                      f"Could not save: {ex}")
        bb.button(QDialogButtonBox.StandardButton.Save).clicked.connect(_save)
        bb.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _view_remote_file(self, entry):
        """Download a remote file to a persistent temp dir, dispatch to
        viewer. The temp file is NOT deleted right away - external
        programs may open it asynchronously. Cleanup happens on
        disconnect_remote() or app exit."""
        import tempfile
        # Per-session temp dir (created once per lister)
        if not hasattr(self, '_remote_tmpdir') or self._remote_tmpdir is None:
            self._remote_tmpdir = Path(tempfile.mkdtemp(prefix="dopus_ftp_"))
            self._remote_tmpfiles = []
        tmp = self._remote_tmpdir / entry.name
        # If same-named file already downloaded, overwrite
        try:
            self.fs.download_to(entry.name, tmp, size=entry.size)
        except Exception as e:
            if self._handle_remote_error(e, f"Download failed: {entry.name}"):
                return
            QMessageBox.warning(self, "Download", f"{entry.name}: {e}")
            return
        self._remote_tmpfiles.append(tmp)
        # Dispatch via the same logic used for local files
        try:
            self._auto_open(tmp)
        except Exception as e:
            QMessageBox.warning(self, "Open", f"Cannot open {entry.name}: {e}")

    def _cleanup_remote_tmp(self):
        """Delete all downloaded-for-view temp files.
        Safe to call multiple times."""
        if not hasattr(self, '_remote_tmpdir') or self._remote_tmpdir is None:
            return
        import shutil
        try:
            shutil.rmtree(self._remote_tmpdir, ignore_errors=True)
        except Exception:
            pass
        self._remote_tmpdir = None
        self._remote_tmpfiles = []

    def set_remote_fs(self, backend, connection_label):
        """Switch this lister to a remote filesystem (connected FTP backend)."""
        from .fs_backend import RemoteFs
        # Clear history when switching contexts
        self.history.clear()
        self.forward_stack.clear()
        self.fs = RemoteFs(backend, connection_label)
        self.refresh()
        self.path_changed.emit(self.fs.pwd())

    def set_search_results_fs(self, search_root: Path,
                                 label: str, files: list):
        """Show a flat list of search-result files in this lister.
        The original directory layout is replaced with a virtual
        view; each entry shows its source folder in the new Folder
        column. Use disconnect_search() (or right-click → Close
        search results) to return to normal browsing.

        files: list of pathlib.Path entries from FindDialog."""
        from .fs_backend import SearchResultsFs
        # Save current local path so disconnect_search can return there
        self._pre_search_path = (self.current_path
                                  if self.fs.kind == 'local' else None)
        self.history.clear()
        self.forward_stack.clear()
        self.fs = SearchResultsFs(search_root, label, files)
        # Tell the model to show the Folder column
        self.model.show_folder_column = True
        # Force a full layout update so the new column appears in
        # the view's header. modelReset is the heavy-handed way -
        # the table view will requery columnCount() and refresh.
        self.model.beginResetModel()
        self.model.endResetModel()
        self.refresh()
        # Title bar shows search context with a close hint
        self.title.setText(
            f" {self.side_label}: 🔎 {label}  "
            f"({len(files)} files - right-click for Close search) ")
        self.path_edit.setText(self.fs.display_path())
        self.path_changed.emit(self.fs.display_path())

    def disconnect_search(self):
        """Exit search-results mode and return to a normal local
        listing. Goes back to the directory the user was in when
        they opened the search dialog, if known."""
        if self.fs.kind != 'search':
            return
        from .fs_backend import LocalFs
        target = (self._pre_search_path
                  if getattr(self, '_pre_search_path', None) is not None
                  else Path.home())
        self.current_path = target
        self.fs = LocalFs(target)
        self.history.clear()
        self.forward_stack.clear()
        # Hide the Folder column again
        self.model.show_folder_column = False
        self.model.beginResetModel()
        self.model.endResetModel()
        self.path_edit.setText(str(target))
        self.title.setText(f" {self.side_label}: {target} ")
        try:
            self.refresh()
        except Exception:
            pass
        self.path_changed.emit(str(target))

    def disconnect_remote(self, confirm=True):
        """Switch back from remote to the local filesystem.
        confirm=True  -> ask the user first (for user-initiated disconnects)
        confirm=False -> just do it (after a connection-lost event)"""
        if self.fs.kind != 'remote':
            return
        if confirm:
            label = self.fs.label if hasattr(self.fs, 'label') else ''
            # Title adapts to the remote-backend kind so the
            # dialog isn't misleading - QDrive mounts say
            # "Disconnect Quopus Drive", FTP says "Disconnect FTP".
            if label.startswith('qdrive://'):
                disc_title = "Disconnect Quopus Drive"
            else:
                disc_title = "Disconnect FTP"
            reply = QMessageBox.question(
                self, disc_title,
                f"Disconnect from {label} and return to local filesystem?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            self.fs.close()
        except Exception:
            pass
        # Clean up temp files downloaded for viewing during this session
        self._cleanup_remote_tmp()
        from .fs_backend import LocalFs
        self.current_path = Path.home()
        self.fs = LocalFs(self.current_path)
        self.history.clear()
        self.forward_stack.clear()
        # Clear visuals immediately so stale FTP path text doesn't persist
        self.path_edit.setText(str(self.current_path))
        self.title.setText(f" {self.side_label}: {self.current_path} ")
        self.model.clear()
        try:
            self.refresh()
        except Exception:
            # If even local refresh fails (e.g. home unreadable) leave the
            # lister empty rather than crashing
            pass
        self.path_changed.emit(str(self.current_path))

    # ------------------------------------------------------------------
    # FTP "save current state" helpers - wired to the right-click
    # context menu when the lister is in remote mode.
    # ------------------------------------------------------------------
    def _ftp_find_bookmark(self, label):
        """Look up a saved FTP bookmark by name in the main config.
        Returns (config_dict, bookmark_dict, index) or (cfg, None, -1)
        if not found. Centralised so the three menu actions share the
        same lookup and error message."""
        main = self.window()
        cfg = getattr(main, 'config', None)
        if cfg is None:
            return None, None, -1
        bms = cfg.get('ftp_bookmarks', [])
        for i, b in enumerate(bms):
            if b.get('name') == label:
                return cfg, b, i
        return cfg, None, -1

    def _ftp_no_bookmark_warning(self, label):
        """Show a uniform warning for the case when the active FTP
        connection wasn't started from a saved bookmark, so we have
        nothing persistent to attach the requested change to."""
        QMessageBox.information(
            self, "FTP",
            f"This connection is not tied to a saved bookmark "
            f"(label: '{label}'). Open the FTP connect dialog "
            f"(Strg+F), fill in 'Save as:' and click 'Save Bookmark' "
            f"first - then this menu entry can find the bookmark "
            f"to update.")

    def _ftp_save_cwd_to_bookmark(self, label, pwd):
        """Persist the current remote directory as the bookmark's
        default 'remote_path'. Next time the user connects via that
        bookmark (drive button, action button, ftp_site, ftp_upload)
        the backend will cwd() into this path automatically."""
        cfg, bm, _ = self._ftp_find_bookmark(label)
        if bm is None:
            self._ftp_no_bookmark_warning(label)
            return
        old = (bm.get('remote_path') or '').strip()
        new = (pwd or '/').strip() or '/'
        if old == new:
            QMessageBox.information(
                self, "FTP bookmark",
                f"Bookmark '{label}' already has '{new}' as its "
                f"default remote dir - nothing to do.")
            return
        bm['remote_path'] = new
        from .config import save_config
        save_config(cfg)
        msg = (f"Bookmark '{label}': default remote dir set to '{new}'"
                + (f" (was '{old}')" if old else ""))
        try:
            self.window()._status(msg)
        except Exception:
            pass
        QMessageBox.information(self, "FTP bookmark", msg)

    def _ftp_add_as_action_button(self, label, action_kind, save_cwd=None):
        """Add the current FTP connection to the 6x6 action-button
        grid. Uses the existing ActionDispatcher helper so we get the
        same cell-picker UI as the connect dialog's checkboxes.

        action_kind: 'ftp_site' (just connect) or 'ftp_upload' (connect
                      + cwd + upload selection from other panel).
        save_cwd:    if given (typical for ftp_upload), also persist
                      the current pwd into the bookmark's remote_path
                      so the upload always lands in this directory.
        """
        cfg, bm, _ = self._ftp_find_bookmark(label)
        if bm is None:
            self._ftp_no_bookmark_warning(label)
            return
        # Optionally update the bookmark's saved cwd first - this is
        # what makes "upload to current dir" stick across sessions
        # rather than only working for the next click.
        if save_cwd:
            new_cwd = (save_cwd or '/').strip() or '/'
            if bm.get('remote_path') != new_cwd:
                bm['remote_path'] = new_cwd
                from .config import save_config
                save_config(cfg)
                try:
                    self.window()._status(
                        f"Bookmark '{label}': default remote dir "
                        f"-> '{new_cwd}'")
                except Exception:
                    pass
        # Build the kw dict the same shape ActionDispatcher's
        # _save_ftp_as_action_button expects from the connect dialog.
        suffix = " upload" if action_kind == 'ftp_upload' else ""
        kw = {
            'name':         label,
            'action_kind':  action_kind,
            'action_label': f"{label}{suffix}",
        }
        try:
            actions = self.window().actions
        except AttributeError:
            QMessageBox.warning(self, "FTP", "Action dispatcher not available.")
            return
        actions._save_ftp_as_action_button(kw)

    # ------------------------------------------------------------------
    # Quopus Drive variants of the FTP "save current state" helpers.
    # We can't share the FTP code 1:1 because:
    #   - QDrive bookmarks live in qdrive_bookmarks.json (loaded via
    #     qdrive_backend.load_bookmarks()), not in cfg['ftp_bookmarks']
    #   - The action that one-click-reconnects is 'qdrive_site', not
    #     'ftp_site'
    #   - The lister's fs.label for a QDrive mount is the qdrive://
    #     URL, not the bookmark name - we have to match it back to a
    #     saved bookmark by host+initial_drive
    # The cell-picker for placing the button on the grid is the same
    # one FTP uses (actions._save_ftp_as_action_button), which has
    # been generalized to accept 'qdrive_site' as a valid action_kind.
    # ------------------------------------------------------------------
    def _qdrive_find_active_bookmark(self):
        """Find the QDrive bookmark matching the currently mounted
        connection. Returns the QDriveBookmark or None if none
        matches (e.g. the user connected via the dialog without
        clicking 'Save Bookmark').

        Matching is on host + drive: a bookmark wins if its
        bookmark.host equals the current connection's host and
        bookmark.initial_drive matches the current drive name (or
        is empty - meaning "any drive on this host" which is fine
        as a fuzzy match).
        """
        if self.fs.kind != 'remote':
            return None
        label_hint = getattr(self.fs, 'label', '') or ''
        if not label_hint.startswith('qdrive://'):
            return None
        # Parse "qdrive://host/drive"
        try:
            tail = label_hint[len('qdrive://'):]
            host, _, drive = tail.partition('/')
        except Exception:
            return None
        if not host:
            return None
        # Also pull the connection object if accessible - more
        # reliable for client_name matching when host is e.g. an
        # IP that differs between two bookmarks.
        conn = getattr(self.fs, '_conn', None)
        active_client = (conn.bookmark.client_name
                         if conn is not None else None)
        try:
            from . import qdrive_backend as qd
            all_bms = qd.load_bookmarks()
        except Exception:
            return None
        # Prefer exact match: host + drive + client_name
        best = None
        for bm in all_bms:
            if bm.host != host:
                continue
            # client_name is the strongest disambiguator if we
            # have it
            if active_client and bm.client_name != active_client:
                continue
            if bm.initial_drive and drive and \
                    bm.initial_drive != drive:
                continue
            best = bm
            # If exact drive match, return immediately
            if bm.initial_drive == drive:
                return bm
        return best

    def _qdrive_save_cwd_to_bookmark(self, bookmark, pwd):
        """Persist the current remote directory and drive into the
        QDrive bookmark so future qdrive_site connects land here
        directly. Also pins initial_drive so the drive picker
        dialog is skipped next time."""
        # Figure out the current drive from the lister's label
        try:
            label_hint = getattr(self.fs, 'label', '') or ''
            tail = label_hint[len('qdrive://'):]
            _host, _, current_drive = tail.partition('/')
        except Exception:
            current_drive = bookmark.initial_drive
        # Modify the stored bookmark in place and persist the
        # whole list. Both fields - initial_drive and
        # initial_path - are part of the official dataclass now,
        # so this is a straight save_bookmarks() round-trip.
        from . import qdrive_backend as qd
        bms = qd.load_bookmarks()
        updated = False
        for bm in bms:
            if bm.name == bookmark.name:
                bm.initial_drive = (current_drive
                                     or bm.initial_drive)
                bm.initial_path = pwd or "/"
                updated = True
                break
        if not updated:
            QMessageBox.warning(
                self, "Quopus Drive bookmark",
                f"Could not find bookmark "
                f"'{bookmark.name}' in qdrive_bookmarks.json "
                f"- nothing saved.")
            return
        qd.save_bookmarks(bms)
        QMessageBox.information(
            self, "Quopus Drive bookmark",
            f"Saved current drive '{current_drive}' and path "
            f"'{pwd}' as defaults for bookmark "
            f"'{bookmark.name}'.\n\n"
            f"Next time you click the qdrive_site button you "
            f"will land here directly.")

    def _qdrive_add_as_action_button(self, bookmark):
        """Add the active QDrive connection's bookmark as a
        one-click action button. Uses the shared cell-picker on
        the action dispatcher - same UI as FTP, just with
        'qdrive_site' as the action_kind so a button click
        triggers QDrive's connect handler, not FTP's."""
        try:
            actions = self.window().actions
        except AttributeError:
            QMessageBox.warning(
                self, "Quopus Drive",
                "Action dispatcher not available.")
            return
        kw = {
            'name':         bookmark.name,
            'action_kind':  'qdrive_site',
            'action_label': bookmark.name,
        }
        actions._save_ftp_as_action_button(kw)

    def _open_qdrive_dialog(self):
        """Open the Quopus Drive connect dialog. Used by the
        right-click hint when the user is on an active QDrive
        connection but didn't save it as a bookmark - they need
        to hit Save Bookmark before we can place it on a
        button."""
        try:
            actions = self.window().actions
            actions.dispatch('qdrive', param=None)
        except Exception as e:
            QMessageBox.warning(
                self, "Quopus Drive",
                f"Could not open the connect dialog: {e}")

    # ------------------------------------------------------------------

    # List of extensions we consider "text" for internal TextReader routing
    # when the association dispatch falls through to auto mode.
    _AUTO_TEXT_EXTS = {
        "", ".txt", ".nfo", ".diz", ".readme", ".log", ".asc",
        ".me", ".1st", ".md", ".guide", ".doc", ".rtf",
        ".seq", ".pet", ".c64", ".ans",
        ".asm", ".s", ".a", ".a68k", ".i", ".inc", ".mac",
        ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
        ".py", ".pyw", ".rb", ".pl", ".lua", ".sh", ".bash",
        ".zsh", ".fish", ".ps1", ".bat", ".cmd",
        ".html", ".htm", ".css", ".js", ".ts", ".json", ".xml",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".pas", ".e", ".rexx", ".rx", ".bas", ".bb",
        ".go", ".rs", ".java", ".kt", ".swift", ".vb",
        ".tex", ".bib", ".org", ".rst",
        ".csv", ".tsv", ".sql", ".mk", ".makefile",
    }
    _AUTO_TEXT_NAMES = {"makefile", "readme", "license", "authors"}

    def _dispatch_view(self, p: Path, action='viewer'):
        """
        Open the file `p` according to its configured file-association.
        action = 'viewer' for F3/Read, 'editor' for F4/Edit.
        Falls back to the "*" wildcard entry when the extension has none.
        """
        # Resolve the main window's config (holds file_assoc). The lister
        # doesn't carry the config directly, but the top-level window does.
        cfg = None
        w = self.window()
        if hasattr(w, 'config'):
            cfg = w.config

        if cfg is None:
            # Safety fallback: no config available, use legacy routing
            self._auto_open(p)
            return

        from .file_assoc import get_assoc, run_external
        handler = get_assoc(cfg, p.suffix.lower(), action)

        if handler.get("mode") == "external":
            try:
                run_external(handler, p)
            except Exception as ex:
                QMessageBox.warning(
                    self, "External program",
                    f"Cannot launch program:\n{handler.get('program','')}\n\n{ex}")
            return

        # Internal mode - dispatch by 'type'
        t = handler.get("type", "auto")
        if t == "auto":
            self._auto_open(p)
        elif t == "text":
            TextReader(p, self).exec()
        elif t == "image":
            from .image_viewer import ImageViewer
            ImageViewer(p, self).exec()
        elif t == "archive":
            from .archive_viewer import ArchiveViewer
            # Non-modal so Quopus stays interactive while the user is
            # browsing inside the archive viewer (extracting RAR
            # files can take minutes; .exec() would freeze the rest
            # of the app for that whole time even with the inner
            # operations themselves backgrounded).
            v = ArchiveViewer(p, self)
            v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            v.show()
        elif t == "hex":
            HexReader(p, self).exec()
        elif t == "c64disasm":
            # LNX-archive-wrapped-in-PRG (e.g. 'game.lnx.prg' from BBS
            # uploads) and ZipCode parts (e.g. '1!FOO.prg' .. '4!FOO.prg')
            # both route here by extension because .prg is hard-coded
            # to c64disasm in DEFAULT_ASSOC. Catch them and show the
            # disk viewer instead - those files aren't really 6502
            # programs.
            from .cbmfiles import (is_lnx_in_prg, is_zipcode_part,
                                     CbmDiskDialog)
            if is_lnx_in_prg(p):
                d = CbmDiskDialog.from_lnx_prg(p, self)
                if d is not None:
                    d.exec()
                return
            if is_zipcode_part(p):
                d = CbmDiskDialog.from_zipcode(p, self)
                if d is not None:
                    d.exec()
                return
            from .c64_disasm import C64DisasmViewer
            C64DisasmViewer(p, self).exec()
        elif t == "crt_toolkit":
            # C64 .crt cartridge image: open the dedicated CRT
            # toolkit (header inspection, per-bank CHIP packets,
            # hex/disasm bank view, raw bank extraction, EAPI /
            # EasyFS / Yeti detection, embedded-blob scanner,
            # GMod2 EEPROM read/write). Non-modal so the user can
            # browse the cart while doing other things in Quopus.
            from .crt_toolkit import open_crt_toolkit, CrtParseError
            try:
                open_crt_toolkit(p, self)
            except CrtParseError as e:
                # Not a valid CRT file (corrupt or non-C64). Fall
                # back to the 6502 disassembler so the user sees
                # *something* useful instead of a hard error.
                from PyQt6.QtWidgets import QMessageBox
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("CRT Toolkit")
                box.setText(
                    f"Not a valid VICE CRT file:\n{e}\n\n"
                    "Open in the 6502 disassembler instead?")
                box.setStandardButtons(
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No)
                if box.exec() == QMessageBox.StandardButton.Yes:
                    from .c64_disasm import C64DisasmViewer
                    C64DisasmViewer(p, self).exec()
            except Exception as e:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "CRT Toolkit",
                    f"Failed to open cartridge:\n{e}")
        elif t == "modplay":
            from .mod_player import ModPlayerDialog
            if ModPlayerDialog.check_audio_available(self):
                # show() (not exec()) so Quopus stays responsive while
                # the player runs. Qt keeps the dialog alive via the
                # parent=self link until the user closes it.
                ModPlayerDialog(p, self).show()
        elif t == "sidplay":
            from .sid_player import SIDPlayerDialog
            if SIDPlayerDialog.check_audio_available(self):
                SIDPlayerDialog(p, self).show()
        elif t == "c64emu":
            # File-association type "c64emu" launches the file in
            # the configured C64 emulator instead of opening an
            # internal viewer. Path and arg template are taken from
            # config['c64_emulator'] / config['c64_emulator_args'] -
            # shared with the C64 Disasm Viewer's F5 run-in-emu key.
            # First-time use prompts for the path.
            from .c64_disasm import run_in_c64_emulator
            from .config import save_config
            mw = self.window()
            cfg = getattr(mw, 'config', {}) if mw else {}
            run_in_c64_emulator(
                p, self, cfg,
                lambda: save_config(cfg) if cfg else None)
        elif t == "retrogfx":
            # C64 graphics viewer: charset / Koala / Hi-Res bitmap.
            # Format-detection passiert anhand der Dateigroesse in
            # show_retro_gfx_viewer. Nicht-modal damit der User mehrere
            # Bilder gleichzeitig vergleichen kann.
            from .retro_gfx_viewer import show_retro_gfx_viewer
            show_retro_gfx_viewer(p, self)
        elif t == "amigaguide":
            from .amigaguide_viewer import AmigaGuideViewer
            AmigaGuideViewer(p, self).exec()
        else:
            self._auto_open(p)

    def _auto_open(self, p: Path):
        """Automatic type detection (used when assoc type = 'auto')."""
        # AmigaGuide hypertext files (extension OR @DATABASE magic)
        if self._is_amigaguide(p):
            from .amigaguide_viewer import AmigaGuideViewer
            AmigaGuideViewer(p, self).exec()
            return
        # Tracker module files (.mod / .xm / .s3m / .it etc.)
        from .mod_player import is_module_file, ModPlayerDialog
        if is_module_file(p):
            if ModPlayerDialog.check_audio_available(self):
                # Non-modal: keep Quopus usable while playing. Qt
                # owns the dialog via the parent=self link.
                ModPlayerDialog(p, self).show()
            return
        # SID music files - SIDs would otherwise match the C64-binary
        # detection below, so check this first.
        from .sid_player import is_sid_file, SIDPlayerDialog
        if is_sid_file(p):
            if SIDPlayerDialog.check_audio_available(self):
                SIDPlayerDialog(p, self).show()
            return
        # Amiga ADF disk images. Check before generic archive
        # detection since the file extension is unique enough.
        if p.suffix.lower() == ".adf":
            try:
                from .adf_viewer import ADFDiskDialog
                from .adf import ADFError
                try:
                    ADFDiskDialog(str(p), self).show()
                except ADFError as e:
                    QMessageBox.warning(
                        self, "ADF",
                        f"Cannot open ADF:\n{e}")
            except Exception as e:
                QMessageBox.warning(
                    self, "ADF", str(e))
            return
        # CBM disk images + LNX-archives-wrapped-in-PRG + ZipCode
        # parts. These all need to come BEFORE the c64_disasm test -
        # .lnx.prg and N!FOO.prg would otherwise be routed to the
        # disassembler since their extension is .prg, and .d64/.d71/
        # etc. would also fall through to the assembler-or-archive
        # code paths.
        from .cbmfiles import (is_cbm_disk, is_lnx_in_prg,
                                 is_zipcode_part, CbmDiskDialog)
        if is_cbm_disk(p):
            CbmDiskDialog(p, self).exec()
            return
        if is_lnx_in_prg(p):
            d = CbmDiskDialog.from_lnx_prg(p, self)
            if d is not None:
                d.exec()
            return
        if is_zipcode_part(p):
            d = CbmDiskDialog.from_zipcode(p, self)
            if d is not None:
                d.exec()
            return
        # C64 PRG / BIN / SID files - 6502 disassembler
        from .c64_disasm import is_c64_binary, C64DisasmViewer
        if is_c64_binary(p):
            C64DisasmViewer(p, self).exec()
            return
        from .archive_viewer import is_archive, ArchiveViewer
        if is_archive(p):
            v = ArchiveViewer(p, self)
            v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            v.show()
            return
        from .image_viewer import is_image, ImageViewer
        if is_image(p):
            ImageViewer(p, self).exec()
            return
        ext = p.suffix.lower()
        if ext in self._AUTO_TEXT_EXTS or p.name.lower() in self._AUTO_TEXT_NAMES:
            TextReader(p, self).exec()
        else:
            self._open_file(p)

    def _is_amigaguide(self, p: Path) -> bool:
        """Detect AmigaGuide files: by extension first, then by magic
        (first non-empty line starts with @DATABASE)."""
        if not p.is_file(): return False
        ext = p.suffix.lower()
        if ext in (".guide", ".hlp"):
            return True
        # Magic check - read first 4 KB
        try:
            with open(p, 'rb') as f:
                head = f.read(4096)
        except Exception:
            return False
        try:
            text = head.decode('iso-8859-1', errors='replace')
        except Exception:
            return False
        # Skip whitespace/empty lines
        for line in text.split('\n')[:20]:
            s = line.strip()
            if not s: continue
            if s.upper().startswith('@DATABASE'):
                return True
            return False
        return False

    def _open_file(self, p):
        try:
            if platform.system() == "Windows":
                os.startfile(str(p))
            elif platform.system() == "Darwin":
                subprocess.run(["open", str(p)])
            else:
                subprocess.run(["xdg-open", str(p)])
        except Exception as e:
            QMessageBox.warning(self, "Quopus", f"Cannot open: {e}")

    def _ctx_menu(self, pos):
        self.got_focus.emit(self)
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {C.WB_GREY}; color: {C.BLACK};
                    border: 1px solid {C.BLACK};
                    font-family: "Topaz","Courier New",monospace; }}
            QMenu::item:selected {{ background-color: {C.SELECTED}; color: {C.WHITE}; }}
        """)
        idx = self.view.indexAt(pos)

        # If remote mode, offer Disconnect prominently at the top.
        # The label adapts to which kind of remote backend is
        # mounted (FTP vs Quopus Drive) by inspecting the fs
        # label - QDriveFs uses "qdrive://..." as its label, the
        # FTP backend uses bookmark names or "ftp://...".
        if self.fs.kind == 'remote':
            label_hint = getattr(self.fs, 'label', '') or ''
            is_qdrive = label_hint.startswith('qdrive://')
            if is_qdrive:
                disc_text = "🔌 Disconnect Quopus Drive  (back to local)"
            else:
                disc_text = "🔌 Disconnect FTP  (back to local)"
            a_disc = menu.addAction(disc_text,
                                     self.disconnect_remote)
            # Make the disconnect entry visually stand out
            font = a_disc.font(); font.setBold(True); a_disc.setFont(font)
            menu.addSeparator()
            # ----- Save-current-remote-state shortcuts -----
            cur_label = label_hint or '?'
            try:
                cur_pwd = self.fs.pwd() or '/'
            except Exception:
                cur_pwd = '/'
            if is_qdrive:
                # Quopus Drive path: find the matching bookmark
                # in qdrive_bookmarks.json by host+drive.
                # The label is "qdrive://<host>/<drive>" - we
                # parse it back out and look the bookmark up.
                qd_bm = self._qdrive_find_active_bookmark()
                if qd_bm is not None:
                    bm_name = qd_bm.name
                    menu.addAction(
                        f"💾 Save current dir as default for "
                        f"'{bm_name}'  ({cur_pwd})",
                        lambda b=qd_bm, p=cur_pwd:
                            self._qdrive_save_cwd_to_bookmark(b, p))
                    menu.addAction(
                        f"➕ Add as action button (connect to "
                        f"'{bm_name}')",
                        lambda b=qd_bm:
                            self._qdrive_add_as_action_button(b))
                else:
                    # Active connection but no matching saved
                    # bookmark - tell the user how to fix.
                    a = menu.addAction(
                        f"➕ Add as action button  "
                        f"(no saved bookmark yet)")
                    a.setEnabled(False)
                    menu.addAction(
                        "💡 Open Quopus Drive dialog to save "
                        "this as a bookmark first",
                        lambda: self._open_qdrive_dialog())
            else:
                # Classic FTP path
                menu.addAction(
                    f"💾 Save current dir as default for "
                    f"'{cur_label}'  ({cur_pwd})",
                    lambda lbl=cur_label, p=cur_pwd:
                        self._ftp_save_cwd_to_bookmark(lbl, p))
                menu.addAction(
                    f"➕ Add as action button (connect to "
                    f"'{cur_label}')",
                    lambda lbl=cur_label:
                        self._ftp_add_as_action_button(
                            lbl, 'ftp_site'))
                menu.addAction(
                    f"⬆ Add as upload action button "
                    f"(upload to '{cur_label}' : {cur_pwd})",
                    lambda lbl=cur_label, p=cur_pwd:
                        self._ftp_add_as_action_button(
                            lbl, 'ftp_upload', save_cwd=p))
            menu.addSeparator()
        elif self.fs.kind == 'search':
            a_close = menu.addAction(
                "✕ Close search results  (back to folder)",
                self.disconnect_search)
            font = a_close.font(); font.setBold(True); a_close.setFont(font)
            menu.addSeparator()

        has_sel = idx.isValid() and (
            bool(self.selected_or_tagged()) or
            bool(self.selected_entries()))

        # ----- Top-level "fast access" items (no submenu) -----
        # The single most-common actions stay at top level so a
        # double-click-equivalent is one click away. Everything
        # else lives under a grouped submenu.
        if has_sel:
            menu.addAction("Read (internal)", self._read_selected)
            menu.addAction("Open external", self._open_selected)
            # Multi-SID parallel playback: only when 2-4 SID files
            # are tagged. Stays at top-level because it's a one-shot
            # "I want this NOW" action - hiding it in a submenu
            # would just add a click.
            try:
                from .sid_player import is_sid_file
                tagged_paths = [
                    Path(t) for t in self.selected_or_tagged()
                    if Path(t).is_file()]
                tagged_sids = [t for t in tagged_paths
                               if is_sid_file(t)]
                if (2 <= len(tagged_sids) <= 4
                        and len(tagged_sids) == len(tagged_paths)):
                    a_multi = menu.addAction(
                        f"▶ Play as multi-SID  "
                        f"({len(tagged_sids)} tunes in parallel)",
                        lambda sids=tagged_sids:
                            self._play_multi_sid(sids))
                    f = a_multi.font()
                    f.setBold(True)
                    a_multi.setFont(f)
            except Exception:
                pass
            menu.addSeparator()

            # ----- Open ► submenu (other read/inspect actions) -----
            open_menu = menu.addMenu("Open ►")
            open_menu.setStyleSheet(menu.styleSheet())
            open_menu.addAction("Hex Read (internal)",
                                self._hex_selected)
            open_menu.addAction("C64 Disassemble (6502)...",
                                self._disasm_selected)
            # Run in C64 emulator (VICE / x64sc / configured one).
            # Only enabled when the selected file has an extension
            # known to be runnable as a C64 program/image. Same
            # set as the DB browser uses so behaviour is
            # consistent between both views.
            _runnable_exts = {
                ".prg", ".p00", ".d64", ".d71", ".d81",
                ".g64", ".g71", ".d80", ".d82", ".crt",
                ".tap", ".t64"}
            sel_paths = self._selected_paths()
            sel_runnable = (
                len(sel_paths) >= 1
                and sel_paths[0].suffix.lower()
                    in _runnable_exts
                and sel_paths[0].is_file())
            a_run = open_menu.addAction(
                "Run in Emulator")
            a_run.setEnabled(sel_runnable)
            a_run.triggered.connect(self._run_selected_in_emulator)

            # ----- Edit ► submenu (rename, delete, tag) -----
            edit_menu = menu.addMenu("Edit ►")
            edit_menu.setStyleSheet(menu.styleSheet())
            edit_menu.addAction("Rename", self._rename_selected)
            edit_menu.addAction("Delete", self._delete_selected)
            edit_menu.addSeparator()
            edit_menu.addAction("Tag / Untag", lambda: (
                self.model.toggle_tag(idx.row())
                if idx.isValid() else None))
            edit_menu.addAction("Invert tags", self.model.invert_tags)
            edit_menu.addAction("Clear all tags", self.model.clear_tags)

            # ----- Info ► submenu (Info, PETSCII convert) -----
            info_menu = menu.addMenu("Info ►")
            info_menu.setStyleSheet(menu.styleSheet())
            info_menu.addAction("Info", self._info_selected)
            info_menu.addAction("ASCII ↔ PETSCII convert...",
                                self._petscii_convert_dialog)
            menu.addSeparator()

            # ----- Actions ► and Assign-to-button ► (existing) -----
            # Both kept where they were since they're the most
            # extensible (any user-defined action can show up here)
            # and people already know to look here for them.
            actions_menu = menu.addMenu("Actions ►")
            actions_menu.setStyleSheet(menu.styleSheet())
            self._build_actions_submenu(actions_menu)

            assign_menu = menu.addMenu("Assign to button ►")
            assign_menu.setStyleSheet(menu.styleSheet())
            self._build_actions_submenu(assign_menu, for_assign=True)
            menu.addSeparator()

            # ----- Drives & Buttons ► submenu --------------------
            # "Add as drive button" / "Assign to action button" for
            # the selected folder + the current folder are both
            # here. Empty if no folder selected and current folder
            # is somehow not addable - rare.
            sel_paths = self.selected_or_tagged()
            sel_dirs = [p for p in sel_paths if p.is_dir()]
            drives_menu = menu.addMenu("Drives & Buttons ►")
            drives_menu.setStyleSheet(menu.styleSheet())
            if sel_dirs:
                first = sel_dirs[0]
                drives_menu.addAction(
                    f"Add '{first.name}' as drive button",
                    lambda p=first: self._req_add_drive(p))
                drives_menu.addAction(
                    f"Assign '{first.name}' to action button...",
                    lambda p=first: self._assign_folder_to_button(p))
                drives_menu.addSeparator()
            cur_name = (self.current_path.name
                        or str(self.current_path))
            if self.fs.kind == 'remote':
                drives_menu.addAction(
                    "Add this FTP location as drive button",
                    self._req_add_ftp_bookmark)
            else:
                drives_menu.addAction(
                    f"Add current folder '{cur_name}' as "
                    f"drive button",
                    lambda: self._req_add_drive(self.current_path))
                drives_menu.addAction(
                    f"Assign current folder '{cur_name}' to "
                    f"action button...",
                    lambda: self._assign_folder_to_button(
                        self.current_path))
            menu.addSeparator()

        # ----- Folder ► submenu (always shown) -----
        folder_menu = menu.addMenu("Folder ►")
        folder_menu.setStyleSheet(menu.styleSheet())
        folder_menu.addAction(
            "Makedir here",
            lambda: self.makedir_requested.emit(self))
        folder_menu.addAction("Refresh", self.refresh)
        folder_menu.addAction("Parent", self.parent_dir)

        # ----- Sort ► submenu (always shown) -----
        # Quick column-sort options without having to right-click
        # the header. Each entry shows a ✓ next to the currently
        # active sort key.
        from .dirmodel import (DirModel, COL_NAME, COL_EXT,
                                COL_SIZE, COL_DATE)
        sort_menu = menu.addMenu("Sort ►")
        sort_menu.setStyleSheet(menu.styleSheet())
        cur_sk = self.model.sort_key
        rev = self.model.reverse
        # Indicator for the current sort key + direction
        arrow = " ↓" if rev else " ↑"

        def _mk_sort(label, sort_key):
            check = "✓ " if cur_sk == sort_key else "    "
            arr = arrow if cur_sk == sort_key else ""
            a = sort_menu.addAction(f"{check}{label}{arr}")
            a.triggered.connect(
                lambda checked=False, k=sort_key:
                    self._set_sort_key(k))
            return a

        _mk_sort("Name", DirModel.SORT_NAME)
        _mk_sort("Extension", DirModel.SORT_EXT)
        _mk_sort("Size", DirModel.SORT_SIZE)
        _mk_sort("Date", DirModel.SORT_TIME)
        sort_menu.addSeparator()
        a_rev = sort_menu.addAction(
            "Reverse sort order"
            + ("  (currently descending)" if rev
               else "  (currently ascending)"))
        a_rev.triggered.connect(self._toggle_sort_reverse)

        # ----- View ► submenu (size column display) -----
        view_menu = menu.addMenu("View ►")
        view_menu.setStyleSheet(menu.styleSheet())
        cur_size = "blocks" if self.model.show_blocks else "bytes"
        view_menu.addAction(
            ("✓ " if cur_size == "bytes" else "    ")
            + "Size: bytes (4K, 1.2M, ...)",
            lambda: self._set_size_display("bytes"))
        view_menu.addAction(
            ("✓ " if cur_size == "blocks" else "    ")
            + "Size: C64 blocks (256 B = 1 bl)",
            lambda: self._set_size_display("blocks"))
        menu.addSeparator()

        # ----- Shuffle play ► submenu -----
        shuffle_menu = menu.addMenu("Shuffle play ►")
        shuffle_menu.setStyleSheet(menu.styleSheet())
        shuffle_menu.addAction(
            "▶ SIDs from here",
            lambda: self._shuffle_play_sids())
        shuffle_menu.addAction(
            "▶ Modules (MOD/XM/...) from here",
            lambda: self._shuffle_play_mods())

        menu.exec(self.view.mapToGlobal(pos))

    def _set_sort_key(self, sort_key):
        """Apply a specific sort key from the Sort submenu.
        Keeps the current 'reverse' setting if the user is just
        switching keys; clicking the same key in the header
        toggles reverse (separate code path). The submenu also
        provides an explicit 'Reverse sort order' entry."""
        self.model.set_sort(sort_key, reverse=self.model.reverse)
        self._save_sort_state()

    def _toggle_sort_reverse(self):
        """Flip the ascending/descending direction without
        changing the sort column."""
        self.model.toggle_reverse()
        self._save_sort_state()

    def _shuffle_play_sids(self):
        """Recursively scan current dir for .sid files, then open
        the SID Player in shuffle mode with the first random track."""
        self._start_shuffle_scan('sid')

    def _shuffle_play_mods(self):
        """Recursively scan current dir for tracker module files."""
        self._start_shuffle_scan('mod')

    def _start_shuffle_scan(self, kind: str):
        """Kick off a background ShuffleScanner over self.current_path,
        showing a progress dialog. When it finishes, opens the
        appropriate player (kind='sid' or 'mod')."""
        from .shuffle import ShuffleScanner
        from PyQt6.QtWidgets import QProgressDialog
        root = Path(self.current_path)
        if not root.exists():
            QMessageBox.warning(self, "Shuffle",
                                  f"Directory does not exist:\n{root}")
            return
        # Predicate per kind
        if kind == 'sid':
            from .sid_player import is_sid_file
            predicate = is_sid_file
            label = "SID files"
        else:
            from .mod_player import is_module_file
            predicate = is_module_file
            label = "tracker modules"
        # Progress dialog with cancel
        pd = QProgressDialog(
            f"Scanning '{root.name}' for {label}...",
            "Cancel", 0, 0, self)
        pd.setWindowTitle("Shuffle Mode")
        pd.setMinimumDuration(200)
        pd.setAutoClose(False)
        pd.setAutoReset(False)
        # Background scanner
        scanner = ShuffleScanner(root, predicate, parent=self)
        # Keep a reference so it isn't GC'd
        self._active_scanner = scanner
        def on_progress(n):
            pd.setLabelText(
                f"Scanning '{root.name}' for {label}...\nFound: {n}")
        def on_done(files):
            pd.close()
            self._active_scanner = None
            if not files:
                QMessageBox.information(
                    self, "Shuffle",
                    f"No {label} found in:\n{root}\n\n"
                    f"(Searched recursively up to 50,000 files.)")
                return
            self._launch_shuffle_player(kind, files)
        def on_cancel():
            scanner.stop()
            scanner.wait(500)
            self._active_scanner = None
        scanner.progress.connect(on_progress)
        scanner.finished_with_files.connect(on_done)
        pd.canceled.connect(on_cancel)
        scanner.start()
        pd.show()

    def _launch_shuffle_player(self, kind: str, files):
        """Open the right player with shuffle_files set. The first
        track to play is files[0] (already shuffled by the scanner)."""
        if not files: return
        first = files[0]
        try:
            if kind == 'sid':
                from .sid_player import SIDPlayerDialog
                if not SIDPlayerDialog.check_audio_available(self):
                    return
                dlg = SIDPlayerDialog(first, shuffle_files=files,
                                        parent=self)
            else:
                from .mod_player import ModPlayerDialog
                if not ModPlayerDialog.check_audio_available(self):
                    return
                dlg = ModPlayerDialog(first, shuffle_files=files,
                                        parent=self)
            dlg.show()
        except Exception as e:
            QMessageBox.critical(
                self, "Shuffle",
                f"Could not start shuffle player:\n{e}")

    def _assign_folder_to_button(self, path):
        """Enter button-assignment mode with goto_dir action + the path."""
        w = self.window()
        if hasattr(w, 'start_button_assignment'):
            # Pass the path as the "param" so the full dialog has it pre-filled
            label = path.name or str(path)
            if len(label) > 14:
                label = label[:14]
            w.start_button_assignment(
                'goto_dir',
                label,
                default_param=str(path),
            )

    def _req_add_drive(self, path):
        """Ask user for a label, then emit add_drive_requested signal."""
        default_label = path.name or str(path)
        # Clean up label - Amiga-style device names are short
        if len(default_label) > 12:
            default_label = default_label[:12]
        if not default_label.endswith(":"):
            default_label = default_label + ":"
        label, ok = QInputDialog.getText(
            self, "Add drive button",
            f"Label for:\n{path}\n\n(leave empty to cancel)",
            text=default_label)
        if not ok or not label.strip():
            return
        self.add_drive_requested.emit(label.strip(), str(path))

    def _req_add_ftp_bookmark(self):
        """Bookmark the current FTP connection as a drive button.
        Pulls host/port/user from the active backend and asks the
        main window to insert it via the standard FTP-bookmark
        dialog (so the user can review the fields before saving)."""
        if self.fs.kind != 'remote':
            return
        backend = getattr(self.fs, 'backend', None)
        host = getattr(backend, 'host', '') if backend else ''
        port = getattr(backend, 'port', 21) if backend else 21
        user = getattr(backend, 'user', '') if backend else ''
        # Current remote path
        try:
            cur_path = str(self.current_path) or "/"
        except Exception:
            cur_path = "/"
        self.add_ftp_bookmark_requested.emit({
            "label": (host or "FTP").upper(),
            "host":  host,
            "port":  port,
            "user":  user,
            "path":  cur_path,
            "mode":  "passive",
        })

    def selected_paths(self):
        """Return pathlib.Path for each currently-selected ROW.

        Wichtig: QTreeView.selectedIndexes() liefert pro Row EIN
        QModelIndex pro Spalte - bei N Spalten und M selektierten
        Rows kommen N*M Indizes raus. Wenn wir naiv ueber alle
        iterieren und die path appenden, taucht jede Datei N mal in
        der Liste auf - die war die Ursache fuer "GetSizes zaehlt
        alles 4x" Bug (bei 4 sichtbaren Spalten: Name/Ext/Size/Date).
        Wir dedupen ueber row().
        """
        out = []
        seen_rows = set()
        for idx in self.view.selectedIndexes():
            r = idx.row()
            if r in seen_rows:
                continue
            seen_rows.add(r)
            e = self.model.entry_at(r)
            if e: out.append(Path(e.path))
        return out

    def selected_or_tagged(self):
        """Return pathlib.Path objects of the current selection.
        Only meaningful for LOCAL filesystem - for remote, use
        selected_entries() instead."""
        if self.fs.kind == 'remote':
            # Returning local-paths for remote files is meaningless.
            # Callers must switch to selected_entries() in that case.
            return []
        tagged = [Path(p) for p in self.model.tagged_paths()]
        if tagged:
            return tagged
        sel = self.selected_paths()
        if sel:
            return sel
        idx = self.view.currentIndex()
        if idx.isValid():
            e = self.model.entry_at(idx.row())
            if e:
                return [Path(e.path)]
        return []

    def handle_dropped_paths(self, src_paths, is_move):
        """Called by the QTreeView subclass when files are dropped onto
        this lister. Copies (or moves on Shift) the source paths into
        this lister's current directory. Uses the same chunked-copy +
        progress dialog as F5/F6 by going through the action dispatcher.
        """
        if self.fs.kind != 'local':
            QMessageBox.information(self, "Drop",
                "Cannot drop directly onto a remote/FTP lister yet.")
            return
        main = self.window()
        other_lister = None
        if hasattr(main, "left_lister") and hasattr(main, "right_lister"):
            other_lister = (main.right_lister if self is main.left_lister
                            else main.left_lister)
        # If the dropped files all live in the OTHER lister's directory,
        # use that lister directly as the source so the copy logic uses
        # cross-FS paths. Otherwise build a synthetic shim source.
        same_dir = (other_lister is not None
                    and all(p.parent == other_lister.current_path
                            for p in src_paths))
        if same_dir:
            other_lister.model.clear_tags()
            wanted = {str(p) for p in src_paths}
            for i in range(other_lister.model.rowCount()):
                e = other_lister.model.entry_at(i)
                if e and e.path in wanted:
                    other_lister.model.toggle_tag(i)
            src = other_lister
        else:
            src = _DropSourceShim(src_paths)
        dst = self
        main.actions._transfer(src, dst, move=is_move)
        self.refresh()

    def selected_entries(self):
        """Return FsEntry-like objects of the current selection,
        works for both local and remote filesystems."""
        from .fs_backend import FsEntry
        tagged_paths = set(self.model.tagged_paths())
        entries = []
        if tagged_paths:
            for e in self.model.entries:
                if e.path in tagged_paths:
                    entries.append(FsEntry(
                        name=e.name, path=e.path, is_dir=e.is_dir,
                        size=e.size, mtime=e.mtime))
            return entries
        # Selection from the view
        rows = set()
        for idx in self.view.selectionModel().selectedIndexes():
            rows.add(idx.row())
        if not rows:
            idx = self.view.currentIndex()
            if idx.isValid():
                rows.add(idx.row())
        for r in sorted(rows):
            e = self.model.entry_at(r)
            if e:
                entries.append(FsEntry(
                    name=e.name, path=e.path, is_dir=e.is_dir,
                    size=e.size, mtime=e.mtime))
        return entries

    def select_all(self):
        self.view.selectAll()

    def select_none(self):
        self.view.clearSelection()
        self.model.clear_tags()

    def _read_selected(self):
        for p in self.selected_or_tagged():
            if p.is_file():
                self._dispatch_view(p, action='viewer')
                return

    def _edit_selected(self):
        """F4 — open with the file's configured editor."""
        for p in self.selected_or_tagged():
            if p.is_file():
                self._dispatch_view(p, action='editor')
                return

    def _selected_paths(self):
        """Return the list of currently-selected (or tagged) paths.
        Thin wrapper around selected_or_tagged() that returns a
        plain list for easier indexing in context-menu enable
        logic.
        """
        try:
            return list(self.selected_or_tagged() or [])
        except Exception:
            return []

    def _run_selected_in_emulator(self):
        """Launch the selected file in the configured C64
        emulator (VICE / x64sc / etc). Uses the same launcher
        as the file-association 'c64emu' type and the DB
        browser's Run action - one config knob for all three
        entry points.

        For multi-selection we run the FIRST runnable item only;
        rclone-style multi-item batch runs go through the DB
        browser's sequential runner, not via right-click in the
        lister.
        """
        paths = self._selected_paths()
        if not paths:
            return
        p = paths[0]
        if not p.is_file():
            QMessageBox.warning(
                self, "Run in Emulator",
                f"Not a file: {p}")
            return
        from .c64_disasm import run_in_c64_emulator
        from .config import save_config
        mw = self.window()
        cfg = getattr(mw, 'config', {}) if mw else {}
        try:
            run_in_c64_emulator(
                p, self, cfg,
                lambda: save_config(cfg) if cfg else None)
        except Exception as e:
            QMessageBox.warning(
                self, "Run in Emulator",
                f"Could not launch emulator:\n\n{e}")

    def _hex_selected(self):
        for p in self.selected_or_tagged():
            if p.is_file():
                HexReader(p, self).exec(); return

    def _disasm_selected(self):
        """Force-open the C64 6502 disassembler on the selected file,
        regardless of its extension. Useful for raw memory dumps."""
        for p in self.selected_or_tagged():
            if p.is_file():
                from .c64_disasm import C64DisasmViewer
                try:
                    C64DisasmViewer(p, self).exec()
                except Exception as e:
                    QMessageBox.warning(self, "C64 Disasm", str(e))
                return

    def _play_multi_sid(self, sid_paths):
        """Open the SID player in multi-SID parallel mode with the
        given list of SID files. Files are sorted by name so playback
        order is deterministic regardless of how the user tagged them."""
        from pathlib import Path as _P
        from .sid_player import SIDPlayerDialog
        files = sorted([_P(p) for p in sid_paths],
                        key=lambda x: x.name.lower())
        if not files:
            return
        if not SIDPlayerDialog.check_audio_available(self):
            return
        try:
            # Non-modal so Quopus stays responsive during playback.
            SIDPlayerDialog(files[0], self, multi_files=files).show()
        except Exception as e:
            QMessageBox.warning(self, "Multi-SID Player", str(e))

    def _open_selected(self):
        """'Open external' menu action. If a directory is selected,
        navigate into it. If multiple files are tagged, ask before
        spawning N external programs - some external apps (VICE VSID,
        modplayers, etc.) take a noticeable moment to start, so
        spawning 50 of them is rarely what the user wants."""
        paths = self.selected_or_tagged()
        if not paths: return
        # Directory case - just goto, no spawn
        if paths[0].is_dir():
            self.goto(str(paths[0]))
            return
        files = [p for p in paths if p.is_file()]
        if not files:
            return
        # If many files tagged, confirm before spawning N processes
        if len(files) > 1:
            reply = QMessageBox.question(
                self, "Open external",
                f"Open {len(files)} files in their associated programs?\n\n"
                f"This will start one program instance per file.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # User said no - just open the first one
                self._open_file(files[0])
                return
        for p in files:
            self._open_file(p)

    def _rename_selected(self):
        entries = self.selected_entries()
        if not entries: return
        e = entries[0]
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        if ok and new and new != e.name:
            try:
                if self.fs.kind == 'remote':
                    self.fs.rename(e.name, new)
                else:
                    from pathlib import Path
                    Path(e.path).rename(Path(e.path).parent / new)
                self.refresh()
            except Exception as ex:
                QMessageBox.critical(self, "Quopus", f"Rename failed: {ex}")

    def _delete_selected(self):
        entries = self.selected_entries()
        if not entries: return
        names = "\n".join(e.name for e in entries[:10])
        extra = f"\n...+{len(entries)-10} more" if len(entries) > 10 else ""
        context = "[REMOTE] " if self.fs.kind == 'remote' else ""
        reply = QMessageBox.question(
            self, "Quopus DELETE",
            f"{context}Delete {len(entries)} item(s)?\n\n{names}{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        for e in entries:
            try:
                if self.fs.kind == 'remote':
                    self.fs.delete(e.path)
                else:
                    from pathlib import Path
                    p = Path(e.path)
                    if p.is_dir(): shutil.rmtree(p)
                    else: p.unlink()
            except Exception as ex:
                if self._handle_remote_error(ex, f"Delete failed: {e.name}"):
                    return  # connection dropped - no point continuing
                QMessageBox.warning(self, "Quopus", f"{e.name}: {ex}")
        self.refresh()

    def _info_selected(self):
        paths = self.selected_or_tagged()
        if not paths: return
        p = paths[0]
        try:
            st = p.stat()
            info = (f"Name: {p.name}\n"
                    f"Path: {p.parent}\n"
                    f"Size: {st.st_size} bytes\n"
                    f"Type: {'Directory' if p.is_dir() else 'File'}\n"
                    f"Modified: {datetime.fromtimestamp(st.st_mtime)}\n")
            QMessageBox.information(self, "Info", info)
        except Exception as e:
            QMessageBox.warning(self, "Quopus", str(e))

    def _petscii_convert_dialog(self):
        """Open ASCII<->PETSCII converter for selected/tagged files."""
        paths = [p for p in self.selected_or_tagged() if p.is_file()]
        if not paths:
            QMessageBox.information(self, "Convert",
                "Select one or more files to convert.")
            return
        from .petscii_dialog import PetsciiConvertDialog
        dlg = PetsciiConvertDialog(paths, self)
        if dlg.exec():
            self.refresh()

    def _build_actions_submenu(self, menu, for_assign=False):
        """Fill a QMenu with all available button actions, grouped.
        If for_assign=True, each entry triggers button assignment instead
        of immediate execution."""
        # Find the main window to get the dispatcher
        w = self.window()
        if not hasattr(w, 'actions'):
            return

        # Grouped actions with display labels
        groups = [
            ("Viewers", [
                ("read",         "Read (internal auto)"),
                ("hexread",      "Hex read"),
                ("edit",         "Edit (configured editor)"),
                ("show",         "Show"),
                ("play",         "Play"),
            ]),
            ("File ops", [
                ("copy",         "Copy to other side"),
                ("move",         "Move to other side"),
                ("delete",       "Delete"),
                ("rename",       "Rename"),
                ("multi_rename", "Multi-rename tool..."),
                ("makedir",      "Make directory"),
                ("comment",      "Edit comment"),
                ("datestamp",    "Datestamp"),
                ("protect",      "Protect / attributes"),
                ("checkfit",     "Check fit (disk space)"),
                ("getsizes",     "Get sizes"),
            ]),
            ("Archive", [
                ("archive",      "Create archive..."),
                ("extract",      "Extract archive..."),
            ]),
            ("Convert", [
                ("petscii_convert",     "ASCII ↔ PETSCII (dialog)"),
                ("ascii_to_petscii",    "ASCII → PETSCII (direct)"),
                ("petscii_to_ascii",    "PETSCII → ASCII (direct)"),
            ]),
            ("Shuffle play", [
                ("shuffle_sids",  "Shuffle SIDs from current dir"),
                ("shuffle_mods",  "Shuffle modules from current dir"),
            ]),
            ("Nav / system", [
                ("parent",       "Parent dir"),
                ("root",         "Root"),
                ("goto_dir",     "Go to directory..."),
                ("reread",       "Reread"),
                ("swap",         "Swap sides"),
                ("back",         "Back"),
                ("forward",      "Forward"),
                ("info",         "Info / properties"),
                ("search",       "Search files"),
                ("find",         "Hunt"),
                ("run",          "Run"),
                ("shell",        "Shell"),
                ("print",        "Print"),
                ("ftp",          "FTP connect..."),
                ("buffers",      "Buffers..."),
                ("dir_reverse",  "Directory reverse (AmigaBBS)"),
                ("assign",       "Assign..."),
            ]),
            ("Custom", [
                ("external_script",  "External script (prompt for cmd)"),
                ("execute_command",  "Execute shell command (prompt)"),
                ("custom_cmd",       "Custom command..."),
            ]),
        ]

        for group_name, items in groups:
            sub = menu.addMenu(group_name)
            sub.setStyleSheet(menu.styleSheet())
            for action_name, label in items:
                # Only list actions that actually exist
                if not hasattr(w.actions, f"act_{action_name}"):
                    continue
                if for_assign:
                    sub.addAction(
                        label,
                        lambda a=action_name, lbl=label:
                            self._assign_to_button(a, lbl))
                else:
                    sub.addAction(
                        label,
                        lambda a=action_name:
                            self._run_action_on_selection(a))

    def _assign_to_button(self, action_name, label):
        """Delegate to main window's assignment mode."""
        w = self.window()
        if hasattr(w, 'start_button_assignment'):
            w.start_button_assignment(action_name, label)

    def _run_action_on_selection(self, action_name):
        """Dispatch `action_name` as if it were triggered from a button.
        The selected/tagged files are already picked up by the action via
        src.selected_or_tagged()."""
        w = self.window()
        if not hasattr(w, 'actions'):
            return
        # Make sure this lister is the "active" side so the action sees
        # the right files
        self.got_focus.emit(self)

        from PyQt6.QtWidgets import QInputDialog
        # Some actions benefit from an optional parameter - ask for it
        needs_param = {
            "external_script": "Command line (tokens: %f %F %n %p %d):",
            "execute_command": "Shell command (tokens: %f %F %n %p %d):",
            "custom_cmd":      "Custom command:",
            "ascii_to_petscii": "Output extension (default .pet):",
            "petscii_to_ascii": "Output extension (default .txt):",
        }
        param = None
        # goto_dir from context menu: if a folder is selected, use it;
        # otherwise the current folder (matches "add as drive button" behaviour)
        if action_name == 'goto_dir':
            sel = self.selected_or_tagged()
            target = None
            for p in sel:
                if p.is_dir():
                    target = p; break
            if target is None:
                target = self.current_path
            # Navigate right away, no picker, no dispatch (the action would
            # pop a picker when param is empty, but we already know where).
            self.goto(str(target))
            return

        if action_name in needs_param:
            text, ok = QInputDialog.getText(
                self, "Parameter",
                needs_param[action_name])
            if not ok:
                return
            param = text or None

        w.actions.dispatch(action_name, param)

    def refresh_fonts(self):
        """Re-apply scale-aware fonts to widgets that get their
        font from setFont() rather than CSS. Called by the
        Settings dialog when the user changes scale or
        pointsize-override.

        This covers:
          - The main QTreeView (file listing) - the visible part
            with filenames, sizes, dates, folder annotations
          - The path edit at the top
          - The info bar at the bottom

        Inline stylesheets that use scaled_font_px() get
        refreshed by the dialog's separate unpolish/polish pass
        on every top-level widget, not here.
        """
        from .palette import get_topaz_font
        try:
            self.view.setFont(get_topaz_font(11))
            # Force the model to re-emit dataChanged so the
            # uniform-row-height cache picks up the new metrics.
            self.view.viewport().update()
            self.view.scheduleDelayedItemsLayout()
        except Exception:
            pass
        # The QTreeView's header is a separate widget that
        # inherits the stylesheet font. Trigger a polish
        # explicitly so the new scaled_font_px value takes
        # effect.
        try:
            hdr = self.view.header()
            hdr.style().unpolish(hdr)
            hdr.style().polish(hdr)
            hdr.update()
        except Exception:
            pass
        # Same for the inline stylesheet on the TreeView itself
        # - rebuild it so scaled_font_px() re-evaluates.
        try:
            self._reapply_view_stylesheet()
        except AttributeError:
            # Some legacy lister builds might not have this
            # helper - silently skip
            pass

    def _reapply_view_stylesheet(self):
        """Rebuild the QTreeView's inline stylesheet so the
        embedded scaled_font_px(N) calls re-evaluate against
        the current scale factor. Call this from refresh_fonts."""
        from .palette import LISTER_QSS, SCROLLBAR_QSS, C
        from .config import scaled_font_px
        self.view.setStyleSheet(LISTER_QSS + SCROLLBAR_QSS + f"""
            QHeaderView::section {{
                background-color: {C.WB_GREY};
                color: {C.BLACK};
                padding: 2px 6px;
                border: 1px solid {C.BLACK};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(11)}px;
                font-weight: bold;
            }}
            QTreeView {{ show-decoration-selected: 1; }}
            QTreeView::item {{ border: 0; padding: 0 2px; }}
            QTreeView::branch {{ background: transparent; }}
        """)
