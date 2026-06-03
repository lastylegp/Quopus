# date_time: 2026-06-03 19:48
"""Telegram client for Quopus Commander (MTProto / Telethon).

A full Telegram *user* client embedded as a PyQt6 dialog: it logs
in with your phone number (not a bot), lists your chats, reads and
sends messages, downloads incoming media, and can send files - in
particular the files you've tagged in a Quopus lister.

Why Telethon / MTProto
======================
Telegram has two APIs. The Bot API is HTTP-only and trivial, but a
bot can't see your normal chats - only messages addressed to it. To
read and write *your* conversations we need the MTProto client API,
the same protocol the official apps use. Telethon is the mature
Python implementation. It needs:

  * an api_id / api_hash, registered (free) at https://my.telegram.org
    -> "API development tools". One pair is fine for personal use.
  * a login with your phone number; Telegram sends a code (and, if
    you have 2FA on, also asks for your cloud password).

After the first login Telethon stores an encrypted *session* file
so you don't have to re-enter the code every time.

Threading model
===============
Telethon is asyncio-based; Qt has its own event loop. Mixing them
in one thread deadlocks. So the whole Telethon client lives on a
dedicated worker thread (`_TgWorker`) running its own asyncio loop.
The UI talks to it by scheduling coroutines onto that loop
(`run_coroutine_threadsafe`) and receives results / events back via
Qt signals (which are thread-safe across the thread boundary). The
UI thread never touches the Telethon client directly.

Files written
=============
  <quopus>/config/telegram.cfg          api_id / api_hash / phone
  <quopus>/config/telegram.session       Telethon session (secret!)
  downloads land in the user's chosen folder (default: the active
  lister's directory, passed in by the caller).

SECURITY NOTE: telegram.session grants full access to your Telegram
account - treat it like a password. It must never go on GitHub.
telegram.cfg holds your api_id/api_hash, also keep it private.
"""
from __future__ import annotations

import asyncio
import json
import threading
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Any, Dict

from PyQt6.QtCore import QObject, pyqtSignal, QThread


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
def _log(msg: str):
    """Append a line to config/telegram_debug.log. Off by default;
    set the environment variable QUOPUS_TG_DEBUG=1 to enable it
    when diagnosing chat/loading issues. Logs only harmless
    diagnostics (ids, counts) - never credentials or message text."""
    import os
    if not os.environ.get("QUOPUS_TG_DEBUG"):
        return
    try:
        from .config import CONFIG_DIR
        import time as _t
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "telegram_debug.log", "a",
                  encoding="utf-8") as f:
            f.write("%s  %s\n"
                    % (_t.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _config_path() -> Path:
    from .config import CONFIG_DIR
    return CONFIG_DIR / "telegram.cfg"


def _session_path() -> Path:
    from .config import CONFIG_DIR
    # Telethon appends ".session" itself, so pass the stem.
    return CONFIG_DIR / "telegram"


def load_tg_config() -> dict:
    """Read telegram.cfg (key = value lines). Returns {} if absent."""
    p = _config_path()
    cfg: dict = {}
    if not p.is_file():
        return cfg
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return cfg


def save_tg_config(api_id: str, api_hash: str, phone: str) -> None:
    from .config import CONFIG_DIR
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = _config_path()
    body = (
        "# Quopus Telegram client configuration.\n"
        "# Get api_id / api_hash from https://my.telegram.org\n"
        "#   -> 'API development tools'. This file is PRIVATE -\n"
        "# do not commit it to GitHub.\n"
        f"api_id = {api_id.strip()}\n"
        f"api_hash = {api_hash.strip()}\n"
        f"phone = {phone.strip()}\n"
    )
    p.write_text(body, encoding="utf-8")


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# Lightweight data carriers passed UI <-> worker (plain, picklable-
# ish dicts/dataclasses so nothing Telethon-specific leaks into the
# UI thread).
# ------------------------------------------------------------------
@dataclass
class TgDialog:
    """A chat in the dialog (conversation) list."""
    id: int
    name: str
    is_user: bool
    is_group: bool
    is_channel: bool
    unread: int


@dataclass
class TgMessage:
    id: int
    chat_id: int
    sender: str
    text: str
    timestamp: float
    out: bool                  # True if we sent it
    media: bool                # has a downloadable attachment
    media_kind: str = ""       # 'photo' | 'document' | 'video' | ''
    filename: str = ""
    thumb_b64: str = ""        # tiny inline preview (data URI payload)


# ------------------------------------------------------------------
# Worker thread: owns the asyncio loop + Telethon client
# ------------------------------------------------------------------
class _TgWorker(QObject):
    """Runs Telethon on its own thread/loop. All public methods are
    called from the UI thread and schedule work onto the worker
    loop; results come back through signals."""

    # Connection / auth lifecycle
    sig_status = pyqtSignal(str)              # human-readable status
    sig_error = pyqtSignal(str)               # error text
    sig_need_code = pyqtSignal()              # SMS code required
    sig_need_password = pyqtSignal()          # 2FA cloud password
    sig_authorized = pyqtSignal(str)          # logged in; arg = own name

    # Data
    sig_dialogs = pyqtSignal(list)            # list[TgDialog]
    sig_messages = pyqtSignal('qlonglong', 'qlonglong', list)
    # ^ chat_id, before_id (0 for initial load, else the offset_id
    #   that was requested - so the UI can prepend instead of
    #   replace), list[TgMessage]
    sig_new_message = pyqtSignal(object)      # TgMessage (live update)
    sig_sent = pyqtSignal('qlonglong')        # chat_id (message sent ok)
    sig_download_done = pyqtSignal(str, bool)  # filepath, open_after
    sig_upload_done = pyqtSignal('qlonglong', str)  # chat_id, filename

    def __init__(self, api_id: int, api_hash: str, phone: str):
        super().__init__()
        self._api_id = api_id
        self._api_hash = api_hash
        self._phone = phone
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        # Cache of dialog-id -> Telethon entity, filled in
        # _fetch_dialogs. iter_messages needs a resolvable entity,
        # and a raw integer id isn't always in Telethon's cache
        # (especially right after login), which is why old history
        # sometimes came back empty. We keep the real entities here
        # and hand them to iter_messages.
        self._entities: dict = {}
        self._thread = threading.Thread(
            target=self._run_loop, name="TelegramWorker",
            daemon=True)
        self._ready = threading.Event()
        # Futures the login flow waits on for user-supplied values.
        self._code_future: Optional[Future] = None
        self._password_future: Optional[Future] = None

    # -- thread / loop plumbing ----------------------------------
    def start(self):
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _submit(self, coro) -> Future:
        """Schedule a coroutine on the worker loop from any thread."""
        if self._loop is None:
            f: Future = Future()
            f.set_exception(RuntimeError("worker loop not running"))
            return f
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self):
        """Disconnect and stop the loop cleanly."""
        async def _shutdown():
            try:
                if self._client is not None:
                    await self._client.disconnect()
            except Exception:
                pass
        try:
            if self._loop and self._loop.is_running():
                fut = self._submit(_shutdown())
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # -- login ----------------------------------------------------
    def connect_and_login(self):
        """Kick off connection + sign-in on the worker loop."""
        self._submit(self._do_login())

    async def _do_login(self):
        try:
            from telethon import TelegramClient, events
            from telethon.errors import SessionPasswordNeededError
            self.sig_status.emit("Connecting to Telegram...")
            self._client = TelegramClient(
                str(_session_path()), self._api_id, self._api_hash)
            await self._client.connect()

            if not await self._client.is_user_authorized():
                self.sig_status.emit(
                    "Sending login code to %s..." % self._phone)
                await self._client.send_code_request(self._phone)
                # Wait for the UI to supply the code.
                self._code_future = Future()
                self.sig_need_code.emit()
                code = await asyncio.get_event_loop().run_in_executor(
                    None, self._code_future.result)
                try:
                    await self._client.sign_in(
                        phone=self._phone, code=code)
                except SessionPasswordNeededError:
                    # 2FA cloud password set on the account.
                    self._password_future = Future()
                    self.sig_need_password.emit()
                    pw = await asyncio.get_event_loop()\
                        .run_in_executor(
                            None, self._password_future.result)
                    await self._client.sign_in(password=pw)

            me = await self._client.get_me()
            name = self._display_name_of(me)
            # Live updates for incoming messages.
            self._client.add_event_handler(
                self._on_new_message, events.NewMessage())
            self.sig_authorized.emit(name)
            self.sig_status.emit("Connected as %s" % name)
        except Exception as e:
            self.sig_error.emit("Login failed: %s" % e)
            self.sig_status.emit("Disconnected")
            traceback.print_exc()

    def provide_code(self, code: str):
        if self._code_future is not None and \
                not self._code_future.done():
            self._code_future.set_result(code.strip())

    def provide_password(self, pw: str):
        if self._password_future is not None and \
                not self._password_future.done():
            self._password_future.set_result(pw)

    # -- helpers --------------------------------------------------
    @staticmethod
    def _display_name_of(entity) -> str:
        if entity is None:
            return "?"
        title = getattr(entity, "title", None)
        if title:
            return title
        first = getattr(entity, "first_name", "") or ""
        last = getattr(entity, "last_name", "") or ""
        name = (first + " " + last).strip()
        if name:
            return name
        uname = getattr(entity, "username", None)
        return uname or str(getattr(entity, "id", "?"))

    # -- dialogs / messages --------------------------------------
    def fetch_dialogs(self, limit: int = 100):
        self._submit(self._fetch_dialogs(limit))

    async def _fetch_dialogs(self, limit: int):
        try:
            out = []
            async for d in self._client.iter_dialogs(limit=limit):
                ent = d.entity
                # Remember the entity so fetch_messages can resolve
                # the chat reliably (raw ids alone may not be in
                # Telethon's cache yet).
                try:
                    self._entities[d.id] = ent
                except Exception:
                    pass
                out.append(TgDialog(
                    id=d.id,
                    name=self._display_name_of(ent) or d.name or "?",
                    is_user=bool(d.is_user),
                    is_group=bool(d.is_group),
                    is_channel=bool(d.is_channel),
                    unread=int(getattr(d, "unread_count", 0) or 0),
                ))
            self.sig_dialogs.emit(out)
        except Exception as e:
            self.sig_error.emit("Couldn't load chats: %s" % e)

    def fetch_messages(self, chat_id: int, limit: int = 100,
                       before_id: int = 0):
        """Load messages from chat_id. If before_id is 0, return
        the newest `limit` messages; otherwise return up to `limit`
        messages older than before_id (used for "load more")."""
        _log("fetch_messages requested chat_id=%r limit=%d "
             "before_id=%d" % (chat_id, limit, before_id))
        fut = self._submit(
            self._fetch_messages(chat_id, limit, before_id))

        def _done(f):
            exc = f.exception()
            if exc is not None:
                _log("fetch_messages coroutine raised: %r" % exc)
                self.sig_error.emit(
                    "Couldn't load messages (internal): %s" % exc)
        try:
            fut.add_done_callback(_done)
        except Exception:
            pass

    async def _fetch_messages(self, chat_id: int, limit: int,
                              before_id: int = 0):
        # Resolve the chat to a usable entity. Telethon's
        # iter_messages needs an entity it can turn into an input
        # peer; a bare integer id only works if that id is already
        # in its cache. We try, in order:
        #   1. the entity cached from iter_dialogs (best)
        #   2. get_entity(id)        - works for most chats
        #   3. get_input_entity(id)  - often resolves channels/
        #      supergroups that get_entity can't from a bare id
        #   4. the raw id            - last resort
        target = self._entities.get(chat_id)
        _log("_fetch_messages chat_id=%r cached_entity=%s"
             % (chat_id, target is not None))
        why = []
        if target is None:
            try:
                target = await self._client.get_entity(chat_id)
                self._entities[chat_id] = target
                _log("  get_entity ok: %r" % type(target).__name__)
            except Exception as e:
                why.append("get_entity: %s" % e)
                _log("  get_entity FAILED: %s" % e)
                try:
                    target = await self._client.get_input_entity(
                        chat_id)
                    self._entities[chat_id] = target
                    _log("  get_input_entity ok")
                except Exception as e2:
                    why.append("get_input_entity: %s" % e2)
                    _log("  get_input_entity FAILED: %s" % e2)
                    target = chat_id
        try:
            msgs = []
            # offset_id=0 means "from the most recent"; any other
            # value returns messages strictly older than that id.
            iter_kwargs = {"limit": limit}
            if before_id:
                iter_kwargs["offset_id"] = before_id
            async for m in self._client.iter_messages(
                    target, **iter_kwargs):
                try:
                    msgs.append(await self._to_tgmessage(m, chat_id))
                except Exception as me:
                    # A single bad message must not kill the whole
                    # load - log it and skip.
                    _log("  skipping message %r: %s"
                         % (getattr(m, "id", "?"), me))
            msgs.reverse()  # oldest first for display
            _log("  iter_messages produced %d messages "
                 "(before_id=%d)" % (len(msgs), before_id))
            self.sig_messages.emit(chat_id, before_id, msgs)
        except Exception as e:
            detail = "; ".join(why + ["iter_messages: %s" % e]) \
                if why else ("iter_messages: %s" % e)
            _log("  iter_messages FAILED: %s" % e)
            self.sig_error.emit(
                "Couldn't load messages for this chat. %s" % detail)

    async def _to_tgmessage(self, m, chat_id: int) -> TgMessage:
        sender = "?"
        try:
            s = await m.get_sender()
            sender = self._display_name_of(s)
        except Exception:
            pass
        kind, fname = "", ""
        thumb_b64 = ""
        if m.media is not None:
            if m.photo is not None:
                kind = "photo"
            elif m.video is not None:
                kind = "video"
            elif m.document is not None:
                kind = "document"
                try:
                    fname = m.file.name or ""
                except Exception:
                    fname = ""
            # Grab an inline preview for things that have one
            # (photos, videos, and documents with embedded thumbs).
            # We download a thumbnail, not the full media, so the
            # chat stays responsive - but the *largest* available
            # thumbnail (thumb=-1), since the smallest is far too
            # low-res to display at ~400px. Encoded as base64 for
            # embedding straight into the message HTML.
            if kind in ("photo", "video", "document"):
                try:
                    import io as _io
                    import base64 as _b64
                    buf = _io.BytesIO()
                    # thumb=-1 = largest available thumbnail size.
                    try:
                        await self._client.download_media(
                            m, file=buf, thumb=-1)
                    except Exception:
                        # Some media only expose the smallest; retry.
                        buf = _io.BytesIO()
                        await self._client.download_media(
                            m, file=buf, thumb=0)
                    data = buf.getvalue()
                    if data:
                        thumb_b64 = _b64.b64encode(data)\
                            .decode("ascii")
                except Exception:
                    thumb_b64 = ""
        ts = 0.0
        try:
            ts = m.date.timestamp() if m.date else 0.0
        except Exception:
            pass
        return TgMessage(
            id=m.id, chat_id=chat_id, sender=sender,
            text=m.message or "", timestamp=ts,
            out=bool(m.out), media=(m.media is not None),
            media_kind=kind, filename=fname, thumb_b64=thumb_b64)

    async def _on_new_message(self, event):
        try:
            chat_id = event.chat_id
            tm = await self._to_tgmessage(event.message, chat_id)
            self.sig_new_message.emit(tm)
        except Exception:
            pass

    # -- send / download -----------------------------------------
    def send_message(self, chat_id: int, text: str):
        self._submit(self._send_message(chat_id, text))

    async def _send_message(self, chat_id: int, text: str):
        try:
            await self._client.send_message(chat_id, text)
            self.sig_sent.emit(chat_id)
        except Exception as e:
            self.sig_error.emit("Send failed: %s" % e)

    def send_file(self, chat_id: int, filepath: str,
                  caption: str = ""):
        self._submit(self._send_file(chat_id, filepath, caption))

    async def _send_file(self, chat_id: int, filepath: str,
                         caption: str):
        try:
            await self._client.send_file(
                chat_id, filepath, caption=caption or None)
            import os
            self.sig_upload_done.emit(
                chat_id, os.path.basename(filepath))
        except Exception as e:
            self.sig_error.emit("Upload failed: %s" % e)

    def download_message_media(self, chat_id: int, message_id: int,
                               dest_dir: str,
                               open_after: bool = False):
        self._submit(self._download(
            chat_id, message_id, dest_dir, open_after))

    async def _download(self, chat_id: int, message_id: int,
                        dest_dir: str, open_after: bool = False):
        try:
            # Use the cached entity so get_messages resolves the
            # chat (same reason as in _fetch_messages).
            target = self._entities.get(chat_id, chat_id)
            msg = await self._client.get_messages(
                target, ids=message_id)
            if msg is None or msg.media is None:
                self.sig_error.emit("No media on that message.")
                return
            path = await self._client.download_media(
                msg, file=dest_dir)
            if path:
                self.sig_download_done.emit(str(path), open_after)
            else:
                self.sig_error.emit("Download produced no file.")
        except Exception as e:
            self.sig_error.emit("Download failed: %s" % e)


# ------------------------------------------------------------------
# UI
# ------------------------------------------------------------------
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QTextEdit, QTextBrowser,
    QLineEdit, QPushButton,
    QLabel, QInputDialog, QFileDialog, QMessageBox, QFormLayout,
)
from .palette import C
from .config import scaled_font_px


def _mono_font(px_base=12) -> QFont:
    f = QFont("Topaz", scaled_font_px(px_base))
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


def _is_light(hex_color: str) -> bool:
    """True if a #rrggbb color is light enough to need dark text."""
    try:
        h = hex_color.lstrip("#")
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        # Perceived luminance (rec. 601).
        return (0.299 * r + 0.587 * g + 0.114 * b) > 140
    except Exception:
        return False


class TelegramDialog(QDialog):
    """Embedded Telegram client. Pass the active lister so the
    'send tagged files' button knows what to upload and where to
    drop downloads by default."""

    def __init__(self, parent=None, lister=None):
        super().__init__(parent)
        self._lister = lister
        self._worker: Optional[_TgWorker] = None
        self._dialogs: list[TgDialog] = []
        self._current_chat: Optional[int] = None
        # Cached messages per chat. Survives buffer switches so a
        # second visit is instant. Keyed by chat_id, value is the
        # full known message list (oldest first). New incoming
        # messages append; "load older" prepends. ALSO persisted
        # to disk under cache/telegram/ so a Quopus restart picks
        # the messages back up without hitting the network.
        self._msg_cache: Dict[int, list] = {}
        # Whether we've already loaded the latest batch for a chat
        # this session (so a re-open uses the cache instead of
        # re-fetching everything). Persisted alongside the message
        # cache - if we had a chat synced last session, it counts
        # as still-fresh on the next launch too.
        self._cache_fresh: Dict[int, bool] = {}
        self._cache_dir = self._tg_cache_dir()
        self._load_message_cache_from_disk()
        # How many messages to ask for on initial open of a chat
        # and on each "load older" click.
        self._initial_limit = 100
        self._page_limit = 100
        # While a "load older" request is in flight we ignore
        # further clicks so we don't fan out duplicate requests.
        self._loading_older = False
        # Archived chat IDs - hidden from the main list, shown in
        # the "Archived" view when the user toggles it on. Persisted
        # so it survives restarts.
        self._archived: set = self._load_archived()
        # Whether the chat list currently shows the archive view.
        self._show_archive = False
        self._download_dir = self._guess_download_dir()
        self._load_bubble_colors()
        self.setWindowTitle("Telegram - Quopus Commander")
        self.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(1000, 680)
        self._build_ui()
        # Defer auto-connect until the dialog is shown.
        QTimer.singleShot(50, self._auto_start)

    # -- setup helpers -------------------------------------------
    def _guess_download_dir(self) -> str:
        try:
            cur = getattr(self._lister, "current_path", None)
            if cur is not None and Path(cur).is_dir():
                return str(cur)
        except Exception:
            pass
        return str(Path.home())

    # -- bubble colors (configurable, persisted in quopus.cfg) ----
    def _load_bubble_colors(self):
        """Read the chat-bubble colors from the Quopus config, with
        sensible defaults if they're missing. Prefers the main
        window's in-memory config dict (always the latest) and
        falls back to load_config() from disk for the bootstrap
        case where the dialog opens before any other save has
        happened."""
        cfg = None
        mw = self.parent()
        mw_cfg = getattr(mw, "config", None)
        if isinstance(mw_cfg, dict):
            cfg = mw_cfg
        else:
            try:
                from .config import load_config
                cfg = load_config()
            except Exception:
                cfg = {}
        self._col_out_bg = cfg.get("telegram_out_bg", "#1f6e3a")
        self._col_out_fg = cfg.get("telegram_out_fg", "#eafbe7")
        self._col_in_bg = cfg.get("telegram_in_bg", "#1c3f63")
        self._col_in_fg = cfg.get("telegram_in_fg", "#e7f0fb")

    def _save_bubble_colors(self):
        """Persist the current bubble colors back to quopus.cfg.

        IMPORTANT: We also patch the main window's live config
        dict, because the main window calls save_config(self.config)
        on its own copy at various points (settings dialogs, window
        close, lister actions). If we only wrote to disk here, the
        next main-window save would clobber our color entries with
        the values it had at startup. Same dual-write pattern as
        _save_archived() above."""
        try:
            from .config import load_config, save_config
            # Disk first - fresh round-trip so we don't lose any
            # other keys that may have changed since startup.
            cfg = load_config()
            cfg["telegram_out_bg"] = self._col_out_bg
            cfg["telegram_out_fg"] = self._col_out_fg
            cfg["telegram_in_bg"] = self._col_in_bg
            cfg["telegram_in_fg"] = self._col_in_fg
            save_config(cfg)
            # Then patch the main window's in-memory config so its
            # next save() carries the same values.
            mw = self.parent()
            mw_cfg = getattr(mw, "config", None)
            if isinstance(mw_cfg, dict):
                mw_cfg["telegram_out_bg"] = self._col_out_bg
                mw_cfg["telegram_out_fg"] = self._col_out_fg
                mw_cfg["telegram_in_bg"] = self._col_in_bg
                mw_cfg["telegram_in_fg"] = self._col_in_fg
        except Exception as e:
            QMessageBox.warning(
                self, "Telegram",
                "Couldn't save colors: %s" % e)

    # -- archive persistence ------------------------------------
    def _load_archived(self) -> set:
        """Read the set of archived chat IDs.

        Prefers the main window's live config dict (it's the source
        of truth while the app is running); falls back to loading
        the file from disk for the bootstrap case. Stored as a list
        of ints under 'telegram_archived'."""
        try:
            ids = None
            mw = self.parent()
            mw_cfg = getattr(mw, "config", None)
            if isinstance(mw_cfg, dict) \
                    and "telegram_archived" in mw_cfg:
                ids = mw_cfg.get("telegram_archived", []) or []
            else:
                from .config import load_config
                cfg = load_config()
                ids = cfg.get("telegram_archived", []) or []
            return {int(x) for x in ids}
        except Exception:
            return set()

    def _save_archived(self):
        """Write the archive set back to quopus.cfg.

        IMPORTANT: We also have to update the *in-memory* config
        that the main window holds, because the main window calls
        save_config(self.config) on its own copy at various points
        (settings dialogs, lister actions, window close). If we
        only wrote to disk here, a later save by the main window
        would clobber our entry. Updating its dict in place keeps
        both views in sync."""
        try:
            from .config import load_config, save_config
            ids = sorted(self._archived)
            # Disk first - fresh round-trip so we don't lose any
            # other keys that may have changed in the meantime.
            cfg = load_config()
            cfg["telegram_archived"] = ids
            save_config(cfg)
            # Then patch the main window's live config dict too,
            # so its next save() carries the same value.
            mw = self.parent()
            mw_cfg = getattr(mw, "config", None)
            if isinstance(mw_cfg, dict):
                mw_cfg["telegram_archived"] = ids
        except Exception:
            pass

    # -- persistent message cache ---------------------------------
    def _tg_cache_dir(self) -> "Path":
        """Where per-chat cache JSONs live. Sits under the Quopus
        cache/ tree (which the updater leaves alone) so we don't
        clutter the user's config/ folder."""
        from pathlib import Path
        from .config import CONFIG_DIR
        # cache/ is a sibling of config/ in the standard layout.
        cache_root = Path(CONFIG_DIR).parent / "cache"
        d = cache_root / "telegram"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _msg_to_dict(self, m: TgMessage) -> dict:
        return {
            "id": m.id, "chat_id": m.chat_id,
            "sender": m.sender, "text": m.text,
            "timestamp": m.timestamp, "out": m.out,
            "media": m.media, "media_kind": m.media_kind,
            "filename": m.filename, "thumb_b64": m.thumb_b64,
        }

    def _msg_from_dict(self, d: dict) -> Optional[TgMessage]:
        try:
            return TgMessage(
                id=int(d["id"]), chat_id=int(d["chat_id"]),
                sender=d.get("sender", ""),
                text=d.get("text", ""),
                timestamp=float(d.get("timestamp", 0.0)),
                out=bool(d.get("out", False)),
                media=bool(d.get("media", False)),
                media_kind=d.get("media_kind", ""),
                filename=d.get("filename", ""),
                thumb_b64=d.get("thumb_b64", ""))
        except Exception:
            return None

    def _load_message_cache_from_disk(self):
        """Read every per-chat JSON in cache/telegram/ into
        self._msg_cache. Each loaded chat is also marked
        cache_fresh=True so the next visit skips the network
        round-trip - live updates pull in new messages
        afterwards via _on_new_message."""
        d = self._cache_dir
        if not d.is_dir():
            return
        loaded = 0
        for p in d.glob("chat_*.json"):
            try:
                import json
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            chat_id = data.get("chat_id")
            raw_msgs = data.get("messages") or []
            if not isinstance(chat_id, int):
                continue
            msgs = []
            for d_ in raw_msgs:
                m = self._msg_from_dict(d_)
                if m is not None:
                    msgs.append(m)
            if msgs:
                msgs.sort(key=lambda m: m.id)
                self._msg_cache[chat_id] = msgs
                self._cache_fresh[chat_id] = True
                loaded += 1
        if loaded:
            _log("Loaded %d chat caches from disk" % loaded)

    def _save_chat_cache(self, chat_id: int):
        """Persist the current cache for one chat to its JSON.
        Called whenever _merge_into_cache touches the entry."""
        msgs = self._msg_cache.get(chat_id) or []
        if not msgs:
            return
        # Cap the on-disk size: keep the most recent N messages.
        # The cache is a convenience, not an archive - if someone
        # really needs older messages they can use 'Load older'
        # which fetches from the server.
        MAX_PERSISTED = 500
        if len(msgs) > MAX_PERSISTED:
            persist = msgs[-MAX_PERSISTED:]
        else:
            persist = msgs
        try:
            import json
            payload = {
                "chat_id": chat_id,
                "messages": [self._msg_to_dict(m) for m in persist],
            }
            target = self._cache_dir / ("chat_%d.json" % chat_id)
            target.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8")
        except Exception as e:
            _log("Couldn't persist chat %d: %s" % (chat_id, e))

    def _edit_colors(self):
        """Open a small dialog with four color swatches (outgoing
        bg/fg, incoming bg/fg). Changes apply live to the open chat
        and are saved to the config."""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QColorDialog

        dlg = QDialog(self)
        dlg.setWindowTitle("Chat bubble colors")
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        form = QFormLayout(dlg)

        # Local working copy so Cancel discards.
        work = {
            "out_bg": self._col_out_bg, "out_fg": self._col_out_fg,
            "in_bg": self._col_in_bg, "in_fg": self._col_in_fg,
        }
        swatches = {}

        def make_row(key, label):
            btn = QPushButton()
            btn.setFont(_mono_font(11))
            btn.setFixedWidth(120)

            def refresh():
                c = work[key]
                # Show the hex and tint the button so it's obvious.
                btn.setText(c)
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {c}; "
                    f"color: {'#000' if _is_light(c) else '#fff'}; "
                    f"border: 1px solid {C.BLACK}; padding: 4px; }}")

            def pick():
                col = QColorDialog.getColor(
                    QColor(work[key]), dlg, "Pick %s" % label)
                if col.isValid():
                    work[key] = col.name()
                    refresh()
            btn.clicked.connect(pick)
            swatches[key] = (btn, refresh)
            refresh()
            form.addRow(label, btn)

        make_row("out_bg", "Own message - background")
        make_row("out_fg", "Own message - text")
        make_row("in_bg", "Others - background")
        make_row("in_fg", "Others - text")

        row = QHBoxLayout()
        b_reset = QPushButton("Reset")
        b_ok = QPushButton("Save")
        b_cancel = QPushButton("Cancel")
        for b in (b_reset, b_ok, b_cancel):
            b.setFont(_mono_font(11))
        row.addWidget(b_reset)
        row.addStretch(1)
        row.addWidget(b_ok)
        row.addWidget(b_cancel)
        form.addRow(row)

        def do_reset():
            work["out_bg"] = "#1f6e3a"
            work["out_fg"] = "#eafbe7"
            work["in_bg"] = "#1c3f63"
            work["in_fg"] = "#e7f0fb"
            for _k, (_btn, refresh) in swatches.items():
                refresh()
        b_reset.clicked.connect(do_reset)
        b_ok.clicked.connect(dlg.accept)
        b_cancel.clicked.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._col_out_bg = work["out_bg"]
        self._col_out_fg = work["out_fg"]
        self._col_in_bg = work["in_bg"]
        self._col_in_fg = work["in_fg"]
        self._save_bubble_colors()
        # Re-render the currently open chat with the new colors.
        # No need to refetch from the server - the cache has it.
        if self._current_chat is not None:
            cached = self._msg_cache.get(self._current_chat, [])
            if cached:
                self._rebuild_view(cached)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Status line
        self.lbl_status = QLabel("Not connected")
        self.lbl_status.setStyleSheet(
            f"QLabel {{ color: {C.BLACK}; "
            f"background-color: {C.WB_GREY_LT}; "
            f"border: 1px solid {C.WB_GREY_DK}; padding: 3px; }}")
        self.lbl_status.setFont(_mono_font(11))
        root.addWidget(self.lbl_status)

        # Main split: chats | conversation
        split = QSplitter(Qt.Orientation.Horizontal)

        # Left: chat list
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        # Chats / Archived toggle row, like Telegram's two views.
        # Clicking Archived hides all non-archived chats; clicking
        # Chats brings the main list back.
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(0)
        self.btn_tab_chats = QPushButton("Chats")
        self.btn_tab_archive = QPushButton("Archive")
        for b in (self.btn_tab_chats, self.btn_tab_archive):
            b.setFont(_mono_font(11))
            b.setCheckable(True)
        self.btn_tab_chats.setChecked(True)
        self.btn_tab_chats.clicked.connect(
            lambda: self._switch_archive_view(False))
        self.btn_tab_archive.clicked.connect(
            lambda: self._switch_archive_view(True))
        tab_row.addWidget(self.btn_tab_chats)
        tab_row.addWidget(self.btn_tab_archive)
        lv.addLayout(tab_row)
        self.list_chats = QListWidget()
        self.list_chats.setFont(_mono_font(11))
        self.list_chats.setStyleSheet(
            f"QListWidget {{ background-color: {C.WHITE}; "
            f"color: {C.BLACK}; border: 1px solid {C.WB_GREY_DK}; }} "
            f"QListWidget::item:selected {{ "
            f"background-color: {C.SELECTED}; color: {C.SELECTED_FG}; }}")
        self.list_chats.itemClicked.connect(self._on_chat_selected)
        # Right-click for archive / unarchive.
        self.list_chats.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_chats.customContextMenuRequested.connect(
            self._on_chat_context_menu)
        lv.addWidget(self.list_chats, 1)
        self.btn_refresh = QPushButton("Refresh chats")
        self.btn_refresh.setFont(_mono_font(11))
        self.btn_refresh.clicked.connect(self._refresh_chats)
        lv.addWidget(self.btn_refresh)
        self.btn_colors = QPushButton("Colors...")
        self.btn_colors.setFont(_mono_font(11))
        self.btn_colors.setToolTip(
            "Choose the chat bubble colors (saved in settings)")
        self.btn_colors.clicked.connect(self._edit_colors)
        lv.addWidget(self.btn_colors)
        split.addWidget(left)

        # Right: conversation
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.lbl_chat = QLabel("Select a chat")
        self.lbl_chat.setFont(_mono_font(12))
        self.lbl_chat.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        rv.addWidget(self.lbl_chat)
        # Load-older row above the message view. Telegram lets us
        # paginate back through history; clicking this asks the
        # server for the next page below the oldest cached id.
        older_row = QHBoxLayout()
        older_row.setContentsMargins(0, 0, 0, 0)
        self.btn_load_older = QPushButton("Load older messages")
        self.btn_load_older.setFont(_mono_font(10))
        self.btn_load_older.setToolTip(
            "Fetch the next %d older messages from the server"
            % self._page_limit)
        self.btn_load_older.clicked.connect(self._load_older)
        older_row.addWidget(self.btn_load_older)
        older_row.addStretch(1)
        self.lbl_cache = QLabel("")
        self.lbl_cache.setFont(_mono_font(10))
        self.lbl_cache.setStyleSheet(
            f"QLabel {{ color: {C.WB_GREY_DK}; }}")
        older_row.addWidget(self.lbl_cache)
        rv.addLayout(older_row)
        # QTextBrowser (not QTextEdit) so attachment download links
        # are clickable. We keep links in-app (setOpenLinks False)
        # and route tgdl:<id> clicks to the downloader.
        # Left click  = download to a temp folder and open with the
        #               OS default viewer.
        # Right click = download into the lister's folder (save).
        self.view_msgs = QTextBrowser()
        self.view_msgs.setReadOnly(True)
        self.view_msgs.setOpenLinks(False)
        self.view_msgs.setOpenExternalLinks(False)
        self.view_msgs.anchorClicked.connect(self._on_anchor_clicked)
        self.view_msgs.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.view_msgs.customContextMenuRequested.connect(
            self._on_msgs_context_menu)
        self.view_msgs.setFont(_mono_font(11))
        self.view_msgs.setStyleSheet(
            f"QTextBrowser {{ background-color: {C.WHITE}; "
            f"color: {C.BLACK}; border: 1px solid {C.WB_GREY_DK}; }}")
        rv.addWidget(self.view_msgs, 1)

        # Compose row
        compose = QHBoxLayout()
        self.edit_msg = QLineEdit()
        self.edit_msg.setFont(_mono_font(12))
        self.edit_msg.setPlaceholderText("Type a message...")
        self.edit_msg.setStyleSheet(
            f"QLineEdit {{ background-color: {C.WHITE}; "
            f"color: {C.BLACK}; border: 1px solid {C.WB_GREY_DK}; "
            f"padding: 4px; }}")
        self.edit_msg.returnPressed.connect(self._send_text)
        compose.addWidget(self.edit_msg, 1)
        self.btn_send = QPushButton("Send")
        self.btn_send.setFont(_mono_font(11))
        self.btn_send.clicked.connect(self._send_text)
        compose.addWidget(self.btn_send)
        self.btn_file = QPushButton("Send file...")
        self.btn_file.setFont(_mono_font(11))
        self.btn_file.clicked.connect(self._send_file_picker)
        compose.addWidget(self.btn_file)
        self.btn_tagged = QPushButton("Send tagged")
        self.btn_tagged.setFont(_mono_font(11))
        self.btn_tagged.setToolTip(
            "Send the files tagged in the active Quopus lister")
        self.btn_tagged.clicked.connect(self._send_tagged)
        compose.addWidget(self.btn_tagged)
        rv.addLayout(compose)
        split.addWidget(right)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)

        self._set_compose_enabled(False)

    def _set_compose_enabled(self, on: bool):
        for w in (self.edit_msg, self.btn_send, self.btn_file,
                  self.btn_tagged):
            w.setEnabled(on)

    # -- connection / login --------------------------------------
    def _auto_start(self):
        if not telethon_available():
            QMessageBox.warning(
                self, "Telegram",
                "The 'telethon' package is not installed.\n\n"
                "Install it with:\n    pip install telethon\n\n"
                "then reopen this window.")
            self._set_status("telethon not installed")
            return
        cfg = load_tg_config()
        if not cfg.get("api_id") or not cfg.get("api_hash") \
                or not cfg.get("phone"):
            if not self._run_setup_wizard():
                self._set_status("Setup cancelled")
                return
            cfg = load_tg_config()
        try:
            api_id = int(cfg["api_id"])
        except Exception:
            QMessageBox.warning(
                self, "Telegram",
                "api_id in telegram.cfg must be a number.")
            return
        self._worker = _TgWorker(
            api_id, cfg["api_hash"], cfg["phone"])
        self._wire_worker()
        self._worker.start()
        self._worker.connect_and_login()

    def _run_setup_wizard(self) -> bool:
        """Ask for api_id / api_hash / phone and save them. Returns
        False if the user cancelled."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Telegram setup")
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        form = QFormLayout(dlg)
        info = QLabel(
            "Get your api_id and api_hash (free) from\n"
            "https://my.telegram.org -> API development tools.\n"
            "Enter the phone number of your Telegram account\n"
            "in international format, e.g. +49170...")
        info.setFont(_mono_font(11))
        info.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        form.addRow(info)
        e_id = QLineEdit(); e_hash = QLineEdit(); e_phone = QLineEdit()
        for e in (e_id, e_hash, e_phone):
            e.setFont(_mono_font(12))
            e.setStyleSheet(
                f"QLineEdit {{ background-color: {C.WHITE}; "
                f"color: {C.BLACK}; padding: 3px; }}")
        cfg = load_tg_config()
        e_id.setText(cfg.get("api_id", ""))
        e_hash.setText(cfg.get("api_hash", ""))
        e_phone.setText(cfg.get("phone", ""))
        form.addRow("api_id:", e_id)
        form.addRow("api_hash:", e_hash)
        form.addRow("phone:", e_phone)
        btns = QHBoxLayout()
        ok = QPushButton("Save"); cancel = QPushButton("Cancel")
        ok.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        btns.addWidget(ok); btns.addWidget(cancel)
        form.addRow(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        if not e_id.text().strip() or not e_hash.text().strip() \
                or not e_phone.text().strip():
            QMessageBox.warning(self, "Telegram",
                                "All three fields are required.")
            return False
        save_tg_config(e_id.text(), e_hash.text(), e_phone.text())
        return True

    def _wire_worker(self):
        w = self._worker
        w.sig_status.connect(self._set_status)
        w.sig_error.connect(self._on_error)
        w.sig_need_code.connect(self._ask_code)
        w.sig_need_password.connect(self._ask_password)
        w.sig_authorized.connect(self._on_authorized)
        w.sig_dialogs.connect(self._on_dialogs)
        w.sig_messages.connect(self._on_messages)
        w.sig_new_message.connect(self._on_new_message)
        w.sig_sent.connect(self._on_sent)
        w.sig_download_done.connect(self._on_download_done)
        w.sig_upload_done.connect(self._on_upload_done)

    def _set_status(self, text: str):
        self.lbl_status.setText(text)

    def _on_error(self, text: str):
        self._set_status(text)
        # Per-chat message-load failures shouldn't spawn a modal
        # popup every time you click such a chat - the status line
        # is enough. Connection/login/send errors still pop up so
        # they're not missed.
        low = text.lower()
        quiet = ("couldn't load messages" in low
                 or "no messages" in low)
        if not quiet:
            QMessageBox.warning(self, "Telegram", text)

    def _ask_code(self):
        code, ok = QInputDialog.getText(
            self, "Telegram login",
            "Enter the login code Telegram just sent you:")
        if ok and code.strip():
            self._worker.provide_code(code)
        else:
            self._set_status("Login cancelled (no code)")

    def _ask_password(self):
        pw, ok = QInputDialog.getText(
            self, "Telegram 2FA",
            "Your account has a cloud password (2FA). Enter it:",
            QLineEdit.EchoMode.Password)
        if ok:
            self._worker.provide_password(pw)
        else:
            self._set_status("Login cancelled (no password)")

    def _on_authorized(self, name: str):
        self._set_status("Connected as %s" % name)
        self._refresh_chats()

    # -- chats ----------------------------------------------------
    def _refresh_chats(self):
        if self._worker is not None:
            self._set_status("Loading chats...")
            self._worker.fetch_dialogs(100)

    def _on_dialogs(self, dialogs: list):
        self._dialogs = dialogs
        self._refresh_chat_list()

    def _refresh_chat_list(self):
        """Rebuild the visible chat list from self._dialogs,
        applying the current archive filter."""
        self.list_chats.clear()
        n_archived = sum(1 for d in self._dialogs
                         if d.id in self._archived)
        n_active = len(self._dialogs) - n_archived
        # Update tab labels with counts so the user sees what's
        # where without switching tabs.
        self.btn_tab_chats.setText("Chats (%d)" % n_active)
        self.btn_tab_archive.setText("Archive (%d)" % n_archived)
        for d in self._dialogs:
            archived = d.id in self._archived
            if self._show_archive and not archived:
                continue
            if not self._show_archive and archived:
                continue
            tag = ("@" if d.is_user else
                   "#" if d.is_channel else
                   "*" if d.is_group else " ")
            label = f"{tag} {d.name}"
            if d.unread:
                label += f"  ({d.unread})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, d.id)
            self.list_chats.addItem(item)
        if self._show_archive:
            self._set_status("Archive: %d chat%s" %
                             (n_archived,
                              "" if n_archived == 1 else "s"))
        else:
            self._set_status("%d chats (+%d archived)" %
                             (n_active, n_archived))

    def _switch_archive_view(self, show_archive: bool):
        self._show_archive = show_archive
        # Keep the toggle buttons consistent regardless of how the
        # state was changed.
        self.btn_tab_chats.setChecked(not show_archive)
        self.btn_tab_archive.setChecked(show_archive)
        self._refresh_chat_list()

    def _on_chat_context_menu(self, pos):
        """Right-click on a chat: archive / unarchive."""
        from PyQt6.QtWidgets import QMenu
        it = self.list_chats.itemAt(pos)
        if it is None:
            return
        chat_id = it.data(Qt.ItemDataRole.UserRole)
        is_archived = chat_id in self._archived
        menu = QMenu(self.list_chats)
        if is_archived:
            act = menu.addAction("Unarchive")
        else:
            act = menu.addAction("Archive (hide from main list)")
        chosen = menu.exec(
            self.list_chats.viewport().mapToGlobal(pos))
        if chosen is act:
            if is_archived:
                self._archived.discard(chat_id)
            else:
                self._archived.add(chat_id)
                # If we just archived the open chat, also clear the
                # message view so it doesn't look like the chat is
                # still selected.
                if chat_id == self._current_chat:
                    self._current_chat = None
                    self.view_msgs.clear()
                    self.lbl_chat.setText("Select a chat")
                    self._set_compose_enabled(False)
            self._save_archived()
            self._refresh_chat_list()

    def _on_chat_selected(self, item: QListWidgetItem):
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        self._current_chat = chat_id
        _log("chat selected: %r (id=%r)" % (item.text(), chat_id))
        self.lbl_chat.setText(item.text())
        self._set_compose_enabled(True)
        self._loading_older = False
        # Render cached messages immediately (so re-opening a chat
        # is instant), then either re-fetch the newest batch (only
        # the first time we open this chat in the session) or trust
        # the cache + live updates and skip the network round-trip.
        cached = self._msg_cache.get(chat_id, [])
        cache_is_fresh = bool(self._cache_fresh.get(chat_id))
        if cached:
            _log("  rendering %d cached msgs (fresh=%s)"
                 % (len(cached), cache_is_fresh))
            self._rebuild_view(cached)
            if cache_is_fresh:
                self._set_status(
                    "%d messages (cached)" % len(cached))
            else:
                self._set_status(
                    "Showing %d cached messages - syncing..."
                    % len(cached))
        else:
            self.view_msgs.clear()
            self._set_status("Loading...")
        self._update_cache_label()
        # Only hit the network when we haven't fetched this chat
        # yet in this session. Live updates (_on_new_message) keep
        # the cache current after that point, so repeated chat
        # switches are local-only - no network spam, no flicker.
        if cache_is_fresh and cached:
            _log("  cache fresh - skipping server fetch")
            return
        if self._worker is not None:
            self._worker.fetch_messages(
                chat_id, self._initial_limit, before_id=0)
        else:
            _log("  WORKER IS NONE - not connected?")
            self._set_status("Not connected yet")

    # -- messages -------------------------------------------------
    def _on_messages(self, chat_id: int, before_id: int,
                     msgs: list):
        _log("_on_messages chat_id=%r current=%r before_id=%r "
             "count=%d"
             % (chat_id, self._current_chat, before_id, len(msgs)))

        # Merge into cache regardless of which chat is showing, so
        # background fetches (e.g. left-over from a quick chat
        # switch) still populate the cache for a future visit.
        merged = self._merge_into_cache(chat_id, msgs)

        # "load older" releases the throttle.
        if before_id:
            self._loading_older = False

        if chat_id != self._current_chat:
            _log("  not current chat - updated cache only")
            return

        self._rebuild_view(merged)
        if not merged:
            self._set_status("No messages in this chat (or none "
                             "loaded yet)")
            return
        if before_id:
            self._set_status(
                "Loaded %d older - %d total cached"
                % (len(msgs), len(merged)))
        else:
            self._cache_fresh[chat_id] = True
            self._set_status(
                "%d messages (cached)" % len(merged))
        self._update_cache_label()

    def _merge_into_cache(self, chat_id: int,
                          incoming: list) -> list:
        """Merge a batch of messages into the per-chat cache,
        de-duplicating by message id and keeping oldest-first
        order. Returns the merged list. Persists the chat to
        disk after each merge so the cache survives restarts."""
        cached = self._msg_cache.get(chat_id, [])
        if not cached:
            self._msg_cache[chat_id] = list(incoming)
            if incoming:
                self._save_chat_cache(chat_id)
            return self._msg_cache[chat_id]
        if not incoming:
            return cached
        # Build a dict by id so duplicates collapse; incoming wins
        # (it's the freshest copy from the server).
        by_id = {m.id: m for m in cached}
        for m in incoming:
            by_id[m.id] = m
        merged = sorted(by_id.values(), key=lambda m: m.id)
        self._msg_cache[chat_id] = merged
        self._save_chat_cache(chat_id)
        return merged

    def _load_older(self):
        """User asked for older messages - look up the oldest id
        we have and ask the worker for the next page below it."""
        if self._loading_older:
            return
        if self._current_chat is None or self._worker is None:
            return
        cached = self._msg_cache.get(self._current_chat, [])
        if not cached:
            return
        oldest_id = cached[0].id
        self._loading_older = True
        self._set_status("Loading older messages...")
        _log("load_older chat=%r oldest_id=%r limit=%d"
             % (self._current_chat, oldest_id, self._page_limit))
        self._worker.fetch_messages(
            self._current_chat, self._page_limit,
            before_id=oldest_id)

    def _update_cache_label(self):
        """Show '<n> cached' next to the load-older button so the
        user can see how far back they've already pulled."""
        if self._current_chat is None:
            self.lbl_cache.setText("")
            return
        cached = self._msg_cache.get(self._current_chat, [])
        if not cached:
            self.lbl_cache.setText("")
        else:
            self.lbl_cache.setText("%d in cache" % len(cached))

    def _on_anchor_clicked(self, url):
        """Left click on an attachment link: download to a temp
        folder and open with the OS default app. Right-click goes
        through _on_msgs_context_menu instead, which saves into the
        lister folder."""
        try:
            s = url.toString()
        except Exception:
            s = str(url)
        # Keep the view exactly where it is. QTextBrowser tends to
        # jump to the top on a link click; we save the scrollbar
        # position and restore it (now and again on the next event
        # loop turn, after Qt has done its own scrolling).
        sb = self.view_msgs.verticalScrollBar()
        pos = sb.value()

        def _restore():
            try:
                sb.setValue(pos)
            except Exception:
                pass
        _restore()
        QTimer.singleShot(0, _restore)

        msg_id = self._parse_tgdl(s)
        if msg_id is None:
            return
        if self._current_chat is None or self._worker is None:
            return
        # Open: stash into a per-session temp dir so we don't litter
        # the user's working folder with files they only want to
        # look at quickly.
        import tempfile as _tf
        if not getattr(self, "_tg_tmp_dir", None):
            self._tg_tmp_dir = _tf.mkdtemp(prefix="quopus_tg_")
        self._set_status("Opening attachment...")
        self._worker.download_message_media(
            self._current_chat, msg_id, self._tg_tmp_dir,
            open_after=True)

    def _on_msgs_context_menu(self, pos):
        """Right click: if it's on an attachment link, offer Save
        (download into the lister folder). Otherwise no menu."""
        msg_id = self._anchor_msg_id_at(pos)
        if msg_id is None:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self.view_msgs)
        act_save = menu.addAction(
            "Save attachment to lister folder")
        chosen = menu.exec(
            self.view_msgs.viewport().mapToGlobal(pos))
        if chosen is act_save:
            if self._current_chat is None or self._worker is None:
                return
            dest = self._download_dir
            self._set_status(
                "Saving attachment to %s ..." % dest)
            self._worker.download_message_media(
                self._current_chat, msg_id, dest,
                open_after=False)

    def _anchor_msg_id_at(self, pos):
        """Return the tgdl message id at a viewport position, or
        None if there's no attachment link under the cursor."""
        href = self.view_msgs.anchorAt(pos)
        return self._parse_tgdl(href) if href else None

    @staticmethod
    def _parse_tgdl(href: str):
        if not href or not href.startswith("tgdl:"):
            return None
        try:
            return int(href.split(":", 1)[1])
        except Exception:
            return None

    def _message_html(self, m: TgMessage) -> str:
        """Build the HTML for one message bubble. QTextEdit's HTML
        engine is limited, so we keep it to simple block elements
        with inline styles - a div with a background, aligned left
        or right via the align attribute - rather than tables, which
        render unreliably when inserted repeatedly."""
        import time as _t
        import html as _html
        when = ""
        if m.timestamp:
            try:
                when = _t.strftime("%H:%M", _t.localtime(m.timestamp))
            except Exception:
                when = ""
        who = "me" if m.out else (m.sender or "?")

        # Text part of the body.
        text_part = _html.escape(m.text or "").replace("\n", "<br>")

        # Media part: an inline thumbnail (if we have one) plus a
        # clickable download link. The link uses a custom scheme
        # tgdl:<message_id> that we intercept in _on_anchor_clicked
        # to download the full media into the lister folder.
        media_part = ""
        if m.media:
            label = m.filename or m.media_kind or "media"
            label_html = _html.escape(label)
            if m.thumb_b64:
                media_part += (
                    f'<br><img src="data:image/jpeg;base64,'
                    f'{m.thumb_b64}" '
                    f'style="max-width:400px;max-height:400px;"><br>')
            else:
                media_part += "<br>"
            media_part += (
                f'<a href="tgdl:{m.id}" '
                f'style="color:{(self._col_out_fg if m.out else self._col_in_fg)};">'
                f'&#11015; {label_html}</a>')

        body_html = text_part + media_part
        if not body_html.strip():
            body_html = " "  # keep the bubble visible even if empty
        who_html = _html.escape(who)

        if m.out:
            align = "right"
            bg = self._col_out_bg
            fg = self._col_out_fg
        else:
            align = "left"
            bg = self._col_in_bg
            fg = self._col_in_fg

        head = who_html + (f" &middot; {when}" if when else "")
        # A right/left-aligned paragraph holds an inline-ish block
        # (we fake the bubble width with margins so the opposite
        # side keeps a gutter). QTextEdit honours align on <p> and
        # background-color on a nested element.
        margin = ("margin:3px 4px 3px 30%;" if align == "right"
                  else "margin:3px 30% 3px 4px;")
        return (
            f'<p align="{align}" style="margin:1px 0;">'
            f'<span style="background-color:{bg};color:{fg};'
            f'{margin}padding:4px 8px;">'
            f'<small style="color:{fg};">{head}</small><br>'
            f'{body_html}</span></p>'
        )

    def _rebuild_view(self, msgs: list):
        """Render the whole message list in one shot. Much more
        reliable than many incremental insertHtml() calls. Inserts
        a centered date label whenever the calendar day changes
        between two consecutive messages."""
        parts = []
        prev_day = None
        for m in msgs:
            day = self._day_key(m.timestamp)
            if day != prev_day:
                parts.append(self._date_separator_html(m.timestamp))
                prev_day = day
            parts.append(self._message_html(m))
        html = ("<html><body style=\"margin:0;\">"
                + "".join(parts) + "</body></html>")
        self.view_msgs.setHtml(html)
        # Remember the latest day key so _append_message can decide
        # whether to insert a fresh separator before a live update.
        self._last_render_day = prev_day
        # Try to scroll to the latest message multiple times: once
        # right away (works when there are few messages and layout
        # is instant), and again after a few Qt event-loop turns
        # in case the document is heavy (images, lots of HTML) and
        # the layout pass hasn't finished yet. Without the later
        # attempts, the view ends up parked at the top because
        # scrollBar.maximum() was still 0 when we asked.
        self._scroll_to_bottom()
        QTimer.singleShot(0, self._scroll_to_bottom)
        QTimer.singleShot(50, self._scroll_to_bottom)
        QTimer.singleShot(200, self._scroll_to_bottom)

    @staticmethod
    def _day_key(ts: float) -> str:
        """Calendar-day identifier in local time, used to detect
        when two messages need a date separator between them."""
        import time as _t
        return _t.strftime("%Y-%m-%d", _t.localtime(ts))

    def _date_separator_html(self, ts: float) -> str:
        """A centered pill-style label between message bubbles -
        matches Telegram's day separator. 'Today' / 'Yesterday' /
        'Wed, 13 May 2026'."""
        import time as _t
        now = _t.time()
        today = _t.strftime("%Y-%m-%d", _t.localtime(now))
        yday = _t.strftime("%Y-%m-%d",
                           _t.localtime(now - 86400))
        day = _t.strftime("%Y-%m-%d", _t.localtime(ts))
        if day == today:
            label = "Today"
        elif day == yday:
            label = "Yesterday"
        else:
            label = _t.strftime("%a, %d %b %Y",
                                _t.localtime(ts))
        return (
            '<div align="center" '
            'style="margin: 12px 0 6px 0;">'
            '<span style="background-color:#cfd8dc; color:#37474f;'
            ' padding: 2px 10px; border-radius: 9px;'
            ' font-size: 11px;">'
            + label + "</span></div>")

    def _scroll_to_bottom(self):
        """Move both the cursor and the scrollbar to the end of
        the document. Belt-and-suspenders: setHtml() doesn't lay
        out synchronously, so the scrollbar's maximum() can still
        be 0 right after we set the content. Moving the text
        cursor first forces layout, then ensureCursorVisible() +
        scrollbar slam catches whichever stage Qt is currently
        in."""
        try:
            from PyQt6.QtGui import QTextCursor
            cur = self.view_msgs.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            self.view_msgs.setTextCursor(cur)
            self.view_msgs.ensureCursorVisible()
            sb = self.view_msgs.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def _append_message(self, m: TgMessage):
        """Append a single new message (live update). Uses the same
        bubble HTML as the bulk renderer, and inserts a date label
        first when the live message starts a new calendar day."""
        from PyQt6.QtGui import QTextCursor
        cur = self.view_msgs.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.view_msgs.setTextCursor(cur)
        day = self._day_key(m.timestamp)
        if getattr(self, "_last_render_day", None) != day:
            self.view_msgs.insertHtml(
                self._date_separator_html(m.timestamp))
            self._last_render_day = day
        self.view_msgs.insertHtml(self._message_html(m))
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_new_message(self, m: TgMessage):
        # Always merge the incoming message into the per-chat
        # cache, even if the chat isn't currently displayed.
        # Without this the cache would go stale the moment the
        # user switched away, and our skip-refetch logic would
        # then show outdated content on the next visit.
        try:
            self._merge_into_cache(m.chat_id, [m])
        except Exception:
            pass
        # Live update only if it's for the open chat; otherwise
        # bump the unread hint by refreshing the chat list lazily.
        if m.chat_id == self._current_chat:
            self._append_message(m)
        else:
            # Light touch: just reflect there's activity in status.
            self._set_status("New message in another chat")

    # -- sending --------------------------------------------------
    def _send_text(self):
        if self._current_chat is None or self._worker is None:
            return
        text = self.edit_msg.text().strip()
        if not text:
            return
        self._worker.send_message(self._current_chat, text)
        self.edit_msg.clear()

    def _on_sent(self, chat_id: int):
        # Re-pull the tail so our just-sent line shows with its real
        # server timestamp / ordering. A small batch is enough -
        # the cache holds the rest.
        if chat_id == self._current_chat and self._worker is not None:
            self._worker.fetch_messages(chat_id, 30, before_id=0)

    def _send_file_picker(self):
        if self._current_chat is None or self._worker is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Send file(s) to this chat",
            self._download_dir)
        for p in paths:
            self._worker.send_file(self._current_chat, p)
        if paths:
            self._set_status("Uploading %d file(s)..." % len(paths))

    def _send_tagged(self):
        if self._current_chat is None or self._worker is None:
            return
        files = self._tagged_files()
        if not files:
            QMessageBox.information(
                self, "Telegram",
                "No tagged files in the active lister.\n"
                "Tag files there first, then use 'Send tagged'.")
            return
        for p in files:
            self._worker.send_file(self._current_chat, str(p))
        self._set_status("Uploading %d tagged file(s)..." % len(files))

    def _tagged_files(self) -> list:
        """Collect tagged file paths from the lister, falling back
        to the current selection if nothing is tagged."""
        L = self._lister
        if L is None:
            return []
        # Try a few likely interfaces without hard-coupling.
        for attr in ("tagged_paths", "get_tagged_paths",
                     "selected_paths", "get_selected_paths"):
            fn = getattr(L, attr, None)
            if callable(fn):
                try:
                    res = fn()
                    paths = [Path(p) for p in res
                             if Path(p).is_file()]
                    if paths:
                        return paths
                except Exception:
                    continue
        # Model-level tag set used by DirModel.
        try:
            model = getattr(L, "model", None)
            if model is not None and hasattr(model, "tagged_paths"):
                return [Path(p) for p in model.tagged_paths()
                        if Path(p).is_file()]
        except Exception:
            pass
        return []

    def _on_upload_done(self, chat_id: int, filename: str):
        self._set_status("Sent file: %s" % filename)
        if chat_id == self._current_chat and self._worker is not None:
            self._worker.fetch_messages(chat_id, 30, before_id=0)

    # -- downloads ------------------------------------------------
    def _download_selected_media(self):
        """Hook for a future 'download' action - downloads media of
        the message currently referenced. Kept simple for now: the
        message list is text, so we expose download via double-click
        later. Placeholder to keep the API stable."""
        pass

    def _on_download_done(self, path: str, open_after: bool):
        if open_after:
            # Left-click flow: hand the file to a viewer. Prefer the
            # lister's own dispatcher so the user's file-association
            # settings decide (TextReader for .txt, ImageViewer for
            # .png, ArchiveViewer for .zip, ...). Falls back to the
            # OS default opener if no lister is available or the
            # internal dispatch fails.
            self._set_status("Opening: %s" % path)
            self._view_via_lister_or_os(path)
            return
        # Right-click flow: file went into the lister folder. Bump
        # the lister so it appears, and confirm with a small popup.
        self._set_status("Saved: %s" % path)
        try:
            if self._lister is not None and \
                    hasattr(self._lister, "refresh"):
                self._lister.refresh()
        except Exception:
            pass
        QMessageBox.information(
            self, "Telegram", "Saved:\n%s" % path)

    def _view_via_lister_or_os(self, path: str):
        """Open a downloaded file with the user's configured
        viewer/editor for that extension (TextReader, HexReader,
        ImageViewer, ArchiveViewer, etc.) via the lister's
        _dispatch_view. Falls back to the OS default opener."""
        p = Path(path)
        used_internal = False
        if self._lister is not None and \
                hasattr(self._lister, "_dispatch_view"):
            try:
                self._lister._dispatch_view(p, action="viewer")
                used_internal = True
            except Exception as e:
                # Internal viewer raised - fall through to OS.
                self._set_status(
                    "Internal viewer failed (%s), trying system..."
                    % e)
        if not used_internal:
            self._open_with_os(path)

    def _open_with_os(self, path: str):
        """Hand a file to the operating system's default opener,
        the same way the lister's 'open' action does."""
        import platform as _pl, subprocess as _sp, os as _os
        try:
            if _pl.system() == "Windows":
                _os.startfile(path)
            elif _pl.system() == "Darwin":
                _sp.Popen(["open", path])
            else:
                _sp.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(
                self, "Telegram",
                "Couldn't open the file:\n%s" % e)

    # -- lifecycle ------------------------------------------------
    def closeEvent(self, ev):
        try:
            if self._worker is not None:
                self._worker.stop()
        except Exception:
            pass
        # Best-effort cleanup of the temp folder used for "open"
        # downloads. Files there are throwaway previews; if any
        # are still open in an external viewer the rmtree call
        # silently fails, which is fine.
        try:
            tmp = getattr(self, "_tg_tmp_dir", None)
            if tmp:
                import shutil as _sh
                _sh.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
        super().closeEvent(ev)
