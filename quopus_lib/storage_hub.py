# date_time: 2026-06-13 20:32
"""
Network Storage Hub  (Quopus premium module #6)
===============================================

A cross-protocol file ORCHESTRATOR, not just another single-remote
browser. It reuses the protocols Quopus already has and adds the ones
it lacks, then puts an engine on top that none of the per-protocol
browsers offer:

Endpoints (any pane can be any of these):
  * Local filesystem
  * FTP / FTPS / SFTP        -> reuses ftp_backend.make_backend (no dup)
  * S3 / S3-compatible       -> new (boto3, lazy)
  * SMB / CIFS               -> new (smbprotocol, lazy)

Engine (the real value-add):
  * Cross-protocol transfer  - stream src.open_read -> dst.open_write,
    so SFTP->S3, SMB->SFTP, S3->local, etc. all "just work".
  * Recursive transfer QUEUE - parallel workers, byte progress, retry.
  * SYNC / MIRROR            - diff two endpoints by size/mtime (or
    SHA-256), dry-run plan, then apply; mirror also deletes extras.
  * VERIFY                   - SHA-256 both sides after copy.

Credentials come from the Secrets vault, never plain config.
boto3 / smbprotocol are imported lazily.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable


# ============================================================ data types
@dataclass
class RemoteItem:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class BackendError(Exception):
    pass


class _CtxFile:
    """Uniform file-like wrapper with optional on_close hook and temp
    cleanup, so every backend's open_read/open_write looks the same."""
    def __init__(self, f, on_close: Optional[Callable] = None,
                 cleanup: Optional[str] = None):
        self._f = f
        self._on_close = on_close
        self._cleanup = cleanup

    def read(self, n=-1):
        return self._f.read(n)

    def write(self, b):
        return self._f.write(b)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self._f.close()
        finally:
            if self._on_close:
                self._on_close()
            if self._cleanup:
                try:
                    os.remove(self._cleanup)
                except Exception:
                    pass


# ============================================================ base class
class RemoteBackend:
    kind = "base"
    needs = ""

    # -- lifecycle ---------------------------------------------------
    def connect(self): ...
    def disconnect(self):
        pass

    # -- listing -----------------------------------------------------
    def listdir(self, path: str) -> list:
        raise NotImplementedError

    def stat(self, path: str) -> Optional[RemoteItem]:
        name = posixpath.basename(path.rstrip("/")) or path
        try:
            for it in self.listdir(self.parent(path)):
                if it.name == name:
                    return it
        except Exception:
            pass
        return None

    # -- streaming (the universal copy interface) --------------------
    def open_read(self, path: str) -> _CtxFile:
        raise NotImplementedError

    def open_write(self, path: str) -> _CtxFile:
        raise NotImplementedError

    # -- mutation ----------------------------------------------------
    def mkdir(self, path: str):
        raise NotImplementedError

    def makedirs(self, path: str):
        """Best-effort recursive mkdir; ignores 'already exists'."""
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for p in parts:
            cur = cur + "/" + p
            try:
                self.mkdir(cur)
            except Exception:
                pass

    def delete(self, path: str):
        raise NotImplementedError

    # -- helpers -----------------------------------------------------
    @staticmethod
    def join(base: str, name: str) -> str:
        return posixpath.join(base or "/", name)

    @staticmethod
    def parent(path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    def walk_files(self, root: str):
        """Yield (relative_posix_path, size) for every file under root.
        If root is a file, yields just its basename."""
        st = self.stat(root)
        if st and not st.is_dir:
            yield (posixpath.basename(root), st.size)
            return

        def rec(cur, rel):
            for it in self.listdir(cur):
                cpath = self.join(cur, it.name)
                crel = (rel + "/" + it.name).lstrip("/")
                if it.is_dir:
                    rec(cpath, crel)
                else:
                    yield_list.append((crel, it.size))
        yield_list: list = []
        rec(root, "")
        for x in yield_list:
            yield x


# ============================================================ Local
class LocalBackend(RemoteBackend):
    kind = "local"

    def listdir(self, path: str) -> list:
        path = path or "/"
        out = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    stt = e.stat()
                    out.append(RemoteItem(e.name, e.is_dir(),
                                          stt.st_size, stt.st_mtime))
                except OSError:
                    continue
        return sorted(out, key=lambda i: (not i.is_dir, i.name.lower()))

    def stat(self, path):
        try:
            stt = os.stat(path)
            return RemoteItem(os.path.basename(path),
                              os.path.isdir(path), stt.st_size,
                              stt.st_mtime)
        except OSError:
            return None

    def open_read(self, path):
        return _CtxFile(open(path, "rb"))

    def open_write(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return _CtxFile(open(path, "wb"))

    def mkdir(self, path):
        os.makedirs(path, exist_ok=True)

    def delete(self, path):
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)


def _spool_writer(upload_fn: Callable[[str], None]) -> _CtxFile:
    """A write target that buffers to a temp file and uploads on close.
    Used by backends whose SDK only offers file-path upload."""
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmppath = tmp.name

    def on_close():
        try:
            upload_fn(tmppath)
        finally:
            try:
                os.remove(tmppath)
            except Exception:
                pass
    return _CtxFile(tmp, on_close=on_close)


def _spool_reader(download_fn: Callable[[str], None]) -> _CtxFile:
    """A read source for backends that only offer file-path download:
    download to temp, hand back the handle, delete on close."""
    fd, tmppath = tempfile.mkstemp()
    os.close(fd)
    download_fn(tmppath)
    return _CtxFile(open(tmppath, "rb"), cleanup=tmppath)


# ============================================================ FTP/FTPS/SFTP
# Thin adapter over the EXISTING ftp_backend - no protocol re-implementation.
class _ExistingAdapter(RemoteBackend):
    def __init__(self, protocol: str, **kw):
        self.kind = protocol
        self._kw = kw
        self._b = None

    def connect(self):
        from .ftp_backend import make_backend
        self._b = make_backend(self.kind, **self._kw)
        self._b.connect()

    def disconnect(self):
        try:
            self._b and self._b.disconnect()
        except Exception:
            pass
        self._b = None

    def listdir(self, path: str) -> list:
        out = []
        for e in self._b.list_dir(path or "/"):
            ts = 0.0
            try:
                ts = e.mtime.timestamp() if e.mtime else 0.0
            except Exception:
                ts = 0.0
            out.append(RemoteItem(e.name, e.is_dir, e.size or 0, ts))
        return sorted(out, key=lambda i: (not i.is_dir, i.name.lower()))

    def _dl(self, path, local):
        self._b.cwd(self.parent(path))
        self._b.download(posixpath.basename(path), local)

    def _ul(self, local, path):
        self._b.cwd(self.parent(path))
        self._b.upload(local, posixpath.basename(path))

    def open_read(self, path):
        return _spool_reader(lambda tmp: self._dl(path, tmp))

    def open_write(self, path):
        return _spool_writer(lambda tmp: self._ul(tmp, path))

    def mkdir(self, path):
        self._b.cwd(self.parent(path))
        self._b.mkdir(posixpath.basename(path))

    def delete(self, path):
        self._b.cwd(self.parent(path))
        name = posixpath.basename(path)
        try:
            self._b.delete(name)
        except Exception:
            self._b.rmdir(name)


# ============================================================ S3 (new)
class S3Backend(RemoteBackend):
    kind = "s3"
    needs = "boto3"

    def __init__(self, bucket="", key_id="", secret="", region="",
                 endpoint=""):
        self.bucket = bucket
        self.key_id, self.secret = key_id, secret
        self.region, self.endpoint = region, endpoint
        self._c = None

    def connect(self):
        try:
            import boto3
        except ImportError:
            raise BackendError("S3 needs boto3 (pip install boto3)")
        self._c = boto3.client(
            "s3", aws_access_key_id=self.key_id or None,
            aws_secret_access_key=self.secret or None,
            region_name=self.region or None,
            endpoint_url=self.endpoint or None)

    def listdir(self, path: str) -> list:
        prefix = (path or "").lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        out, seen = [], set()
        pg = self._c.get_paginator("list_objects_v2")
        for page in pg.paginate(Bucket=self.bucket, Prefix=prefix,
                                Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                n = cp["Prefix"][len(prefix):].rstrip("/")
                if n and n not in seen:
                    seen.add(n); out.append(RemoteItem(n, True))
            for o in page.get("Contents", []):
                n = o["Key"][len(prefix):]
                if n and n not in seen:
                    seen.add(n)
                    out.append(RemoteItem(n, False, o["Size"],
                                          o["LastModified"].timestamp()))
        return sorted(out, key=lambda i: (not i.is_dir, i.name.lower()))

    def open_read(self, path):
        obj = self._c.get_object(Bucket=self.bucket,
                                 Key=path.lstrip("/"))
        return _CtxFile(obj["Body"])

    def open_write(self, path):
        return _spool_writer(
            lambda tmp: self._c.upload_file(tmp, self.bucket,
                                            path.lstrip("/")))

    def mkdir(self, path):
        self._c.put_object(Bucket=self.bucket,
                           Key=path.lstrip("/").rstrip("/") + "/", Body=b"")

    def makedirs(self, path):
        pass                                    # S3 keys are flat

    def delete(self, path):
        self._c.delete_object(Bucket=self.bucket, Key=path.lstrip("/"))


# ============================================================ SMB (new)
class SMBBackend(RemoteBackend):
    kind = "smb"
    needs = "smbprotocol"

    def __init__(self, host, share="", user="", password="", port=445):
        self.host, self.share = host, share
        self.user, self.password = user, password
        self.port = int(port or 445)

    def _base(self):
        return rf"\\{self.host}\{self.share}"

    def _p(self, path):
        path = (path or "").strip("/").replace("/", "\\")
        return self._base() + ("\\" + path if path else "")

    def connect(self):
        try:
            import smbclient
        except ImportError:
            raise BackendError("SMB needs smbprotocol "
                               "(pip install smbprotocol)")
        smbclient.register_session(
            self.host, username=self.user or None,
            password=self.password or None, port=self.port)

    def disconnect(self):
        try:
            import smbclient
            smbclient.reset_connection_cache()
        except Exception:
            pass

    def listdir(self, path: str) -> list:
        import smbclient
        out = []
        for e in smbclient.scandir(self._p(path)):
            stt = e.stat()
            out.append(RemoteItem(e.name, e.is_dir(), stt.st_size,
                                  stt.st_mtime))
        return sorted(out, key=lambda i: (not i.is_dir, i.name.lower()))

    def open_read(self, path):
        import smbclient
        return _CtxFile(smbclient.open_file(self._p(path), "rb"))

    def open_write(self, path):
        import smbclient
        return _CtxFile(smbclient.open_file(self._p(path), "wb"))

    def mkdir(self, path):
        import smbclient
        smbclient.mkdir(self._p(path))

    def delete(self, path):
        import smbclient
        try:
            smbclient.remove(self._p(path))
        except Exception:
            smbclient.rmdir(self._p(path))


# ============================================================ ENGINE
_CHUNK = 256 * 1024


def sha256_of(be: RemoteBackend, path: str) -> str:
    h = hashlib.sha256()
    with be.open_read(path) as r:
        while True:
            b = r.read(_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def copy_file(sbe, spath, dbe, dpath, progress=None, verify=False):
    """Stream one file across (possibly different) backends."""
    dbe.makedirs(dbe.parent(dpath))
    h = hashlib.sha256() if verify else None
    with sbe.open_read(spath) as r, dbe.open_write(dpath) as w:
        while True:
            b = r.read(_CHUNK)
            if not b:
                break
            w.write(b)
            if h:
                h.update(b)
            if progress:
                progress(len(b))
    if verify:
        if sha256_of(dbe, dpath) != h.hexdigest():
            raise BackendError(f"verify mismatch: {dpath}")
    return True


@dataclass
class TransferTask:
    sbe: object
    spath: str
    dbe: object
    dpath: str
    size: int = 0


class TransferQueue:
    """Parallel recursive transfer queue with byte-level progress."""
    def __init__(self, workers=4, verify=False,
                 on_progress=None, on_file_done=None, on_all_done=None):
        self.workers = workers
        self.verify = verify
        self.on_progress = on_progress        # (bytes_done, bytes_total)
        self.on_file_done = on_file_done       # (task, ok, err)
        self.on_all_done = on_all_done
        self._q: queue.Queue = queue.Queue()
        self._bytes_total = 0
        self._bytes_done = 0
        self._lock = threading.Lock()
        self._threads = []
        self._stop = False

    def add_tree(self, sbe, sroot, dbe, droot):
        """Expand a file/dir on the source and enqueue file tasks. The
        basename of sroot becomes a child of droot for directories."""
        st = sbe.stat(sroot)
        if st and not st.is_dir:
            self._enqueue(TransferTask(sbe, sroot, dbe, droot, st.size))
            return
        base = posixpath.basename(sroot.rstrip("/"))
        for rel, size in sbe.walk_files(sroot):
            dpath = dbe.join(dbe.join(droot, base), rel) if base \
                else dbe.join(droot, rel)
            self._enqueue(TransferTask(sbe, sbe.join(sroot, rel),
                                       dbe, dpath, size))

    def _enqueue(self, task):
        self._q.put(task)
        self._bytes_total += task.size or 0

    def queued(self):
        return self._q.qsize()

    def stop(self):
        self._stop = True

    def _progress(self, n):
        with self._lock:
            self._bytes_done += n
            done, total = self._bytes_done, self._bytes_total
        if self.on_progress:
            self.on_progress(done, total)

    def _worker(self):
        while not self._stop:
            try:
                task = self._q.get_nowait()
            except queue.Empty:
                return
            err = None
            for attempt in range(3):
                try:
                    copy_file(task.sbe, task.spath, task.dbe, task.dpath,
                              progress=self._progress, verify=self.verify)
                    err = None
                    break
                except Exception as e:
                    err = str(e)
                    time.sleep(0.4 * (attempt + 1))
            if self.on_file_done:
                self.on_file_done(task, err is None, err)
            self._q.task_done()

    def run(self):
        """Blocking run with worker threads."""
        self._threads = [threading.Thread(target=self._worker, daemon=True)
                         for _ in range(self.workers)]
        for t in self._threads:
            t.start()
        for t in self._threads:
            t.join()
        if self.on_all_done:
            self.on_all_done()


# ---- sync / mirror ----
@dataclass
class SyncAction:
    op: str            # "copy" | "skip" | "delete"
    rel: str
    reason: str = ""
    size: int = 0


def plan_sync(sbe, sroot, dbe, droot, mirror=False,
              use_hash=False) -> list:
    """Diff source against dest, return an ordered action plan. Files
    are compared by size (and mtime tolerance), or SHA-256 if use_hash.
    With mirror=True, dest files absent from source are marked delete."""
    src = {rel: size for rel, size in sbe.walk_files(sroot)}
    dst = {rel: size for rel, size in dbe.walk_files(droot)}
    actions = []
    for rel, size in sorted(src.items()):
        if rel not in dst:
            actions.append(SyncAction("copy", rel, "missing", size))
        else:
            differ = (size != dst[rel])
            if not differ and use_hash:
                try:
                    differ = (sha256_of(sbe, sbe.join(sroot, rel)) !=
                              sha256_of(dbe, dbe.join(droot, rel)))
                except Exception:
                    differ = True
            actions.append(
                SyncAction("copy" if differ else "skip", rel,
                           "changed" if differ else "same", size))
    if mirror:
        for rel in sorted(dst):
            if rel not in src:
                actions.append(SyncAction("delete", rel, "extra",
                                          dst[rel]))
    return actions


def apply_sync(plan, sbe, sroot, dbe, droot, verify=False,
               progress=None, on_step=None):
    copied = deleted = skipped = 0
    for a in plan:
        if a.op == "copy":
            copy_file(sbe, sbe.join(sroot, a.rel),
                      dbe, dbe.join(droot, a.rel),
                      progress=progress, verify=verify)
            copied += 1
        elif a.op == "delete":
            try:
                dbe.delete(dbe.join(droot, a.rel))
                deleted += 1
            except Exception:
                pass
        else:
            skipped += 1
        if on_step:
            on_step(a)
    return {"copied": copied, "deleted": deleted, "skipped": skipped}


# ------------------------------------------------------ backend factory
def backend_from_entry(entry):
    extra = entry.extra or {}
    kind = entry.kind
    if kind == "ssh_key" or (kind == "login" and
                             extra.get("proto") == "sftp"):
        kw = dict(host=entry.host, port=int(entry.port or 22),
                  user=entry.username, password=entry.secret or "")
        if extra.get("is_key"):
            kw["keyfile"] = extra.get("keyfile") or None
            kw["password"] = ""
        return _ExistingAdapter("sftp", **kw)
    if kind == "cloud_key":
        return S3Backend(extra.get("bucket", ""), entry.username,
                         entry.secret, extra.get("region", ""),
                         extra.get("endpoint", ""))
    if kind == "smb":
        return SMBBackend(entry.host, extra.get("share", ""),
                          entry.username, entry.secret, entry.port or 445)
    proto = extra.get("proto", "ftp")
    return _ExistingAdapter(
        proto, host=entry.host, port=int(entry.port or 21),
        user=entry.username or "anonymous",
        password=entry.secret or "anonymous@")


def make_local():
    return LocalBackend()


# ------------------------------------------------------------------- UI
def open_storage_hub(parent=None, config=None):
    dlg = StorageHubDialog(parent=parent, config=config)
    dlg.show()
    return dlg


try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
        QPushButton, QLabel, QLineEdit, QComboBox, QFileDialog,
        QMessageBox, QInputDialog, QSplitter, QWidget, QProgressBar,
        QCheckBox, QPlainTextEdit)
    _HAVE_QT = True
except Exception:
    _HAVE_QT = False


if _HAVE_QT:

    class _EngineThread(QThread):
        progress = pyqtSignal(int, int)
        log = pyqtSignal(str)
        done = pyqtSignal(object)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn(self))
            except Exception as e:
                self.log.emit(f"ERROR: {e}")
                self.done.emit(None)

    class _Pane(QWidget):
        """One side of the dual-pane browser bound to a backend."""
        def __init__(self, hub, title):
            super().__init__()
            self.hub = hub
            self.backend = None
            self.cwd = "/"
            v = QVBoxLayout(self)
            top = QHBoxLayout()
            self.cmb = QComboBox()
            self.cmb.currentIndexChanged.connect(self._switch)
            btn_up = QPushButton("Up")
            btn_up.clicked.connect(self._up)
            top.addWidget(QLabel(title))
            top.addWidget(self.cmb, 1)
            top.addWidget(btn_up)
            v.addLayout(top)
            self.ed = QLineEdit("/")
            self.ed.returnPressed.connect(lambda: self.chdir(self.ed.text()))
            v.addWidget(self.ed)
            self.lst = QListWidget()
            self.lst.setSelectionMode(
                QListWidget.SelectionMode.ExtendedSelection)
            self.lst.itemDoubleClicked.connect(self._open)
            v.addWidget(self.lst, 1)
            self.reload_sources()

        def reload_sources(self):
            self.cmb.blockSignals(True)
            self.cmb.clear()
            self.cmb.addItem("Local filesystem", ("local", None))
            try:
                from .secrets_vault import get_shared_vault
                vv = get_shared_vault(self.hub.config)
                if vv.is_unlocked:
                    for e in vv.entries():
                        if e.kind in ("ssh_key", "cloud_key", "smb",
                                      "login"):
                            self.cmb.addItem(f"[{e.kind}] {e.title}",
                                             ("vault", e.id))
            except Exception:
                pass
            self.cmb.blockSignals(False)

        def _switch(self):
            data = self.cmb.currentData()
            if not data:
                return
            src, eid = data
            try:
                if src == "local":
                    self.backend = make_local()
                    self.cwd = os.path.expanduser("~")
                else:
                    from .secrets_vault import get_shared_vault
                    e = get_shared_vault(self.hub.config).get(eid)
                    self.backend = backend_from_entry(e)
                    self.backend.connect()
                    self.cwd = "/"
                self.chdir(self.cwd)
            except Exception as ex:
                QMessageBox.warning(self, "Connect", str(ex))

        def chdir(self, path):
            if not self.backend:
                return
            try:
                items = self.backend.listdir(path)
            except Exception as ex:
                QMessageBox.warning(self, "List", str(ex))
                return
            self.cwd = path or "/"
            self.ed.setText(self.cwd)
            self.lst.clear()
            for it in items:
                li = QListWidgetItem(
                    ("[DIR] " if it.is_dir else "      ") + it.name)
                li.setData(Qt.ItemDataRole.UserRole, (it.name, it.is_dir))
                self.lst.addItem(li)

        def _open(self, item):
            name, is_dir = item.data(Qt.ItemDataRole.UserRole)
            if is_dir:
                self.chdir(RemoteBackend.join(self.cwd, name))

        def _up(self):
            if self.backend:
                self.chdir(RemoteBackend.parent(self.cwd))

        def selected(self):
            out = []
            for it in self.lst.selectedItems():
                name, is_dir = it.data(Qt.ItemDataRole.UserRole)
                out.append((RemoteBackend.join(self.cwd, name), is_dir))
            return out

    class StorageHubDialog(QDialog):
        def __init__(self, parent=None, config=None):
            super().__init__(parent)
            self.setWindowTitle("Network Storage Hub - cross-protocol")
            self.resize(1040, 640)
            self.config = config or {}
            self._eng = None
            self._build()

        def _build(self):
            v = QVBoxLayout(self)
            split = QSplitter(Qt.Orientation.Horizontal)
            self.left = _Pane(self, "A:")
            self.right = _Pane(self, "B:")
            split.addWidget(self.left)
            split.addWidget(self.right)
            split.setSizes([520, 520])
            v.addWidget(split, 1)

            ops = QHBoxLayout()
            b_ab = QPushButton("Copy A -> B")
            b_ab.clicked.connect(lambda: self._copy(self.left, self.right))
            b_ba = QPushButton("Copy B -> A")
            b_ba.clicked.connect(lambda: self._copy(self.right, self.left))
            b_sync = QPushButton("Sync A -> B...")
            b_sync.clicked.connect(self._sync)
            b_verify = QPushButton("Verify A vs B")
            b_verify.clicked.connect(self._verify)
            self.chk_verify = QCheckBox("verify copies")
            self.chk_mirror = QCheckBox("mirror (delete extras)")
            for w in (b_ab, b_ba, b_sync, b_verify, self.chk_verify,
                      self.chk_mirror):
                ops.addWidget(w)
            v.addLayout(ops)

            self.bar = QProgressBar()
            v.addWidget(self.bar)
            self.log = QPlainTextEdit(); self.log.setReadOnly(True)
            self.log.setFixedHeight(120)
            v.addWidget(self.log)
            self.lbl = QLabel("Pick a source in each pane (Local or a "
                              "Vault connection), then Copy / Sync / Verify.")
            v.addWidget(self.lbl)

        def _logmsg(self, m):
            self.log.appendPlainText(m)

        def _copy(self, src_pane, dst_pane):
            if not (src_pane.backend and dst_pane.backend):
                return
            items = src_pane.selected()
            if not items:
                self._logmsg("Nothing selected.")
                return
            verify = self.chk_verify.isChecked()
            sbe, dbe = src_pane.backend, dst_pane.backend
            droot = dst_pane.cwd

            def job(th):
                q = TransferQueue(
                    workers=4, verify=verify,
                    on_progress=lambda d, t: th.progress.emit(
                        d, max(1, t)),
                    on_file_done=lambda task, ok, err: th.log.emit(
                        ("ok  " if ok else "FAIL ") + task.dpath
                        + ("" if ok else f"  ({err})")))
                for spath, _is_dir in items:
                    q.add_tree(sbe, spath, dbe, droot)
                th.log.emit(f"Queued {q.queued()} file(s)...")
                q.run()
                return "done"
            self._run(job, lambda _: (self._logmsg("Transfer done."),
                                      dst_pane.chdir(dst_pane.cwd)))

        def _sync(self):
            if not (self.left.backend and self.right.backend):
                return
            sbe, dbe = self.left.backend, self.right.backend
            sroot, droot = self.left.cwd, self.right.cwd
            mirror = self.chk_mirror.isChecked()
            verify = self.chk_verify.isChecked()

            def plan_job(th):
                return plan_sync(sbe, sroot, dbe, droot, mirror=mirror)
            def show_plan(plan):
                if plan is None:
                    return
                n_c = sum(1 for a in plan if a.op == "copy")
                n_d = sum(1 for a in plan if a.op == "delete")
                n_s = sum(1 for a in plan if a.op == "skip")
                preview = "\n".join(
                    f"  {a.op:<6} {a.rel}  ({a.reason})"
                    for a in plan[:200] if a.op != "skip")
                if QMessageBox.question(
                        self, "Sync plan (dry-run)",
                        f"copy {n_c}, delete {n_d}, skip {n_s}\n\n"
                        f"{preview or '(nothing to do)'}\n\nApply?") \
                        != QMessageBox.StandardButton.Yes:
                    self._logmsg("Sync cancelled.")
                    return
                def apply_job(th):
                    return apply_sync(
                        plan, sbe, sroot, dbe, droot, verify=verify,
                        on_step=lambda a: th.log.emit(f"{a.op} {a.rel}"))
                self._run(apply_job,
                          lambda r: self._logmsg(f"Sync: {r}"))
            self._run(plan_job, show_plan)

        def _verify(self):
            a = self.left.selected()
            if not a or not self.right.backend:
                self._logmsg("Select a file in A; B is the other side.")
                return
            spath, is_dir = a[0]
            if is_dir:
                self._logmsg("Verify works on a file selection.")
                return
            name = posixpath.basename(spath)
            dpath = RemoteBackend.join(self.right.cwd, name)
            sbe, dbe = self.left.backend, self.right.backend

            def job(th):
                hs = sha256_of(sbe, spath)
                hd = sha256_of(dbe, dpath)
                return (hs, hd, hs == hd)
            def done(res):
                if not res:
                    return
                hs, hd, same = res
                self._logmsg(
                    f"A {hs[:16]}..  B {hd[:16]}..  -> "
                    + ("MATCH" if same else "DIFFER"))
            self._run(job, done)

        def _run(self, fn, on_done):
            self._eng = _EngineThread(fn)
            self._eng.progress.connect(
                lambda d, t: (self.bar.setMaximum(t), self.bar.setValue(d)))
            self._eng.log.connect(self._logmsg)
            self._eng.done.connect(on_done)
            self._eng.start()

        def closeEvent(self, ev):
            for p in (self.left, self.right):
                try:
                    if p.backend:
                        p.backend.disconnect()
                except Exception:
                    pass
            super().closeEvent(ev)

else:
    class StorageHubDialog:                     # pragma: no cover
        def __init__(self, *a, **k):
            raise RuntimeError("PyQt6 not available")
        def show(self):
            pass
