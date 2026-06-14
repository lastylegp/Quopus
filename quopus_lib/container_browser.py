# date_time: 2026-06-13 19:56
"""
Container / Image Browser  (Quopus premium module #18)
======================================================

Browse Docker images and containers like a filesystem: list images
and containers, walk a container's (or image's) files, copy files in
and out, and run a command in a running container.

Filesystem access uses the Docker archive API (get_archive /
put_archive), which streams a tar of the requested path - no host
mount needed. For an image we materialise a throw-away stopped
container to read from.

The docker SDK is imported lazily; the tar-listing core is pure and
unit-tested.
"""

from __future__ import annotations

import io
import os
import posixpath
import tarfile
from dataclasses import dataclass
from typing import Optional


@dataclass
class FSItem:
    name: str
    is_dir: bool
    size: int = 0
    mode: int = 0


# ---------------------------------------------------- tar listing core
def list_archive_children(tar_bytes: bytes, base_name: str) -> list:
    """Given a tar produced by Docker get_archive(<path>), return the
    IMMEDIATE children of <path>. Docker prefixes members with the
    basename of the requested path, e.g. archiving '/etc' yields
    members 'etc/', 'etc/hosts', 'etc/ssl/', ... We strip that prefix
    and collapse to one level."""
    children: dict = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
        for m in tf.getmembers():
            name = m.name
            # normalise: drop the leading base component
            parts = name.split("/")
            if parts and parts[0] == base_name:
                parts = parts[1:]
            parts = [p for p in parts if p not in ("", ".")]
            if not parts:
                continue
            first = parts[0]
            deeper = len(parts) > 1
            if first not in children:
                children[first] = FSItem(
                    name=first,
                    is_dir=(m.isdir() if not deeper else True),
                    size=(0 if deeper else m.size),
                    mode=m.mode)
            else:
                if deeper:
                    children[first].is_dir = True
    return sorted(children.values(),
                  key=lambda i: (not i.is_dir, i.name.lower()))


def extract_single_file(tar_bytes: bytes, member_basename: str) -> bytes:
    """Pull the bytes of one regular file out of a get_archive tar."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
        for m in tf.getmembers():
            if m.isfile() and posixpath.basename(m.name) == member_basename:
                f = tf.extractfile(m)
                return f.read() if f else b""
    raise FileNotFoundError(member_basename)


def make_put_tar(local_path: str, arcname: str) -> bytes:
    """Build a tar for put_archive from a local file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(local_path, arcname=arcname)
    return buf.getvalue()


# ----------------------------------------------------------- docker hub
class DockerError(Exception):
    pass


class DockerHub:
    needs = "docker"

    def __init__(self, base_url: str = ""):
        try:
            import docker
        except ImportError:
            raise DockerError("needs the docker SDK (pip install docker)")
        try:
            self._c = (docker.DockerClient(base_url=base_url)
                       if base_url else docker.from_env())
            self._c.ping()
        except Exception as e:
            raise DockerError(f"cannot reach Docker daemon: {e}")
        self._scratch = {}     # image_id -> throwaway container

    # -- listings ----------------------------------------------------
    def list_images(self) -> list:
        out = []
        for img in self._c.images.list():
            tags = img.tags or ["<none>:<none>"]
            out.append({"id": img.short_id, "tags": tags,
                        "size": img.attrs.get("Size", 0)})
        return out

    def list_containers(self, all_=True) -> list:
        out = []
        for c in self._c.containers.list(all=all_):
            out.append({"id": c.short_id, "name": c.name,
                        "image": (c.image.tags or ["<none>"])[0],
                        "status": c.status})
        return out

    # -- filesystem --------------------------------------------------
    def _container_for(self, target: dict):
        """target = {'type':'container','id':..} or
                     {'type':'image','id':..}. Returns a docker
        container object to read from (creating a scratch one for an
        image)."""
        if target["type"] == "container":
            return self._c.containers.get(target["id"])
        # image: reuse / create a stopped scratch container
        iid = target["id"]
        cont = self._scratch.get(iid)
        if cont is None:
            cont = self._c.containers.create(iid, command="/bin/true")
            self._scratch[iid] = cont
        return cont

    def listdir(self, target: dict, path: str) -> list:
        cont = self._container_for(target)
        path = path or "/"
        try:
            bits, _ = cont.get_archive(path)
        except Exception as e:
            raise DockerError(f"cannot read {path}: {e}")
        data = b"".join(bits)
        base = posixpath.basename(path.rstrip("/")) or path.strip("/")
        return list_archive_children(data, base)

    def read_file(self, target: dict, path: str) -> bytes:
        cont = self._container_for(target)
        bits, _ = cont.get_archive(path)
        data = b"".join(bits)
        return extract_single_file(data, posixpath.basename(path))

    def download(self, target: dict, remote: str, local: str):
        with open(local, "wb") as f:
            f.write(self.read_file(target, remote))

    def upload(self, target: dict, local: str, remote_dir: str):
        cont = self._container_for(target)
        tar = make_put_tar(local, os.path.basename(local))
        if not cont.put_archive(remote_dir or "/", tar):
            raise DockerError("put_archive failed")

    def exec_cmd(self, container_id: str, cmd) -> str:
        c = self._c.containers.get(container_id)
        if isinstance(cmd, str):
            cmd = ["/bin/sh", "-c", cmd]
        res = c.exec_run(cmd, demux=False)
        out = res.output
        return out.decode("utf-8", "replace") if isinstance(out, bytes) \
            else str(out)

    def cleanup(self):
        for cont in self._scratch.values():
            try:
                cont.remove(force=True)
            except Exception:
                pass
        self._scratch.clear()


# ------------------------------------------------------------------- UI
def open_container_browser(parent=None, config=None):
    dlg = ContainerBrowserDialog(parent=parent, config=config)
    dlg.show()
    return dlg


try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
        QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
        QSplitter, QWidget, QTabWidget, QInputDialog, QPlainTextEdit)
    _HAVE_QT = True
except Exception:
    _HAVE_QT = False


if _HAVE_QT:

    class _Job(QThread):
        ok = pyqtSignal(object)
        fail = pyqtSignal(str)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.ok.emit(self._fn())
            except Exception as e:
                self.fail.emit(str(e))

    class ContainerBrowserDialog(QDialog):
        def __init__(self, parent=None, config=None):
            super().__init__(parent)
            self.setWindowTitle("Container / Image Browser")
            self.resize(900, 600)
            self.config = config or {}
            self.hub: Optional[DockerHub] = None
            self.target = None
            self.cwd = "/"
            self._job = None
            self._build()
            self._connect()

        def _build(self):
            v = QVBoxLayout(self)
            split = QSplitter(Qt.Orientation.Horizontal)

            left = QWidget(); ll = QVBoxLayout(left)
            self.tabs = QTabWidget()
            self.lst_img = QListWidget()
            self.lst_img.itemDoubleClicked.connect(
                lambda it: self._pick("image", it))
            self.lst_con = QListWidget()
            self.lst_con.itemDoubleClicked.connect(
                lambda it: self._pick("container", it))
            self.tabs.addTab(self.lst_img, "Images")
            self.tabs.addTab(self.lst_con, "Containers")
            ll.addWidget(self.tabs, 1)
            btn_refresh = QPushButton("Refresh")
            btn_refresh.clicked.connect(self._refresh)
            ll.addWidget(btn_refresh)
            split.addWidget(left)

            right = QWidget(); rl = QVBoxLayout(right)
            nav = QHBoxLayout()
            self.btn_up = QPushButton("Up")
            self.btn_up.clicked.connect(self._up)
            self.ed_path = QLineEdit("/")
            self.ed_path.returnPressed.connect(
                lambda: self._chdir(self.ed_path.text()))
            nav.addWidget(self.btn_up); nav.addWidget(self.ed_path, 1)
            rl.addLayout(nav)
            self.lst_files = QListWidget()
            self.lst_files.itemDoubleClicked.connect(self._open_item)
            rl.addWidget(self.lst_files, 1)
            ops = QHBoxLayout()
            for label, slot in (("Download", self._download),
                                ("Upload", self._upload),
                                ("Exec...", self._exec)):
                b = QPushButton(label); b.clicked.connect(slot)
                ops.addWidget(b)
            rl.addLayout(ops)
            self.out = QPlainTextEdit(); self.out.setReadOnly(True)
            self.out.setFixedHeight(120)
            rl.addWidget(self.out)
            split.addWidget(right)
            split.setSizes([300, 600])
            v.addWidget(split, 1)
            self.lbl = QLabel("Connecting to Docker...")
            v.addWidget(self.lbl)

        def _connect(self):
            def job():
                return DockerHub()
            self._run(job, self._on_hub)

        def _on_hub(self, hub):
            self.hub = hub
            self.lbl.setText("Connected to Docker.")
            self._refresh()

        def _refresh(self):
            if not self.hub:
                return
            self._run(self.hub.list_images, self._fill_images)
            self._run(self.hub.list_containers, self._fill_containers)

        def _fill_images(self, imgs):
            self.lst_img.clear()
            for im in imgs:
                it = QListWidgetItem(f"{', '.join(im['tags'])}  "
                                     f"({im['id']})")
                it.setData(Qt.ItemDataRole.UserRole, im["id"])
                self.lst_img.addItem(it)

        def _fill_containers(self, cons):
            self.lst_con.clear()
            for c in cons:
                it = QListWidgetItem(
                    f"{c['name']}  [{c['status']}]  {c['image']}")
                it.setData(Qt.ItemDataRole.UserRole, c["id"])
                self.lst_con.addItem(it)

        def _pick(self, kind, item):
            self.target = {"type": kind,
                           "id": item.data(Qt.ItemDataRole.UserRole)}
            self.cwd = "/"
            self._chdir("/")

        def _chdir(self, path):
            if not (self.hub and self.target):
                return
            self._run(lambda: self.hub.listdir(self.target, path),
                      lambda items: self._show(path, items))

        def _show(self, path, items):
            self.cwd = path or "/"
            self.ed_path.setText(self.cwd)
            self.lst_files.clear()
            for it in items:
                label = ("[DIR] " if it.is_dir else "      ") + it.name
                li = QListWidgetItem(label)
                li.setData(Qt.ItemDataRole.UserRole, (it.name, it.is_dir))
                self.lst_files.addItem(li)
            self.lbl.setText(f"{self.cwd} - {len(items)} item(s)")

        def _open_item(self, item):
            name, is_dir = item.data(Qt.ItemDataRole.UserRole)
            if is_dir:
                self._chdir(posixpath.join(self.cwd, name))

        def _up(self):
            self._chdir(posixpath.dirname(self.cwd.rstrip("/")) or "/")

        def _download(self):
            it = self.lst_files.currentItem()
            if not it:
                return
            name, is_dir = it.data(Qt.ItemDataRole.UserRole)
            if is_dir:
                return
            local, _ = QFileDialog.getSaveFileName(self, "Download", name)
            if not local:
                return
            remote = posixpath.join(self.cwd, name)
            self._run(
                lambda: self.hub.download(self.target, remote, local),
                lambda _: self.lbl.setText(f"Downloaded {name}"))

        def _upload(self):
            if self.target and self.target["type"] == "image":
                QMessageBox.information(
                    self, "Upload",
                    "Upload into an image's scratch container only; "
                    "pick a running container to persist.")
            local, _ = QFileDialog.getOpenFileName(self, "Upload")
            if not local:
                return
            self._run(
                lambda: self.hub.upload(self.target, local, self.cwd),
                lambda _: self._chdir(self.cwd))

        def _exec(self):
            if not self.target or self.target["type"] != "container":
                QMessageBox.information(self, "Exec",
                                        "Pick a running container.")
                return
            cmd, ok = QInputDialog.getText(self, "Exec", "command:")
            if not ok or not cmd:
                return
            self._run(
                lambda: self.hub.exec_cmd(self.target["id"], cmd),
                lambda out: self.out.setPlainText(out))

        def _run(self, fn, on_ok):
            self._job = _Job(fn)
            self._job.ok.connect(on_ok)
            self._job.fail.connect(
                lambda m: (self.lbl.setText("Error"),
                           QMessageBox.warning(self, "Docker", m)))
            self._job.start()

        def closeEvent(self, ev):
            try:
                if self.hub:
                    self.hub.cleanup()
            except Exception:
                pass
            super().closeEvent(ev)

else:
    class ContainerBrowserDialog:               # pragma: no cover
        def __init__(self, *a, **k):
            raise RuntimeError("PyQt6 not available")
        def show(self):
            pass
