# date_time: 2026-05-29 18:40
"""
Main window layout:

  [screen titlebar QUOPUS.1]
  [left_lister]  [B/R/S/A]  [right_lister]
  +--------+--------------------------------+
  | dev    | action button bank (cols 1-7)  |
  | column |                                |
  | T:     |                                |
  | DF1:   |                                |
  | RAM:   |                                |
  | ...    |                                |
  | Conf1  |                                |
  | (slid) |                                |
  +--------+--------------------------------+
  [status bar]

Active lister tracked via got_focus; TAB switches active side.
Tags persist in each lister's model; always visible.
"""
from datetime import datetime

import psutil
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QInputDialog, QSizePolicy,
    QSplitter,
)

from .palette import (
    C, SCREEN_TITLEBAR_QSS, STATUSBAR_QSS, INFOBAR_QSS, button_qss
)
from .config import load_config, save_config
from .lister import FileLister
from .dirmodel import DirModel
from .device_panel import DeviceColumn
from .actions import ActionDispatcher
from .dialogs import ButtonConfigDialog


# MIME type used to identify our own button drags. We embed the
# (row, col, layer) of the source button as ASCII so the drop
# target can swap configs.  layer = 'main' or 'shift' so a drag
# started in the main grid can't accidentally land in the
# Shift-layer view (different button set entirely).
_BUTTON_DRAG_MIME = "application/x-quopus-button"


class _DraggableButton(QPushButton):
    """A QPushButton that emits a drag when the user holds the left
    mouse button and drags past the system's drag-distance threshold.
    Short clicks still fire the normal `clicked` signal.

    Drop targets are also _DraggableButton instances; on drop, the
    main window's _swap_buttons(src, dst) is called with the two
    grid positions decoded from the MIME payload.
    """

    def __init__(self, label="", parent=None, *,
                  grid_pos=(0, 0), layer='main', main_window=None):
        super().__init__(label, parent)
        self._grid_pos = grid_pos          # (row, col)
        self._layer = layer                 # 'main' / 'shift' / 'shift_alt'
        self._main_window = main_window     # back-pointer for swap
        self._drag_start_pos = None
        self.setAcceptDrops(True)

    # ---- drag start ----------------------------------------------------
    def mousePressEvent(self, ev):
        # Remember the press position; we don't START the drag here -
        # short clicks must still propagate to clicked() normally.
        # Only when the mouse moves past QApplication.startDragDistance()
        # do we transition into a drag (mouseMoveEvent).
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = ev.position().toPoint()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if (self._drag_start_pos is None
                or not (ev.buttons() & Qt.MouseButton.LeftButton)):
            super().mouseMoveEvent(ev)
            return
        # Use Qt's startDragDistance (typ. 4-10 px depending on platform)
        # so a small jitter on a normal click doesn't trigger a drag.
        from PyQt6.QtWidgets import QApplication
        if ((ev.position().toPoint() - self._drag_start_pos).manhattanLength()
                < QApplication.startDragDistance()):
            super().mouseMoveEvent(ev)
            return
        # Cancel the press state on the underlying button so the click
        # signal won't fire when we release the mouse mid-drag.
        self.setDown(False)
        self._drag_start_pos = None
        self._begin_drag()

    def _begin_drag(self):
        from PyQt6.QtCore import QMimeData
        from PyQt6.QtGui import QDrag, QPixmap
        r, c = self._grid_pos
        mime = QMimeData()
        # Payload: "row,col,layer". Plain ASCII keeps it simple to
        # parse on the drop side.
        mime.setData(_BUTTON_DRAG_MIME,
                      f"{r},{c},{self._layer}".encode('ascii'))
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Use a snapshot of the button as the drag preview so the user
        # sees what they're moving.
        pix = self.grab()
        # Slightly translucent preview so it's clearly "in flight".
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QPainter, QColor
        ghost = QPixmap(pix.size())
        ghost.fill(_Qt.GlobalColor.transparent)
        p = QPainter(ghost)
        p.setOpacity(0.7)
        p.drawPixmap(0, 0, pix)
        p.end()
        drag.setPixmap(ghost)
        drag.setHotSpot(ghost.rect().center())
        drag.exec(Qt.DropAction.MoveAction)

    # ---- drop --------------------------------------------------------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(_BUTTON_DRAG_MIME):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(_BUTTON_DRAG_MIME):
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if not ev.mimeData().hasFormat(_BUTTON_DRAG_MIME):
            return super().dropEvent(ev)
        try:
            payload = bytes(ev.mimeData().data(
                _BUTTON_DRAG_MIME)).decode('ascii')
            sr_s, sc_s, src_layer = payload.split(',', 2)
            src_pos = (int(sr_s), int(sc_s))
        except Exception:
            return super().dropEvent(ev)
        # Refuse cross-layer drops: the three button grids (main /
        # Shift / Shift+Alt) are distinct sets and swapping across
        # would be confusing. The user can switch layers and drag
        # within each.
        if src_layer != self._layer:
            ev.ignore()
            return
        if src_pos == self._grid_pos:
            ev.ignore()       # dropped on self - no-op
            return
        if self._main_window is not None:
            self._main_window._swap_buttons(
                src_pos, self._grid_pos, layer=self._layer)
        ev.acceptProposedAction()


class QuopusMain(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        # Stash a pointer to the live config on the QApplication
        # so that helper functions in config.py (scaled_font_px,
        # current_font_scale) can read it without needing to pass
        # cfg through dozens of stylesheet-building call sites.
        # The setattr is harmless if QApplication.instance() is
        # None during testing.
        try:
            from PyQt6.QtWidgets import QApplication as _QApp
            _app = _QApp.instance()
            if _app is not None:
                _app._quopus_cfg = self.config
        except Exception:
            pass
        self.buffers = []
        self.actions = ActionDispatcher(self)
        self._active_side = 'left'

        self.setWindowTitle(
            "Quopus Commander v1.0 by lA-sTYLe/Quantum 05/2026 (inspired by Directory Opus 4)")
        self.setStyleSheet(f"QMainWindow, QWidget {{ background-color: {C.WB_GREY}; }}")

        # Restore window geometry from config (or default).
        # Use frameGeometry()-based save so the position survives
        # window-decorator quirks on different platforms (Linux X11
        # in particular reports different coords from Windows).
        geom = self.config.get("window_geometry", {})
        w = geom.get("w", 1280)
        h = geom.get("h", 780)
        x = geom.get("x")
        y = geom.get("y")
        self.resize(w, h)
        if x is not None and y is not None:
            # Defer the move() to after show() - some window managers
            # (X11, especially) ignore move() before the window is
            # mapped, so we set position once Qt has actually placed
            # the window. The restore_position is then triggered by
            # showEvent.
            self._pending_move = (x, y)
        else:
            self._pending_move = None
        # Apply maximized/fullscreen state after the window is shown
        self._restore_state = geom.get("state", "normal")

        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_stats)
        QTimer.singleShot(100, lambda: (self._update_stats(), self.timer.start(1000)))

        self.left_lister.set_active(True)
        self.right_lister.set_active(False)

        # Global modifier-key tracker: install an event filter on the
        # QApp so we see Shift / Alt press/release no matter which
        # child widget has focus. Three layers are available:
        #
        #   main      - no modifier held: config["buttons"]
        #   shift     - Shift held:       config["buttons_shift"]
        #   shift_alt - Shift+Alt held:   config["buttons_shift_alt"]
        #
        # _active_layer is the single source of truth ("main",
        # "shift", or "shift_alt"). The legacy boolean
        # _shift_layer_active is kept as a backwards-compatible
        # alias (True when on either shift OR shift_alt) for callers
        # that haven't been updated to read the new attribute.
        self._active_layer = "main"
        self._shift_layer_active = False
        # When True (set by Ctrl+T cycle) the modifier-hold tracking
        # is ignored - the layer is "sticky" and only Ctrl+T can
        # flip it. Ctrl+T cycles through main -> shift -> shift_alt
        # -> main; releasing Shift/Alt does not change the layer
        # while sticky is True.
        self._layer_toggle_sticky = False
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

    def _build_ui(self):
        # Menu bar built directly from action_catalog.ACTION_GROUPS,
        # so it stays in sync with whatever's in the action picker
        # (right-click any button). Top-level menus = group names
        # (Viewers, File operations, Navigation, ..., System +
        # Custom Modules if any are loaded). Each entry under a
        # menu dispatches the same action key the picker would.
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0); main.setSpacing(0)

        # The old "QUOPUS.1" header that used to sit here was
        # redundant once the menu bar landed above (and each
        # lister already has its own QUOPUS.x: <path> title bar).
        # Removing it gives the listers more vertical room.

        # Listers row
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 2, 2)
        body_layout.setSpacing(2)

        # The two listers live inside a QSplitter so the user can
        # drag the divider to resize them - same convention as
        # Double / Total Commander. The middle mini-button column
        # (B/R/S/A) sits between them as a fixed third pane so it
        # follows the divider naturally.
        self._lister_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._lister_splitter.setHandleWidth(4)
        self._lister_splitter.setChildrenCollapsible(False)
        body_layout.addWidget(self._lister_splitter, 1)

        self.left_lister = FileLister(self.config["left_path"], "QUOPUS.1")
        # Lister can't reach the main window's config inside its own
        # __init__ (parent is set later), so apply size_display here
        # explicitly. This survives restart because size_display is
        # persisted in quopus.cfg.
        size_mode = self.config.get("size_display", "bytes")
        self.left_lister.model.show_blocks = (size_mode == "blocks")
        self.left_lister.path_changed.connect(lambda p: self._save_path("left_path", p))
        self.left_lister.got_focus.connect(self._on_lister_focus)
        self.left_lister.tab_pressed.connect(self._on_tab)
        self.left_lister.makedir_requested.connect(
            lambda lister: self.actions.act_makedir(lister, lister, None))
        self.left_lister.add_drive_requested.connect(self._on_add_drive_request)
        self.left_lister.add_ftp_bookmark_requested.connect(
            self._on_add_ftp_bookmark_request)
        self._lister_splitter.addWidget(self.left_lister)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        mid.setContentsMargins(0, 40, 0, 40)
        for label, tip, handler in [
            ("B", "Buffers", lambda: self.actions.dispatch("buffers")),
            ("R", "Reread both", lambda: (self.left_lister.refresh(),
                                          self.right_lister.refresh())),
            ("S", "Swap", lambda: self.actions.dispatch("swap")),
            ("A", "Select All (active)", lambda: self._active_lister()[0].select_all()),
        ]:
            b = QPushButton(label)
            b.setStyleSheet(button_qss("mid"))
            b.setFixedSize(26, 26); b.setToolTip(tip); b.clicked.connect(handler)
            mid.addWidget(b)
        mid.addStretch()
        mid_wrap = QWidget(); mid_wrap.setLayout(mid)
        mid_wrap.setFixedWidth(28)
        self._lister_splitter.addWidget(mid_wrap)

        self.right_lister = FileLister(self.config["right_path"], "QUOPUS.2")
        # Apply size_display setting (see comment above for left_lister)
        self.right_lister.model.show_blocks = (size_mode == "blocks")
        self.right_lister.path_changed.connect(lambda p: self._save_path("right_path", p))
        self.right_lister.got_focus.connect(self._on_lister_focus)
        self.right_lister.tab_pressed.connect(self._on_tab)
        self.right_lister.makedir_requested.connect(
            lambda lister: self.actions.act_makedir(lister, lister, None))
        self.right_lister.add_drive_requested.connect(self._on_add_drive_request)
        self.right_lister.add_ftp_bookmark_requested.connect(
            self._on_add_ftp_bookmark_request)
        self._lister_splitter.addWidget(self.right_lister)

        # The middle button column should never grow when the user
        # drags the splitter - it's just a hairline of buttons.
        self._lister_splitter.setStretchFactor(0, 1)   # left lister
        self._lister_splitter.setStretchFactor(1, 0)   # mid column
        self._lister_splitter.setStretchFactor(2, 1)   # right lister

        # Restore saved divider position. We store (left, mid,
        # right) widths so it survives a restart. Default is 50/50.
        saved_sizes = self.config.get("lister_splitter_sizes")
        if (isinstance(saved_sizes, (list, tuple))
                and len(saved_sizes) == 3
                and all(isinstance(x, int) and x >= 0
                        for x in saved_sizes)):
            self._lister_splitter.setSizes(list(saved_sizes))
        self._lister_splitter.splitterMoved.connect(
            self._on_splitter_moved)

        # Apply saved column widths to both listers
        widths = self.config.get("column_widths", {})
        if widths:
            self.left_lister.apply_column_widths(widths)
            self.right_lister.apply_column_widths(widths)

        # Hand each lister a direct reference to the main window's
        # config dict. This bypasses the self.window() climb that
        # used to fail at this point in startup (the body widget
        # hasn't been added to `main` yet, so the lister's window
        # ancestor doesn't yet resolve to QuopusMainWindow).
        # _build_drives_bar() looks for `_mw_config` first.
        self.left_lister._mw_config = self.config
        self.right_lister._mw_config = self.config

        # Rebuild both drive bars now that they can see the real
        # config - they used the "amiga" fallback during __init__.
        for lst in (self.left_lister, self.right_lister):
            try:
                lst.refresh_drives_bar()
            except Exception as e:
                print(f"[main] initial drives bar refresh: {e}")

        # Apply saved sort state per side (key + reverse). Read the
        # new QUOPUS.* keys first, fall back to legacy DOPUS.* keys
        # from pre-rename configs.
        sort_state = self.config.get("sort_state", {})
        if sort_state:
            left_ss = (sort_state.get("QUOPUS.1")
                       or sort_state.get("DOPUS.1"))
            right_ss = (sort_state.get("QUOPUS.2")
                        or sort_state.get("DOPUS.2"))
            self.left_lister.apply_sort_state(left_ss)
            self.right_lister.apply_sort_state(right_ss)

        main.addWidget(body, 1)

        # Bottom bar: [DeviceColumn] [button grid (cols 1-7)]
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(2, 2, 2, 2)
        bottom_layout.setSpacing(2)

        # Device column (col 0 of the bottom bar)
        self.device_column = DeviceColumn(self.config.get("drives", []))
        self.device_column.setFixedWidth(110)
        self.device_column.navigate_requested.connect(self._on_device_nav)
        self.device_column.devices_changed.connect(self._on_devices_changed)
        bottom_layout.addWidget(self.device_column)

        # Action buttons - use vertical stack of horizontal rows
        # QGridLayout spreads unevenly; HBoxLayout with stretch=1 per button
        # guarantees equal width and zero gap.
        btn_frame = QFrame()
        btn_frame.setStyleSheet(f"QFrame {{ background-color: {C.WB_GREY}; }}")
        self.button_area_layout = QVBoxLayout(btn_frame)
        self.button_area_layout.setSpacing(1)
        self.button_area_layout.setContentsMargins(0, 0, 0, 0)
        self._rebuild_buttons()
        bottom_layout.addWidget(btn_frame, 1)

        main.addWidget(bottom)

        # F-key hint bar (Norton/Total Commander style)
        fkeys = QWidget()
        fkr = QHBoxLayout(fkeys)
        fkr.setContentsMargins(2, 1, 2, 1); fkr.setSpacing(1)
        for label, action in [
            ("F2 Refresh", "_refresh"),
            ("F3 View",    "read"),
            ("F4 Edit",    "edit"),
            ("F5 Copy",    "copy"),
            ("F6 Move",    "move"),
            ("F7 Makedir", "makedir"),
            ("F8 Delete",  "delete"),
            ("Alt+F4 Exit", "quit"),
        ]:
            b = QPushButton(label)
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {C.WB_GREY};
                    color: {C.BLACK};
                    border: 1px solid {C.BLACK};
                    font-family: "Topaz-8","Topaz","Courier New",monospace;
                    font-size: {scaled_font_px(11)}px;
                    padding: 2px 4px;
                }}
                QPushButton:hover {{
                    background-color: {C.SELECTED};
                    color: {C.WHITE};
                }}
                QPushButton:pressed {{
                    background-color: {C.ACTIVE_BG};
                    color: {C.ACTIVE_FG};
                }}
            """)
            b.setFixedHeight(20)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if action == "quit":
                b.clicked.connect(self.close)
            elif action == "_refresh":
                b.clicked.connect(lambda chk: (self.left_lister.refresh(),
                                               self.right_lister.refresh()))
            else:
                b.clicked.connect(lambda chk, a=action: self.actions.dispatch(a))
            fkr.addWidget(b, 1)
        main.addWidget(fkeys)

        # Status bar
        status = QWidget()
        sr = QHBoxLayout(status); sr.setContentsMargins(2, 2, 2, 2); sr.setSpacing(1)
        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(STATUSBAR_QSS); sr.addWidget(self.lbl_status, 1)
        self.lbl_ram = QLabel(" RAM "); self.lbl_ram.setStyleSheet(STATUSBAR_QSS)
        sr.addWidget(self.lbl_ram)
        self.lbl_cpu = QLabel(" CPU "); self.lbl_cpu.setStyleSheet(STATUSBAR_QSS)
        sr.addWidget(self.lbl_cpu)
        self.lbl_temp = QLabel(" TEMP "); self.lbl_temp.setStyleSheet(STATUSBAR_QSS)
        sr.addWidget(self.lbl_temp)
        self.lbl_time = QLabel(" 00:00:00 "); self.lbl_time.setStyleSheet(STATUSBAR_QSS)
        sr.addWidget(self.lbl_time)
        main.addWidget(status)

        self._setup_hotkeys()

    def _fire_builtin_hotkey(self, combo: str):
        """Invoke the handler that was bound to a given key combo
        in _setup_hotkeys. Used by act_hotkey so a button can do
        whatever F1/Ctrl+B/Alt+U does without us having to mirror
        every binding into a separate action method.

        `combo` strings are canonical Qt sequence strings like
        "Alt+U", "Ctrl+Shift+F", "F11". Case matters for letters
        but Qt is forgiving about the modifier order. We try the
        raw string first, then a normalized version.
        """
        handlers = getattr(self, '_hotkey_handlers', None)
        if not handlers:
            return
        fn = handlers.get(combo)
        if fn is None:
            # Try a Qt-normalized version of the combo - lets the
            # user store "ctrl+b" or "Ctrl+b" and still hit the
            # registered handler.
            from PyQt6.QtGui import QKeySequence
            canon = QKeySequence(combo).toString()
            fn = handlers.get(canon)
        if fn is None:
            return
        fn()

    def _setup_hotkeys(self):
        """Total Commander / Norton Commander style function keys."""
        # Map every bound combo to its handler so act_hotkey can
        # fire them by name (e.g. when a button config has
        # action='hotkey' and param='Ctrl+B'). The map is keyed by
        # the canonical Qt sequence string so 'Ctrl+B' and 'ctrl+b'
        # would NOT collide - we normalize on lookup instead.
        self._hotkey_handlers = {}

        def bind(seq, fn):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(fn)
            self._hotkey_handlers[seq] = fn
            return sc

        # --- F-keys ---
        bind("F1",  self._show_readme)                              # Help: README
        bind("F2",  lambda: (self.left_lister.refresh(),
                             self.right_lister.refresh()))           # Refresh
        bind("F3",  lambda: self.actions.dispatch("read"))           # View
        bind("F4",  lambda: self.actions.dispatch("edit"))           # Edit
        bind("F5",  lambda: self.actions.dispatch("copy"))           # Copy
        bind("F6",  lambda: self.actions.dispatch("move"))           # Move
        bind("F7",  lambda: self.actions.dispatch("makedir"))        # New folder
        bind("F8",  lambda: self.actions.dispatch("delete"))         # Delete
        bind("Del", lambda: self.actions.dispatch("delete"))
        bind("F9",  lambda: self.actions.dispatch("hexread"))        # Hex
        bind("F10", lambda: self.actions.dispatch("config"))         # Config

        # --- Shift+F-keys ---
        bind("Shift+F4",  self._hotkey_new_text_file)                # New text file + edit
        bind("Shift+F5",  self._hotkey_copy_same_dir)                # Copy with new name (same dir)
        bind("Shift+F6",  lambda: self.actions.dispatch("rename"))   # Inline rename
        bind("Shift+Del", self._hotkey_shift_delete)                 # Permanent delete
        bind("Shift+F10", self._hotkey_context_menu)                 # Context menu

        # --- Alt+F-keys ---
        bind("Alt+F1",  lambda: self._focus_drive_panel('left'))     # Left drive select
        bind("Alt+F2",  lambda: self._focus_drive_panel('right'))    # Right drive select
        bind("Alt+F3",  self._hotkey_alt_f3)                         # Alt viewer
        bind("Alt+F4",  self.close)
        bind("Alt+F5",  lambda: self.actions.dispatch("archive"))    # Pack
        bind("Alt+F7",  lambda: self.actions.dispatch("search"))     # Search
        bind("Alt+F",   self._hotkey_show_fileid_diz)                # FILE_ID.DIZ preview
        bind("Alt+F9",  lambda: self.actions.dispatch("extract"))    # Unpack
        bind("Alt+F10", lambda: self.actions.dispatch("archive"))    # Archive (fallback)
        bind("Alt+F11", lambda: self.actions.dispatch("compare"))    # File compare
        bind("Alt+U",   lambda: self.actions.dispatch("u64view"))    # Ultimate64 stream viewer
        bind("Alt+Return", lambda: self.actions.dispatch("info"))    # Properties
        bind("Alt+Enter",  lambda: self.actions.dispatch("info"))

        # --- Ctrl combinations ---
        bind("Ctrl+A",     lambda: self._active_lister()[0].select_all())
        bind("Ctrl+B",     self._hotkey_branch_view)                 # Flat branch view
        bind("Ctrl+C",     self._hotkey_clip_copy)                   # Clipboard copy
        bind("Ctrl+D",     self._hotkey_hotlist)                     # Directory hotlist
        bind("Ctrl+F",     lambda: self.actions.dispatch("ftp"))     # FTP connect (TC style)
        bind("Ctrl+H",     lambda: self.actions.dispatch("find"))
        bind("Ctrl+I",     lambda: self._active_lister()[0].model.invert_tags())
        bind("Ctrl+L",     lambda: self.actions.dispatch("getsizes"))  # Compute sizes
        bind("Ctrl+M",     lambda: self.actions.dispatch("multi_rename"))  # Multi-rename tool
        bind("Ctrl+N",     lambda: self.actions.dispatch("ftp"))       # New FTP (same as Ctrl+F)
        bind("Ctrl+Q",     self._hotkey_quick_view)                    # Quick view in opposite panel
        bind("Ctrl+R",     lambda: (self.left_lister.refresh(),
                                    self.right_lister.refresh()))
        bind("Ctrl+S",     self._hotkey_search_filter)                 # Quick search filter
        bind("Ctrl+T",     self._hotkey_toggle_button_layer)         # Toggle button layer (main <-> shift)
        bind("Ctrl+U",     lambda: self.actions.dispatch("swap"))
        bind("Ctrl+V",     self._hotkey_clip_paste)                    # Clipboard paste
        bind("Ctrl+W",     self._hotkey_tab_stub)                      # Close tab (not implemented)
        bind("Ctrl+X",     self._hotkey_clip_cut)                      # Clipboard cut
        bind("Ctrl+Z",     lambda: self.actions.dispatch("comment"))   # Edit comment
        bind("Ctrl+Space", self._hotkey_toggle_tag)                    # Toggle tag of current row
        bind("Ctrl+PgUp",  lambda: self._active_lister()[0].parent_dir())
        bind("Ctrl+\\",    lambda: self._active_lister()[0].root_dir()) # Go to root
        bind("Ctrl+Return",       self._hotkey_copy_filename)           # Copy filename to clipboard
        bind("Ctrl+Enter",        self._hotkey_copy_filename)
        bind("Ctrl+Shift+Return", self._hotkey_copy_fullpath)           # Copy full path
        bind("Ctrl+Shift+Enter",  self._hotkey_copy_fullpath)
        bind("Ctrl+Shift+F",      self._hotkey_ftp_disconnect)          # FTP disconnect
        bind("Backspace",  self._hotkey_backspace)

        # Ctrl+Fn sort shortcuts (TC style)
        bind("Ctrl+F1",    lambda: self._hotkey_view_mode('short'))    # Brief view
        bind("Ctrl+F2",    lambda: self._hotkey_view_mode('full'))     # Full/details view
        bind("Ctrl+F3",    lambda: self._hotkey_sort_col(DirModel.SORT_NAME))
        bind("Ctrl+F4",    lambda: self._hotkey_sort_col(DirModel.SORT_EXT))
        bind("Ctrl+F5",    lambda: self._hotkey_sort_col(DirModel.SORT_TIME))
        bind("Ctrl+F6",    lambda: self._hotkey_sort_col(DirModel.SORT_SIZE))

        # Send path: Ctrl+Left = send active path to right lister, Ctrl+Right = to left
        bind("Ctrl+Left",  lambda: self._hotkey_send_path('left'))
        bind("Ctrl+Right", lambda: self._hotkey_send_path('right'))

        # --- Numpad tagging (Norton/TC classic) ---
        bind("Num+*",      lambda: self._active_lister()[0].model.invert_tags())
        bind("Num++",      self._hotkey_tag_by_pattern)               # Tag by wildcard
        bind("Num+-",      self._hotkey_untag_by_pattern)             # Untag by wildcard

    # ==================================================================
    # Hotkey helper methods
    # ==================================================================
    def _hotkey_backspace(self):
        """Backspace = parent dir, but only when no QLineEdit has focus
        (so you can still delete characters in the path bar)."""
        from PyQt6.QtWidgets import QApplication, QLineEdit
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit):
            return
        self._active_lister()[0].parent_dir()

    def _hotkey_new_text_file(self):
        """Shift+F4: create an empty text file and open in editor."""
        from PyQt6.QtWidgets import QInputDialog
        src, _ = self._active_lister()
        name, ok = QInputDialog.getText(self, "New text file",
                                         "Filename:", text="new.txt")
        if not ok or not name.strip():
            return
        p = src.current_path / name.strip()
        try:
            if not p.exists():
                p.write_text("", encoding="utf-8")
            src.refresh()
            # Dispatch to configured editor for that file
            src._dispatch_view(p, action='editor')
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "New file", str(e))

    def _hotkey_copy_same_dir(self):
        """Shift+F5: copy selected file under a new name in the same directory."""
        import shutil
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths: return
        p = paths[0]
        new_name, ok = QInputDialog.getText(
            self, "Copy as...", f"New name for {p.name}:", text=p.name)
        if not ok or not new_name.strip() or new_name == p.name: return
        target = p.parent / new_name.strip()
        if target.exists():
            if QMessageBox.question(self, "Overwrite",
                f"{target.name} exists. Overwrite?"
                ) != QMessageBox.StandardButton.Yes:
                return
        try:
            if p.is_dir():
                shutil.copytree(p, target, dirs_exist_ok=True)
            else:
                shutil.copy2(p, target)
            src.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Copy", str(e))

    def _hotkey_shift_delete(self):
        """Shift+Del: permanent delete (skip recycle bin).
        Our act_delete already does direct delete; this is the same action
        but we keep the binding for TC muscle memory."""
        self.actions.dispatch("delete")

    def _hotkey_context_menu(self):
        """Shift+F10: show the lister context menu at the current row."""
        from PyQt6.QtCore import QPoint
        src, _ = self._active_lister()
        # Position menu near the current row
        idx = src.view.currentIndex()
        if idx.isValid():
            rect = src.view.visualRect(idx)
            pos = src.view.mapToGlobal(rect.center())
        else:
            pos = src.view.mapToGlobal(QPoint(20, 20))
        src._ctx_menu(src.view.mapFromGlobal(pos))

    def _hotkey_alt_f3(self):
        """Alt+F3: Toggle between internal text viewer and system default.
        Simpler: open with system default app."""
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths: return
        src._open_file(paths[0])

    def _focus_drive_panel(self, which):
        """Alt+F1/F2: open a drive picker for left/right side."""
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtGui import QCursor
        menu = QMenu(self)
        lister = self.left_lister if which == 'left' else self.right_lister
        for drv in self.config.get("drives", []):
            label = drv.get('label', '?')
            path  = drv.get('path', '')
            menu.addAction(f"{label}   ({path})",
                           lambda p=path, l=lister: l.goto(p))
        menu.exec(QCursor.pos())

    def _hotkey_search_filter(self):
        """Ctrl+S: type-ahead filter. Prompt for a pattern and tag matching entries."""
        from PyQt6.QtWidgets import QInputDialog
        src, _ = self._active_lister()
        patt, ok = QInputDialog.getText(self, "Quick filter",
            "Substring to match (case-insensitive):")
        if not ok or not patt: return
        p = patt.lower()
        src.model.clear_tags()
        matches = 0
        for i, e in enumerate(src.model.entries):
            if p in e.name.lower():
                src.model.tagged.add(e.path); matches += 1
        src.model.layoutChanged.emit()
        self.lbl_status.setText(f" Filtered: {matches} match(es) for '{patt}' ")

    def _hotkey_toggle_tag(self):
        """Ctrl+Space: toggle tag on currently highlighted row."""
        src, _ = self._active_lister()
        idx = src.view.currentIndex()
        if idx.isValid():
            src.model.toggle_tag(idx.row())

    def _hotkey_send_path(self, target):
        """Ctrl+Left/Right: send the active side's path to the other side."""
        src, dst = self._active_lister()
        # target='left' means send active path to the LEFT lister, etc.
        receiver = self.left_lister if target == 'left' else self.right_lister
        receiver.goto(str(src.current_path))

    def _hotkey_tag_by_pattern(self):
        """Num+ : tag files matching a wildcard pattern (fnmatch-style)."""
        import fnmatch
        from PyQt6.QtWidgets import QInputDialog
        src, _ = self._active_lister()
        patt, ok = QInputDialog.getText(self, "Tag by pattern",
            "Wildcard (e.g. *.txt, *.c, *.lha):", text="*")
        if not ok or not patt: return
        n = 0
        for e in src.model.entries:
            if fnmatch.fnmatch(e.name, patt):
                src.model.tagged.add(e.path); n += 1
        src.model.layoutChanged.emit()
        self.lbl_status.setText(f" Tagged {n} file(s) matching '{patt}' ")

    def _hotkey_untag_by_pattern(self):
        """Num- : untag files matching a wildcard pattern."""
        import fnmatch
        from PyQt6.QtWidgets import QInputDialog
        src, _ = self._active_lister()
        patt, ok = QInputDialog.getText(self, "Untag by pattern",
            "Wildcard:", text="*")
        if not ok or not patt: return
        n = 0
        to_remove = [e.path for e in src.model.entries
                     if fnmatch.fnmatch(e.name, patt) and e.path in src.model.tagged]
        for pth in to_remove:
            src.model.tagged.discard(pth); n += 1
        src.model.layoutChanged.emit()
        self.lbl_status.setText(f" Untagged {n} file(s) matching '{patt}' ")

    # --- Clipboard ------------------------------------------------------
    def _hotkey_clip_copy(self):
        """Ctrl+C: copy selected paths to the system clipboard as URLs."""
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QMimeData, QUrl
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths:
            self.lbl_status.setText(" Nothing to copy "); return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        mime.setText("\n".join(str(p) for p in paths))
        QApplication.clipboard().setMimeData(mime)
        self._clip_cut_paths = None  # clear cut state
        self.lbl_status.setText(f" Copied {len(paths)} path(s) to clipboard ")

    def _hotkey_clip_cut(self):
        """Ctrl+X: mark selected paths as cut (copy+remember for paste)."""
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QMimeData, QUrl
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths:
            self.lbl_status.setText(" Nothing to cut "); return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        mime.setText("\n".join(str(p) for p in paths))
        # Set a custom flag so our paste knows to delete source
        mime.setData("application/x-quopus-cut", b"1")
        QApplication.clipboard().setMimeData(mime)
        self._clip_cut_paths = list(paths)
        self.lbl_status.setText(f" Cut {len(paths)} path(s) - ready to paste ")

    def _hotkey_clip_paste(self):
        """Ctrl+V: paste files from clipboard into the active directory."""
        import shutil
        from pathlib import Path
        from PyQt6.QtWidgets import QApplication, QMessageBox
        src, _ = self._active_lister()
        mime = QApplication.clipboard().mimeData()
        if not mime.hasUrls():
            self.lbl_status.setText(" Clipboard has no files "); return
        is_cut = bool(mime.data("application/x-quopus-cut").size())
        dest_dir = src.current_path
        n_ok = 0; errors = []
        for url in mime.urls():
            if not url.isLocalFile(): continue
            p = Path(url.toLocalFile())
            if not p.exists(): continue
            target = dest_dir / p.name
            try:
                if is_cut:
                    shutil.move(str(p), str(target))
                elif p.is_dir():
                    shutil.copytree(p, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(p, target)
                n_ok += 1
            except Exception as e:
                errors.append(f"{p.name}: {e}")
        src.refresh()
        # Refresh sibling too if it's the source
        if is_cut:
            _, other = self._active_lister()
            other.refresh()
            QApplication.clipboard().clear()
        msg = f"Pasted {n_ok}" + (" (move)" if is_cut else " (copy)")
        if errors:
            msg += f"; {len(errors)} error(s)"
            QMessageBox.warning(self, "Paste", "\n".join(errors[:10]))
        self.lbl_status.setText(f" {msg} ")

    def _hotkey_copy_filename(self):
        """Ctrl+Enter: copy selected filename (basename) to clipboard."""
        from PyQt6.QtWidgets import QApplication
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths: return
        text = "\n".join(p.name for p in paths)
        QApplication.clipboard().setText(text)
        self.lbl_status.setText(f" Copied filename(s): {text[:60]} ")

    def _hotkey_copy_fullpath(self):
        """Ctrl+Shift+Enter: copy selected full paths to clipboard."""
        from PyQt6.QtWidgets import QApplication
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths: return
        text = "\n".join(str(p) for p in paths)
        QApplication.clipboard().setText(text)
        self.lbl_status.setText(f" Copied full path(s) ")

    # --- Directory hotlist ----------------------------------------------
    def _hotkey_hotlist(self):
        """Ctrl+D: directory hotlist - quick jump to favorite folders.
        Stored in config['hotlist'] as a list of paths."""
        from PyQt6.QtWidgets import QMenu, QInputDialog
        from PyQt6.QtGui import QCursor
        from pathlib import Path
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: {C.WB_GREY}; color: {C.BLACK};
                    border: 1px solid {C.BLACK};
                    font-family: 'Topaz','Courier New',monospace; }}
            QMenu::item {{ padding: 4px 24px; }}
            QMenu::item:selected {{ background-color: {C.SELECTED}; color: white; }}
        """)
        hotlist = self.config.setdefault("hotlist", [])
        src, _ = self._active_lister()
        if hotlist:
            for path in hotlist:
                menu.addAction(path,
                               lambda p=path, l=src: l.goto(p))
            menu.addSeparator()
        menu.addAction(
            f"Add current: {src.current_path}",
            lambda p=str(src.current_path): self._hotlist_add(p))
        if hotlist:
            menu.addAction("Remove an entry...", self._hotlist_remove)
        menu.exec(QCursor.pos())

    def _hotlist_add(self, path):
        lst = self.config.setdefault("hotlist", [])
        if path not in lst:
            lst.append(path)
            from .config import save_config
            save_config(self.config)
            self.lbl_status.setText(f" Added to hotlist: {path} ")

    def _hotlist_remove(self):
        from PyQt6.QtWidgets import QInputDialog
        lst = self.config.get("hotlist", [])
        if not lst: return
        item, ok = QInputDialog.getItem(self, "Remove from hotlist",
            "Entry to remove:", lst, 0, False)
        if ok and item in lst:
            lst.remove(item)
            from .config import save_config
            save_config(self.config)
            self.lbl_status.setText(f" Removed from hotlist: {item} ")

    # --- Quick view -----------------------------------------------------
    def _hotkey_quick_view(self):
        """Ctrl+Q: show preview of the selected file in the opposite panel.
        We approximate by opening the internal TextReader/ImageViewer
        (the 'opposite panel' concept would need a real split-view)."""
        src, _ = self._active_lister()
        paths = src.selected_or_tagged()
        if not paths:
            return
        src._dispatch_view(paths[0], action='viewer')

    # --- Branch view (Ctrl+B) -------------------------------------------
    def _hotkey_branch_view(self):
        """Ctrl+B: recursively list all files under the active directory
        and display them as a flat list (Total Commander 'branch view').
        Pressing Ctrl+B again (in branch mode) exits back to normal."""
        src, _ = self._active_lister()
        if getattr(src, '_branch_mode', False):
            # Exit branch mode
            src._branch_mode = False
            src.refresh()
            self.lbl_status.setText(" Branch view off ")
            return
        from .dirmodel import DirEntry
        import os
        root = src.current_path
        entries = []
        try:
            for dirpath, dirs, files in os.walk(root):
                for fname in files:
                    fp = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    # Display relative path for clarity
                    rel = os.path.relpath(fp, root)
                    entries.append(DirEntry(
                        path=fp, name=rel, is_dir=False,
                        size=st.st_size, mtime=st.st_mtime,
                    ))
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Branch view", str(e))
            return
        src._branch_mode = True
        src.model.set_entries(entries)
        self.lbl_status.setText(
            f" Branch view: {len(entries)} file(s) under {root}  (Ctrl+B to exit) ")

    # --- Sort shortcuts (Ctrl+F3..F6) ------------------------------------
    def _hotkey_sort_col(self, key):
        src, _ = self._active_lister()
        # If already sorted by this key, toggle reverse (TC behaviour)
        if src.model.sort_key == key:
            src.model.toggle_reverse()
        else:
            src.model.set_sort(key, reverse=False)
        # Update visual indicators
        if hasattr(src, '_update_sort_header_labels'):
            src._update_sort_header_labels()
        # Persist to config so the choice survives a restart
        src._save_sort_state()

    def _hotkey_view_mode(self, mode):
        """Ctrl+F1/F2: switch between brief (name only) and full view.
        Our lister always shows full details; this is a stub for TC parity."""
        self.lbl_status.setText(
            " Brief/full view modes not available in this build ")

    # --- Tabs (stub) -----------------------------------------------------
    def _hotkey_tab_stub(self):
        """Ctrl+W: tabs not implemented in this build."""
        self.lbl_status.setText(" Tabs are not implemented in this build ")

    def _hotkey_toggle_button_layer(self):
        """Ctrl+T: cycle the button area through main -> Shift ->
        Shift+Alt -> main. Unlike modifier-hold (which switches only
        while keys are held), this cycle is persistent - the layer
        stays switched until Ctrl+T is pressed again.

        Sets self._layer_toggle_sticky while NOT on the main layer,
        so the modifier-key event filter (which normally swaps
        layers based on Shift/Alt being held) does not interfere.
        Cycling back to "main" clears the sticky flag.
        """
        # Cycle order: main -> shift -> shift_alt -> main
        cycle = ["main", "shift", "shift_alt"]
        cur = getattr(self, '_active_layer', 'main')
        try:
            idx = cycle.index(cur)
        except ValueError:
            idx = 0
        new_layer = cycle[(idx + 1) % len(cycle)]
        self._set_active_layer(new_layer, sticky=(new_layer != "main"))
        try:
            self._rebuild_buttons()
        except Exception:
            pass
        name = self._layer_name()
        self.lbl_status.setText(
            f" Button layer: {name}"
            f"{'  (Ctrl+T to cycle)' if new_layer != 'main' else ''} ")

    def _set_active_layer(self, layer: str, sticky: bool = False):
        """Single point of truth for switching layers. Keeps the
        legacy _shift_layer_active flag in sync so callers that
        haven't migrated to _active_layer still get the right
        behaviour (it's True for any non-main layer)."""
        if layer not in ("main", "shift", "shift_alt"):
            layer = "main"
        self._active_layer = layer
        self._shift_layer_active = (layer != "main")
        self._layer_toggle_sticky = sticky

    # --- F1 help ---------------------------------------------------------
    def _show_readme(self):
        """F1: show README.md in the internal text viewer.
        Looks for README.md in the app directory; falls back to a minimal
        help message if not found."""
        from pathlib import Path
        from PyQt6.QtWidgets import QMessageBox
        # Locate README.md: same dir as quopus.py or the lib parent
        candidates = [
            Path(__file__).resolve().parent.parent / "README.md",
            Path.cwd() / "README.md",
        ]
        readme = next((p for p in candidates if p.is_file()), None)
        if readme is None:
            QMessageBox.information(
                self, "Help",
                "README.md not found in the Quopus directory.\n\n"
                "F1 should open README.md next to quopus.py.")
            return
        from .readers import TextReader
        r = TextReader(readme, self)
        r.setWindowTitle("Quopus - Help (README.md)")
        r.exec()

    # --- FTP disconnect --------------------------------------------------
    def _hotkey_show_fileid_diz(self):
        """Alt+F: toggle FILE_ID.DIZ preview in the OPPOSITE lister.
        While active, the preview updates whenever the selection in the
        active lister changes (arrow keys, click)."""
        src, dst = self._active_lister()
        # Already showing DIZ on the other side? turn it off
        if getattr(dst, '_diz_panel_active', False):
            self._diz_panel_hide(dst)
            return
        # Or maybe it's active on the active side (user toggled it then switched)
        if getattr(src, '_diz_panel_active', False):
            self._diz_panel_hide(src)
            return
        self._diz_panel_show(dst, src)
        self._diz_panel_update()

    def _diz_panel_show(self, target_lister, source_lister):
        """Install a DIZ preview overlay over target_lister."""
        from PyQt6.QtWidgets import QPlainTextEdit, QLabel, QVBoxLayout, QWidget

        panel = QWidget(target_lister)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)

        title = QLabel("  FILE_ID.DIZ  —  (Alt+F to close)  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        lay.addWidget(title)

        te = QPlainTextEdit()
        te.setReadOnly(True)
        te.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        te.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: #000000; color: #cccccc;
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(13)}px;
                border: 1px solid {C.BLACK};
                padding: 4px;
            }}
        """)
        lay.addWidget(te, 1)

        target_lister._diz_panel = panel
        target_lister._diz_panel_text = te
        target_lister._diz_panel_title = title
        target_lister._diz_panel_active = True

        self._diz_source_lister = source_lister
        sel_model = source_lister.view.selectionModel()
        if sel_model is not None:
            try:
                sel_model.currentChanged.disconnect(self._diz_panel_update)
            except TypeError:
                pass
            sel_model.currentChanged.connect(self._diz_panel_update)
        try:
            source_lister.got_focus.disconnect(self._diz_panel_update)
        except Exception: pass
        source_lister.got_focus.connect(self._diz_panel_update)

        panel.setGeometry(0, 0, target_lister.width(), target_lister.height())
        panel.raise_()
        panel.show()

        # Install filter so we can resize overlay when lister resizes
        target_lister.installEventFilter(self)

        self.lbl_status.setText(" FILE_ID.DIZ preview ON — Alt+F to close ")

    def _diz_panel_hide(self, target_lister):
        panel = getattr(target_lister, '_diz_panel', None)
        if panel is not None:
            panel.deleteLater()
        target_lister._diz_panel = None
        target_lister._diz_panel_text = None
        target_lister._diz_panel_title = None
        target_lister._diz_panel_active = False
        src = getattr(self, '_diz_source_lister', None)
        if src is not None:
            try:
                src.view.selectionModel().currentChanged.disconnect(
                    self._diz_panel_update)
            except Exception: pass
            try:
                src.got_focus.disconnect(self._diz_panel_update)
            except Exception: pass
        self._diz_source_lister = None
        self.lbl_status.setText(" FILE_ID.DIZ preview OFF ")

    def _diz_panel_update(self, *args):
        """Refresh the DIZ panel based on the cursor row in source lister."""
        src = getattr(self, '_diz_source_lister', None)
        if src is None: return
        dst = None
        for l in (self.left_lister, self.right_lister):
            if getattr(l, '_diz_panel_active', False):
                dst = l; break
        if dst is None: return
        te = dst._diz_panel_text
        title = dst._diz_panel_title
        if te is None: return

        idx = src.view.currentIndex()
        if not idx.isValid():
            te.setPlainText("(no file selected)")
            return
        e = src.model.entry_at(idx.row())
        if not e:
            te.setPlainText("")
            return
        from pathlib import Path
        if src.fs.kind == 'remote':
            te.setPlainText(f"[FTP] {e.name}\n\n"
                            "FILE_ID.DIZ preview not supported for remote files.")
            if title:
                title.setText(f"  FILE_ID.DIZ  —  {e.name}  (Alt+F to close)  ")
            return
        p = Path(e.path)
        diz = self._extract_diz(p)
        if diz is None:
            te.setPlainText(f"{e.name}\n\n(no FILE_ID.DIZ found)")
        else:
            # Strip ANSI colour codes for clean display - the panel isn't
            # an ANSI terminal
            import re as _re
            diz = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', diz)
            te.setPlainText(diz.rstrip() + "\n")
        if title:
            title.setText(f"  FILE_ID.DIZ  —  {e.name}  (Alt+F to close)  ")

    def _extract_diz(self, path):
        """Return FILE_ID.DIZ content (str) from a path, or None.
        Path can be:
          - archive (ZIP/TAR/LHA): searches for FILE_ID.DIZ inside
          - directory: looks for FILE_ID.DIZ / FILE_ID.TXT
          - DMS file: extracts the ASCII banner from the header
          - NFO/TXT/DIZ file: reads content directly
          - anything with a sidecar .diz/.nfo: uses that
        """
        from pathlib import Path
        import zipfile, tarfile

        if path.is_dir():
            for n in ("FILE_ID.DIZ", "file_id.diz", "File_id.diz",
                      "FILE_ID.TXT", "file_id.txt"):
                p = path / n
                if p.is_file():
                    try: return p.read_text(errors='replace')
                    except Exception: return None
            return None

        ext = path.suffix.lower()

        def _match(name):
            return name.lower().split("/")[-1] in (
                "file_id.diz", "file_id.txt")

        if ext == ".zip":
            try:
                with zipfile.ZipFile(path) as zf:
                    for info in zf.infolist():
                        if _match(info.filename):
                            try:
                                return zf.read(info).decode(
                                    "cp437", errors='replace')
                            except Exception:
                                return zf.read(info).decode(
                                    errors='replace')
            except Exception:
                pass
            # fall through to sidecar

        elif ext in (".tar", ".gz", ".bz2", ".xz", ".tgz",
                     ".tbz", ".tbz2", ".txz"):
            try:
                with tarfile.open(path) as tf:
                    for m in tf.getmembers():
                        if m.isfile() and _match(m.name):
                            f = tf.extractfile(m)
                            if f:
                                try:
                                    return f.read().decode(
                                        "cp437", errors='replace')
                                except Exception:
                                    return f.read().decode(errors='replace')
            except Exception:
                pass

        elif ext in (".lha", ".lzh"):
            try:
                import lhafile
                lf = lhafile.Lhafile(str(path))
                for info in lf.infolist():
                    if info.directory: continue
                    if _match(info.filename):
                        data = lf.read(info.filename)
                        try:
                            return data.decode("cp437", errors='replace')
                        except Exception:
                            return data.decode(errors='replace')
            except ImportError:
                return "(install 'lhafile' to read LHA archives)"
            except Exception:
                pass

        elif ext == ".dms":
            # DMS files may have a FILE_ID.DIZ stored as a special track
            # (block number 80, above the usual 0-79 disk tracks).
            try:
                data = path.read_bytes()
                if data[:4] == b"DMS!":
                    diz = self._extract_dms_diz_track(data)
                    if diz:
                        return diz
                    # Fallback: the header banner (old behaviour)
                    banner = self._extract_dms_banner(data)
                    if banner:
                        return ("(no FILE_ID.DIZ track - showing header "
                                "banner instead)\n\n" + banner)
            except Exception:
                pass

        # Plain text file: if its name is FILE_ID.DIZ or similar,
        # or if its extension is text-like, look for BEGIN/END markers
        # first - many scene .nfo files wrap the DIZ in markers like:
        #   @BEGIN_FILE_ID.DIZ
        #   ... diz content ...
        #   @END_FILE_ID.DIZ
        # If markers are found, return just what's between them.
        # Otherwise return the whole file (it IS the diz).
        if path.name.lower() in ("file_id.diz", "file_id.txt") \
           or ext in (".nfo", ".diz", ".txt", ".info", ".me", ".1st",
                      ".readme", ".asc"):
            try:
                data = path.read_bytes()
                text = data.decode("cp437", errors='replace')
                extracted = self._extract_between_markers(text)
                return extracted if extracted is not None else text
            except Exception:
                return None

        # Sidecar .diz/.nfo next to the archive? (e.g. foo.zip + foo.diz)
        for suf in (".diz", ".DIZ", ".nfo", ".NFO"):
            sidecar = path.with_suffix(suf)
            if sidecar.is_file():
                try:
                    return sidecar.read_bytes().decode(
                        "cp437", errors='replace')
                except Exception: pass

        return None

    def _extract_between_markers(self, text):
        """If the text contains @BEGIN_FILE_ID.DIZ ... @END_FILE_ID.DIZ
        (or variants), return only the content between them. Otherwise
        return None (caller will use the full text)."""
        import re as _re
        # Handle @BEGIN..@END style (scene releases)
        m = _re.search(r'@BEGIN[_\s]*FILE[_\s]*ID\.?DIZ\s*(.*?)\s*@END[_\s]*FILE[_\s]*ID\.?DIZ',
                        text, _re.DOTALL | _re.IGNORECASE)
        if m:
            content = m.group(1)
            # If content starts on same line as marker with spaces, trim
            content = content.lstrip('\r\n')
            return content.rstrip() + "\n"
        # ASCII-markup style
        m = _re.search(r'-+BEGIN\s+FILE[_\s]*ID\.?DIZ-+\s*(.*?)\s*-+END\s+FILE[_\s]*ID\.?DIZ-+',
                        text, _re.DOTALL | _re.IGNORECASE)
        if m:
            return m.group(1).lstrip('\r\n').rstrip() + "\n"
        return None

    def _extract_dms_diz_track(self, data):
        """Scan DMS track headers looking for a special FILE_ID.DIZ track.
        DMS stores the DIZ as block #80 (above normal 0-79 disk tracks),
        typically uncompressed with rlen == ulen.
        Returns decoded DIZ text, or None."""
        import struct
        # DMS track header format: 'TR' + block_nr(word) + _(word)
        # + rlen(word) + ulen(word) + flags(byte) + type(byte)
        # + crc_data(word) + crc_hdr(word) = 18 bytes header,
        # followed by `rlen` bytes of payload.
        i = 0
        while i < len(data) - 18:
            p = data.find(b'TR', i)
            if p < 0: break
            if p + 18 > len(data):
                break
            try:
                block_nr = struct.unpack('>H', data[p+2:p+4])[0]
                rlen = struct.unpack('>H', data[p+6:p+8])[0]
                ulen = struct.unpack('>H', data[p+8:p+10])[0]
            except Exception:
                i = p + 1; continue
            # A real track has a plausible rlen (between a few and ~32K)
            # and block_nr <= ~85 (allowing for overhead tracks)
            if 8 <= rlen <= 32768 and 0 <= block_nr <= 200:
                # Special track 80 (and sometimes other high numbers)
                # holds FILE_ID.DIZ
                if block_nr == 80 and rlen == ulen and rlen < 2048:
                    payload_start = p + 18
                    payload = data[payload_start:payload_start + rlen]
                    return self._clean_dms_diz(payload)
                # Skip past this track's payload to the next TR marker
                i = p + 18 + rlen
                continue
            i = p + 1
        return None

    def _clean_dms_diz(self, payload):
        """Clean up an uncompressed DIZ payload from a DMS track.
        The first bytes are often binary/alphanumeric control bytes
        (length/RLE markers) before the actual ASCII-art banner begins."""
        import re as _re
        if not payload:
            return None
        text = payload.decode('cp437', errors='replace')
        # Strip ANSI codes up front
        text = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

        # Find the start of the real ASCII art. Banners always begin with
        # a run of box-drawing / frame characters: . | _ - + / \ ` space
        # Skip any leading bytes that aren't one of those (a control byte,
        # a random letter/digit from a length prefix, etc.).
        BOX_CHARS = set(" .|_-+/\\`'\t\n\r")
        start_idx = 0
        for i, ch in enumerate(text):
            # A position is a valid start if it's a box char AND the next
            # 3 chars are also all box chars (so we don't match a single
            # dot inside a name)
            if ch in BOX_CHARS and all(
                    j < len(text) and text[j] in BOX_CHARS
                    for j in range(i, min(i + 4, len(text)))):
                start_idx = i
                break

        text = text[start_idx:]
        # Trim trailing garbage
        text = text.rstrip('\x00 \t\r\n')
        if len(text) < 8:
            return None
        return text


        """Extract the banner text from a DMS file's header.
        The banner starts at offset 0x50. It ends at the first occurrence
        of a DMS track marker ('TR' followed by small binary fields),
        or when 8+ consecutive binary bytes appear.
        Returns cp437-decoded text with ANSI codes stripped, or None."""
        if len(data) < 0x60: return None
        start = 0x50
        limit = min(len(data), 16 * 1024)
        end = start

        # Find the first track marker - 'TR' + byte < 0x20 (typically null)
        # The first track marker ends the banner
        tr_pos = -1
        for i in range(start, limit - 3):
            if data[i:i+2] == b'TR' and data[i+2] < 0x20 and data[i+3] < 0x20:
                tr_pos = i
                break

        if tr_pos > start:
            end = tr_pos
        else:
            # Fallback: find a run of 8+ binary bytes
            consecutive_bin = 0
            for i in range(start, limit):
                b = data[i]
                is_text = (
                    0x20 <= b < 0x7F or
                    b in (0x09, 0x0A, 0x0D) or
                    b == 0x1B or
                    0xA0 <= b <= 0xFE
                )
                if is_text:
                    consecutive_bin = 0
                    end = i + 1
                else:
                    consecutive_bin += 1
                    if consecutive_bin >= 8:
                        break

        if end - start < 16:
            return None
        banner = data[start:end]
        banner = banner.rstrip(b'\x00\x20\t\r\n')
        if not banner: return None
        text = banner.decode("cp437", errors='replace')
        # Strip ANSI escape sequences (ESC[...m) so the DIZ panel shows
        # clean text. Keep the actual characters.
        import re as _re
        text = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        return text

    def _show_diz_popup(self, text, source_name):
        """Display DIZ text in a small Quopus-styled popup."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, \
            QPlainTextEdit, QPushButton, QHBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(f"FILE_ID.DIZ  —  {source_name}")
        dlg.resize(540, 340)
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        root = QVBoxLayout(dlg)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(2)

        title = QLabel(f"  FILE_ID.DIZ   -   {source_name}  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        te = QPlainTextEdit(text.rstrip() + "\n")
        te.setReadOnly(True)
        te.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        te.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: #000000; color: #cccccc;
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(13)}px;
                border: 1px solid {C.BLACK};
                padding: 4px;
            }}
        """)
        root.addWidget(te, 1)

        br = QHBoxLayout(); br.addStretch()
        bc = QPushButton("Close (Esc)")
        bc.setStyleSheet(button_qss("red"))
        bc.setFixedWidth(120)
        bc.setDefault(True)
        bc.clicked.connect(dlg.accept)
        br.addWidget(bc)
        root.addLayout(br)

        from PyQt6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Escape"), dlg, dlg.accept)

        dlg.exec()

    def _hotkey_ftp_disconnect(self):
        """Ctrl+Shift+F: disconnect any mounted FTP session in the listers,
        switch back to local filesystem."""
        any_disconnected = False
        for lister in (self.left_lister, self.right_lister):
            if lister.fs.kind == 'remote':
                lister.disconnect_remote()
                any_disconnected = True
        if any_disconnected:
            self.lbl_status.setText(" FTP disconnected ")
        else:
            self.lbl_status.setText(" No active FTP connection ")

    def _on_device_nav(self, dev: dict, target: str):
        """A drive button was clicked. dev is the full device dict
        (with label/path/type/etc). target is one of:
            'normal' - the bookmark's configured default (open_in)
            'both'   - both panels at once (shift+click forces this)
            'right'  - right panel only (middle click forces this)

        Path resolution rules:
          - dev['path']        -> default path, used for left + active
          - dev['path_right']  -> optional, used for right when set;
                                  falls back to dev['path'] otherwise
          - dev['open_in']     -> default click target ('active',
                                  'both', 'left', 'right'); only used
                                  when `target == 'normal'`
        """
        kind = dev.get("type", "local")
        if kind == "ftp":
            self._connect_ftp_bookmark(dev, target)
            return
        path_left = dev.get("path", "")
        if not path_left:
            return
        # Right path falls back to left path if not configured
        path_right = dev.get("path_right") or path_left

        # Resolve target. Modifier shortcuts (Shift = both, Middle =
        # right) override whatever the bookmark's open_in says, so
        # the user always has manual control. Plain click consults
        # the bookmark's configured open_in.
        if target == 'normal':
            effective = dev.get("open_in", "active")
        else:
            effective = target

        if effective == 'both':
            self.left_lister.goto(path_left)
            self.right_lister.goto(path_right)
            self._status(f"-> L: {path_left}  /  R: {path_right}")
        elif effective == 'left':
            self.left_lister.goto(path_left)
            self._status(f"-> L: {path_left}")
        elif effective == 'right':
            self.right_lister.goto(path_right)
            self._status(f"-> R: {path_right}")
        else:
            # 'active' - whatever panel is focused, but use the
            # side-specific path. If the active panel is the right
            # one and a path_right is configured, use it; otherwise
            # use the main path.
            src, _ = self._active_lister()
            if src is self.right_lister and path_right != path_left:
                src.goto(path_right)
                self._status(f"-> {path_right}")
            else:
                src.goto(path_left)
                self._status(f"-> {path_left}")

    def _connect_ftp_bookmark(self, dev: dict, target: str):
        """Open an FTP connection from a saved bookmark dict.
        Falls back to a password prompt if none is stored."""
        from PyQt6.QtWidgets import QInputDialog, QLineEdit
        host = dev.get("host", "")
        port = dev.get("port", 21)
        user = dev.get("user", "anonymous")
        password = dev.get("password")
        if password is None:
            password, ok = QInputDialog.getText(
                self, f"FTP password for {user}@{host}",
                f"Password:", QLineEdit.EchoMode.Password)
            if not ok:
                return
        # Pick which lister(s) get the connection
        targets = []
        if target == 'both':
            targets = [self.left_lister, self.right_lister]
        elif target == 'right':
            targets = [self.right_lister]
        else:
            src, _ = self._active_lister()
            targets = [src]
        try:
            from .ftp_backend import make_backend
            for lst in targets:
                backend = make_backend(
                    protocol=dev.get("protocol", "ftp"),
                    host=host, port=port,
                    user=user, password=password)
                backend.connect()
                init = dev.get("path", "/")
                if init and init != "/":
                    try: backend.cwd(init)
                    except Exception: pass
                label = dev.get("label", host)
                lst.set_remote_fs(backend, label)
            self._status(f"FTP -> {host}")
        except Exception as e:
            QMessageBox.warning(self, "FTP", f"Connect failed: {e}")

    def _on_devices_changed(self):
        self.config["drives"] = self.device_column.devices
        save_config(self.config)

    def _buttons_layer(self):
        """Return the currently-active button-grid list, depending on
        self._active_layer. Always returns an editable reference into
        self.config so writes propagate.

        Layer mapping:
          "main"       -> config["buttons"]
          "shift"      -> config["buttons_shift"]
          "shift_alt"  -> config["buttons_shift_alt"]

        For the two non-main layers, the config entry is auto-created
        as an empty 6x6 grid if it doesn't exist yet (upgrade path).
        """
        layer = getattr(self, '_active_layer', 'main')
        if layer == "shift":
            if "buttons_shift" not in self.config \
                    or not isinstance(self.config["buttons_shift"], list):
                self.config["buttons_shift"] = [
                    [None]*6 for _ in range(6)]
            return self.config["buttons_shift"]
        if layer == "shift_alt":
            if "buttons_shift_alt" not in self.config \
                    or not isinstance(self.config["buttons_shift_alt"], list):
                self.config["buttons_shift_alt"] = [
                    [None]*6 for _ in range(6)]
            return self.config["buttons_shift_alt"]
        return self.config["buttons"]

    def _layer_name(self):
        """Human label of the active layer for status messages."""
        layer = getattr(self, '_active_layer', 'main')
        return {
            "main":      "main layer",
            "shift":     "Shift-layer",
            "shift_alt": "Shift+Alt-layer",
        }.get(layer, "main layer")

    def _apply_size_display(self, mode: str):
        """Switch both listers' Size columns between 'bytes' and
        'blocks' display, and persist the choice to config."""
        if mode not in ("bytes", "blocks"):
            return
        self.config["size_display"] = mode
        errors = []
        for which, lst in (("left", self.left_lister),
                            ("right", self.right_lister)):
            try:
                lst.model.show_blocks = (mode == "blocks")
                # Force a real repaint of the Size column. layoutChanged
                # alone isn't enough on QTreeView - the cell's display
                # text comes from a cached QString that only refreshes
                # when dataChanged is emitted with DisplayRole. Emit
                # for the whole Size column (col 2) across all rows.
                from .dirmodel import COL_SIZE
                from PyQt6.QtCore import Qt as _Qt
                n = len(lst.model.order)
                if n > 0:
                    tl = lst.model.index(0, COL_SIZE)
                    br = lst.model.index(n - 1, COL_SIZE)
                    lst.model.dataChanged.emit(
                        tl, br, [_Qt.ItemDataRole.DisplayRole])
                # Also force the view to update visually
                lst.view.viewport().update()
            except Exception as e:
                errors.append(f"{which}: {e}")
                import traceback; traceback.print_exc()
        save_config(self.config)
        if errors:
            self._status(f"Size display: {mode}  (errors: "
                          f"{'; '.join(errors)})")
        else:
            self._status(f"Size display switched to: {mode}")

    def _on_add_ftp_bookmark_request(self, initial: dict):
        """Lister asked to bookmark its current FTP connection.
        Open the FTP-bookmark dialog pre-populated with the active
        connection's details so the user can adjust the label and
        save it as a drive button."""
        from .device_panel import _FtpBookmarkDialog
        from PyQt6.QtWidgets import QDialog, QMessageBox
        devs = self.device_column.devices
        if len(devs) >= 40:
            QMessageBox.information(
                self, "Drive button",
                "Maximum 40 drive buttons reached.")
            return
        dlg = _FtpBookmarkDialog(self, initial=initial)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.result_dict()
        devs.append(entry)
        self.device_column.devices = devs
        self.device_column._rebuild()
        self.config["drives"] = devs
        save_config(self.config)
        self._status(
            f"Added FTP bookmark: {entry['label']} -> "
            f"{entry['host']} ({len(devs)}/40)")

    def _on_add_drive_request(self, label, path):
        """Lister asked to add current/selected folder as a drive button.
        Opens the full folder-bookmark dialog so the user can also
        configure the right-panel path and the open-in mode (active
        / both / left / right) instead of just getting a plain entry."""
        from PyQt6.QtWidgets import QMessageBox
        from .device_panel import _FolderBookmarkDialog
        from PyQt6.QtWidgets import QDialog
        devs = self.device_column.devices
        if len(devs) >= 40:
            QMessageBox.information(
                self, "Drive button",
                "Maximum 40 drive buttons reached.\n"
                "Right-click an existing drive button to remove one first.")
            return
        # Pre-populate the dialog with the suggested label + path,
        # then let the user adjust everything (right-path, open-in,
        # ...) before saving.
        dlg = _FolderBookmarkDialog(self, initial={
            "type":   "local",
            "label":  label,
            "path":   path,
            "open_in": "active",
        })
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.result_dict()
        # Avoid duplicates (same path already there - just confirm)
        for d in devs:
            if d.get("path") == entry["path"] \
                    and d.get("type", "local") == "local":
                QMessageBox.information(
                    self, "Drive button",
                    f"That path is already assigned to '{d['label']}'.")
                return
        devs.append(entry)
        self.device_column.devices = devs
        self.device_column._rebuild()
        self.config["drives"] = devs
        save_config(self.config)
        self._status(
            f"Added drive button: {entry['label']} -> {entry['path']} "
            f"({len(devs)}/40)")

    def _on_lister_focus(self, lister):
        if lister is self.left_lister:
            self._active_side = 'left'
        else:
            self._active_side = 'right'
        self.left_lister.set_active(self._active_side == 'left')
        self.right_lister.set_active(self._active_side == 'right')

    def _on_tab(self, from_lister):
        other = (self.right_lister if from_lister is self.left_lister
                 else self.left_lister)
        other.focus_list()

    def _active_lister(self):
        if self._active_side == 'right':
            return self.right_lister, self.left_lister
        return self.left_lister, self.right_lister

    def _save_path(self, key, path):
        self.config[key] = path
        save_config(self.config)

    def _build_menu_bar(self):
        """Build the application menu bar from the central
        action_catalog.ACTION_GROUPS list. Top-level menus are the
        group names (Viewers, File operations, Navigation, ...).
        Each menu entry is one action; clicking it dispatches the
        same action key the right-click picker / button-bar uses,
        so all three UIs stay consistent automatically.
        """
        from .action_catalog import get_action_groups
        from PyQt6.QtGui import QAction

        # Bare QMainWindow.menuBar() makes one on demand.
        mb = self.menuBar()
        mb.clear()

        # Style the menu bar so it matches Quopus's existing
        # workbench look (grey background, white selection on
        # hover) instead of the OS-default native bar.
        mb.setStyleSheet(f"""
            QMenuBar {{
                background-color: {C.WB_GREY};
                color: {C.BLACK};
                font-family: 'Topaz','Courier New',monospace;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 3px 8px;
            }}
            QMenuBar::item:selected {{
                background-color: {C.SELECTED};
                color: {C.WHITE};
            }}
            QMenu {{
                background-color: {C.WB_GREY};
                color: {C.BLACK};
                border: 1px solid {C.BLACK};
                font-family: 'Topaz','Courier New',monospace;
            }}
            QMenu::item {{ padding: 3px 20px; }}
            QMenu::item:selected {{
                background-color: {C.SELECTED};
                color: {C.WHITE};
            }}
            QMenu::separator {{
                height: 1px;
                background: {C.BLACK};
                margin: 3px 6px;
            }}
        """)

        # include_custom=True appends a "Custom Modules" group
        # built from the loaded user plugins. If no plugins are
        # loaded that group is skipped automatically.
        groups = get_action_groups(include_custom=True)
        for group_name, items in groups:
            if not items:
                continue
            menu = mb.addMenu(group_name)
            for key, label in items:
                act = QAction(label, self)
                # Bind the action key as a closure default so
                # each menu item dispatches its own action and
                # not whatever the last loop iteration assigned.
                act.triggered.connect(
                    lambda _checked=False, k=key:
                        self.actions.dispatch(k))
                menu.addAction(act)

    def _on_splitter_moved(self, _pos=None, _idx=None):
        """Persist the lister splitter widths whenever the user
        finishes dragging the divider. Stored as a 3-element list
        (left, middle button column, right) so it round-trips
        through JSON cleanly."""
        try:
            sizes = self._lister_splitter.sizes()
            self.config["lister_splitter_sizes"] = list(sizes)
            save_config(self.config)
        except Exception:
            pass

    def showEvent(self, event):
        """Apply restored maximize/fullscreen state once the window is
        actually visible (calling these before show() is unreliable).
        Also apply the pending position restore here - some window
        managers (X11) ignore move() before the window is mapped."""
        super().showEvent(event)
        if getattr(self, '_state_applied', False):
            return
        self._state_applied = True
        # Apply the saved position (only if not in a maximized state)
        state = getattr(self, '_restore_state', 'normal')
        pending = getattr(self, '_pending_move', None)
        if pending is not None and state == 'normal':
            from PyQt6.QtCore import QTimer as _QT
            x, y = pending
            # Defer to next event-loop tick so the window manager has
            # finished placing the window. On X11/Wayland this is the
            # difference between "move ignored" and "move applied".
            _QT.singleShot(0, lambda: self.move(x, y))
        from PyQt6.QtCore import QTimer as _QT
        if state == 'fullscreen':
            _QT.singleShot(0, self.showFullScreen)
        elif state == 'maximized':
            _QT.singleShot(0, self.showMaximized)

    def changeEvent(self, event):
        """Track the geometry just before fullscreen so we can restore it.
        Also save state changes (maximize, restore, fullscreen) immediately."""
        from PyQt6.QtCore import QEvent as _QE
        if event.type() == _QE.Type.WindowStateChange:
            old = event.oldState()
            from PyQt6.QtCore import Qt as _Qt
            if not (old & _Qt.WindowState.WindowFullScreen) \
               and self.isFullScreen():
                # Capture the windowed position+size separately so
                # _save_window_geometry can reproduce them on next
                # restart without drift. Using pos() (NOT frameGeometry)
                # so it matches what move() expects.
                self._pre_fullscreen_pos = self.pos()
                self._pre_fullscreen_size = self.size()
            # Persist new state
            if getattr(self, '_state_applied', False):
                self._save_window_geometry()
                save_config(self.config)
        super().changeEvent(event)

    def _rebuild_buttons(self):
        # Clear
        while self.button_area_layout.count():
            item = self.button_area_layout.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()

        # Track all button widgets by (row, col) for assignment mode
        self._button_widgets = {}
        self._in_assignment_mode = getattr(self, '_in_assignment_mode', False)

        # Pick which button layer to display based on _active_layer
        # (set by the modifier-key event filter or by Ctrl+T). The
        # shift / shift_alt layers are auto-initialised to an empty
        # 6x6 grid that the user can fill via right-click → Edit on
        # each cell.
        active = getattr(self, '_active_layer', 'main')
        if active == "shift":
            buttons = self.config.get("buttons_shift",
                                          [[None]*6 for _ in range(6)])
            layer_name = 'shift'
        elif active == "shift_alt":
            buttons = self.config.get("buttons_shift_alt",
                                          [[None]*6 for _ in range(6)])
            layer_name = 'shift_alt'
        else:
            buttons = self.config["buttons"]
            layer_name = 'main'

        # Build one HBox per row. Each button has stretch=1 -> equal width,
        # no gaps.
        for r, row in enumerate(buttons):
            row_widget = QWidget()
            row_widget.setStyleSheet(f"background-color: {C.WB_GREY};")
            hbox = QHBoxLayout(row_widget)
            hbox.setSpacing(1)
            hbox.setContentsMargins(0, 0, 0, 0)
            for c, btn_cfg in enumerate(row):
                if btn_cfg is None or not btn_cfg.get("label"):
                    # Placeholder - still right-clickable to fill it,
                    # AND a valid drop target so the user can drag a
                    # populated button onto an empty slot.
                    btn = _DraggableButton(
                        "· empty ·", grid_pos=(r, c),
                        layer=layer_name, main_window=self)
                    btn.setStyleSheet(button_qss("mid"))
                    btn.setEnabled(True)
                    btn.setMinimumHeight(22)
                    btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Fixed)
                    btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, rr=r, cc=c: self._edit_single_button(rr, cc))
                    # Left-click does nothing (until assignment mode triggers)
                    btn.clicked.connect(
                        lambda chk, rr=r, cc=c: self._button_clicked(rr, cc, "", None))
                    hbox.addWidget(btn, 1)
                    self._button_widgets[(r, c)] = btn
                    continue
                btn = _DraggableButton(
                    btn_cfg["label"], grid_pos=(r, c),
                    layer=layer_name, main_window=self)
                btn.setStyleSheet(button_qss(btn_cfg.get("color", "blue")))
                btn.setMinimumHeight(22)
                btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
                action = btn_cfg["action"]
                param = btn_cfg.get("param")
                # Per-button options that some actions honour. We pass
                # them alongside `param` so the action can choose to
                # use them or ignore them. Only external_script and
                # execute_command currently inspect these.
                btn_opts = {
                    "show_output": bool(btn_cfg.get("show_output")),
                    "refresh_after": bool(btn_cfg.get("refresh_after")),
                    "in_terminal": bool(btn_cfg.get("in_terminal")),
                }
                # Hover events for preview overlay (tooltip text or image)
                btn._hover_text  = btn_cfg.get("hover_text", "") or ""
                btn._hover_image = btn_cfg.get("hover_image", "") or ""
                if btn._hover_text or btn._hover_image:
                    btn.installEventFilter(self)
                btn.clicked.connect(
                    lambda chk, a=action, p=param, o=btn_opts,
                            rr=r, cc=c:
                    self._button_clicked(rr, cc, a, p, o))
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda pos, rr=r, cc=c: self._edit_single_button(rr, cc))
                hbox.addWidget(btn, 1)
                self._button_widgets[(r, c)] = btn
            self.button_area_layout.addWidget(row_widget)
        # After re-laying-out the buttons, refresh global hotkeys.
        # This is idempotent - clears all old button-shortcut
        # bindings and creates new QShortcut objects for whatever's
        # in the current button grid.
        self._rebuild_button_hotkeys()

    def _rebuild_button_hotkeys(self):
        """Walk the active button layer and register a global
        QShortcut for every button entry that has a 'hotkey' set.

        Called from _rebuild_buttons after every button-grid
        change (assignment, swap, layer toggle). Old shortcuts
        from previous calls are deleted so we don't end up with
        zombie bindings firing actions for buttons that no longer
        exist.

        Conflicts (two buttons binding the same combo, or a button
        binding a combo that's already a built-in hotkey like
        Alt+F11) are not actively prevented - Qt picks whichever
        QShortcut was registered first when the combo arrives.
        Built-in hotkeys (registered via _setup_hotkeys at start)
        almost always win because they're created first.
        """
        from PyQt6.QtGui import QShortcut, QKeySequence
        # Tear down old bindings
        old = getattr(self, '_button_shortcuts', None)
        if old:
            for sc in old:
                try:
                    sc.setEnabled(False)
                    sc.deleteLater()
                except Exception:
                    pass
        self._button_shortcuts = []

        # Walk the *active* layer - same logic as _rebuild_buttons
        # so the user sees only the bindings for the layer they're
        # currently looking at. Cycle the layer (Ctrl+T) and the
        # bindings flip too.
        buttons = self._buttons_layer()

        for r, row in enumerate(buttons):
            for c, btn_cfg in enumerate(row):
                if not btn_cfg:
                    continue
                hk = btn_cfg.get("hotkey", "")
                if not hk:
                    continue
                seq = QKeySequence(hk)
                if seq.isEmpty():
                    continue
                # Capture (r,c) per-iteration via default-arg
                # trick. We re-look up the button entry at trigger
                # time so any subsequent edits (label, action,
                # param) take effect without rebuilding the
                # shortcut.
                sc = QShortcut(seq, self)
                sc.activated.connect(
                    lambda rr=r, cc=c: self._fire_button_hotkey(rr, cc))
                self._button_shortcuts.append(sc)

    def _fire_button_hotkey(self, r, c):
        """Trigger the button at (r,c) on the currently-active
        layer. Fetches the live config rather than capturing it at
        bind time, so hotkey behaviour stays in sync if the user
        edits the button later."""
        buttons = self._buttons_layer()
        try:
            btn_cfg = buttons[r][c]
        except (IndexError, TypeError):
            return
        if not btn_cfg:
            return
        action = btn_cfg.get("action", "read")
        param = btn_cfg.get("param", "")
        opts = {
            "show_output":   bool(btn_cfg.get("show_output")),
            "refresh_after": bool(btn_cfg.get("refresh_after")),
            "in_terminal":   bool(btn_cfg.get("in_terminal")),
        }
        self._button_clicked(r, c, action, param, opts)

    def _swap_buttons(self, src_pos, dst_pos, layer='main'):
        """Swap two button cells inside one button layer. Called from
        _DraggableButton.dropEvent when the user drags one button
        onto another. Persists the change to disk and rebuilds the
        button area so the new positions show immediately.

        `layer` is 'main' or 'shift' to pick the right config list.
        Cross-layer drops are refused upstream in dropEvent, so we
        don't have to worry about that case here.
        """
        if src_pos == dst_pos:
            return
        if layer == 'shift':
            # The shift layer may not exist in old configs; fall
            # back to an empty 6x6 grid in that case (consistent with
            # how _rebuild_buttons reads it).
            cfg = self.config.get("buttons_shift")
            if cfg is None:
                cfg = [[None]*6 for _ in range(6)]
                self.config["buttons_shift"] = cfg
        elif layer == 'shift_alt':
            # Same fallback for the third (Shift+Alt) layer - first-time
            # users have no buttons_shift_alt key in their config yet.
            cfg = self.config.get("buttons_shift_alt")
            if cfg is None:
                cfg = [[None]*6 for _ in range(6)]
                self.config["buttons_shift_alt"] = cfg
        else:
            cfg = self.config["buttons"]
        sr, sc = src_pos
        dr, dc = dst_pos
        # Bounds check: out-of-range drops shouldn't happen given the
        # grid is fixed 6x6, but a defensive guard avoids IndexErrors
        # if the config ever shrinks for any reason.
        if not (0 <= sr < len(cfg) and 0 <= sc < len(cfg[sr])):
            return
        if not (0 <= dr < len(cfg) and 0 <= dc < len(cfg[dr])):
            return
        cfg[sr][sc], cfg[dr][dc] = cfg[dr][dc], cfg[sr][sc]
        save_config(self.config)
        self._rebuild_buttons()
        self._status(
            f"Swapped buttons ({sr+1},{sc+1}) <-> ({dr+1},{dc+1})")

    def _button_clicked(self, r, c, action, param, opts=None):
        """A button was clicked. In assignment mode, reassign it instead of
        dispatching."""
        if getattr(self, '_in_assignment_mode', False):
            self._finish_button_assignment(r, c)
            return
        self.actions.dispatch(action, param, opts=opts)

    # ==================================================================
    # Button hover preview (text or image overlay)
    # ==================================================================
    def eventFilter(self, obj, event):
        """Show the hover overlay when the mouse enters a button with
        a configured hover_text or hover_image.
        Also keeps the DIZ-panel overlay sized to its parent lister."""
        from PyQt6.QtCore import QEvent
        et = event.type()
        if et == QEvent.Type.Enter:
            txt   = getattr(obj, '_hover_text', "")
            image = getattr(obj, '_hover_image', "")
            if txt or image:
                self._show_hover_overlay(txt, image, obj)
                return False
        elif et == QEvent.Type.Leave:
            if hasattr(obj, '_hover_text') or hasattr(obj, '_hover_image'):
                self._hide_hover_overlay()
                return False
        elif et == QEvent.Type.Resize:
            # Keep DIZ overlay sized to its parent lister
            panel = getattr(obj, '_diz_panel', None)
            if panel is not None:
                panel.setGeometry(0, 0, obj.width(), obj.height())
        return super().eventFilter(obj, event)

    def _show_hover_overlay(self, text, image_path, src_button):
        """Display the hover overlay.
        Sized to fit its content (image or text), placed in the upper
        half of the window but never touching the buttons."""
        if not hasattr(self, '_hover_overlay'):
            from PyQt6.QtWidgets import QLabel
            self._hover_overlay = QLabel(self)
            self._hover_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._hover_overlay.hide()

        from pathlib import Path as _Path
        from PyQt6.QtGui import QPixmap
        ov = self._hover_overlay
        ov.clear()

        # Compute max available area above buttons
        bot_y = self.height() - 100
        try:
            first_row_item = self.button_area_layout.itemAt(0)
            if first_row_item and first_row_item.widget():
                btn_top_global = first_row_item.widget().mapTo(
                    self, first_row_item.widget().rect().topLeft())
                bot_y = btn_top_global.y() - 8
        except Exception:
            pass

        margin_x = 20
        margin_top = 10
        max_w = max(100, self.width() - 2 * margin_x)
        max_h = max(60, bot_y - margin_top)

        # Pick styling based on content
        is_image = False
        if image_path and _Path(image_path).is_file():
            pix = QPixmap(image_path)
            if not pix.isNull():
                is_image = True
                # Only scale down if image is larger than the available area
                if pix.width() > max_w or pix.height() > max_h:
                    pix = pix.scaled(
                        max_w, max_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                ov.setPixmap(pix)
            else:
                ov.setText(f"[image not loadable:\n{image_path}]")
        elif image_path:
            ov.setText(f"[image not found:\n{image_path}]")
        else:
            ov.setText(text or "")

        if is_image:
            # Tight padding, no intrusive border, size-to-image
            ov.setStyleSheet(
                f"QLabel {{ background-color: rgba(0, 0, 0, 180); "
                f"border: 1px solid {C.ACTIVE_BG}; padding: 2px; }}")
            # Overlay size = image size + tiny padding
            ov_w = pix.width() + 6
            ov_h = pix.height() + 6
            # Center horizontally AND vertically in the available area
            # between the listers' top (~margin_top) and the buttons
            x = (self.width() - ov_w) // 2
            available_top = margin_top
            available_bottom = bot_y
            y = available_top + (available_bottom - available_top - ov_h) // 2
            y = max(margin_top, y)
            if y + ov_h > bot_y:
                ov_h = bot_y - y
            ov.setGeometry(max(0, x), y, ov_w, ov_h)
        else:
            # Text overlay: fit-to-text with comfortable padding
            ov.setStyleSheet(f"""
                QLabel {{
                    background-color: rgba(0, 0, 0, 220);
                    color: {C.ACTIVE_FG};
                    font-family: "Topaz-8","Topaz","Courier New",monospace;
                    font-size: {scaled_font_px(14)}px;
                    border: 1px solid {C.ACTIVE_BG};
                    padding: 6px 10px;
                }}
            """)
            ov.adjustSize()
            w = min(ov.width(), max_w)
            h = min(ov.height(), max_h)
            x = (self.width() - w) // 2
            # Place text closer to the buttons (bottom-aligned with a small
            # gap), so it appears just above the hovered button rather than
            # up by the title bar
            y = bot_y - h - 16
            y = max(margin_top, y)
            ov.setGeometry(max(0, x), y, w, h)

        ov.raise_()
        ov.show()

    def _hide_hover_overlay(self):
        if hasattr(self, '_hover_overlay'):
            self._hover_overlay.hide()

    # ==================================================================
    # Button assignment mode
    # ==================================================================
    def start_button_assignment(self, action_name, display_label,
                                 default_param=""):
        """Enter the assignment mode: next button click will be bound to
        `action_name` with label `display_label`.
        default_param is pre-filled in the edit dialog (used e.g. when
        assigning a folder path via goto_dir)."""
        from PyQt6.QtCore import Qt as QtCore_Qt
        from PyQt6.QtGui import QCursor

        self._in_assignment_mode = True
        self._pending_assignment = {
            'action': action_name,
            'label':  display_label,
            'param':  default_param,
        }
        # Highlight all buttons
        for (r, c), btn in self._button_widgets.items():
            btn.setEnabled(True)   # make empty ones clickable too
            btn.setStyleSheet(self._assignment_highlight_qss())
            btn.setCursor(QtCore_Qt.CursorShape.PointingHandCursor)
        # Status line (gelb)
        self.lbl_status.setText(
            f"  ⚙ ASSIGNMENT MODE — click a button to assign '{display_label}' "
            f"  |  ESC or click elsewhere to cancel  ")
        self.lbl_status.setStyleSheet(
            f"QLabel {{ background-color: {C.BTN_ORANGE}; color: #000000; "
            f"font-family: 'Topaz-8','Courier New',monospace; font-weight: bold; "
            f"padding: 2px 8px; }}")

        # Install escape hotkey just for this mode
        if not hasattr(self, '_assign_escape_sc'):
            from PyQt6.QtGui import QShortcut, QKeySequence
            self._assign_escape_sc = QShortcut(QKeySequence("Escape"), self)
            self._assign_escape_sc.activated.connect(self._cancel_button_assignment)
        self._assign_escape_sc.setEnabled(True)

    def _assignment_highlight_qss(self):
        """Return a QSS that makes a button look 'pickable' during assign mode."""
        return (f"QPushButton {{ background-color: {C.ACTIVE_BG}; "
                f"color: {C.ACTIVE_FG}; "
                f"font-family: 'Topaz-8','Courier New',monospace; "
                f"font-weight: bold; "
                f"border: 2px solid #ffff00; }}"
                f"QPushButton:hover {{ background-color: {C.ACTIVE_FG}; "
                f"color: {C.ACTIVE_BG}; }}")

    def _finish_button_assignment(self, r, c):
        """Button at (r,c) was clicked while in assignment mode: assign it.
        Shows a small dialog to let the user pick the label, color, and
        optional parameter before saving."""
        pa = self._pending_assignment

        # Exit visual assignment mode FIRST (so dialog doesn't inherit highlight)
        self._in_assignment_mode = False
        self._assign_escape_sc.setEnabled(False)
        self._rebuild_buttons()
        self.lbl_status.setStyleSheet(STATUSBAR_QSS)

        # Default label from action display name
        default_label = pa['label']

        # Propose a shorter default if the text is long
        if len(default_label) > 12:
            # Keep just the first word or until '('
            short = default_label.split('(')[0].strip()
            if len(short) <= 14:
                default_label = short

        # Show compact edit dialog
        dlg = _ButtonAssignEditDialog(
            initial_label=default_label,
            initial_color="orange",
            initial_param=pa.get('param', ''),
            action_name=pa['action'],
            grid_pos=(r, c),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.lbl_status.setText(" Assignment cancelled ")
            self._pending_assignment = None
            return

        result = dlg.result_entry()
        new_entry = {
            "label":  result['label'],
            "action": result.get('action') or pa['action'],
            "color":  result['color'],
        }
        if result['param']:           new_entry["param"] = result['param']
        if result.get('hover_text'):  new_entry["hover_text"] = result['hover_text']
        if result.get('hover_image'): new_entry["hover_image"] = result['hover_image']
        if result.get('show_output'):    new_entry["show_output"] = True
        if result.get('refresh_after'):  new_entry["refresh_after"] = True
        if result.get('in_terminal'):    new_entry["in_terminal"] = True
        if result.get('hotkey'):         new_entry["hotkey"] = result['hotkey']
        # Write into the layer that was active when the user picked
        # the cell - so Shift+right-click on a Shift-layer cell edits
        # the Shift entry, not the main one.
        self._buttons_layer()[r][c] = new_entry

        from .config import save_config
        save_config(self.config)

        self._pending_assignment = None
        self._rebuild_buttons()
        self.lbl_status.setText(
            f"  Assigned '{result['label']}' to button ({r+1},{c+1})  ")

    def _cancel_button_assignment(self):
        """ESC pressed during assignment mode - back to normal."""
        if not getattr(self, '_in_assignment_mode', False):
            return
        self._in_assignment_mode = False
        self._pending_assignment = None
        if hasattr(self, '_assign_escape_sc'):
            self._assign_escape_sc.setEnabled(False)
        self._rebuild_buttons()
        self.lbl_status.setStyleSheet(STATUSBAR_QSS)
        self.lbl_status.setText(" Assignment cancelled ")

    def _edit_single_button(self, r, c):
        layer = self._buttons_layer()
        current = layer[r][c] or \
                  {"label": "", "action": "read", "color": "blue",
                   "param": "", "hover_text": "", "hover_image": ""}
        dlg = _ButtonAssignEditDialog(
            initial_label=current.get("label", ""),
            initial_color=current.get("color", "blue"),
            initial_param=current.get("param", ""),
            action_name=current.get("action", "read"),
            grid_pos=(r, c),
            parent=self,
            initial_hover_text=current.get("hover_text", ""),
            initial_hover_image=current.get("hover_image", ""),
            initial_show_output=current.get("show_output", False),
            initial_refresh_after=current.get("refresh_after", False),
            initial_in_terminal=current.get("in_terminal", False),
            initial_hotkey=current.get("hotkey", ""),
        )
        # Hint in window title which layer is being edited so the
        # user doesn't get confused if Shift slipped while clicking
        dlg.setWindowTitle(
            f"Edit button ({r+1},{c+1}) - {self._layer_name()}")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.result_entry()
        if not result["label"]:
            # Empty label = clear the slot
            layer[r][c] = None
            save_config(self.config)
            self._rebuild_buttons()
            return
        new_entry = {
            "label":  result["label"],
            "action": result["action"] or current.get("action", "read"),
            "color":  result["color"],
        }
        if result["param"]:        new_entry["param"] = result["param"]
        if result["hover_text"]:   new_entry["hover_text"] = result["hover_text"]
        if result["hover_image"]:  new_entry["hover_image"] = result["hover_image"]
        # Persist the per-button shell options only when set, to keep
        # configs clean for actions that don't use them.
        if result.get("show_output"):
            new_entry["show_output"] = True
        if result.get("refresh_after"):
            new_entry["refresh_after"] = True
        if result.get("in_terminal"):
            new_entry["in_terminal"] = True
        if result.get("hotkey"):
            new_entry["hotkey"] = result["hotkey"]
        layer[r][c] = new_entry
        save_config(self.config)
        self._rebuild_buttons()

    def _status(self, msg):
        self.lbl_status.setText(f" {msg} ")

    def _update_stats(self):
        self.lbl_time.setText(" " + datetime.now().strftime("%H:%M:%S") + " ")
        # psutil reads /proc/meminfo etc; under fd-exhaustion this
        # throws OSError(24, "Too many open files"). The status bar
        # is a nice-to-have, not a critical path - if we can't read
        # stats this tick, just show placeholders and try again on
        # the next timer fire (which is usually after the heavy
        # work that exhausted the fd budget has completed).
        try:
            mem = psutil.virtual_memory()
            used = (mem.total - mem.available) / (1024**3)
            total = mem.total / (1024**3)
            self.lbl_ram.setText(f" RAM:{used:.1f}/{total:.1f}G ")
        except (OSError, Exception):
            self.lbl_ram.setText(" RAM:--/--G ")
        try:
            self.lbl_cpu.setText(
                f" CPU:{psutil.cpu_percent(interval=None):.0f}% ")
        except (OSError, Exception):
            self.lbl_cpu.setText(" CPU:--% ")
        temp = "N/A"
        try:
            if hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures()
                if temps:
                    for key in ("coretemp","k10temp","cpu_thermal","acpitz",
                                "zenpower","it8686","nct6798"):
                        if key in temps and temps[key]:
                            temp = f"{temps[key][0].current:.0f}C"; break
                    else:
                        first = next(iter(temps))
                        if temps[first]:
                            temp = f"{temps[first][0].current:.0f}C"
        except Exception:
            pass
        self.lbl_temp.setText(f" TEMP:{temp} ")

    def _save_window_geometry(self):
        """Capture current window geometry/state into self.config.
        Does NOT write to disk - caller does that. Called on every
        resize/move/state-change so we always have a fresh value.

        Uses self.pos() and self.size() for save - these are the EXACT
        values the matching restore call uses (move(x,y) + resize(w,h)).
        Earlier versions used frameGeometry() which caused the window
        to drift down/right by the title-bar/border thickness on every
        restart, because frameGeometry().x()/y() include the decoration
        but move() expects the client-area position."""
        try:
            if self.isFullScreen():
                state = 'fullscreen'
                geo_pt = getattr(self, '_pre_fullscreen_pos', None)
                geo_sz = getattr(self, '_pre_fullscreen_size', None)
                if geo_pt is None or geo_sz is None:
                    ng = self.normalGeometry()
                    geo_pt = ng.topLeft()
                    geo_sz = ng.size()
            elif self.isMaximized():
                state = 'maximized'
                ng = self.normalGeometry()
                geo_pt = ng.topLeft()
                geo_sz = ng.size()
            else:
                state = 'normal'
                # self.pos() is what move() reads back to. Pairing
                # them like this guarantees no drift on restore.
                geo_pt = self.pos()
                geo_sz = self.size()
            data = {
                "state": state,
                "x": geo_pt.x(),
                "y": geo_pt.y(),
                "w": max(400, geo_sz.width()),
                "h": max(300, geo_sz.height()),
            }
            self.config["window_geometry"] = data
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Don't save during initial show (state not applied yet)
        if getattr(self, '_state_applied', False):
            self._save_window_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, '_state_applied', False):
            self._save_window_geometry()

    def eventFilter(self, obj, event):
        """Global modifier-key tracker. Watches Shift and Alt press/
        release on any focused widget so the action-button bank can
        swap to one of three layers:

          no mod       -> main      (config["buttons"])
          Shift        -> shift     (config["buttons_shift"])
          Shift + Alt  -> shift_alt (config["buttons_shift_alt"])
          Alt only     -> main      (Alt+F-keys are commander hotkeys,
                                     not a layer)

        We don't track the modifier transition itself - that's
        fragile with key-repeat and focus changes. Instead, on any
        Shift/Alt press or release we query Qt for the CURRENT
        modifier state and derive the layer from that. Robust to
        the user mashing modifiers in any order.
        """
        from PyQt6.QtCore import QEvent as _QE
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import QApplication
        et = event.type()
        if et == _QE.Type.KeyPress or et == _QE.Type.KeyRelease:
            key = event.key()
            if key in (_Qt.Key.Key_Shift, _Qt.Key.Key_Alt):
                # Auto-repeat fires KeyPress/KeyRelease alternately
                # while holding the key. Ignore synthetic repeats
                # so the layer doesn't flicker.
                if event.isAutoRepeat():
                    return False
                # If the user has explicitly cycled the layer with
                # Ctrl+T (sticky mode), modifier-hold should NOT
                # change layers - that would undo their explicit
                # choice on the next press/release.
                if getattr(self, '_layer_toggle_sticky', False):
                    return False
                # Query the live modifier state. queryKeyboardModifiers
                # asks the OS for the current physical state, which is
                # accurate even mid-event when Qt's cached event.modifiers()
                # might not yet reflect the key whose press/release we're
                # processing.
                mods = QApplication.queryKeyboardModifiers()
                has_shift = bool(mods & _Qt.KeyboardModifier.ShiftModifier)
                has_alt   = bool(mods & _Qt.KeyboardModifier.AltModifier)
                if has_shift and has_alt:
                    want_layer = "shift_alt"
                elif has_shift:
                    want_layer = "shift"
                else:
                    want_layer = "main"
                if want_layer != getattr(self, '_active_layer', 'main'):
                    self._set_active_layer(want_layer, sticky=False)
                    try:
                        self._rebuild_buttons()
                    except Exception:
                        pass
                return False  # don't swallow - other handlers still want it
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        # Capture latest geometry one more time
        self._save_window_geometry()

        # Clean up any active FTP connections + tmp files
        for lister in (self.left_lister, self.right_lister):
            try:
                if lister.fs.kind == 'remote':
                    lister.fs.close()
            except Exception:
                pass
            try:
                lister._cleanup_remote_tmp()
            except Exception:
                pass
        save_config(self.config)
        event.accept()

    def _refresh_dynamic_stylesheets(self):
        """Called by config.refresh_all_widgets_font() after the
        user changes scale settings in the Appearance dialog.
        Triggers a font/stylesheet refresh on every Quopus-
        managed sub-widget that needs explicit help to pick up
        the new scaled_font_px() values.

        Why this is needed: QApplication.setFont() and a global
        unpolish/polish pass cover widgets WITHOUT inline CSS.
        But widgets with hardcoded f-string stylesheets (lister
        TreeView headers, action button styling, etc.) only
        re-render their CSS when setStyleSheet() is called
        explicitly. This hook calls those refresh methods.
        """
        # Listers: refresh treeview font + header stylesheet
        for lister in (self.left_lister, self.right_lister):
            try:
                lister.refresh_fonts()
            except Exception as e:
                print(f"[font refresh] lister failed: {e}")
        # Action button area: rebuild stylesheets if a refresh
        # hook exists. The action_button_area widget keeps its
        # styling in f-strings that need re-evaluation.
        for attr in ('action_button_area',
                      'left_drive_column',
                      'right_drive_column'):
            w = getattr(self, attr, None)
            if w is None:
                continue
            for hook in ('refresh_fonts',
                          '_refresh_stylesheet'):
                fn = getattr(w, hook, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as e:
                        print(f"[font refresh] {attr}.{hook} "
                              f"failed: {e}")
                    break


# =====================================================================
# Compact button-assignment edit dialog
# =====================================================================
from PyQt6.QtWidgets import QDialog, QLineEdit, QGridLayout, QFormLayout
from .palette import BUTTON_STYLES, WB_TITLEBAR_INACTIVE_QSS
from .config import scaled_font_px


class _ButtonAssignEditDialog(QDialog):
    """Small dialog shown after a user picks a button in assignment mode.
    Lets them tweak label, color, param, and hover preview (text or image)."""

    def __init__(self, initial_label, initial_color, initial_param,
                 action_name, grid_pos, parent=None,
                 initial_hover_text="", initial_hover_image="",
                 initial_show_output=False, initial_refresh_after=False,
                 initial_in_terminal=False, initial_hotkey=""):
        super().__init__(parent)
        self.setWindowTitle("Assign to button")
        self.resize(640, 480)
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        self._current_color = initial_color \
            if initial_color in BUTTON_STYLES else "orange"

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4); root.setSpacing(6)

        title = QLabel(
            f"  Button ({grid_pos[0]+1}, {grid_pos[1]+1})  ")
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(title)

        form = QFormLayout(); form.setSpacing(6)

        edit_qss = (
            f"QLineEdit {{ background-color: #ffffff; color: #000000; "
            f"border: 1px solid #000000; padding: 3px 6px; "
            f"font-family: 'Topaz','Courier New',monospace; font-size: {scaled_font_px(12)}px; }}")

        # Action picker - hierarchical menu built from the central
        # catalog. action_catalog.ACTION_GROUPS is shared with the
        # F10 Action-buttons bulk dialog so both editors show the
        # same actions in the same order; previously each dialog
        # had its own list and they drifted out of sync.
        from .action_catalog import (
            action_label_map, build_action_picker_button,
        )
        self._action_label_map = action_label_map()
        # Tracked separately because we want to set the initial
        # key BEFORE the picker triggers its on_change callback
        # (which calls _on_action_change defined further down).
        self._action_key = (
            action_name if action_name in self._action_label_map
            else "read")

        def _set_action_key(k):
            # Called from the picker when the user selects a new
            # action, AND used internally for the initial value.
            # Fires _on_action_change for placeholder updates etc.
            self._action_key = k
            _on_action_change(k)
        self._set_action_key = _set_action_key

        # _on_action_change is defined further down in this method
        # (it tweaks le_param's placeholder). The build call below
        # only invokes the callback when the user PICKS something,
        # not at construction time, so the forward reference is
        # safe - by the time the user clicks anything _on_action_change
        # is fully defined.
        self.btn_action = build_action_picker_button(
            self, self._action_key, _set_action_key,
            include_empty=False)

        form.addRow("Action:", self.btn_action)

        self.le_label = QLineEdit(initial_label)
        self.le_label.setStyleSheet(edit_qss)
        self.le_label.selectAll()
        form.addRow("Label:", self.le_label)

        # Color picker button - click opens a grid of all BUTTON_STYLES
        self.btn_color = QPushButton()
        self.btn_color.setFixedWidth(140)
        self.btn_color.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        form.addRow("Color:", self.btn_color)

        # Optional parameter
        self.le_param = QLineEdit(initial_param)
        self.le_param.setPlaceholderText("(optional, e.g. -n %f)")
        self.le_param.setStyleSheet(edit_qss)
        form.addRow("Param:", self.le_param)

        # Refresh placeholder/hint when the action changes - e.g.
        # ftp_site needs a bookmark name, goto_dir needs a path.
        # Takes the new key directly now (no combobox to query).
        def _on_action_change(key):
            if key == "ftp_site":
                self.le_param.setPlaceholderText(
                    "name of saved FTP bookmark (e.g. CSDB)")
            elif key == "ftp_upload":
                self.le_param.setPlaceholderText(
                    "name of saved FTP bookmark - selection from "
                    "other panel will be uploaded after connect")
            elif key == "goto_dir":
                self.le_param.setPlaceholderText(
                    "directory path to navigate to")
            elif key in ("external_script", "execute_command",
                          "custom_cmd"):
                self.le_param.setPlaceholderText(
                    "command + args; %f=file, %p=cwd, %F=selected list")
            else:
                self.le_param.setPlaceholderText("(optional, e.g. -n %f)")
        # Apply for the initial action (the menu button's
        # _set_action_key will call _on_action_change on user
        # selection later)
        _on_action_change(self._action_key)

        hint = QLabel(
            "  Tokens for Param:  %f = first file, %F = all selected, "
            "%n = basename, %p = current dir, %d = other-side dir\n"
            "  Extension gate:  {file|crt,prg} - the action only "
            "runs if every selected file ends with one of those "
            "extensions (case-insensitive). On pass it's rewritten "
            "to %f so use it ANYWHERE you'd use %f.")
        hint.setStyleSheet(f"QLabel {{ color: #444; font-size: {scaled_font_px(10)}px; }}")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # Per-button options for actions that spawn an external
        # process. These are honoured by external_script,
        # execute_command, custom_cmd, run, and shell - any action
        # that runs a subprocess. For actions that don't run a
        # subprocess (read, copy, etc.) they're simply ignored, so
        # the row stays visible regardless of which action is
        # selected: easier to reason about than hidden options.
        from PyQt6.QtWidgets import QCheckBox
        opts_row = QHBoxLayout(); opts_row.setSpacing(12)
        self.cb_show_output = QCheckBox("Show output (keep shell open)")
        self.cb_show_output.setChecked(bool(initial_show_output))
        self.cb_show_output.setToolTip(
            "Capture stdout/stderr in a Quopus output window. Best "
            "for non-interactive tools (unp64, exomizer, build "
            "scripts) where you want to see what the program prints. "
            "Cannot send keyboard input back to the program.")
        opts_row.addWidget(self.cb_show_output)
        self.cb_in_terminal = QCheckBox("In Terminal")
        self.cb_in_terminal.setChecked(bool(initial_in_terminal))
        self.cb_in_terminal.setToolTip(
            "Launch in a real terminal window (cmd.exe on Windows, "
            "xterm/gnome-terminal/konsole on Linux). Use this for "
            "INTERACTIVE programs that need keyboard input - telnet, "
            "ssh, vim, mc, REPLs, etc. The window stays open after "
            "the program exits so you can read any final messages.")
        opts_row.addWidget(self.cb_in_terminal)
        self.cb_refresh_after = QCheckBox("Refresh panels after")
        self.cb_refresh_after.setChecked(bool(initial_refresh_after))
        self.cb_refresh_after.setToolTip(
            "Re-read both panels after the command finishes - useful "
            "for tools that drop new files into the current directory.")
        opts_row.addWidget(self.cb_refresh_after)
        opts_row.addStretch(1)
        # Show output and In Terminal are mutually exclusive: capture
        # via pipe vs. spawn a real TTY. Toggling one off the other.
        def _on_show_toggled(checked):
            if checked: self.cb_in_terminal.setChecked(False)
        def _on_terminal_toggled(checked):
            if checked: self.cb_show_output.setChecked(False)
        self.cb_show_output.toggled.connect(_on_show_toggled)
        self.cb_in_terminal.toggled.connect(_on_terminal_toggled)
        opts_wrap = QWidget(); opts_wrap.setLayout(opts_row)
        form.addRow("Options:", opts_wrap)

        # Hotkey - global shortcut that triggers this button when
        # Quopus has focus. Uses QKeySequenceEdit so the user just
        # clicks in and presses the key combo they want; Qt parses
        # and displays it as text like "Ctrl+Shift+P". Empty = no
        # hotkey.
        from PyQt6.QtWidgets import QKeySequenceEdit
        hotkey_row = QHBoxLayout(); hotkey_row.setSpacing(2)
        self.kse_hotkey = QKeySequenceEdit()
        # Limit to one chord (a single combo). Without this, Qt
        # accepts up to 4 keys in sequence which is rarely what
        # the user wants and clutters the saved config.
        self.kse_hotkey.setMaximumSequenceLength(1)
        if initial_hotkey:
            from PyQt6.QtGui import QKeySequence
            self.kse_hotkey.setKeySequence(QKeySequence(initial_hotkey))
        self.kse_hotkey.setToolTip(
            "Click here, then press the key combo to assign. "
            "Supported combos include F-keys (F1-F12), Ctrl+letter, "
            "Alt+letter, Shift+F-keys etc. Leave empty for none.")
        hotkey_row.addWidget(self.kse_hotkey, 1)
        b_clear_hk = QPushButton("Clear")
        b_clear_hk.setFixedWidth(60)
        b_clear_hk.setStyleSheet(button_qss("red"))
        b_clear_hk.setToolTip("Remove hotkey from this button")
        b_clear_hk.clicked.connect(self.kse_hotkey.clear)
        hotkey_row.addWidget(b_clear_hk)
        hk_wrap = QWidget(); hk_wrap.setLayout(hotkey_row)
        form.addRow("Hotkey:", hk_wrap)

        # Hover preview: text ORR image (image takes precedence if set)
        self.le_hover_text = QLineEdit(initial_hover_text)
        self.le_hover_text.setPlaceholderText("(optional tooltip shown on hover)")
        self.le_hover_text.setStyleSheet(edit_qss)
        form.addRow("Hover text:", self.le_hover_text)

        # Image picker row
        hv_row = QHBoxLayout(); hv_row.setSpacing(2)
        self.le_hover_image = QLineEdit(initial_hover_image)
        self.le_hover_image.setPlaceholderText(
            "(optional PNG/JPG to preview on hover)")
        self.le_hover_image.setStyleSheet(edit_qss)
        hv_row.addWidget(self.le_hover_image, 1)
        b_browse = QPushButton("...")
        b_browse.setFixedWidth(34)
        b_browse.setStyleSheet(button_qss("mid"))
        b_browse.clicked.connect(self._browse_hover_image)
        hv_row.addWidget(b_browse)
        b_clear = QPushButton("×")
        b_clear.setFixedWidth(28)
        b_clear.setStyleSheet(button_qss("red"))
        b_clear.clicked.connect(lambda: self.le_hover_image.setText(""))
        hv_row.addWidget(b_clear)
        hv_wrap = QWidget(); hv_wrap.setLayout(hv_row)
        form.addRow("Hover image:", hv_wrap)

        hint2 = QLabel(
            "  Image overrides text when both are set. "
            "Image is shown in the upper half of the window.")
        hint2.setStyleSheet(f"QLabel {{ color: #444; font-size: {scaled_font_px(10)}px; }}")
        hint2.setWordWrap(True)
        form.addRow("", hint2)

        wrap = QWidget(); wrap.setLayout(form)
        root.addWidget(wrap)
        root.addStretch()

        # Buttons
        btn_row = QHBoxLayout(); btn_row.addStretch()
        b_ok = QPushButton("OK")
        b_ok.setStyleSheet(button_qss("orange")); b_ok.setFixedWidth(100)
        b_ok.setDefault(True); b_ok.clicked.connect(self.accept)
        btn_row.addWidget(b_ok)
        b_cancel = QPushButton("Cancel")
        b_cancel.setStyleSheet(button_qss("red")); b_cancel.setFixedWidth(100)
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_cancel)
        root.addLayout(btn_row)

        self.le_label.setFocus()

    def _refresh_color_btn(self):
        bg, fg = BUTTON_STYLES[self._current_color]
        self.btn_color.setText(self._current_color)
        self.btn_color.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; "
            f"border: 1px solid #000000; padding: 3px 6px; "
            f"font-family: 'Topaz','Courier New',monospace; "
            f"font-weight: bold; }}")

    def _pick_color(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Pick a color")
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(6, 6, 6, 6); lay.setSpacing(4)
        lay.addWidget(QLabel("Click a color:"))
        grid = QGridLayout(); grid.setSpacing(2)
        lay.addLayout(grid)
        names = list(BUTTON_STYLES.keys())
        cols = 5
        for i, name in enumerate(names):
            bg, fg = BUTTON_STYLES[name]
            r, c = divmod(i, cols)
            b = QPushButton(name)
            b.setFixedSize(130, 30)
            b.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: {fg}; "
                f"border: 1px solid #000000; "
                f"font-family: 'Topaz','Courier New',monospace; "
                f"font-weight: bold; font-size: {scaled_font_px(11)}px; }}"
                f"QPushButton:hover {{ border: 2px solid #ffff00; }}")
            def pick(_=None, n=name):
                self._current_color = n
                self._refresh_color_btn()
                dlg.accept()
            b.clicked.connect(pick)
            grid.addWidget(b, r, c)
        dlg.exec()

    def _browse_hover_image(self):
        from PyQt6.QtWidgets import QFileDialog
        current = self.le_hover_image.text().strip()
        start = current if current else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select hover image", start,
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;All files (*.*)")
        if path:
            self.le_hover_image.setText(path)

    def result_entry(self):
        return {
            "label":          self.le_label.text().strip() or "?",
            "action":         self._action_key,
            "color":          self._current_color,
            "param":          self.le_param.text().strip(),
            "hover_text":     self.le_hover_text.text().strip(),
            "hover_image":    self.le_hover_image.text().strip(),
            "show_output":    self.cb_show_output.isChecked(),
            "refresh_after":  self.cb_refresh_after.isChecked(),
            "in_terminal":    self.cb_in_terminal.isChecked(),
            # Empty string when no hotkey is set; otherwise a Qt-
            # canonical string like "Ctrl+Shift+P" that QKeySequence
            # can re-parse on the next dialog open.
            "hotkey":         self.kse_hotkey.keySequence().toString(),
        }
