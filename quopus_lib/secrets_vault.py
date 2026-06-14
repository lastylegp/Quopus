# date_time: 2026-06-13 19:45
"""
Secrets / Password Manager  (Quopus premium module #15)
=======================================================

A local, password-protected vault for logins, SSH keys, cloud access
keys, SMB credentials, secure notes and TOTP 2FA seeds.

It is the credential backbone for the other network/cloud modules
(Network Storage Hub, Cloud Dashboard, Container Browser, Network
Scanner): they look up a stored entry instead of asking the user to
re-type secrets every time.

Security model
--------------
* The vault is encrypted with a key derived from the user's MASTER
  PASSWORD via scrypt (N=2^15, r=8, p=1) - NOT the Quopus qpe master
  key. So the vendor cannot read user secrets; only the user's
  password unlocks the vault.
* Payload is AES-256-GCM (authenticated) - tampering is detected.
* The password is never written to disk; the derived key lives only in
  memory while unlocked and is dropped on lock/close.
* Clipboard copies auto-clear after a timeout.

Vault file: CONFIG_DIR/secrets.qvault   (override via config
'secrets_vault_path'). Format:

    MAGIC(8) VER(1) SALT(16) Nlog2(1) R(1) P(1) NONCE(12) CIPHERTEXT...

Public API (for other modules)
------------------------------
    get_shared_vault()            -> Vault singleton
    Vault.is_unlocked
    Vault.unlock(password)        -> bool
    Vault.entries(kind=None)      -> list[Entry]
    Vault.find(title=..., tag=...) -> list[Entry]
    Vault.get(entry_id)           -> Entry | None
    Entry.secret                  -> the password / key / token

TOTP is RFC 6238 (HMAC-SHA1, 30s, 6 digits) - stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import struct
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_MAGIC = b"QVLT1\0\0\0"
_VERSION = 1
_KDF_NLOG2 = 15          # scrypt N = 2**15 = 32768
_KDF_R = 8
_KDF_P = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_KEYLEN = 32


# ---------------------------------------------------------------- crypto
def _derive_key(password: str, salt: bytes,
                nlog2: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=1 << nlog2, r=r, p=p, dklen=_KEYLEN,
        maxmem=128 * (1 << nlog2) * r + (1 << 24))


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def encrypt_vault(data: dict, password: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(password, salt, _KDF_NLOG2, _KDF_R, _KDF_P)
    pt = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ct = _aesgcm(key).encrypt(nonce, pt, _MAGIC)
    return (_MAGIC + bytes([_VERSION]) + salt
            + bytes([_KDF_NLOG2, _KDF_R, _KDF_P]) + nonce + ct)


def decrypt_vault(blob: bytes, password: str) -> dict:
    """Raises ValueError on wrong password / corrupt / bad format."""
    if len(blob) < 8 + 1 + _SALT_LEN + 3 + _NONCE_LEN + 16:
        raise ValueError("vault file too short / corrupt")
    if blob[:8] != _MAGIC:
        raise ValueError("not a Quopus vault file")
    off = 8
    ver = blob[off]; off += 1
    if ver != _VERSION:
        raise ValueError(f"unsupported vault version {ver}")
    salt = blob[off:off + _SALT_LEN]; off += _SALT_LEN
    nlog2, r, p = blob[off], blob[off + 1], blob[off + 2]; off += 3
    nonce = blob[off:off + _NONCE_LEN]; off += _NONCE_LEN
    ct = blob[off:]
    key = _derive_key(password, salt, nlog2, r, p)
    try:
        pt = _aesgcm(key).decrypt(nonce, ct, _MAGIC)
    except Exception:
        raise ValueError("wrong password or corrupted vault")
    return json.loads(pt.decode("utf-8"))


# ------------------------------------------------------------------ TOTP
def totp(secret_b32: str, when: Optional[float] = None,
         period: int = 30, digits: int = 6,
         algo: str = "SHA1") -> str:
    """RFC 6238 TOTP. secret_b32 is a base32 string (spaces ignored)."""
    when = time.time() if when is None else when
    key = base64.b32decode(_pad_b32(secret_b32))
    counter = int(when) // period
    msg = struct.pack(">Q", counter)
    digestmod = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
                 "SHA512": hashlib.sha512}.get(algo.upper(), hashlib.sha1)
    h = hmac.new(key, msg, digestmod).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_remaining(period: int = 30, when: Optional[float] = None) -> int:
    when = time.time() if when is None else when
    return period - int(when) % period


def _pad_b32(s: str) -> bytes:
    s = "".join(s.split()).upper().replace("-", "")
    pad = (-len(s)) % 8
    return (s + "=" * pad).encode("ascii")


# -------------------------------------------------------- password maker
_PW_LOWER = "abcdefghijklmnopqrstuvwxyz"
_PW_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PW_DIGITS = "0123456789"
_PW_SYMBOLS = "!@#$%^&*()-_=+[]{};:,.?/"


def gen_password(length: int = 20, lower=True, upper=True,
                 digits=True, symbols=True,
                 avoid_ambiguous=True) -> str:
    pool = ""
    if lower:   pool += _PW_LOWER
    if upper:   pool += _PW_UPPER
    if digits:  pool += _PW_DIGITS
    if symbols: pool += _PW_SYMBOLS
    if not pool:
        pool = _PW_LOWER + _PW_UPPER + _PW_DIGITS
    if avoid_ambiguous:
        for ch in "Il1O0o":
            pool = pool.replace(ch, "")
    return "".join(_secrets.choice(pool) for _ in range(max(4, length)))


# ----------------------------------------------------------------- model
ENTRY_KINDS = ("login", "ssh_key", "cloud_key", "smb", "note")


@dataclass
class Entry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str = "login"                 # one of ENTRY_KINDS
    title: str = ""
    username: str = ""                  # user / access-key-id / login
    secret: str = ""                    # password / secret-key / private key
    host: str = ""                      # host / endpoint / bucket region
    port: str = ""
    url: str = ""
    totp_secret: str = ""               # base32 2FA seed (optional)
    extra: dict = field(default_factory=dict)  # provider-specific fields
    tags: list = field(default_factory=list)
    notes: str = ""
    created: float = field(default_factory=time.time)
    modified: float = field(default_factory=time.time)

    def touch(self):
        self.modified = time.time()

    def has_totp(self) -> bool:
        return bool(self.totp_secret.strip())

    def current_totp(self) -> str:
        return totp(self.totp_secret) if self.has_totp() else ""


# ----------------------------------------------------------------- Vault
class Vault:
    """In-memory vault, persisted to an encrypted file. Unlock with the
    master password, mutate, save. Thread-affinity: use from the UI
    thread (cheap operations)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict[str, Entry] = {}
        self._password: Optional[str] = None
        self._meta = {"created": time.time()}

    # -- state --------------------------------------------------------
    @property
    def is_unlocked(self) -> bool:
        return self._password is not None

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def lock(self):
        self._password = None
        self._entries = {}

    # -- create / unlock ---------------------------------------------
    def create(self, password: str):
        """Initialise a brand-new empty vault and save it."""
        if not password:
            raise ValueError("master password required")
        self._password = password
        self._entries = {}
        self._meta = {"created": time.time()}
        self.save()

    def unlock(self, password: str) -> bool:
        try:
            blob = self.path.read_bytes()
            data = decrypt_vault(blob, password)
        except FileNotFoundError:
            return False
        except ValueError:
            return False
        self._password = password
        self._meta = data.get("meta", {"created": time.time()})
        self._entries = {}
        for raw in data.get("entries", []):
            e = Entry(**{k: raw.get(k, getattr(Entry(), k))
                         for k in Entry().__dict__})
            self._entries[e.id] = e
        return True

    def change_password(self, new_password: str):
        if not self.is_unlocked:
            raise RuntimeError("vault is locked")
        if not new_password:
            raise ValueError("master password required")
        self._password = new_password
        self.save()

    # -- persistence --------------------------------------------------
    def save(self):
        if not self.is_unlocked:
            raise RuntimeError("vault is locked")
        data = {
            "meta": self._meta,
            "entries": [asdict(e) for e in self._entries.values()],
        }
        blob = encrypt_vault(data, self._password)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)        # atomic

    # -- CRUD ---------------------------------------------------------
    def add(self, entry: Entry) -> Entry:
        entry.touch()
        self._entries[entry.id] = entry
        return entry

    def update(self, entry: Entry):
        entry.touch()
        self._entries[entry.id] = entry

    def delete(self, entry_id: str):
        self._entries.pop(entry_id, None)

    def get(self, entry_id: str) -> Optional[Entry]:
        return self._entries.get(entry_id)

    def entries(self, kind: Optional[str] = None) -> list:
        out = [e for e in self._entries.values()
               if kind is None or e.kind == kind]
        return sorted(out, key=lambda e: (e.kind, e.title.lower()))

    def find(self, title: str = "", tag: str = "",
             host: str = "") -> list:
        res = []
        for e in self.entries():
            if title and title.lower() not in e.title.lower():
                continue
            if tag and tag.lower() not in [t.lower() for t in e.tags]:
                continue
            if host and host.lower() not in e.host.lower():
                continue
            res.append(e)
        return res


# ----------------------------------------------------- shared singleton
_shared: Optional[Vault] = None


def vault_path(config: Optional[dict] = None) -> Path:
    """Resolve the vault file path (config override or default)."""
    if config and config.get("secrets_vault_path"):
        return Path(config["secrets_vault_path"])
    try:
        from .config import CONFIG_DIR
        return CONFIG_DIR / "secrets.qvault"
    except Exception:
        return Path.home() / ".quopus" / "secrets.qvault"


def get_shared_vault(config: Optional[dict] = None) -> Vault:
    """Return a process-wide Vault other modules can consume. They must
    check `.is_unlocked` and prompt via open_vault_dialog() if needed."""
    global _shared
    if _shared is None:
        _shared = Vault(vault_path(config))
    return _shared


# ------------------------------------------------------------------- UI
def open_vault_dialog(parent=None, config=None):
    """Open the Secrets Manager dialog (modal). Returns the Vault."""
    dlg = SecretsVaultDialog(parent=parent, config=config)
    dlg.exec()
    return dlg.vault


try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
        QLineEdit, QPushButton, QLabel, QFormLayout, QComboBox,
        QPlainTextEdit, QMessageBox, QApplication, QWidget, QInputDialog,
        QSplitter)
    _HAVE_QT = True
except Exception:                              # headless / no Qt
    _HAVE_QT = False


if _HAVE_QT:

    class SecretsVaultDialog(QDialog):
        def __init__(self, parent=None, config=None):
            super().__init__(parent)
            self.setWindowTitle("Secrets / Password Manager")
            self.resize(820, 540)
            self.config = config or {}
            self.vault = get_shared_vault(self.config)
            self._current: Optional[Entry] = None
            self._totp_timer = QTimer(self)
            self._totp_timer.timeout.connect(self._tick_totp)
            self._clip_token = 0
            self._build()
            if self.vault.is_unlocked:
                self._show_main()
            else:
                self._show_unlock()

        # -- layout ---------------------------------------------------
        def _build(self):
            self.outer = QVBoxLayout(self)

            # unlock pane
            self.pane_unlock = QWidget()
            ul = QVBoxLayout(self.pane_unlock)
            self.lbl_unlock = QLabel()
            self.ed_pw = QLineEdit()
            self.ed_pw.setEchoMode(QLineEdit.EchoMode.Password)
            self.ed_pw.returnPressed.connect(self._do_unlock)
            self.btn_unlock = QPushButton("Unlock")
            self.btn_unlock.clicked.connect(self._do_unlock)
            ul.addStretch(1)
            ul.addWidget(self.lbl_unlock)
            ul.addWidget(self.ed_pw)
            ul.addWidget(self.btn_unlock)
            ul.addStretch(1)
            self.outer.addWidget(self.pane_unlock)

            # main pane
            self.pane_main = QWidget()
            ml = QVBoxLayout(self.pane_main)
            top = QHBoxLayout()
            self.ed_search = QLineEdit()
            self.ed_search.setPlaceholderText("Search title / host / tag...")
            self.ed_search.textChanged.connect(self._refresh_list)
            self.cmb_kind = QComboBox()
            self.cmb_kind.addItem("all", None)
            for k in ENTRY_KINDS:
                self.cmb_kind.addItem(k, k)
            self.cmb_kind.currentIndexChanged.connect(self._refresh_list)
            btn_add = QPushButton("New")
            btn_add.clicked.connect(self._new_entry)
            btn_del = QPushButton("Delete")
            btn_del.clicked.connect(self._delete_entry)
            btn_lock = QPushButton("Lock")
            btn_lock.clicked.connect(self._lock)
            for w in (self.ed_search, self.cmb_kind, btn_add, btn_del,
                      btn_lock):
                top.addWidget(w)
            ml.addLayout(top)

            split = QSplitter(Qt.Orientation.Horizontal)
            self.lst = QListWidget()
            self.lst.currentItemChanged.connect(self._select)
            split.addWidget(self.lst)

            self.detail = QWidget()
            f = QFormLayout(self.detail)
            self.d_title = QLineEdit()
            self.d_kind = QComboBox()
            for k in ENTRY_KINDS:
                self.d_kind.addItem(k, k)
            self.d_user = QLineEdit()
            self.d_secret = QLineEdit()
            self.d_secret.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_show = QPushButton("show")
            self.btn_show.setCheckable(True)
            self.btn_show.toggled.connect(
                lambda on: self.d_secret.setEchoMode(
                    QLineEdit.EchoMode.Normal if on
                    else QLineEdit.EchoMode.Password))
            self.btn_gen = QPushButton("generate")
            self.btn_gen.clicked.connect(
                lambda: self.d_secret.setText(gen_password()))
            self.btn_copy = QPushButton("copy")
            self.btn_copy.clicked.connect(self._copy_secret)
            secret_row = QHBoxLayout()
            secret_row.addWidget(self.d_secret, 1)
            secret_row.addWidget(self.btn_show)
            secret_row.addWidget(self.btn_gen)
            secret_row.addWidget(self.btn_copy)
            secret_w = QWidget(); secret_w.setLayout(secret_row)
            self.d_host = QLineEdit()
            self.d_port = QLineEdit()
            self.d_url = QLineEdit()
            self.d_totp = QLineEdit()
            self.d_totp.setPlaceholderText("base32 2FA seed (optional)")
            self.lbl_totp = QLabel("-")
            self.d_tags = QLineEdit()
            self.d_tags.setPlaceholderText("comma,separated,tags")
            self.d_notes = QPlainTextEdit()
            self.d_notes.setFixedHeight(80)
            f.addRow("Title", self.d_title)
            f.addRow("Kind", self.d_kind)
            f.addRow("User / Key-ID", self.d_user)
            f.addRow("Secret", secret_w)
            f.addRow("Host / Endpoint", self.d_host)
            f.addRow("Port", self.d_port)
            f.addRow("URL", self.d_url)
            f.addRow("TOTP seed", self.d_totp)
            f.addRow("TOTP now", self.lbl_totp)
            f.addRow("Tags", self.d_tags)
            f.addRow("Notes", self.d_notes)
            self.btn_save = QPushButton("Save entry")
            self.btn_save.clicked.connect(self._save_entry)
            f.addRow(self.btn_save)
            split.addWidget(self.detail)
            split.setSizes([260, 560])
            ml.addWidget(split, 1)
            self.outer.addWidget(self.pane_main)

        # -- unlock flow ---------------------------------------------
        def _show_unlock(self):
            self.pane_main.hide()
            self.pane_unlock.show()
            if self.vault.exists:
                self.lbl_unlock.setText(
                    "Enter the master password to unlock the vault:")
                self.btn_unlock.setText("Unlock")
            else:
                self.lbl_unlock.setText(
                    "No vault yet. Choose a master password to CREATE "
                    "one.\n(Cannot be recovered if you forget it.)")
                self.btn_unlock.setText("Create vault")
            self.ed_pw.setFocus()

        def _do_unlock(self):
            pw = self.ed_pw.text()
            if not pw:
                return
            if not self.vault.exists:
                try:
                    self.vault.create(pw)
                except Exception as e:
                    QMessageBox.warning(self, "Vault", str(e))
                    return
                self._show_main()
                return
            if self.vault.unlock(pw):
                self.ed_pw.clear()
                self._show_main()
            else:
                QMessageBox.warning(
                    self, "Vault", "Wrong password or corrupted vault.")
                self.ed_pw.selectAll()

        def _show_main(self):
            self.pane_unlock.hide()
            self.pane_main.show()
            self._refresh_list()
            self._totp_timer.start(1000)

        def _lock(self):
            self.vault.lock()
            self._current = None
            self._totp_timer.stop()
            self._show_unlock()

        # -- list / select -------------------------------------------
        def _refresh_list(self):
            self.lst.blockSignals(True)
            self.lst.clear()
            q = self.ed_search.text().strip().lower()
            kind = self.cmb_kind.currentData()
            for e in self.vault.entries(kind):
                hay = (e.title + " " + e.host + " "
                       + " ".join(e.tags)).lower()
                if q and q not in hay:
                    continue
                it = QListWidgetItem(f"[{e.kind}]  {e.title or '(no title)'}")
                it.setData(Qt.ItemDataRole.UserRole, e.id)
                self.lst.addItem(it)
            self.lst.blockSignals(False)

        def _select(self, item, _prev=None):
            if not item:
                return
            e = self.vault.get(item.data(Qt.ItemDataRole.UserRole))
            if not e:
                return
            self._current = e
            self.d_title.setText(e.title)
            self.d_kind.setCurrentIndex(
                max(0, list(ENTRY_KINDS).index(e.kind)
                    if e.kind in ENTRY_KINDS else 0))
            self.d_user.setText(e.username)
            self.d_secret.setText(e.secret)
            self.d_host.setText(e.host)
            self.d_port.setText(e.port)
            self.d_url.setText(e.url)
            self.d_totp.setText(e.totp_secret)
            self.d_tags.setText(", ".join(e.tags))
            self.d_notes.setPlainText(e.notes)
            self._tick_totp()

        def _new_entry(self):
            e = Entry(title="New entry")
            self.vault.add(e)
            self._refresh_list()
            for i in range(self.lst.count()):
                if self.lst.item(i).data(
                        Qt.ItemDataRole.UserRole) == e.id:
                    self.lst.setCurrentRow(i)
                    break

        def _save_entry(self):
            e = self._current
            if not e:
                return
            e.title = self.d_title.text().strip()
            e.kind = self.d_kind.currentData()
            e.username = self.d_user.text()
            e.secret = self.d_secret.text()
            e.host = self.d_host.text().strip()
            e.port = self.d_port.text().strip()
            e.url = self.d_url.text().strip()
            e.totp_secret = self.d_totp.text().strip()
            e.tags = [t.strip() for t in self.d_tags.text().split(",")
                      if t.strip()]
            e.notes = self.d_notes.toPlainText()
            self.vault.update(e)
            try:
                self.vault.save()
            except Exception as ex:
                QMessageBox.warning(self, "Vault", f"Save failed: {ex}")
                return
            self._refresh_list()

        def _delete_entry(self):
            e = self._current
            if not e:
                return
            if QMessageBox.question(
                    self, "Delete",
                    f"Delete '{e.title}'?") != \
                    QMessageBox.StandardButton.Yes:
                return
            self.vault.delete(e.id)
            self._current = None
            try:
                self.vault.save()
            except Exception:
                pass
            self._refresh_list()

        # -- totp + clipboard ----------------------------------------
        def _tick_totp(self):
            e = self._current
            if e and e.has_totp():
                try:
                    code = e.current_totp()
                    self.lbl_totp.setText(
                        f"{code}   ({totp_remaining()}s)")
                except Exception:
                    self.lbl_totp.setText("invalid seed")
            else:
                self.lbl_totp.setText("-")

        def _copy_secret(self):
            QApplication.clipboard().setText(self.d_secret.text())
            self._clip_token += 1
            tok = self._clip_token
            # auto-clear after 20s
            QTimer.singleShot(20000, lambda: self._clear_clip(tok))

        def _clear_clip(self, tok):
            if tok == self._clip_token:
                try:
                    QApplication.clipboard().clear()
                except Exception:
                    pass

        def closeEvent(self, ev):
            self._totp_timer.stop()
            super().closeEvent(ev)

else:                                          # headless stub
    class SecretsVaultDialog:                  # pragma: no cover
        def __init__(self, *a, **k):
            raise RuntimeError("PyQt6 not available")
        def exec(self):
            return 0
