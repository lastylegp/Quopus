"""Internal archive viewer. Supports zip, tar.gz, tar.bz2, lha (read-only).

Shows a Quopus-style list of files inside the archive, lets you:
- double-click a text-ish file to read it (without extracting to disk)
- extract selected (tagged) files to a chosen folder
- see sizes, packed sizes, compression ratios
"""
from __future__ import annotations

import io
import os
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QFileDialog, QMessageBox,
    QHeaderView,
)

from .palette import (
    C, button_qss, fmt_size, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS,
)
from .config import scaled_font_px


def _is_archive_path(path: Path) -> bool:
    ext = path.suffix.lower()
    compound = ''.join(path.suffixes[-2:]).lower()
    return (ext in {'.zip', '.lha', '.lzh', '.tar', '.tgz', '.tbz', '.tbz2',
                    '.rar', '.gz'}
            or compound in {'.tar.gz', '.tar.bz2', '.tar.xz'})


class _ArchiveEntry:
    __slots__ = ('name', 'size', 'compressed', 'mtime', 'is_dir', 'ratio', 'raw')
    def __init__(self, name, size, compressed, mtime, is_dir, raw):
        self.name = name
        self.size = size
        self.compressed = compressed
        self.mtime = mtime
        self.is_dir = is_dir
        self.ratio = (1 - compressed / size) if size else 0.0
        self.raw = raw  # native archive member handle (ZipInfo / TarInfo / etc.)


class ArchiveBackend:
    """Uniform interface over different archive libraries."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.kind = self._detect_kind()
        self._handle = None
        self._entries: list[_ArchiveEntry] = []
        self._open()

    def _detect_kind(self) -> str:
        name = self.path.name.lower()
        if name.endswith(('.tar.gz', '.tgz')): return 'tar.gz'
        if name.endswith(('.tar.bz2', '.tbz', '.tbz2')): return 'tar.bz2'
        if name.endswith('.tar.xz'): return 'tar.xz'
        if name.endswith('.tar'): return 'tar'
        if name.endswith('.zip'): return 'zip'
        if name.endswith(('.lha', '.lzh')): return 'lha'
        if name.endswith('.rar'): return 'rar'
        if name.endswith('.gz'): return 'gz'   # bare gzip (single file)
        return 'zip'  # fallback guess

    def _open(self):
        k = self.kind
        if k == 'zip':
            self._handle = zipfile.ZipFile(self.path, 'r')
            for zi in self._handle.infolist():
                is_dir = zi.is_dir() or zi.filename.endswith('/')
                try:
                    mt = datetime(*zi.date_time) if zi.date_time else None
                except ValueError:
                    mt = None
                self._entries.append(_ArchiveEntry(
                    name=zi.filename,
                    size=zi.file_size,
                    compressed=zi.compress_size,
                    mtime=mt, is_dir=is_dir, raw=zi,
                ))
        elif k.startswith('tar'):
            mode_map = {
                'tar': 'r:', 'tar.gz': 'r:gz',
                'tar.bz2': 'r:bz2', 'tar.xz': 'r:xz',
            }
            self._handle = tarfile.open(self.path, mode_map[k])
            for ti in self._handle.getmembers():
                try:
                    mt = datetime.fromtimestamp(ti.mtime)
                except (OverflowError, OSError):
                    mt = None
                self._entries.append(_ArchiveEntry(
                    name=ti.name,
                    size=ti.size,
                    compressed=ti.size,  # tar entries aren't individually compressed
                    mtime=mt, is_dir=ti.isdir(), raw=ti,
                ))
        elif k == 'lha':
            try:
                import lhafile  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "LHA/LZH support requires 'lhafile':  pip install lhafile")
            self._handle = lhafile.Lhafile(str(self.path))

            seen_paths = set()
            seen_dirs = set()

            for li in self._handle.infolist():
                fname = li.filename or ""
                # Normalize separators - LHA on Amiga can use '/' or '\\'.
                # Collapse any duplicate separators that lhafile produces
                # when stitching the directory header onto the basename.
                fname = fname.replace('\\', '/')
                while '//' in fname:
                    fname = fname.replace('//', '/')
                fname = fname.strip('/')   # no trailing/leading slash
                if not fname:
                    continue
                if fname in seen_paths:
                    continue
                seen_paths.add(fname)

                file_size = getattr(li, 'file_size', 0) or 0
                compress_size = getattr(li, 'compress_size', 0) or 0
                # An entry is a directory ONLY when both sizes are zero
                # AND the original filename ended with a slash. lhafile's
                # `directory` attribute is the parent path (a string), not
                # a bool — never use it for that.
                orig = (li.filename or "").replace('\\', '/')
                is_dir = orig.endswith('/') and file_size == 0

                mt = None
                try:
                    if hasattr(li, 'date_time') and li.date_time:
                        if hasattr(li.date_time, 'year'):
                            mt = li.date_time
                        else:
                            mt = datetime(*li.date_time)
                except Exception:
                    mt = None

                self._entries.append(_ArchiveEntry(
                    name=fname,
                    size=file_size,
                    compressed=compress_size,
                    mtime=mt, is_dir=is_dir, raw=li,
                ))
                if is_dir:
                    seen_dirs.add(fname)

                # Synthesize directory rows for any parent paths not
                # explicitly listed, since lhafile doesn't always emit
                # them as separate entries.
                if '/' in fname:
                    parts = fname.split('/')
                    for i in range(1, len(parts)):
                        prefix = '/'.join(parts[:i])
                        if prefix and prefix not in seen_dirs \
                           and prefix not in seen_paths:
                            seen_dirs.add(prefix)
                            seen_paths.add(prefix)
                            self._entries.append(_ArchiveEntry(
                                name=prefix,
                                size=0, compressed=0,
                                mtime=None, is_dir=True, raw=None,
                            ))

        elif k == 'rar':
            try:
                import rarfile  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "RAR support requires 'rarfile':  pip install rarfile\n"
                    "It also needs unrar.exe / unrar binary on the system PATH.")
            # rarfile only walks $PATH for its tool. On Windows neither
            # WinRAR's UnRAR.exe nor 7-Zip's 7z.exe end up in PATH by
            # default, so we proactively check the known install dirs
            # and point rarfile at whichever we find. This is a no-op
            # on Linux unless the user has the binary in /usr/bin etc.
            self._configure_rarfile_tools(rarfile)
            try:
                self._handle = rarfile.RarFile(str(self.path))
            except rarfile.RarCannotExec:
                raise RuntimeError(self._rar_tool_help_text())
            for ri in self._handle.infolist():
                fname = ri.filename.replace('\\', '/')
                is_dir = ri.is_dir() if hasattr(ri, 'is_dir') \
                         else fname.endswith('/')
                fname = fname.rstrip('/')
                try:
                    mt = ri.date_time
                    if mt and not hasattr(mt, 'year'):
                        mt = datetime(*mt)
                except Exception:
                    mt = None
                self._entries.append(_ArchiveEntry(
                    name=fname,
                    size=ri.file_size or 0,
                    compressed=ri.compress_size or 0,
                    mtime=mt, is_dir=is_dir, raw=ri,
                ))

        elif k == 'gz':
            # Bare gzip: a single compressed file, name = the original
            # filename (or path stem if not stored). gzip exposes one
            # logical entry. We probe the uncompressed size only if cheap.
            import gzip
            inner_name = self.path.stem  # foo.txt.gz -> foo.txt
            # Try to read the original filename from gzip header (if any)
            try:
                with open(self.path, 'rb') as f:
                    if f.read(2) == b'\x1f\x8b':
                        f.seek(3); flags = f.read(1)[0]
                        f.seek(10)
                        if flags & 0x08:   # FNAME bit set
                            name_bytes = bytearray()
                            while True:
                                ch = f.read(1)
                                if not ch or ch == b'\x00': break
                                name_bytes += ch
                            try:
                                inner_name = name_bytes.decode(
                                    'latin-1', errors='replace')
                            except Exception:
                                pass
            except Exception:
                pass
            try:
                compressed = self.path.stat().st_size
            except Exception:
                compressed = 0
            try:
                mt = datetime.fromtimestamp(self.path.stat().st_mtime)
            except Exception:
                mt = None
            # Uncompressed size is at the end of the gzip file (last 4 bytes,
            # mod 2^32). Cheap to read.
            uncompressed = 0
            try:
                with open(self.path, 'rb') as f:
                    f.seek(-4, 2)
                    import struct
                    uncompressed = struct.unpack('<I', f.read(4))[0]
            except Exception:
                pass
            self._handle = None  # we'll open on demand for read
            self._entries.append(_ArchiveEntry(
                name=inner_name,
                size=uncompressed,
                compressed=compressed,
                mtime=mt, is_dir=False, raw=None,
            ))

    def entries(self): return self._entries

    def read(self, entry: _ArchiveEntry) -> bytes:
        if self.kind == 'zip':
            return self._handle.read(entry.raw)
        if self.kind.startswith('tar'):
            f = self._handle.extractfile(entry.raw)
            return f.read() if f else b''
        if self.kind == 'lha':
            if entry.raw is None:
                return b''
            return self._handle.read(entry.raw.filename)
        if self.kind == 'rar':
            if entry.raw is None:
                return b''
            try:
                return self._handle.read(entry.raw.filename)
            except Exception as e:
                # rarfile raises RarCannotExec ('Cannot find working
                # tool') for the missing-binary case. Other RarError
                # subclasses (RarWrongPassword, RarCRCError, ...) get
                # passed through unchanged so the caller sees the
                # actual reason, not the generic help text.
                import rarfile as _rf
                if isinstance(e, _rf.RarCannotExec):
                    raise RuntimeError(self._rar_tool_help_text())
                raise
        if self.kind == 'gz':
            import gzip
            with gzip.open(self.path, 'rb') as f:
                return f.read()
        return b''

    # ------------------------------------------------------------------
    # RAR helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _configure_rarfile_tools(rarfile_mod):
        """Try to locate UnRAR.exe and 7z.exe in their standard
        Windows install dirs and feed the absolute paths into
        rarfile's module-level config. rarfile would otherwise only
        look up bare names via shutil.which() which silently misses
        anything outside PATH (the typical situation on Windows).

        Sets each *_TOOL only if a) it currently points to a bare
        tool name (rarfile default) and b) we can find an absolute
        match. Existing user overrides are kept untouched."""
        import os
        candidates_unrar = [
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files\WinRAR\Rar.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\Rar.exe",
        ]
        candidates_7z = [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]
        # Only override if rarfile is still using the default bare
        # tool name, i.e. the user hasn't pointed it elsewhere.
        if getattr(rarfile_mod, 'UNRAR_TOOL', '') in ('unrar', 'UnRAR'):
            for p in candidates_unrar:
                if os.path.isfile(p):
                    rarfile_mod.UNRAR_TOOL = p
                    break
        if getattr(rarfile_mod, 'SEVENZIP_TOOL', '') in ('7z', '7za'):
            for p in candidates_7z:
                if os.path.isfile(p):
                    rarfile_mod.SEVENZIP_TOOL = p
                    break

    @staticmethod
    def _rar_tool_help_text():
        """Detailed help shown when rarfile cannot find any working
        tool. Same text is reused for the open-time and extract-time
        failure paths so the user gets a consistent message."""
        return (
            "Cannot extract RAR: no working unrar/7z tool found.\n\n"
            "Install ONE of these and try again:\n"
            "  - UnRAR (Windows command-line, freeware):\n"
            "      https://www.rarlab.com/rar_add.htm\n"
            "      Drop UnRAR.exe into C:\\Program Files\\WinRAR\\ "
            "or add its folder to PATH.\n"
            "  - 7-Zip (Windows): https://www.7-zip.org/  - default\n"
            "      install path C:\\Program Files\\7-Zip\\ is auto-detected.\n"
            "  - Linux: apt install unrar  or  apt install p7zip-full")

    def extract_to(self, entries, outdir: Path):
        outdir.mkdir(parents=True, exist_ok=True)
        for e in entries:
            if e.is_dir:
                (outdir / e.name).mkdir(parents=True, exist_ok=True)
                continue
            data = self.read(e)
            target = outdir / e.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def close(self):
        if self._handle is not None:
            try: self._handle.close()
            except Exception: pass
            self._handle = None


class ArchiveViewer(QDialog):
    """Quopus-styled internal archive viewer."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Archive: {self.path.name}")
        self.resize(900, 620)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "archive_viewer")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        try:
            self.backend = ArchiveBackend(self.path)
        except Exception as e:
            QMessageBox.warning(self, "Archive", f"Cannot open archive:\n{e}")
            self.backend = None
            self.close()
            return
        # Async extract state. Set during _extract_async; cleared
        # when the worker finishes. closeEvent checks it so the
        # backend doesn't get torn down while a thread is still
        # using it.
        self._extract_worker = None
        self._extract_dlg = None
        # Async single-file-read state (for View / Hex buttons).
        # Same GC-protection rationale as _extract_worker.
        self._read_worker = None
        self._read_dlg = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2); layout.setSpacing(2)

        total_size = sum(e.size for e in self.backend.entries() if not e.is_dir)
        total_comp = sum(e.compressed for e in self.backend.entries() if not e.is_dir)
        ratio = (1 - total_comp / total_size) if total_size else 0.0
        n_files = sum(1 for e in self.backend.entries() if not e.is_dir)
        n_dirs = sum(1 for e in self.backend.entries() if e.is_dir)

        title = QLabel(
            f"  {self.backend.kind.upper()}: {self.path.name}   "
            f"({n_files} files, {n_dirs} dirs, "
            f"{fmt_size(total_comp)} -> {fmt_size(total_size)}, "
            f"{ratio*100:.1f}% saved)  "
        )
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        # Toolbar
        tool = QHBoxLayout()
        tool.setSpacing(2)

        self.btn_view = QPushButton("View  (Enter)")
        self.btn_view.setStyleSheet(button_qss("red"))
        self.btn_view.clicked.connect(self._view_selected)
        tool.addWidget(self.btn_view)

        self.btn_hex = QPushButton("Hex")
        self.btn_hex.setStyleSheet(button_qss("purple"))
        self.btn_hex.clicked.connect(self._hex_selected)
        tool.addWidget(self.btn_hex)

        self.btn_extract = QPushButton("Extract All...")
        self.btn_extract.setStyleSheet(button_qss("orange"))
        self.btn_extract.clicked.connect(self._extract_all)
        tool.addWidget(self.btn_extract)

        self.btn_extract_sel = QPushButton("Extract Selected")
        self.btn_extract_sel.setStyleSheet(button_qss("orange"))
        self.btn_extract_sel.clicked.connect(self._extract_selected)
        tool.addWidget(self.btn_extract_sel)

        tool.addStretch()

        self.btn_close = QPushButton("Close  (Esc)")
        self.btn_close.setStyleSheet(button_qss("red"))
        self.btn_close.clicked.connect(self.accept)
        tool.addWidget(self.btn_close)
        layout.addLayout(tool)

        # File list
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Name", "Size", "Packed", "Ratio", "Date"])
        from quopus_lib.window_state import install_table_state
        install_table_state(self.tree, "archive_viewer:tree")
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(
            QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {C.LISTER_BG};
                color: {C.LISTER_FG};
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: {scaled_font_px(12)}px;
                border: 1px solid {C.BLACK};
                selection-background-color: {C.SELECTED};
                selection-color: {C.WHITE};
            }}
            QHeaderView::section {{
                background-color: {C.WB_GREY};
                color: {C.BLACK};
                font-family: "Topaz-8","Topaz",monospace;
                font-weight: bold;
                padding: 2px 8px;
                border: 1px solid {C.BLACK};
            }}
            {SCROLLBAR_QSS}
        """)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        self._populate()

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Hotkeys inside the archive viewer
        QShortcut(QKeySequence("Return"), self, self._view_selected)
        QShortcut(QKeySequence("Enter"), self, self._view_selected)
        QShortcut(QKeySequence("F3"), self, self._view_selected)
        QShortcut(QKeySequence("F4"), self, self._hex_selected)
        QShortcut(QKeySequence("Escape"), self, self.accept)

    def _populate(self):
        self.tree.clear()
        for e in self.backend.entries():
            display_name = e.name + ("/" if e.is_dir else "")
            size_s = "<DIR>" if e.is_dir else fmt_size(e.size)
            comp_s = "" if e.is_dir else fmt_size(e.compressed)
            ratio_s = "" if e.is_dir or not e.size else f"{e.ratio*100:.0f}%"
            date_s = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else ""
            it = QTreeWidgetItem([display_name, size_s, comp_s, ratio_s, date_s])
            it.setData(0, Qt.ItemDataRole.UserRole, e)
            if e.is_dir:
                it.setForeground(0, Qt.GlobalColor.blue)
            self.tree.addTopLevelItem(it)

    def _selected_entries(self):
        out = []
        for it in self.tree.selectedItems():
            e = it.data(0, Qt.ItemDataRole.UserRole)
            if e: out.append(e)
        return out

    def _view_selected(self):
        """View the selected file in the appropriate internal viewer.
        Reading the bytes from the archive is done on a background
        QThread so the dialog stays responsive while RAR's unrar
        subprocess does its thing - same problem as Extract had."""
        items = self._selected_entries()
        if not items: return
        e = items[0]
        if e.is_dir: return
        self._read_async_then(e, kind='view')

    def _hex_selected(self):
        items = self._selected_entries()
        if not items: return
        e = items[0]
        if e.is_dir: return
        self._read_async_then(e, kind='hex')

    def _read_async_then(self, entry, kind: str):
        """Common worker plumbing for View / Hex: launch a thread
        that calls backend.read(entry), show a tiny progress dialog
        if it takes >250ms, then dispatch to the right viewer with
        the resulting bytes.

        `kind` is 'view' (route through TextReader / ImageViewer
        based on file type) or 'hex' (always HexReader).
        """
        from PyQt6.QtCore import QThread, pyqtSignal, Qt as _Qt
        from PyQt6.QtWidgets import QProgressDialog

        backend = self.backend

        class _ReadWorker(QThread):
            done = pyqtSignal(bool, object, str)   # ok, data_or_None, error_message

            def __init__(self, ent):
                super().__init__()
                self.ent = ent
                self._cancel = False

            def cancel(self):
                self._cancel = True

            def run(self):
                try:
                    # backend.read() is the slow call: for RAR it
                    # spawns unrar.exe and blocks until the member
                    # is fully decoded. Cancelling mid-read isn't
                    # straightforward (subprocess handles it itself
                    # only on SIGTERM) - we just ignore the result
                    # if cancelled.
                    data = backend.read(self.ent)
                    if self._cancel:
                        self.done.emit(False, None, "cancelled")
                    else:
                        self.done.emit(True, data, "")
                except Exception as ex:
                    self.done.emit(False, None, str(ex))

        worker = _ReadWorker(entry)
        # Keep a strong ref so it doesn't get GC'd mid-run.
        self._read_worker = worker

        # Tiny progress dialog with indeterminate bar (we can't
        # report fine-grained progress from inside rarfile). Set
        # minimumDuration so quick reads don't pop a flashing dlg.
        dlg = QProgressDialog(
            f"Reading {entry.name}...", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Read from archive")
        dlg.setMinimumDuration(250)   # only show if read takes >0.25s
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowModality(_Qt.WindowModality.WindowModal)
        dlg.canceled.connect(worker.cancel)
        self._read_dlg = dlg

        def _on_done(ok, data, err):
            dlg.close()
            self._read_worker = None
            self._read_dlg = None
            if not ok:
                if err == "cancelled":
                    self.lbl_status.setText(" Read cancelled ")
                else:
                    QMessageBox.warning(
                        self, "Read", f"Cannot read: {err}")
                return
            self._spawn_viewer(entry, data, kind)

        worker.done.connect(_on_done)
        worker.start()

    def _spawn_viewer(self, entry, data: bytes, kind: str):
        """Write the freshly-read bytes to a temp file and open the
        right internal viewer on it. Called once the background
        read completes successfully."""
        # Preserve the original file extension in the temp file so
        # that QImageReader / is_image() work correctly. The path
        # prefix doesn't matter, but the suffix must be the real one.
        orig_name = (entry.name.rsplit('/', 1)[-1]
                                .rsplit('\\', 1)[-1])
        tmp_prefix = "dopus_view" if kind == 'view' else "dopus_hex"
        tmp = (Path(tempfile.gettempdir())
                / f"{tmp_prefix}_{os.getpid()}_{orig_name}")
        try:
            tmp.write_bytes(data)
        except Exception as ex:
            QMessageBox.warning(self, "View",
                                  f"Cannot write temp: {ex}")
            return
        title_suffix = f"  (from {self.path.name})"
        try:
            if kind == 'hex':
                from .readers import HexReader
                v = HexReader(tmp, self)
                v.setWindowTitle(f"Hex: {entry.name}{title_suffix}")
            else:
                from .image_viewer import is_image, ImageViewer
                if is_image(tmp):
                    v = ImageViewer(tmp, self)
                else:
                    from .readers import TextReader
                    v = TextReader(tmp, self)
                v.setWindowTitle(f"View: {entry.name}{title_suffix}")
            # Keep this viewer modal-ish: while the user is reading
            # this file we don't want them clicking a different one
            # in the list and ending up with two readers fighting
            # over the same temp file. exec() also keeps tmp alive
            # until the user closes the viewer.
            v.exec()
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    def _other_panel_path(self):
        """Return the local current_path of the OTHER lister (the one
        that didn't open this archive viewer), or None if the other
        side is unavailable / non-local. The viewer's parent() is the
        lister that triggered the open, so we just walk to the main
        window and pick the opposite side."""
        try:
            src_lister = self.parent()
            if src_lister is None:
                return None
            mw = src_lister.window()
            if mw is None or not hasattr(mw, 'left_lister') \
                    or not hasattr(mw, 'right_lister'):
                return None
            other = (mw.right_lister if src_lister is mw.left_lister
                      else mw.left_lister)
            # Only meaningful for local FS - extracting onto an FTP
            # mount would need staging through temp files; just fall
            # back to the picker in that case.
            if getattr(other.fs, 'kind', None) != 'local':
                return None
            return Path(other.current_path)
        except Exception:
            return None

    def _pick_extract_target(self, n_items):
        """Decide where to extract. Default: the other panel's
        current_path. The user gets a small confirmation dialog with
        a Browse button so they can override - same shortcut as
        Enter to accept the default. Returns Path or None (cancel)."""
        other = self._other_panel_path()
        if other is None:
            # No usable "other panel" target - fall back to the
            # classic picker so the user can still extract somewhere.
            outdir = QFileDialog.getExistingDirectory(
                self, "Extract to...")
            return Path(outdir) if outdir else None
        # Confirmation dialog: Yes = extract to other panel,
        # custom button = browse, Cancel = abort. QMessageBox is
        # fine here, no need for a full custom dialog.
        from PyQt6.QtWidgets import QMessageBox as _QMB
        box = _QMB(self)
        box.setWindowTitle("Extract")
        box.setIcon(_QMB.Icon.Question)
        box.setText(
            f"Extract {n_items} item(s) to the other panel?\n\n"
            f"Target: {other}")
        b_ok = box.addButton("Extract here", _QMB.ButtonRole.AcceptRole)
        b_browse = box.addButton("Browse...", _QMB.ButtonRole.ActionRole)
        b_cancel = box.addButton("Cancel", _QMB.ButtonRole.RejectRole)
        box.setDefaultButton(b_ok)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_ok:
            return other
        if clicked is b_browse:
            outdir = QFileDialog.getExistingDirectory(
                self, "Extract to...", str(other))
            return Path(outdir) if outdir else None
        return None

    def _refresh_other_panel(self):
        """After a successful extract, ask the other lister to reread
        its directory so the new files show up immediately."""
        try:
            src_lister = self.parent()
            mw = src_lister.window()
            other = (mw.right_lister if src_lister is mw.left_lister
                      else mw.left_lister)
            other.refresh()
        except Exception:
            pass

    def _extract_all(self):
        outdir = self._pick_extract_target(len(self.backend.entries()))
        if outdir is None: return
        self._extract_async(self.backend.entries(), outdir, "all")

    def _extract_selected(self):
        items = self._selected_entries()
        if not items:
            self.lbl_status.setText(" Nothing selected ")
            return
        outdir = self._pick_extract_target(len(items))
        if outdir is None: return
        self._extract_async(items, outdir, "selected")

    def _extract_async(self, entries, outdir, mode_label: str):
        """Run extract_to() on a background QThread with a visible
        progress dialog. The synchronous version was fine for ZIPs
        (in-process) but blocks the UI for many seconds on RARs,
        which spawn unrar.exe / 7z.exe per entry. From the user's
        perspective Quopus appears frozen.

        Behaviour:
          - QProgressDialog is shown immediately (non-modal but
            modal-looking via WindowModal).
          - Cancel button stops the worker between files; in-flight
            file is finished but no further entries are extracted.
          - On error / cancel a clean status line + message box
            inform the user.
          - The dialog is kept on `self` to prevent GC mid-run.
        """
        from PyQt6.QtCore import QThread, pyqtSignal, Qt as _Qt
        from PyQt6.QtWidgets import QProgressDialog

        # Pre-walk the entries we'll process so the progress bar has
        # a meaningful denominator. Excluding directories from the
        # count - they extract instantly via mkdir.
        files = [e for e in entries if not e.is_dir]
        total_files = len(files)
        total_bytes = sum(e.size for e in files)
        if total_files == 0:
            # Just create the directory entries synchronously and
            # we're done - no worker needed.
            try:
                self.backend.extract_to(entries, outdir)
                self.lbl_status.setText(
                    f" Extracted {len(entries)} entries to {outdir} ")
                self._refresh_other_panel()
            except Exception as e:
                QMessageBox.warning(self, "Extract", f"Extract failed: {e}")
            return

        backend = self.backend
        archive_path = self.path

        class _ExtractWorker(QThread):
            # done_files, total_files, done_bytes, total_bytes, name
            progress = pyqtSignal(int, int, int, int, str)
            done = pyqtSignal(bool, str)   # ok, error_message

            def __init__(self, entries, outdir):
                super().__init__()
                self.entries = entries
                self.outdir = outdir
                self._cancel = False

            def cancel(self):
                self._cancel = True

            def run(self):
                # RAR archives use a dedicated bulk path: spawning
                # unrar.exe per-file (via backend.read) is 50x slower
                # than letting unrar process everything in one call.
                # Total Commander does the latter; we match that.
                if backend.kind == 'rar':
                    self._run_rar_bulk()
                    return
                done_b = 0
                done_f = 0
                try:
                    self.outdir.mkdir(parents=True, exist_ok=True)
                    for e in self.entries:
                        if self._cancel:
                            raise InterruptedError("cancelled")
                        if e.is_dir:
                            (self.outdir / e.name).mkdir(
                                parents=True, exist_ok=True)
                            continue
                        self.progress.emit(done_f, total_files,
                                            done_b, total_bytes, e.name)
                        try:
                            data = backend.read(e)
                        except Exception as ex:
                            raise RuntimeError(
                                f"failed to read {e.name}: {ex}")
                        target = self.outdir / e.name
                        target.parent.mkdir(parents=True,
                                              exist_ok=True)
                        target.write_bytes(data)
                        done_b += e.size
                        done_f += 1
                        self.progress.emit(done_f, total_files,
                                            done_b, total_bytes, e.name)
                    self.done.emit(True, "")
                except InterruptedError:
                    self.done.emit(False, "cancelled")
                except Exception as ex:
                    self.done.emit(False, str(ex))

            def _run_rar_bulk(self):
                """Extract a RAR via the rarfile module, file-by-file.

                Earlier versions tried to spawn unrar.exe directly
                with bulk flags - that was faster on Linux but
                crashed on Windows with access violations
                (exit code 0xC0000005) when rarfile resolved to a
                tool with a different CLI than we assumed.

                Going through rarfile.read() per member spawns the
                tool fresh per file (slower) but rarfile knows how
                to call whatever's actually installed - UnRAR, 7z,
                WinRAR, unar, bsdtar - so it Just Works.

                Per-member iteration also gives us accurate progress
                reporting and responsive Cancel between files.
                """
                done_b = 0
                done_f = 0
                try:
                    self.outdir.mkdir(parents=True, exist_ok=True)
                    for e in self.entries:
                        if self._cancel:
                            self.done.emit(False, "cancelled")
                            return
                        if e.is_dir:
                            (self.outdir / e.name).mkdir(
                                parents=True, exist_ok=True)
                            continue
                        # Emit progress BEFORE the read so the dialog
                        # shows what's currently being decoded.
                        self.progress.emit(done_f, total_files,
                                            done_b, total_bytes, e.name)
                        try:
                            data = backend.read(e)
                        except Exception as ex:
                            raise RuntimeError(
                                f"failed to read {e.name}: {ex}")
                        target = self.outdir / e.name
                        target.parent.mkdir(parents=True,
                                              exist_ok=True)
                        target.write_bytes(data)
                        done_b += e.size
                        done_f += 1
                        self.progress.emit(done_f, total_files,
                                            done_b, total_bytes, e.name)
                    self.done.emit(True, "")
                except Exception as ex:
                    self.done.emit(False, str(ex))

        worker = _ExtractWorker(entries, outdir)
        # Keep a reference so the worker doesn't get GC'd mid-run.
        self._extract_worker = worker

        # QProgressDialog uses 32-bit signed ints; scale bytes down
        # to a 0..10000 tick range. 0.01% precision is plenty.
        PROGRESS_TICKS = 10000
        def _scale(b):
            if total_bytes <= 0: return 0
            return min(PROGRESS_TICKS,
                        int(b * PROGRESS_TICKS / total_bytes))

        dlg = QProgressDialog(
            f"Extracting from {self.path.name}...", "Cancel",
            0, PROGRESS_TICKS, self)
        dlg.setWindowTitle("Extract")
        dlg.setMinimumDuration(0)        # show immediately
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        # WindowModal blocks input on this dialog only, leaves the
        # rest of Quopus interactive. Better than ApplicationModal
        # because the user can switch panels / close other dialogs.
        dlg.setWindowModality(_Qt.WindowModality.WindowModal)
        dlg.show()
        dlg.canceled.connect(worker.cancel)
        self._extract_dlg = dlg

        speed = {'last_b': 0, 'last_t': 0.0, 'bps': 0.0}

        def _on_progress(done_f, total_f, done_b, total_b, name):
            import time as _t
            now = _t.monotonic()
            if speed['last_t'] == 0:
                speed['last_t'] = now
                speed['last_b'] = done_b
            dt = now - speed['last_t']
            if dt >= 0.25:
                speed['bps'] = (done_b - speed['last_b']) / dt
                speed['last_b'] = done_b
                speed['last_t'] = now
            bps = speed['bps']
            speed_text = f" @ {fmt_size(int(bps))}/s" if bps > 0 else ""
            eta_text = ""
            if bps > 0 and total_b > 0 and done_b < total_b:
                rem = (total_b - done_b) / bps
                if rem < 3600:
                    eta_text = (f" - ETA {int(rem//60):d}"
                                  f":{int(rem%60):02d}")
                else:
                    eta_text = (f" - ETA {int(rem//3600)}h"
                                  f"{int((rem%3600)//60):02d}m")
            pct = int(100 * done_b / total_b) if total_b else 0
            dlg.setLabelText(
                f"File {done_f} of {total_f}: {name}\n"
                f"{fmt_size(done_b)} / {fmt_size(total_b)} "
                f"({pct}%){speed_text}{eta_text}")
            dlg.setValue(_scale(done_b))

        def _on_done(ok, err):
            dlg.close()
            self._extract_worker = None
            self._extract_dlg = None
            if ok:
                self.lbl_status.setText(
                    f" Extracted {total_files} file(s) to {outdir} ")
                self._refresh_other_panel()
            elif err == "cancelled":
                self.lbl_status.setText(
                    f" Extraction cancelled (partial output in {outdir}) ")
            else:
                QMessageBox.warning(self, "Extract",
                                      f"Extract failed: {err}")

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        worker.start()

    def _on_double_click(self, item, col):
        e = item.data(0, Qt.ItemDataRole.UserRole)
        if e and not e.is_dir:
            self._view_selected()

    def closeEvent(self, ev):
        # If an extraction OR a single-file read is in flight, ask
        # the worker(s) to stop and wait briefly. Without this,
        # closing the dialog while a worker is using `self.backend`
        # can crash on Windows (rarfile holds a subprocess pipe
        # that gets torn down by backend.close() below).
        for attr in ('_extract_worker', '_read_worker'):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                w.cancel()
                w.wait(3000)  # max 3 s
        if self.backend: self.backend.close()
        super().closeEvent(ev)


def is_archive(path) -> bool:
    return _is_archive_path(Path(path))
