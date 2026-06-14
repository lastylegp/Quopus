# date_time: 2026-06-13 19:56
"""
Cloud Storage Dashboard  (Quopus premium module #17)
====================================================

Connect S3 / S3-compatible, Google Cloud Storage or Azure Blob via a
Secrets-vault cloud_key entry and get, per bucket/container:
  - object count + total size, broken down by storage class
  - largest objects, oldest objects
  - "orphans": incomplete multipart uploads and zero-byte folder
    markers (wasted space / cost)
  - a rough MONTHLY cost estimate from a built-in price table

Provider SDKs (boto3 / google-cloud-storage / azure-storage-blob) are
imported lazily; the analytics layer is pure and unit-tested.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ObjInfo:
    key: str
    size: int
    storage_class: str = "STANDARD"
    last_modified: float = 0.0


@dataclass
class BucketStats:
    bucket: str
    count: int = 0
    total_size: int = 0
    by_class: dict = field(default_factory=dict)     # class -> (count,bytes)
    largest: list = field(default_factory=list)      # top ObjInfo
    oldest: list = field(default_factory=list)
    zero_byte_markers: int = 0


# ----- rough monthly storage price per GB (USD), for ESTIMATE only ----
# Sources are list prices and drift; treat as ballpark, not billing.
PRICE_PER_GB_MONTH = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "INTELLIGENT_TIERING": 0.023,
    "GLACIER": 0.0036,
    "GLACIER_IR": 0.004,
    "DEEP_ARCHIVE": 0.00099,
    # GCS
    "NEARLINE": 0.010,
    "COLDLINE": 0.004,
    "ARCHIVE": 0.0012,
    # Azure
    "HOT": 0.0184,
    "COOL": 0.01,
    "COLD": 0.0036,
}
_DEFAULT_PRICE = 0.023


def estimate_monthly_cost(by_class: dict) -> float:
    """by_class: {storage_class: (count, bytes)} -> est USD/month."""
    total = 0.0
    for sc, (_, nbytes) in by_class.items():
        gb = nbytes / (1024 ** 3)
        total += gb * PRICE_PER_GB_MONTH.get(sc.upper(), _DEFAULT_PRICE)
    return round(total, 4)


def aggregate(bucket: str, objects, top_n: int = 10) -> BucketStats:
    """Fold an iterable of ObjInfo into BucketStats. Streaming-friendly:
    keeps only running totals + top-N lists, so it scales to millions
    of objects without holding them all."""
    st = BucketStats(bucket=bucket)
    largest: list = []
    oldest: list = []
    for o in objects:
        st.count += 1
        st.total_size += o.size
        sc = (o.storage_class or "STANDARD").upper()
        c, b = st.by_class.get(sc, (0, 0))
        st.by_class[sc] = (c + 1, b + o.size)
        if o.size == 0 and o.key.endswith("/"):
            st.zero_byte_markers += 1
        # maintain top-N largest
        largest.append(o)
        if len(largest) > top_n * 4:
            largest.sort(key=lambda x: x.size, reverse=True)
            del largest[top_n:]
        oldest.append(o)
        if len(oldest) > top_n * 4:
            oldest.sort(key=lambda x: x.last_modified)
            del oldest[top_n:]
    largest.sort(key=lambda x: x.size, reverse=True)
    oldest.sort(key=lambda x: x.last_modified)
    st.largest = largest[:top_n]
    st.oldest = [o for o in oldest if o.last_modified][:top_n]
    return st


def human(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if f < 1024 or u == "PB":
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} PB"


# --------------------------------------------------------- providers
class CloudProvider:
    name = "base"
    needs = ""

    def list_buckets(self) -> list:
        raise NotImplementedError
    def iter_objects(self, bucket: str):
        raise NotImplementedError
    def incomplete_multipart(self, bucket: str) -> int:
        return 0


class S3Provider(CloudProvider):
    name = "s3"
    needs = "boto3"

    def __init__(self, key_id="", secret="", region="", endpoint=""):
        try:
            import boto3
        except ImportError:
            raise RuntimeError("S3 needs boto3 (pip install boto3)")
        self._c = boto3.client(
            "s3", aws_access_key_id=key_id or None,
            aws_secret_access_key=secret or None,
            region_name=region or None, endpoint_url=endpoint or None)

    def list_buckets(self):
        return [b["Name"] for b in
                self._c.list_buckets().get("Buckets", [])]

    def iter_objects(self, bucket):
        pg = self._c.get_paginator("list_objects_v2")
        for page in pg.paginate(Bucket=bucket):
            for o in page.get("Contents", []):
                yield ObjInfo(o["Key"], o["Size"],
                              o.get("StorageClass", "STANDARD"),
                              o["LastModified"].timestamp())

    def incomplete_multipart(self, bucket):
        try:
            n = 0
            pg = self._c.get_paginator("list_multipart_uploads")
            for page in pg.paginate(Bucket=bucket):
                n += len(page.get("Uploads", []))
            return n
        except Exception:
            return 0


class GCSProvider(CloudProvider):
    name = "gcs"
    needs = "google-cloud-storage"

    def __init__(self, project="", credentials_json=""):
        try:
            from google.cloud import storage
        except ImportError:
            raise RuntimeError(
                "GCS needs google-cloud-storage "
                "(pip install google-cloud-storage)")
        if credentials_json:
            import json
            from google.oauth2 import service_account
            info = json.loads(credentials_json)
            creds = service_account.Credentials.from_service_account_info(
                info)
            self._c = storage.Client(project=project or info.get(
                "project_id"), credentials=creds)
        else:
            self._c = storage.Client(project=project or None)

    def list_buckets(self):
        return [b.name for b in self._c.list_buckets()]

    def iter_objects(self, bucket):
        for blob in self._c.list_blobs(bucket):
            yield ObjInfo(blob.name, blob.size or 0,
                          (blob.storage_class or "STANDARD"),
                          blob.updated.timestamp() if blob.updated else 0)


class AzureProvider(CloudProvider):
    name = "azure"
    needs = "azure-storage-blob"

    def __init__(self, connection_string=""):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise RuntimeError(
                "Azure needs azure-storage-blob "
                "(pip install azure-storage-blob)")
        self._svc = BlobServiceClient.from_connection_string(
            connection_string)

    def list_buckets(self):
        return [c.name for c in self._svc.list_containers()]

    def iter_objects(self, bucket):
        cont = self._svc.get_container_client(bucket)
        for b in cont.list_blobs():
            yield ObjInfo(b.name, b.size or 0,
                          (getattr(b, "blob_tier", None) or "HOT"),
                          b.last_modified.timestamp()
                          if b.last_modified else 0)


def provider_from_entry(entry):
    """Build a CloudProvider from a vault cloud_key Entry. The provider
    is chosen by extra['provider'] (s3|gcs|azure), default s3."""
    extra = entry.extra or {}
    prov = (extra.get("provider") or "s3").lower()
    if prov == "gcs":
        return GCSProvider(extra.get("project", ""), entry.secret)
    if prov == "azure":
        return AzureProvider(entry.secret)
    return S3Provider(entry.username, entry.secret,
                      extra.get("region", ""), extra.get("endpoint", ""))


# ------------------------------------------------------------------- UI
def open_cloud_dashboard(parent=None, config=None):
    dlg = CloudDashboardDialog(parent=parent, config=config)
    dlg.show()
    return dlg


try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
        QPushButton, QLabel, QPlainTextEdit, QMessageBox, QSplitter,
        QWidget)
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

    class CloudDashboardDialog(QDialog):
        def __init__(self, parent=None, config=None):
            super().__init__(parent)
            self.setWindowTitle("Cloud Storage Dashboard")
            self.resize(880, 580)
            self.config = config or {}
            self.provider: Optional[CloudProvider] = None
            self._job = None
            self._build()
            self._load_accounts()

        def _build(self):
            v = QVBoxLayout(self)
            split = QSplitter(Qt.Orientation.Horizontal)
            left = QWidget(); ll = QVBoxLayout(left)
            ll.addWidget(QLabel("Cloud accounts (Vault cloud_key):"))
            self.lst_acc = QListWidget()
            self.lst_acc.itemDoubleClicked.connect(self._connect)
            ll.addWidget(self.lst_acc, 1)
            ll.addWidget(QLabel("Buckets:"))
            self.lst_buckets = QListWidget()
            self.lst_buckets.itemDoubleClicked.connect(self._analyze)
            ll.addWidget(self.lst_buckets, 2)
            split.addWidget(left)
            self.out = QPlainTextEdit()
            self.out.setReadOnly(True)
            split.addWidget(self.out)
            split.setSizes([280, 600])
            v.addWidget(split, 1)
            self.lbl = QLabel("Pick a cloud account.")
            v.addWidget(self.lbl)

        def _load_accounts(self):
            self.lst_acc.clear()
            try:
                from .secrets_vault import get_shared_vault
                vv = get_shared_vault(self.config)
                if not vv.is_unlocked:
                    self.lst_acc.addItem("(unlock the Vault first)")
                    return
                for e in vv.entries("cloud_key"):
                    it = QListWidgetItem(e.title)
                    it.setData(Qt.ItemDataRole.UserRole, e.id)
                    self.lst_acc.addItem(it)
            except Exception as e:
                self.lst_acc.addItem(f"(vault error: {e})")

        def _connect(self, *a):
            it = self.lst_acc.currentItem()
            if not it:
                return
            eid = it.data(Qt.ItemDataRole.UserRole)
            if not eid:
                from .secrets_vault import open_vault_dialog
                open_vault_dialog(self, self.config)
                self._load_accounts()
                return
            from .secrets_vault import get_shared_vault
            e = get_shared_vault(self.config).get(eid)
            self.lbl.setText("Connecting...")
            self._run(lambda: provider_from_entry(e), self._on_provider)

        def _on_provider(self, prov):
            self.provider = prov
            self._run(prov.list_buckets, self._on_buckets)

        def _on_buckets(self, names):
            self.lst_buckets.clear()
            for n in names:
                self.lst_buckets.addItem(n)
            self.lbl.setText(f"{len(names)} bucket(s). Double-click to "
                             f"analyze.")

        def _analyze(self, item):
            bucket = item.text()
            self.lbl.setText(f"Analyzing {bucket}... (streaming objects)")

            def job():
                st = aggregate(bucket, self.provider.iter_objects(bucket))
                mp = self.provider.incomplete_multipart(bucket)
                return st, mp
            self._run(job, self._show_stats)

        def _show_stats(self, payload):
            st, mp = payload
            lines = [f"Bucket: {st.bucket}",
                     f"Objects: {st.count:,}",
                     f"Total size: {human(st.total_size)}",
                     ""]
            lines.append("By storage class:")
            for sc, (c, b) in sorted(st.by_class.items()):
                lines.append(f"  {sc:<20} {c:>8,}  {human(b)}")
            cost = estimate_monthly_cost(st.by_class)
            lines += ["",
                      f"Estimated cost: ~${cost:.2f}/month "
                      f"(list-price ballpark)",
                      "",
                      f"Orphans / waste:",
                      f"  incomplete multipart uploads: {mp}",
                      f"  zero-byte folder markers:     "
                      f"{st.zero_byte_markers}",
                      "",
                      "Largest objects:"]
            for o in st.largest[:8]:
                lines.append(f"  {human(o.size):>10}  {o.key}")
            if st.oldest:
                lines += ["", "Oldest objects:"]
                for o in st.oldest[:6]:
                    ts = time.strftime("%Y-%m-%d",
                                       time.localtime(o.last_modified))
                    lines.append(f"  {ts}  {o.key}")
            self.out.setPlainText("\n".join(lines))
            self.lbl.setText("Done.")

        def _run(self, fn, on_ok):
            self._job = _Job(fn)
            self._job.ok.connect(on_ok)
            self._job.fail.connect(
                lambda m: (self.lbl.setText("Error"),
                           QMessageBox.warning(self, "Cloud", m)))
            self._job.start()

else:
    class CloudDashboardDialog:                 # pragma: no cover
        def __init__(self, *a, **k):
            raise RuntimeError("PyQt6 not available")
        def show(self):
            pass
