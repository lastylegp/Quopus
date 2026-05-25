"""ADF disk image viewer + editor dialog.

A file-manager-like dialog for browsing and modifying Amiga .adf
disk dumps. Left side is a directory tree, right side a preview
pane. The toolbar has read-only ops (extract) and write ops
(add file, rename, delete, set protection, validate, etc.) -
write ops only commit to disk when the user explicitly saves.

Backed by quopus_lib.adf.ADFImage - pure Python, supports OFS
and FFS, can validate the bitmap, create new disks, and switch
the bootblock between OFS and FFS.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QTreeWidget,
    QTreeWidgetItem, QPushButton, QLabel, QFileDialog,
    QMessageBox, QWidget, QPlainTextEdit, QHeaderView,
    QToolBar, QInputDialog, QLineEdit, QCheckBox, QFormLayout,
    QDialogButtonBox, QComboBox, QMenu, QStatusBar,
)

from .adf import ADFImage, ADFError, create_blank_adf


class _NewDiskDialog(QDialog):
    """Modal dialog for picking new-disk parameters: label,
    OFS/FFS, INTL flag, DD/HD size."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New ADF disk")
        form = QFormLayout(self)

        self.label_edit = QLineEdit("Empty")
        self.label_edit.setMaxLength(30)
        form.addRow("Disk label (max 30 chars):", self.label_edit)

        self.fs_combo = QComboBox()
        self.fs_combo.addItem(
            "OFS (Workbench 1.3+, all Amigas)", False)
        self.fs_combo.addItem(
            "FFS (Workbench 2.0+, faster)", True)
        form.addRow("Filesystem:", self.fs_combo)

        self.intl_check = QCheckBox(
            "International characters (INTL)")
        form.addRow("", self.intl_check)

        self.hd_combo = QComboBox()
        self.hd_combo.addItem('DD - 880 KB (3.5")', False)
        self.hd_combo.addItem('HD - 1.76 MB (3.5" HD)', True)
        form.addRow("Density:", self.hd_combo)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def label(self) -> str:
        return self.label_edit.text().strip() or "Empty"

    def ffs(self) -> bool:
        return self.fs_combo.currentData()

    def intl(self) -> bool:
        return self.intl_check.isChecked()

    def hd(self) -> bool:
        return self.hd_combo.currentData()


class _BootBlockDialog(QDialog):
    """Set OFS/FFS/INTL/DIRCACHE flags on the bootblock of an
    existing disk. Doesn't add or remove bootcode - just toggles
    the disk-type flag byte."""

    def __init__(self, img: ADFImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set boot block flags")
        form = QFormLayout(self)
        info = QLabel(
            "<b>Warning:</b> changing the filesystem flag here<br>"
            "only updates the boot block. Existing files keep<br>"
            "their on-disk format - use only when reformatting.")
        info.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(info)

        self.ffs_check = QCheckBox("FFS (vs OFS)")
        self.ffs_check.setChecked(img.is_ffs)
        form.addRow(self.ffs_check)

        self.intl_check = QCheckBox("International characters")
        self.intl_check.setChecked(img.is_intl)
        form.addRow(self.intl_check)

        self.dc_check = QCheckBox("Directory cache")
        self.dc_check.setChecked(img.has_dircache)
        form.addRow(self.dc_check)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def is_ffs(self) -> bool:
        return self.ffs_check.isChecked()

    def is_intl(self) -> bool:
        return self.intl_check.isChecked()

    def has_dircache(self) -> bool:
        return self.dc_check.isChecked()


class ADFDiskDialog(QDialog):
    """Browse, edit, and create Amiga .adf disk images.

    On startup: opens the given .adf, parses bootblock + root,
    populates the tree. Mutation ops update the in-memory image;
    Save commits to disk. Title bar shows * for unsaved changes.
    """

    @classmethod
    def new_disk(cls, parent=None):
        """Convenience constructor: ask the user for new-disk
        parameters, write the file, return a dialog open on it.
        Returns None if the user cancels."""
        nd = _NewDiskDialog(parent)
        if nd.exec() != QDialog.DialogCode.Accepted:
            return None
        save_path, _ = QFileDialog.getSaveFileName(
            parent, "Save new ADF",
            "untitled.adf",
            "Amiga disk images (*.adf);;All files (*)")
        if not save_path:
            return None
        if not save_path.lower().endswith(".adf"):
            save_path += ".adf"
        try:
            create_blank_adf(
                save_path,
                label=nd.label(),
                ffs=nd.ffs(),
                intl=nd.intl(),
                hd=nd.hd())
        except Exception as e:
            QMessageBox.critical(
                parent, "New ADF",
                f"Couldn't create disk:\n{e}")
            return None
        return cls(save_path, parent)

    def __init__(self, adf_path: str, parent=None):
        super().__init__(parent)
        self.img = ADFImage(adf_path)
        self.adf_path = Path(adf_path)
        self._update_title()
        self.resize(1000, 650)

        root = QVBoxLayout(self)

        self.toolbar = QToolBar()
        self._build_toolbar()
        root.addWidget(self.toolbar)

        self.info_label = QLabel()
        self._refresh_info()
        root.addWidget(self.info_label)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Name", "Size", "Protection", "Date", "Comment"])
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            self.tree.header().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(
            self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(
            self._on_double_click)
        self.tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(
            self._show_context_menu)
        split.addWidget(self.tree)

        # Preview pane
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QLabel("Preview")
        self.preview_label.setStyleSheet(
            "QLabel { font-weight: bold; padding: 4px; }")
        right_lay.addWidget(self.preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        f = self.preview.font()
        f.setFamily("Courier")
        f.setStyleHint(f.StyleHint.Monospace)
        self.preview.setFont(f)
        right_lay.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setSizes([550, 450])

        # Status bar
        self.statusbar = QStatusBar()
        root.addWidget(self.statusbar)
        self._refresh_status()

        # Bottom: extract-all + close
        btns = QHBoxLayout()
        self.btn_extract_all = QPushButton(
            "Extract All to Folder...")
        self.btn_extract_all.clicked.connect(self._on_extract_all)
        btns.addWidget(self.btn_extract_all)
        btns.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self._on_close)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

        self._populate_tree()

    # --- chrome -------------------------------------------------

    def _build_toolbar(self):
        """Toolbar mixes file-management and disk-level ops.
        QActions wire into both the toolbar and right-click
        context menu."""
        tb = self.toolbar
        self.act_save = QAction("Save", self)
        self.act_save.setShortcut("Ctrl+S")
        self.act_save.triggered.connect(self._on_save)
        tb.addAction(self.act_save)
        tb.addSeparator()
        self.act_add_file = QAction("Add File...", self)
        self.act_add_file.triggered.connect(self._on_add_file)
        tb.addAction(self.act_add_file)
        self.act_add_dir = QAction("New Folder...", self)
        self.act_add_dir.triggered.connect(self._on_add_dir)
        tb.addAction(self.act_add_dir)
        self.act_extract = QAction("Extract...", self)
        self.act_extract.triggered.connect(self._on_extract)
        tb.addAction(self.act_extract)
        tb.addSeparator()
        self.act_rename = QAction("Rename...", self)
        self.act_rename.triggered.connect(self._on_rename)
        tb.addAction(self.act_rename)
        self.act_delete = QAction("Delete", self)
        self.act_delete.triggered.connect(self._on_delete)
        tb.addAction(self.act_delete)
        self.act_set_comment = QAction("Comment...", self)
        self.act_set_comment.triggered.connect(self._on_set_comment)
        tb.addAction(self.act_set_comment)
        tb.addSeparator()
        self.act_set_label = QAction("Disk Label...", self)
        self.act_set_label.triggered.connect(self._on_set_label)
        tb.addAction(self.act_set_label)
        self.act_boot_flags = QAction("Boot Flags...", self)
        self.act_boot_flags.triggered.connect(self._on_boot_flags)
        tb.addAction(self.act_boot_flags)
        self.act_validate = QAction("Validate", self)
        self.act_validate.triggered.connect(self._on_validate)
        tb.addAction(self.act_validate)
        tb.addSeparator()
        self.act_new_disk = QAction("New Disk...", self)
        self.act_new_disk.triggered.connect(self._on_new_disk)
        tb.addAction(self.act_new_disk)

    def _update_title(self):
        dirty = " *" if self.img.dirty else ""
        self.setWindowTitle(
            f"ADF: {self.img.disk_name}  "
            f"({self.adf_path.name}){dirty}")

    def _refresh_info(self):
        self.info_label.setText(
            f"<b>{self.img.disk_name}</b>  "
            f"&middot; {'FFS' if self.img.is_ffs else 'OFS'}"
            f"{' INTL' if self.img.is_intl else ''}"
            f"{' DIRCACHE' if self.img.has_dircache else ''}"
            f"  &middot; {self.img.block_count} blocks "
            f"({self.img.block_count * 512 // 1024} KB)")
        self.info_label.setTextFormat(Qt.TextFormat.RichText)

    def _refresh_status(self):
        free = self.img.free_block_count()
        free_kb = free * 512 // 1024
        used = self.img.block_count - free
        self.statusbar.showMessage(
            f"Free: {free} blocks ({free_kb} KB)  "
            f"|  Used: {used} blocks")

    # --- tree population ----------------------------------------

    def _populate_tree(self):
        """Rebuild the entire tree from a single walk() pass.
        Called on init and after any operation that changes
        the directory structure. Tree state (expanded folders,
        selection) is NOT preserved - the alternative
        (incremental updates per-op) is much more error-prone
        for the win of preserving scroll position."""
        self.tree.clear()
        root_item = QTreeWidgetItem(self.tree)
        root_item.setText(0, self.img.disk_name + ":")
        root_item.setText(3, self.img.disk_mtime.strftime(
            "%Y-%m-%d %H:%M"))
        root_item.setData(0, Qt.ItemDataRole.UserRole, None)
        root_item.setExpanded(True)

        items_by_path = {"": root_item}
        for full_path, entry in self.img.walk():
            parent_path = "/".join(full_path.split("/")[:-1])
            parent_item = items_by_path.get(parent_path, root_item)
            item = QTreeWidgetItem(parent_item)
            display_name = entry.name
            if entry.is_dir:
                display_name += "/"
            elif entry.is_softlink:
                display_name += " ->"
            item.setText(0, display_name)
            if not entry.is_dir:
                item.setText(1, f"{entry.size_bytes:,}")
            item.setText(2,
                self.img.format_protection(entry.protection))
            item.setText(3, entry.timestamp.strftime(
                "%Y-%m-%d %H:%M"))
            item.setText(4, entry.comment)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            items_by_path[full_path] = item

        self.tree.expandToDepth(0)
        self._refresh_status()
        self._update_title()
        self._refresh_info()

    def _selected_entry(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _parent_dir_block(self) -> int:
        """Block number of the directory to use for Add ops:
        either the currently-selected directory, or the
        directory containing the selected file, or root."""
        entry = self._selected_entry()
        if entry is None:
            return self.img.rootblock_num
        if entry.is_dir:
            return entry.block
        return entry.parent_block or self.img.rootblock_num

    # --- preview ------------------------------------------------

    def _on_selection_changed(self):
        entry = self._selected_entry()
        has_file_or_dir = (
            entry is not None and not entry.is_softlink
            and not entry.is_hardlink)
        self.act_extract.setEnabled(
            has_file_or_dir and not entry.is_dir)
        self.act_rename.setEnabled(has_file_or_dir)
        self.act_delete.setEnabled(has_file_or_dir)
        self.act_set_comment.setEnabled(has_file_or_dir)

        if entry is None or entry.is_dir:
            self.preview_label.setText("Preview")
            self.preview.setPlainText("")
            return
        try:
            data = self.img.read_file(entry.block)
        except ADFError as e:
            self.preview_label.setText(
                f"Preview: {entry.name} (error)")
            self.preview.setPlainText(f"Could not read: {e}")
            return
        sample = data[:1024]
        printable = sum(
            1 for b in sample
            if 32 <= b <= 126 or b in (9, 10, 13))
        if printable >= len(sample) * 0.85 and sample:
            try:
                text = sample.decode("latin-1", errors="replace")
            except Exception:
                text = "(decode failed)"
            shown = text
            if len(data) > 1024:
                shown += f"\n\n[...{len(data) - 1024} more bytes]"
            self.preview_label.setText(
                f"Preview: {entry.name} (text, "
                f"{entry.size_bytes:,} bytes)")
            self.preview.setPlainText(shown)
        else:
            lines = []
            for off in range(0, min(len(sample), 256), 16):
                chunk = sample[off:off + 16]
                hexpart = ' '.join(f'{b:02x}' for b in chunk)
                asciipart = ''.join(
                    chr(b) if 32 <= b < 127 else '.'
                    for b in chunk)
                lines.append(
                    f"{off:04x}  {hexpart:<48s}  {asciipart}")
            if len(data) > 256:
                lines.append(
                    f"\n[...{len(data) - 256} more bytes]")
            self.preview_label.setText(
                f"Preview: {entry.name} (binary, "
                f"{entry.size_bytes:,} bytes)")
            self.preview.setPlainText('\n'.join(lines))

    def _on_double_click(self, item, _column):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None or entry.is_dir:
            return
        self._on_extract()

    def _show_context_menu(self, pos):
        menu = QMenu(self.tree)
        menu.addAction(self.act_extract)
        menu.addSeparator()
        menu.addAction(self.act_add_file)
        menu.addAction(self.act_add_dir)
        menu.addSeparator()
        menu.addAction(self.act_rename)
        menu.addAction(self.act_delete)
        menu.addAction(self.act_set_comment)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # --- extract -----------------------------------------------

    def _on_extract(self):
        entry = self._selected_entry()
        if entry is None or entry.is_dir:
            return
        default = (self.adf_path.parent / entry.name)
        target_str, _ = QFileDialog.getSaveFileName(
            self, "Extract file",
            str(default), "All files (*)")
        if not target_str:
            return
        target = Path(target_str)
        try:
            data = self.img.read_file(entry.block)
            target.write_bytes(data)
        except Exception as e:
            QMessageBox.warning(self, "Extract failed", str(e))
            return
        QMessageBox.information(
            self, "Extract",
            f"Wrote {len(data):,} bytes to:\n{target}")

    def _on_extract_all(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Extract all - choose target folder",
            str(self.adf_path.parent))
        if not folder:
            return
        target_root = Path(folder)
        n_files = 0
        n_dirs = 0
        errors = []
        for full_path, entry in self.img.walk():
            safe_parts = []
            for part in full_path.split("/"):
                cleaned = "".join(
                    c if c not in '<>:"/\\|?*' else '_'
                    for c in part)
                safe_parts.append(cleaned)
            local = target_root / Path(*safe_parts)
            if entry.is_dir:
                local.mkdir(parents=True, exist_ok=True)
                n_dirs += 1
                continue
            if entry.is_softlink or entry.is_hardlink:
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = self.img.read_file(entry.block)
                local.write_bytes(data)
                n_files += 1
            except Exception as e:
                errors.append(f"{full_path}: {e}")
        msg = (f"Extracted {n_files} file(s), "
               f"{n_dirs} folder(s) to:\n{target_root}")
        if errors:
            msg += f"\n\n{len(errors)} error(s):\n"
            msg += "\n".join(errors[:10])
            if len(errors) > 10:
                msg += f"\n...and {len(errors) - 10} more"
        QMessageBox.information(self, "Extract all", msg)

    # --- write operations ---------------------------------------

    def _on_add_file(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Add file to ADF",
            str(self.adf_path.parent),
            "All files (*)")
        if not path_str:
            return
        src = Path(path_str)
        default_name = src.name[:30]
        name, ok = QInputDialog.getText(
            self, "Add file",
            "Filename on ADF (max 30 chars):",
            text=default_name)
        if not ok or not name:
            return
        try:
            data = src.read_bytes()
        except Exception as e:
            QMessageBox.warning(
                self, "Add file",
                f"Couldn't read source:\n{e}")
            return
        try:
            self.img.add_file(
                self._parent_dir_block(), name, data)
        except ADFError as e:
            QMessageBox.warning(self, "Add file", str(e))
            return
        self._populate_tree()

    def _on_add_dir(self):
        name, ok = QInputDialog.getText(
            self, "New folder",
            "Folder name (max 30 chars):")
        if not ok or not name:
            return
        try:
            self.img.add_directory(
                self._parent_dir_block(), name)
        except ADFError as e:
            QMessageBox.warning(self, "New folder", str(e))
            return
        self._populate_tree()

    def _on_rename(self):
        entry = self._selected_entry()
        if entry is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename",
            "New name (max 30 chars):",
            text=entry.name)
        if not ok or not new_name or new_name == entry.name:
            return
        try:
            self.img.rename_entry(entry.block, new_name)
        except ADFError as e:
            QMessageBox.warning(self, "Rename", str(e))
            return
        self._populate_tree()

    def _on_delete(self):
        entry = self._selected_entry()
        if entry is None:
            return
        kind = "folder" if entry.is_dir else "file"
        if entry.is_dir and any(
                slot for slot in entry.hash_table):
            QMessageBox.warning(
                self, "Delete",
                f"Folder {entry.name!r} is not empty.\n"
                f"Delete its contents first.")
            return
        ans = QMessageBox.question(
            self, "Delete",
            f"Delete {kind} {entry.name!r}?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            self.img.delete_file(entry.block)
        except ADFError as e:
            QMessageBox.warning(self, "Delete", str(e))
            return
        self._populate_tree()

    def _on_set_comment(self):
        entry = self._selected_entry()
        if entry is None:
            return
        new_comment, ok = QInputDialog.getText(
            self, "Comment",
            "Comment (max 79 chars):",
            text=entry.comment)
        if not ok:
            return
        try:
            self.img.set_comment(entry.block, new_comment)
        except ADFError as e:
            QMessageBox.warning(self, "Comment", str(e))
            return
        self._populate_tree()

    def _on_set_label(self):
        new_label, ok = QInputDialog.getText(
            self, "Disk label",
            "New disk label (max 30 chars):",
            text=self.img.disk_name)
        if not ok or not new_label:
            return
        try:
            self.img.set_disk_label(new_label)
        except ADFError as e:
            QMessageBox.warning(self, "Disk label", str(e))
            return
        self._populate_tree()

    def _on_boot_flags(self):
        dlg = _BootBlockDialog(self.img, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.img.set_bootblock(
                is_ffs=dlg.is_ffs(),
                is_intl=dlg.is_intl(),
                has_dircache=dlg.has_dircache())
        except ADFError as e:
            QMessageBox.warning(self, "Boot flags", str(e))
            return
        self._populate_tree()

    def _on_validate(self):
        try:
            result = self.img.validate()
        except ADFError as e:
            QMessageBox.warning(self, "Validate", str(e))
            return
        if not result["fixed_bitmap"]:
            QMessageBox.information(
                self, "Validate",
                "Bitmap is consistent - no changes needed.")
        else:
            QMessageBox.information(
                self, "Validate",
                f"Bitmap rebuilt:\n"
                f"  {result['freed_count']} block(s) "
                f"freed (were marked used, "
                f"actually unreachable)\n"
                f"  {result['lost_count']} block(s) "
                f"reclaimed (were marked free, "
                f"actually used)")
        self._populate_tree()

    def _on_new_disk(self):
        """Open another viewer on a fresh blank disk. Doesn't
        touch the current one."""
        dlg = ADFDiskDialog.new_disk(self.parent())
        if dlg is None:
            return
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    # --- save / close -------------------------------------------

    def _on_save(self):
        try:
            self.img.save()
        except Exception as e:
            QMessageBox.warning(self, "Save", str(e))
            return
        self._update_title()
        self.statusbar.showMessage(
            f"Saved to {self.adf_path}", 3000)

    def _on_close(self):
        if self.img.dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "Save changes before closing?",
                (QMessageBox.StandardButton.Save
                 | QMessageBox.StandardButton.Discard
                 | QMessageBox.StandardButton.Cancel))
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Save:
                try:
                    self.img.save()
                except Exception as e:
                    QMessageBox.warning(self, "Save", str(e))
                    return
        self.accept()

    def closeEvent(self, event):
        if self.img.dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "Save changes before closing?",
                (QMessageBox.StandardButton.Save
                 | QMessageBox.StandardButton.Discard
                 | QMessageBox.StandardButton.Cancel))
            if ans == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if ans == QMessageBox.StandardButton.Save:
                try:
                    self.img.save()
                except Exception as e:
                    QMessageBox.warning(self, "Save", str(e))
                    event.ignore()
                    return
        event.accept()
