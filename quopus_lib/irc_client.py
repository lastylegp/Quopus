# date_time: 2026-06-03 01:13
"""IRC client for Quopus Commander.

A full multi-server IRC client embedded as a PyQt6 dialog,
modelled after the classic WeeChat look: a buffer list on the
left, the chat in the middle, the nick list on the right, with a
command/message line at the bottom. Each server connection can be
on multiple channels, support /commands, SASL authentication
(PLAIN / EXTERNAL), TLS, server passwords, and DCC file transfers.

Architecture
============
The IRC protocol library `pydle` is asyncio-based. To keep Qt's
event loop free, every IRC client runs on a dedicated worker
thread with its own asyncio loop (just like the Telegram client),
and the UI talks to it via Qt signals. The worker manages all
server connections; each connection ("network") has its own
pydle.Client instance.

Files written
=============
  <quopus>/config/irc.cfg          server / channel definitions
  DCC downloads land in the lister's folder by default

The config file holds nicknames, passwords and SASL credentials,
so it must NOT go on GitHub - same drill as the other secret
files (telegram.cfg, mail_config.txt, server_config.txt).
"""
from __future__ import annotations

import asyncio
import json
import threading
import traceback
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List

from PyQt6.QtCore import QObject, pyqtSignal


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
def _config_path() -> Path:
    from .config import CONFIG_DIR
    return CONFIG_DIR / "irc.cfg"


@dataclass
class IrcServer:
    """A saved server profile."""
    name: str                       # short alias, e.g. "Libera"
    host: str                       # irc.libera.chat
    port: int = 6697
    tls: bool = True
    nickname: str = ""              # primary nickname
    alt_nicks: List[str] = field(default_factory=list)
    # ^ Tried in order if the primary nickname is already taken
    # (mIRC's "Alternative" field, but we allow more than one).
    username: str = ""              # IDENT / user; defaults to nick
    realname: str = "Quopus IRC"
    server_password: str = ""       # PASS command, optional
    sasl_username: str = ""         # SASL PLAIN, optional
    sasl_password: str = ""
    autojoin: List[str] = field(default_factory=list)  # ["#chan1"]
    autoconnect: bool = False
    # NickServ identification: auto-IDENTIFY after connect.
    nickserv_password: str = ""
    # DCC auto-receive policy: "off" = always ask the user,
    # "trusted" = auto-accept from nicks in dcc_trusted_nicks,
    # "all" = auto-accept everything (use with care). Auto-saved
    # files always go into dcc_auto_dir if set, else the active
    # lister folder.
    dcc_auto: str = "off"
    dcc_trusted_nicks: List[str] = field(default_factory=list)
    dcc_auto_dir: str = ""
    # On-disconnect: try to reconnect automatically?
    auto_reconnect: bool = True
    # Quit message broadcast when the user disconnects.
    quit_message: str = "Quopus IRC"
    # User's own channel bookmarks for this server: list of
    # (channel, optional note). Selected via Connect > Server >
    # Channel submenu and via the editor's Bookmarks pane.
    bookmarks: List[tuple] = field(default_factory=list)
    # If created via a template, remember which one so we can
    # offer the curated channels alongside the user's bookmarks.
    template_origin: str = ""


def load_irc_config() -> Dict[str, IrcServer]:
    """Return a dict of saved server profiles keyed by name."""
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, IrcServer] = {}
    for name, d in raw.get("servers", {}).items():
        try:
            out[name] = IrcServer(
                name=name,
                host=d.get("host", ""),
                port=int(d.get("port", 6697)),
                tls=bool(d.get("tls", True)),
                nickname=d.get("nickname", ""),
                alt_nicks=list(d.get("alt_nicks", []) or []),
                username=d.get("username", ""),
                realname=d.get("realname", "Quopus IRC"),
                server_password=d.get("server_password", ""),
                sasl_username=d.get("sasl_username", ""),
                sasl_password=d.get("sasl_password", ""),
                autojoin=list(d.get("autojoin", []) or []),
                autoconnect=bool(d.get("autoconnect", False)),
                nickserv_password=d.get("nickserv_password", ""),
                dcc_auto=str(d.get("dcc_auto", "off")),
                dcc_trusted_nicks=list(
                    d.get("dcc_trusted_nicks", []) or []),
                dcc_auto_dir=str(d.get("dcc_auto_dir", "") or ""),
                auto_reconnect=bool(d.get("auto_reconnect", True)),
                quit_message=d.get("quit_message", "Quopus IRC"),
                bookmarks=[
                    tuple(b) if isinstance(b, list) else (b, "")
                    for b in d.get("bookmarks", []) or []],
                template_origin=d.get("template_origin", ""),
            )
        except Exception:
            continue
    return out


def save_irc_config(servers: Dict[str, IrcServer]) -> None:
    from .config import CONFIG_DIR
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = _config_path()
    payload = {"servers": {}}
    for name, s in servers.items():
        payload["servers"][name] = {
            "host": s.host, "port": s.port, "tls": s.tls,
            "nickname": s.nickname,
            "alt_nicks": list(s.alt_nicks),
            "username": s.username,
            "realname": s.realname,
            "server_password": s.server_password,
            "sasl_username": s.sasl_username,
            "sasl_password": s.sasl_password,
            "autojoin": list(s.autojoin),
            "autoconnect": s.autoconnect,
            "nickserv_password": s.nickserv_password,
            "dcc_auto": s.dcc_auto,
            "dcc_trusted_nicks": list(s.dcc_trusted_nicks),
            "dcc_auto_dir": s.dcc_auto_dir,
            "auto_reconnect": s.auto_reconnect,
            "quit_message": s.quit_message,
            # Bookmarks are stored as [[channel, note], ...] in
            # JSON to keep it human-readable.
            "bookmarks": [[c, n] for c, n in s.bookmarks],
            "template_origin": s.template_origin,
        }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def pydle_available() -> bool:
    try:
        import pydle  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# Bundled network templates. Picked from a "New from template..."
# button in the server editor so the user doesn't have to research
# host names and ports. Each entry is (display_name, host, port,
# tls, notes_for_user). Ports follow each network's recommended
# TLS port where one is published; plaintext ports (usually 6667)
# are listed too for the few networks that don't run TLS by
# default. Sourced from each network's official server list /
# irchelp.org / archiveteam wiki.
# ------------------------------------------------------------------
IRC_NETWORK_TEMPLATES = [
    # (name, host, port, tls, description)
    ("Libera.Chat", "irc.libera.chat", 6697, True,
     "Successor to freenode; FOSS / Linux / programming"),
    ("OFTC", "irc.oftc.net", 6697, True,
     "Open and Free Technology Community; Debian, Tor, etc."),
    ("EFnet", "irc.efnet.org", 6697, True,
     "Modern-day descendant of the original IRC network"),
    ("IRCnet (open)", "open.ircnet.io", 6667, False,
     "Classic European IRCnet; plaintext (most stable option)"),
    ("IRCnet (TLS)", "ssl.ircnet.io", 6697, True,
     "IRCnet over TLS; some local servers may reject you"),
    ("Undernet", "irc.undernet.org", 6667, False,
     "Long-running general-purpose IRC network"),
    ("DALnet", "irc.dal.net", 6697, True,
     "Friendly chat-oriented network with channel services"),
    ("QuakeNet", "irc.quakenet.org", 6667, False,
     "Largest gaming-focused IRC network"),
    ("Rizon", "irc.rizon.net", 6697, True,
     "Anime / fansub / XDCC hub"),
    ("Abjects", "irc.abjects.net", 6697, True,
     "XDCC / filesharing network (TLS, K-line on rule breaks)"),
    ("hackint", "irc.hackint.org", 6697, True,
     "Hacker / CCC / infosec network"),
    ("GeekShed", "irc.geekshed.net", 6697, True,
     "Family-friendly chat network"),
    ("EsperNet", "irc.esper.net", 6697, True,
     "Open source projects, gaming, general chat"),
    ("SpigotMC", "irc.spi.gt", 6697, True,
     "Minecraft / SpigotMC community"),
    ("Snoonet", "irc.snoonet.org", 6697, True,
     "Reddit-related communities"),
    ("AnonOps", "irc.anonops.com", 6697, True,
     "Activism / anonymous discussion"),
    ("2600net", "irc.2600.net", 6667, False,
     "2600 Magazine / phreaking / hacking community"),
    ("GIMPNet", "irc.gimp.org", 6697, True,
     "GIMP and GNOME developers"),
    ("Perl IRC", "irc.perl.org", 6697, True,
     "Perl programming community"),
    ("Foonetic", "irc.foonetic.net", 6697, True,
     "xkcd / Foonetic community"),
    ("Lunarnet", "irc.lunarirc.net", 6697, True,
     "General chat / radio network"),
    ("PirateIRC", "irc.pirateirc.net", 6697, True,
     "Pirate Party / file sharing topics"),
    ("synIRC", "irc.synirc.net", 6697, True,
     "Anime / gaming / general"),
    ("Geeknode", "irc.geeknode.org", 6697, True,
     "French-speaking geeks / FOSS"),
    ("IRC4Fun", "irc.irc4fun.net", 6697, True,
     "Casual chat network"),
    ("SDF", "irc.sdf.org", 6697, True,
     "Super-Dimension Fortress public-access UNIX"),
    ("R-Type", "irc.r-type.ca", 6697, True,
     "Canadian general-chat network"),
    ("SpotChat", "irc.spotchat.org", 6697, True,
     "Mid-sized friendly chat network"),
    ("Hak5", "irc.hak5.org", 6697, True,
     "Hak5 / hacking / security podcast network"),
    ("Mibbit", "irc.mibbit.net", 6697, True,
     "Network used by the Mibbit webchat"),
    ("Chatlounge", "irc.chatlounge.net", 6697, True,
     "Friendly general-chat network"),
    ("RetroIT", "irc.retroit.org", 6697, True,
     "Retro computing / vintage hardware"),
]


# Pre-curated, well-known channels per network. The dict key
# matches the template name above; each value is a list of
# (channel, short description) entries the user can pick from
# without having to know channel names by heart. Adding more
# is a one-liner. This is shown both in the Connect submenu and
# the server editor's Bookmarks pane.
IRC_TEMPLATE_CHANNELS: Dict[str, list] = {
    "Libera.Chat": [
        ("#libera", "Network help and discussion"),
        ("#python", "Python programming"),
        ("#linux", "General Linux discussion"),
        ("##linux", "Larger Linux community"),
        ("#archlinux", "Arch Linux users"),
        ("#debian", "Debian users"),
        ("#emacs", "Emacs editor"),
        ("#vim", "Vim editor"),
        ("#weechat", "WeeChat IRC client"),
        ("##rust", "Rust programming"),
        ("#git", "Git version control"),
        ("##c", "C programming"),
        ("##c++", "C++ programming"),
        ("#javascript", "JavaScript / web dev"),
        ("##networking", "Networking and routing"),
        ("##security", "InfoSec discussion"),
    ],
    "OFTC": [
        ("#oftc", "OFTC general"),
        ("#debian", "Debian discussion"),
        ("#debian-devel", "Debian developers"),
        ("#tor", "Tor network discussion"),
        ("#tor-dev", "Tor development"),
    ],
    "EFnet": [
        ("#efnet", "EFnet general"),
        ("#help", "Network help"),
        ("#linux", "Linux discussion"),
        ("#c", "C programming"),
    ],
    "IRCnet (open)": [
        ("#ircnet", "Network discussion"),
        ("#help", "Network help and !servers command"),
        ("#irchelp", "Client / server help"),
        ("#linux", "Linux discussion"),
        ("#germany", "Deutschsprachiger Kanal"),
    ],
    "IRCnet (TLS)": [
        ("#ircnet", "Network discussion"),
        ("#help", "Network help"),
    ],
    "Undernet": [
        ("#help", "Network help"),
        ("#undernet", "General undernet"),
        ("#cservice", "Channel service"),
    ],
    "DALnet": [
        ("#help", "Network help"),
        ("#dalnet", "Network discussion"),
        ("#chat", "General chat"),
    ],
    "QuakeNet": [
        ("#feds", "Support channel"),
        ("#help", "Network help"),
        ("#quakenet", "Network discussion"),
    ],
    "Rizon": [
        ("#help", "Network help"),
        ("#news", "Network announcements"),
        ("#rizon", "General discussion"),
        ("#anime", "Anime discussion"),
        ("#manga", "Manga discussion"),
    ],
    "Abjects": [
        ("#abjects", "Network general"),
        ("#help", "Network help"),
        ("#movies", "XDCC movie hub"),
        ("#mp3", "XDCC music"),
        ("#tv", "XDCC TV shows"),
        ("#ebooks", "XDCC ebooks"),
    ],
    "hackint": [
        ("#hackint", "Network general"),
        ("#ccc", "Chaos Computer Club"),
        ("#help", "Network help"),
    ],
    "GeekShed": [
        ("#geekshed", "Network general"),
        ("#jupiterbroadcasting", "Jupiter Broadcasting"),
        ("#help", "Help"),
    ],
    "EsperNet": [
        ("#esper", "Network general"),
        ("#minecraft", "Minecraft chat"),
    ],
    "SpigotMC": [
        ("#spigot", "SpigotMC server admins"),
        ("#spigot-dev", "Plugin development"),
    ],
    "Snoonet": [
        ("#snoonet", "Network general"),
        ("#help", "Network help"),
    ],
    "2600net": [
        ("#2600", "2600 magazine community"),
        ("#hackerspaces", "Hackerspace discussion"),
    ],
    "GIMPNet": [
        ("#gimp", "GIMP image editor"),
        ("#gnome", "GNOME desktop"),
        ("#inkscape", "Inkscape vector editor"),
    ],
    "Perl IRC": [
        ("#perl", "Perl programming"),
        ("#moose", "Moose OO framework"),
    ],
    "SDF": [
        ("#sdf", "SDF members lounge"),
        ("#helpdesk", "Help desk"),
    ],
    "Hak5": [
        ("#hak5", "Hak5 community"),
        ("#wifipineapple", "WiFi Pineapple"),
    ],
    "RetroIT": [
        ("#retro", "Retro computing"),
        ("#amiga", "Amiga"),
        ("#c64", "Commodore 64"),
    ],
    # Networks without curated channels just get user-defined
    # bookmarks; the dict lookup will simply return nothing.
}


def make_server_from_template(template_idx: int,
                              alias: str,
                              nickname: str) -> IrcServer:
    """DEPRECATED in favour of make_server_from_template_name():
    indexing into IRC_NETWORK_TEMPLATES is fragile once user
    templates can be added in between (they sort by name). Kept
    for backwards compatibility with older callers."""
    name, host, port, tls, _descr = IRC_NETWORK_TEMPLATES[template_idx]
    nk = nickname or "quopus"
    return IrcServer(
        name=alias or name,
        host=host, port=port, tls=tls,
        nickname=nk,
        alt_nicks=[nk + "_", nk + "__"],
        template_origin=name)


def template_channels(template_name: str) -> list:
    """Pre-curated channel list for a template. Tries the bundled
    built-ins first, then falls back to user-defined templates.
    Returns an empty list if neither has curated entries."""
    if template_name in IRC_TEMPLATE_CHANNELS:
        return list(IRC_TEMPLATE_CHANNELS[template_name])
    user_tpls = load_user_templates()
    if template_name in user_tpls:
        return list(user_tpls[template_name].get("channels", []))
    return []


# ------------------------------------------------------------------
# User-defined network templates. Stored in a separate config file
# so they survive across sessions and can be hand-edited by power
# users. Structure on disk:
#   {
#     "templates": {
#       "MyNet": {
#         "host": "irc.example.com", "port": 6697, "tls": true,
#         "description": "My private friends-only network",
#         "channels": [["#main","main hangout"], ["#dev",""]]
#       }
#     }
#   }
# Each user template appears in the Add-from-template picker
# alongside the bundled ones, sorted alphabetically and labeled
# "[user]" so they're easy to spot.
# ------------------------------------------------------------------
def _user_templates_path() -> Path:
    from .config import CONFIG_DIR
    return CONFIG_DIR / "irc_user_templates.cfg"


def load_user_templates() -> Dict[str, dict]:
    """Return the user-defined network templates keyed by name.
    Each value is a dict with keys host/port/tls/description and
    a 'channels' list of (channel, note) tuples. Returns {} when
    nothing has been defined yet."""
    p = _user_templates_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    for name, d in (raw.get("templates", {}) or {}).items():
        try:
            chans = []
            for entry in d.get("channels", []) or []:
                if isinstance(entry, (list, tuple)) \
                        and len(entry) >= 1:
                    c = str(entry[0])
                    n = str(entry[1]) if len(entry) > 1 else ""
                    chans.append((c, n))
            out[name] = {
                "host": str(d.get("host", "")),
                "port": int(d.get("port", 6697)),
                "tls": bool(d.get("tls", True)),
                "description": str(d.get("description", "")),
                "channels": chans,
            }
        except Exception:
            continue
    return out


def save_user_templates(templates: Dict[str, dict]) -> None:
    from .config import CONFIG_DIR
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"templates": {}}
    for name, d in templates.items():
        payload["templates"][name] = {
            "host": d.get("host", ""),
            "port": int(d.get("port", 6697)),
            "tls": bool(d.get("tls", True)),
            "description": d.get("description", ""),
            "channels": [[c, n] for c, n in d.get("channels", [])],
        }
    _user_templates_path().write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def all_templates() -> list:
    """Combined list of built-in + user templates, used by the
    'From template...' picker. Each entry is a 6-tuple:
        (name, host, port, tls, description, is_user)
    sorted with built-ins first (as authored), then user-defined
    sorted alphabetically. Keeping the built-in order intact means
    the curated list still reads top-to-bottom in popularity."""
    result = []
    for n, host, port, tls, descr in IRC_NETWORK_TEMPLATES:
        result.append((n, host, port, tls, descr, False))
    for name in sorted(load_user_templates().keys(),
                       key=lambda s: s.lower()):
        d = load_user_templates()[name]
        result.append((name, d["host"], d["port"],
                       d["tls"], d["description"], True))
    return result


def make_server_from_template_name(template_name: str,
                                   alias: str,
                                   nickname: str
                                   ) -> Optional[IrcServer]:
    """Build an IrcServer from any template (built-in or user).
    Returns None if the template name doesn't exist."""
    nk = nickname or "quopus"
    # Built-in?
    for n, host, port, tls, _ in IRC_NETWORK_TEMPLATES:
        if n == template_name:
            return IrcServer(
                name=alias or n,
                host=host, port=port, tls=tls,
                nickname=nk,
                alt_nicks=[nk + "_", nk + "__"],
                template_origin=n)
    # User?
    user = load_user_templates().get(template_name)
    if user is not None:
        return IrcServer(
            name=alias or template_name,
            host=user["host"], port=user["port"],
            tls=user["tls"], nickname=nk,
            alt_nicks=[nk + "_", nk + "__"],
            template_origin=template_name)
    return None


# ------------------------------------------------------------------
# Data carriers UI <-> worker
# ------------------------------------------------------------------
@dataclass
class IrcLine:
    """One line in a buffer. 'kind' colour-codes how it renders:
    msg  = a regular PRIVMSG, action = /me, notice = NOTICE,
    event = join/part/quit/mode/etc, error/info/system = misc."""
    timestamp: float
    nick: str
    text: str
    kind: str = "msg"           # msg | action | notice | event | error | info | system
    own: bool = False           # we sent it


@dataclass
class IrcBuffer:
    """A buffer = the contents of one tab in the buffer list. Can
    be a channel (#name), a private query (nick), or a 'server'
    buffer that holds raw server notices and connection info."""
    server: str                 # IrcServer.name
    name: str                   # '#channel' / 'nick' / '*server*'
    is_channel: bool = False
    is_server: bool = False
    is_query: bool = False
    topic: str = ""
    nicks: List[str] = field(default_factory=list)
    lines: List[IrcLine] = field(default_factory=list)
    unread: int = 0


def _buffer_key(server: str, name: str) -> str:
    """Stable identifier for buffer dicts and UI lookups."""
    return f"{server}/{name}"


@dataclass
class TransferInfo:
    """Bookkeeping for a single DCC transfer. The Transfers window
    keeps a list of these and updates them as progress / done /
    failed signals come in."""
    req_id: int
    server: str
    peer: str                       # the other side's nick
    filename: str                   # local basename (display only)
    size: int                       # total bytes (0 if unknown)
    direction: str                  # 'recv' or 'send'
    state: str = "active"           # active | done | failed | cancelled
    bytes_done: int = 0
    path: str = ""                  # final file path on disk
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


# ------------------------------------------------------------------
# Chat logger
# ------------------------------------------------------------------
class _IrcLogger:
    """Writes chat lines to per-buffer log files. Keeps a small
    cache of open file handles so we don't hammer the FS, writes a
    day-banner on rollover, and flushes after every line so logs
    survive a crash. Whether to log is decided by the global
    enabled flag plus per-buffer overrides ("on"/"off"); the dialog
    consults this via `should_log()` for each incoming line.

    Format mirrors the on-screen chat:
        17:32:15  nick     | hello there
        17:32:18  * nick   action text
        17:32:20  <-- nick has joined #chan
        17:32:22  -ChanServ- welcome
    """

    def __init__(self):
        self._handles: dict = {}  # (server, buffer) -> (path, fd, date)
        self._read_cfg()

    def _read_cfg(self):
        from .config import load_config, CONFIG_DIR
        cfg = load_config()
        self._enabled = bool(cfg.get("irc_log_enabled", False))
        self._overrides = dict(cfg.get("irc_log_overrides", {}) or {})
        d = (cfg.get("irc_log_dir") or "").strip()
        self._dir = Path(d) if d else (CONFIG_DIR / "irc_logs")

    def reload(self):
        """Re-read the config (called when settings change)."""
        # Drop any open handles - their paths may now be invalid.
        self.close_all()
        self._read_cfg()

    def set_global(self, enabled: bool, log_dir: str = ""):
        from .config import load_config, save_config
        cfg = load_config()
        cfg["irc_log_enabled"] = bool(enabled)
        if log_dir is not None:
            cfg["irc_log_dir"] = log_dir
        save_config(cfg)
        self.reload()

    def set_override(self, server: str, buffer: str,
                     state: Optional[str]):
        """state in {"on","off",None}. None removes the override."""
        from .config import load_config, save_config
        cfg = load_config()
        ov = dict(cfg.get("irc_log_overrides", {}) or {})
        key = "%s/%s" % (server, buffer)
        if state in ("on", "off"):
            ov[key] = state
        else:
            ov.pop(key, None)
        cfg["irc_log_overrides"] = ov
        save_config(cfg)
        self.reload()

    def should_log(self, server: str, buffer: str) -> bool:
        key = "%s/%s" % (server, buffer)
        ov = self._overrides.get(key)
        if ov == "on":
            return True
        if ov == "off":
            return False
        return self._enabled

    def is_global_enabled(self) -> bool:
        return self._enabled

    def log_dir(self) -> Path:
        return self._dir

    def override_for(self, server: str,
                     buffer: str) -> Optional[str]:
        return self._overrides.get("%s/%s" % (server, buffer))

    # -- file handling --------------------------------------------
    def _safe_name(self, s: str) -> str:
        """Strip path-hostile characters but keep something
        human-readable (channels start with #, queries with a
        nick)."""
        out = []
        for ch in s:
            if ch in "\\/:*?\"<>|\r\n\t":
                out.append("_")
            else:
                out.append(ch)
        cleaned = "".join(out).strip()
        return cleaned or "buffer"

    def _path_for(self, server: str, buffer: str) -> Path:
        return (self._dir / self._safe_name(server)
                / (self._safe_name(buffer) + ".log"))

    def write_line(self, server: str, buffer: str, text: str):
        """Append one already-formatted line to the buffer's log
        file. Adds a date banner whenever the calendar day changes,
        and flushes so the line is on disk even if Quopus crashes."""
        if not self.should_log(server, buffer):
            return
        import time as _t
        today = _t.strftime("%Y-%m-%d")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            p = self._path_for(server, buffer)
            p.parent.mkdir(parents=True, exist_ok=True)
            key = (server, buffer)
            entry = self._handles.get(key)
            need_banner = False
            if entry is None or entry[0] != p:
                # New file or path changed - open it and write a
                # session banner.
                if entry is not None:
                    try:
                        entry[1].close()
                    except Exception:
                        pass
                fd = open(p, "a", encoding="utf-8",
                          errors="replace")
                fd.write(
                    "\n=== Quopus IRC log opened "
                    "%s | %s/%s ===\n"
                    % (_t.strftime("%Y-%m-%d %H:%M:%S"),
                       server, buffer))
                self._handles[key] = (p, fd, today)
            elif entry[2] != today:
                # Same file, new day - write a day-banner.
                need_banner = True
                self._handles[key] = (entry[0], entry[1], today)
            fd = self._handles[key][1]
            if need_banner:
                fd.write("\n--- %s ---\n" % today)
            fd.write(text + "\n")
            fd.flush()
        except Exception:
            # Logging failures must never break the chat UI.
            pass

    def close_all(self):
        for _key, entry in list(self._handles.items()):
            try:
                entry[1].close()
            except Exception:
                pass
        self._handles.clear()


def _format_log_line(line: "IrcLine") -> str:
    """Render an IrcLine the way the chat shows it, plain-text.
    Mirrors the on-screen format from _append_line_to_view minus
    the HTML / colour."""
    import time as _t
    when = _t.strftime("%H:%M:%S",
                       _t.localtime(line.timestamp))
    nick = line.nick or "*"
    text = line.text or ""
    if line.kind == "event":
        return "%s  <-- %s %s" % (when, nick, text)
    if line.kind == "action":
        return "%s  * %s %s" % (when, nick, text)
    if line.kind == "notice":
        return "%s  -%s- %s" % (when, nick, text)
    if line.kind in ("info", "system"):
        return "%s  -- %s" % (when, text)
    if line.kind == "error":
        return "%s  !! %s" % (when, text)
    # Default: regular message
    return "%s  %s | %s" % (when, nick.rjust(14), text)


# ------------------------------------------------------------------
# Worker thread
# ------------------------------------------------------------------
class _IrcWorker(QObject):
    """All pydle clients live here on a dedicated asyncio loop.
    Public methods (called from the UI thread) schedule coroutines
    onto the worker loop; results come back via Qt signals."""

    # Connection lifecycle / chat events
    sig_status = pyqtSignal(str, str)        # server, status text
    sig_error = pyqtSignal(str, str)         # server, error text
    sig_connected = pyqtSignal(str)          # server name
    sig_disconnected = pyqtSignal(str)       # server name
    sig_buffer_added = pyqtSignal(object)    # IrcBuffer
    sig_buffer_removed = pyqtSignal(str, str)  # server, name
    sig_line = pyqtSignal(str, str, object)  # server, buffer, IrcLine
    sig_nicks = pyqtSignal(str, str, list)   # server, channel, nicks
    sig_topic = pyqtSignal(str, str, str)    # server, channel, topic
    sig_nick_changed = pyqtSignal(str, str)  # server, new own nick

    # DCC
    sig_dcc_offer = pyqtSignal(str, str, str, int, int)
    # ^ server, from_nick, filename, size, request_id
    sig_dcc_started_send = pyqtSignal(str, str, str, int, int)
    # ^ server, to_nick, filename, size, request_id (outbound DCC)
    sig_dcc_progress = pyqtSignal(int, int, int)
    # ^ request_id, bytes_done, bytes_total
    sig_dcc_done = pyqtSignal(int, str)      # request_id, filepath
    sig_dcc_failed = pyqtSignal(int, str)    # request_id, error

    def __init__(self):
        super().__init__()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: Dict[str, "_QuopusPydleClient"] = {}
        self._thread = threading.Thread(
            target=self._run_loop, name="IrcWorker", daemon=True)
        self._ready = threading.Event()
        # DCC bookkeeping
        self._dcc_offers: Dict[int, dict] = {}
        self._next_dcc_id = 1
        self._dcc_lock = threading.Lock()

    # -- loop plumbing -------------------------------------------
    def start(self):
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _submit(self, coro) -> Future:
        if self._loop is None:
            f: Future = Future()
            f.set_exception(RuntimeError("worker loop not running"))
            return f
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _done(f):
            exc = f.exception()
            if exc is not None:
                self.sig_error.emit("", "background task: %s" % exc)
        try:
            fut.add_done_callback(_done)
        except Exception:
            pass
        return fut

    def stop(self):
        async def _shutdown():
            for c in list(self._clients.values()):
                try:
                    await c.disconnect(expected=True)
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

    # -- public API ----------------------------------------------
    def connect_server(self, srv: IrcServer):
        """Create + connect a client for the given server profile."""
        self._submit(self._connect(srv))

    async def _connect(self, srv: IrcServer):
        try:
            import pydle
            self.sig_status.emit(srv.name,
                                 "Connecting to %s..." % srv.host)
            # pydle.Client already bundles every feature class
            # we care about (RFC1459, IRCv3 + SASL, TLS, CTCP,
            # WHOX, ISUPPORT). We just need to mix our event
            # bridge on top, and pass SASL credentials as kwargs
            # to the constructor when the user configured them.
            cls = type("QuopusClient",
                       (_QuopusPydleClient, pydle.Client), {})
            base_nick = srv.nickname or "quopus"
            # Compose the fallback chain. If the user provided
            # alt_nicks, use those; otherwise fall back to
            # nick_/nick__ - same scheme mIRC uses when both main
            # and alternate are taken.
            fallbacks = list(srv.alt_nicks) if srv.alt_nicks \
                else [base_nick + "_", base_nick + "__"]
            client = cls(
                nickname=base_nick,
                username=srv.username or base_nick,
                realname=srv.realname or "Quopus IRC",
                fallback_nicknames=fallbacks,
                sasl_username=srv.sasl_username or None,
                sasl_password=srv.sasl_password or None,
            )
            client._worker = self
            client._srv_name = srv.name
            client._autojoin = list(srv.autojoin)
            # Stash a few options the on_connect handler will use.
            client._nickserv_password = srv.nickserv_password
            client._quit_message = srv.quit_message or "Quopus IRC"
            client._auto_reconnect = bool(srv.auto_reconnect)
            client._srv_profile = srv  # for reconnect
            self._clients[srv.name] = client
            # Server buffer so the user can see raw notices / status
            self.sig_buffer_added.emit(IrcBuffer(
                server=srv.name, name="*server*",
                is_server=True))
            await client.connect(
                hostname=srv.host, port=srv.port,
                tls=srv.tls, tls_verify=False,
                password=srv.server_password or None)
        except Exception as e:
            self.sig_error.emit(srv.name, "Connect failed: %s" % e)
            self.sig_status.emit(srv.name, "Disconnected")
            traceback.print_exc()

    def disconnect_server(self, server_name: str):
        self._submit(self._disconnect(server_name))

    async def _disconnect(self, server_name: str):
        c = self._clients.get(server_name)
        if c is None:
            return
        try:
            qmsg = getattr(c, "_quit_message", "Quopus IRC")
            try:
                await c.raw("QUIT :%s\r\n" % qmsg)
            except Exception:
                pass
            await c.disconnect(expected=True)
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Disconnect failed: %s" % e)
        finally:
            self._clients.pop(server_name, None)
            self.sig_disconnected.emit(server_name)

    def join_channel(self, server_name: str, channel: str):
        self._submit(self._join(server_name, channel))

    async def _join(self, server_name: str, channel: str):
        c = self._clients.get(server_name)
        if c is None:
            self.sig_error.emit(server_name, "Not connected")
            return
        try:
            await c.join(channel)
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Join %s failed: %s" % (channel, e))

    def part_channel(self, server_name: str, channel: str,
                     reason: str = ""):
        self._submit(self._part(server_name, channel, reason))

    async def _part(self, server_name: str, channel: str,
                    reason: str):
        c = self._clients.get(server_name)
        if c is None:
            return
        try:
            await c.part(channel, reason or None)
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Part failed: %s" % e)
        self.sig_buffer_removed.emit(server_name, channel)

    def send_message(self, server_name: str, target: str,
                     text: str):
        self._submit(self._send(server_name, target, text))

    async def _send(self, server_name: str, target: str,
                    text: str):
        c = self._clients.get(server_name)
        if c is None:
            self.sig_error.emit(server_name, "Not connected")
            return
        try:
            await c.message(target, text)
            # NOTE: pydle's IRCv3.2 already echoes the message back
            # locally via on_message() when the server doesn't have
            # the echo-message capability, and the server sends the
            # echo itself when it does. So if we also emit a line
            # here, every PRIVMSG ends up rendered TWICE. Don't.
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Send failed: %s" % e)

    def send_action(self, server_name: str, target: str,
                    text: str):
        """/me action."""
        self._submit(self._action(server_name, target, text))

    async def _action(self, server_name: str, target: str,
                      text: str):
        c = self._clients.get(server_name)
        if c is None:
            return
        try:
            await c.message(target, "\x01ACTION %s\x01" % text)
            # Same story as _send: pydle dispatches our own CTCP
            # back to us via on_ctcp_action when the server doesn't
            # provide echo-message. So skip the manual echo here.
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Action failed: %s" % e)

    def send_raw(self, server_name: str, raw_line: str):
        """Send a raw IRC line - used by /quote and unknown commands."""
        self._submit(self._raw(server_name, raw_line))

    async def _raw(self, server_name: str, raw_line: str):
        c = self._clients.get(server_name)
        if c is None:
            return
        try:
            await c.raw(raw_line + "\r\n")
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Raw send failed: %s" % e)

    def change_nick(self, server_name: str, new_nick: str):
        self._submit(self._nick(server_name, new_nick))

    async def _nick(self, server_name: str, new_nick: str):
        c = self._clients.get(server_name)
        if c is None:
            return
        try:
            await c.set_nickname(new_nick)
        except Exception as e:
            self.sig_error.emit(server_name,
                                "Nick change failed: %s" % e)

    # -- DCC accept / receive ------------------------------------
    def accept_dcc(self, req_id: int, dest_path: str):
        """User accepted a DCC SEND offer; receive the file."""
        self._submit(self._dcc_receive(req_id, dest_path))

    def reject_dcc(self, req_id: int):
        with self._dcc_lock:
            self._dcc_offers.pop(req_id, None)

    async def _dcc_receive(self, req_id: int, dest_path: str):
        with self._dcc_lock:
            offer = self._dcc_offers.get(req_id)
        if offer is None:
            return
        host = offer["host"]; port = offer["port"]
        size = offer["size"]
        try:
            reader, writer = await asyncio.open_connection(
                host, port)
            done = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await reader.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    # DCC SEND wants a 4-byte big-endian ack per
                    # block (some senders require it, others
                    # ignore - harmless to always send).
                    try:
                        writer.write(
                            done.to_bytes(4, "big", signed=False))
                        await writer.drain()
                    except Exception:
                        pass
                    self.sig_dcc_progress.emit(
                        req_id, done, size)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self.sig_dcc_done.emit(req_id, dest_path)
        except Exception as e:
            self.sig_dcc_failed.emit(req_id, str(e))
        finally:
            with self._dcc_lock:
                self._dcc_offers.pop(req_id, None)

    # -- DCC SEND outbound --------------------------------------
    def send_file(self, server_name: str, target_nick: str,
                  filepath: str,
                  advertise_ip: str = "",
                  port_range: tuple = (1024, 65535)):
        """Send a file to target_nick on server_name via DCC SEND.

        We open a listening socket, send a CTCP DCC SEND privmsg
        with our advertised IP + port + size, and wait for the
        recipient to connect (with a timeout). Then we stream the
        file and emit progress / done signals.

        advertise_ip: dotted-quad IP the recipient should connect
        back to. If empty we ask the OS for our outward-facing
        address by opening a UDP socket to a public IP - this
        matches what other IRC clients do and works on most LANs
        with NAT (provided port-forwarding is set up); for purely
        Internet-side senders set it explicitly."""
        self._submit(self._dcc_send(
            server_name, target_nick, filepath,
            advertise_ip, port_range))

    async def _dcc_send(self, server_name: str, target_nick: str,
                        filepath: str, advertise_ip: str,
                        port_range: tuple):
        import os as _os
        client = self._clients.get(server_name)
        if client is None:
            self.sig_error.emit(server_name, "Not connected")
            return
        path = Path(filepath)
        if not path.is_file():
            self.sig_error.emit(
                server_name, "DCC send: file not found: %s"
                % filepath)
            return
        size = path.stat().st_size
        fname = path.name

        # Allocate a request id + a listening socket.
        with self._dcc_lock:
            req_id = self._next_dcc_id
            self._next_dcc_id += 1
        # Pick our outward IP.
        ip = advertise_ip.strip()
        if not ip:
            ip = self._guess_outward_ip()
        # Try to bind anywhere in the requested range.
        server = None
        bound_port = None
        # Use a Future that the connection callback completes.
        loop = asyncio.get_event_loop()
        accepted: Future = Future()

        async def _on_conn(reader, writer):
            # Only the first connection counts.
            if accepted.done():
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            try:
                done = 0
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                        done += len(chunk)
                        self.sig_dcc_progress.emit(
                            req_id, done, size)
                        # DCC SEND acks: receiver returns a
                        # 4-byte big-endian total. We read but
                        # don't strictly require it.
                        try:
                            _ = await asyncio.wait_for(
                                reader.read(4), timeout=0.01)
                        except Exception:
                            pass
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                self.sig_dcc_done.emit(req_id, str(path))
                accepted.set_result(True)
            except Exception as e:
                self.sig_dcc_failed.emit(req_id, str(e))
                if not accepted.done():
                    accepted.set_exception(e)

        # Try ports from the range until one binds.
        for port in range(port_range[0], port_range[1] + 1):
            try:
                server = await asyncio.start_server(
                    _on_conn, host="0.0.0.0", port=port)
                bound_port = port
                break
            except Exception:
                continue
        if server is None or bound_port is None:
            self.sig_dcc_failed.emit(
                req_id, "No free port in %d-%d"
                % port_range)
            return

        # Build the CTCP DCC SEND privmsg.
        ip_int = self._dotted_to_int(ip)
        # Quote filename if it contains spaces.
        send_name = ('"%s"' % fname) if " " in fname else fname
        ctcp = "\x01DCC SEND %s %d %d %d\x01" % (
            send_name, ip_int, bound_port, size)
        try:
            await client.message(target_nick, ctcp)
        except Exception as e:
            server.close()
            self.sig_dcc_failed.emit(
                req_id, "Couldn't send DCC offer: %s" % e)
            return

        # Tell the UI an outbound transfer has begun so it can
        # register a TransferInfo and show progress.
        self.sig_dcc_started_send.emit(
            server_name, target_nick, fname, size, req_id)
        self.sig_status.emit(
            server_name,
            "DCC SEND %s to %s (port %d, %s bytes) - waiting..."
            % (fname, target_nick, bound_port, f"{size:,}"))

        # Wait for the recipient to connect, up to 120 s.
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(accepted)),
                timeout=120)
        except asyncio.TimeoutError:
            self.sig_dcc_failed.emit(
                req_id, "DCC SEND timed out (no connection)")
        except Exception:
            pass
        finally:
            try:
                server.close()
                await server.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _dotted_to_int(ip: str) -> int:
        try:
            a, b, c, d = (int(x) for x in ip.split("."))
            return (a << 24) | (b << 16) | (c << 8) | d
        except Exception:
            return 0

    @staticmethod
    def _guess_outward_ip() -> str:
        """Get the IP this host uses to reach the outside world.
        Doesn't actually send anything - just inspects the socket
        the OS picks for an outbound destination."""
        import socket as _s
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            try:
                s.close()
            except Exception:
                pass


# ------------------------------------------------------------------
# pydle Client subclass that translates IRC events into worker
# signals. Kept as a mixin-friendly base; the actual client class
# is built per-connection by composing pydle feature classes with
# this one (so SASL is only mixed in when credentials are set).
# ------------------------------------------------------------------
class _QuopusPydleClient:
    """Bridges pydle events to _IrcWorker signals. `self._worker`
    and `self._srv_name` are attached by the worker before connect.

    pydle's on_* callbacks are coroutines; we keep them lightweight
    and just emit Qt signals. The signals cross to the UI thread
    safely because Qt's queued connection across threads is the
    default for QObject subclasses created in different threads."""

    async def on_connect(self):
        # pydle base
        try:
            await super().on_connect()
        except Exception:
            pass
        w = self._worker
        w.sig_connected.emit(self._srv_name)
        w.sig_status.emit(self._srv_name,
                          "Connected as %s" % self.nickname)
        # NickServ identification (only if SASL wasn't used).
        ns_pw = getattr(self, "_nickserv_password", "") or ""
        if ns_pw and not getattr(self, "sasl_username", None):
            try:
                await self.message(
                    "NickServ", "IDENTIFY " + ns_pw)
                w.sig_status.emit(
                    self._srv_name,
                    "Sent IDENTIFY to NickServ")
            except Exception as e:
                w.sig_error.emit(
                    self._srv_name,
                    "NickServ identify failed: %s" % e)
        # Autojoin saved channels.
        for ch in getattr(self, "_autojoin", []):
            try:
                await self.join(ch)
            except Exception as e:
                w.sig_error.emit(self._srv_name,
                                 "Autojoin %s: %s" % (ch, e))

    async def on_disconnect(self, expected):
        try:
            await super().on_disconnect(expected)
        except Exception:
            pass
        w = self._worker
        w.sig_disconnected.emit(self._srv_name)
        w.sig_status.emit(self._srv_name, "Disconnected")
        # Auto-reconnect on unexpected drops (unless the user
        # actively asked to disconnect).
        if (not expected
                and getattr(self, "_auto_reconnect", False)):
            srv = getattr(self, "_srv_profile", None)
            if srv is not None:
                w.sig_status.emit(
                    self._srv_name,
                    "Auto-reconnect in 15 seconds...")
                # Defer the reconnect rather than retry-in-place
                # so we don't tangle this disconnect handler.
                async def _retry():
                    await asyncio.sleep(15)
                    try:
                        await w._connect(srv)
                    except Exception:
                        pass
                asyncio.create_task(_retry())

    async def on_join(self, channel, user):
        try:
            await super().on_join(channel, user)
        except Exception:
            pass
        w = self._worker
        if user == self.nickname:
            # We just joined - open a buffer.
            w.sig_buffer_added.emit(IrcBuffer(
                server=self._srv_name, name=channel,
                is_channel=True))
        else:
            line = IrcLine(timestamp=time.time(), nick=user,
                           text="has joined %s" % channel,
                           kind="event")
            w.sig_line.emit(self._srv_name, channel, line)
        # Refresh nick list (pydle keeps channel.users).
        try:
            users = list(self.channels[channel]["users"])
            w.sig_nicks.emit(self._srv_name, channel, sorted(users))
        except Exception:
            pass

    async def on_part(self, channel, user, message=None):
        try:
            await super().on_part(channel, user, message)
        except Exception:
            pass
        w = self._worker
        if user == self.nickname:
            w.sig_buffer_removed.emit(self._srv_name, channel)
        else:
            line = IrcLine(
                timestamp=time.time(), nick=user,
                text="has left %s%s" % (
                    channel, (" (%s)" % message) if message else ""),
                kind="event")
            w.sig_line.emit(self._srv_name, channel, line)
        try:
            users = list(self.channels[channel]["users"])
            w.sig_nicks.emit(self._srv_name, channel, sorted(users))
        except Exception:
            pass

    async def on_quit(self, user, message=None):
        try:
            await super().on_quit(user, message)
        except Exception:
            pass
        w = self._worker
        text = "has quit" + ((" (%s)" % message) if message else "")
        # We don't know which channels they shared; broadcast a
        # short event line to the server buffer.
        line = IrcLine(timestamp=time.time(), nick=user,
                       text=text, kind="event")
        w.sig_line.emit(self._srv_name, "*server*", line)

    async def on_message(self, target, source, message):
        try:
            await super().on_message(target, source, message)
        except Exception:
            pass
        w = self._worker
        # NOTE: CTCP messages (ACTION, DCC, VERSION, ...) do NOT
        # arrive here in pydle 1.x - they're dispatched separately
        # via on_ctcp / on_ctcp_<type>. We handle /me via
        # on_ctcp_action and DCC via on_ctcp_dcc further down.
        is_own = (source == self.nickname)
        kind = "msg"
        text = message
        # Private query? Open a buffer for the sender if needed.
        if target == self.nickname:
            # Incoming DM - the buffer is named after the sender.
            buf = source
            w.sig_buffer_added.emit(IrcBuffer(
                server=self._srv_name, name=source, is_query=True))
        else:
            # Channel message OR our own echo to a channel/query.
            buf = target
        line = IrcLine(timestamp=time.time(), nick=source,
                       text=text, kind=kind, own=is_own)
        w.sig_line.emit(self._srv_name, buf, line)

    async def on_ctcp_action(self, source, target, contents):
        """CTCP ACTION = /me - pydle splits it out so on_message
        never sees it. Render as an action line in the right buf."""
        try:
            await super().on_ctcp_action(source, target, contents)
        except Exception:
            pass
        w = self._worker
        is_own = (source == self.nickname)
        if target == self.nickname:
            buf = source
            w.sig_buffer_added.emit(IrcBuffer(
                server=self._srv_name, name=source, is_query=True))
        else:
            buf = target
        line = IrcLine(timestamp=time.time(), nick=source,
                       text=contents or "", kind="action",
                       own=is_own)
        w.sig_line.emit(self._srv_name, buf, line)

    async def on_ctcp_dcc(self, source, target, contents):
        """DCC offers arrive as CTCP messages. pydle gives us
        `contents` already stripped of the leading 'DCC ' marker.
        Example contents string for a SEND offer:
            'SEND filename 3232235521 49152 12345'
        or with a quoted filename:
            'SEND "my file.zip" 3232235521 49152 12345'"""
        try:
            await super().on_ctcp_dcc(source, target, contents)
        except Exception:
            pass
        # Log so we can see the raw text if parsing fails.
        try:
            self._handle_dcc_request(source, "DCC " + (contents or ""))
        except Exception as e:
            self._worker.sig_error.emit(
                self._srv_name, "DCC parse failed: %s" % e)

    async def on_notice(self, target, source, message):
        try:
            await super().on_notice(target, source, message)
        except Exception:
            pass
        w = self._worker
        # Notices from the server itself go to *server*.
        if source in ("", None) or source == self.network:
            buf = "*server*"
        elif target == self.nickname:
            buf = source
            w.sig_buffer_added.emit(IrcBuffer(
                server=self._srv_name, name=source, is_query=True))
        else:
            buf = target
        line = IrcLine(timestamp=time.time(), nick=source,
                       text=message, kind="notice")
        w.sig_line.emit(self._srv_name, buf, line)

    async def on_nick_change(self, old, new):
        try:
            await super().on_nick_change(old, new)
        except Exception:
            pass
        w = self._worker
        line = IrcLine(timestamp=time.time(), nick=old,
                       text="is now known as %s" % new,
                       kind="event")
        w.sig_line.emit(self._srv_name, "*server*", line)
        # If WE changed nick, tell the UI.
        if new == self.nickname:
            w.sig_nick_changed.emit(self._srv_name, new)

    async def on_topic_change(self, channel, message, by):
        try:
            await super().on_topic_change(channel, message, by)
        except Exception:
            pass
        self._worker.sig_topic.emit(
            self._srv_name, channel, message or "")
        line = IrcLine(
            timestamp=time.time(), nick=by or "*",
            text="changed topic: %s" % (message or ""),
            kind="event")
        self._worker.sig_line.emit(self._srv_name, channel, line)

    async def on_mode_change(self, channel, modes, nick):
        try:
            await super().on_mode_change(channel, modes, nick)
        except Exception:
            pass
        line = IrcLine(
            timestamp=time.time(), nick=nick or "*",
            text="sets mode %s" % " ".join(modes),
            kind="event")
        self._worker.sig_line.emit(self._srv_name, channel, line)

    # -- DCC ---------------------------------------------------
    def _handle_dcc_request(self, source: str, dcc_text: str):
        """Parse 'DCC SEND <filename> <ip> <port> <size>' and pass
        it up so the user can accept or reject. The actual TCP
        transfer is handled in a separate coroutine when they say
        yes.

        Real-world DCC SEND lines from XDCC bots vary a lot:
          DCC SEND filename.mkv 3232235521 49152 12345
          DCC SEND "my file.zip" 3232235521 49152 12345
          DCC SEND [bot]-pack.mkv 3232235521 0 12345 token   (passive)
        We log the raw text into the active server buffer so when
        something fails to parse, the user can see why."""
        w = self._worker
        try:
            parts = self._split_dcc(dcc_text)
            if len(parts) < 5 or parts[0].upper() != "DCC":
                # Not enough fields - surface it so the user knows
                # something tried to arrive.
                w.sig_line.emit(
                    self._srv_name, "*server*",
                    IrcLine(
                        timestamp=time.time(), nick="*",
                        text="Unparseable DCC offer from %s: %r"
                        % (source, dcc_text),
                        kind="error"))
                return
            kind = parts[1].upper()
            if kind != "SEND":
                # We don't handle CHAT / RESUME / ACCEPT yet, but
                # at least let the user see the bot tried.
                w.sig_line.emit(
                    self._srv_name, "*server*",
                    IrcLine(
                        timestamp=time.time(), nick="*",
                        text="Unsupported DCC type %r from %s "
                             "(only SEND is handled)"
                        % (kind, source),
                        kind="info"))
                return
            filename = parts[2]
            try:
                ip_int = int(parts[3])
                port = int(parts[4])
            except ValueError:
                w.sig_line.emit(
                    self._srv_name, "*server*",
                    IrcLine(
                        timestamp=time.time(), nick="*",
                        text="DCC SEND from %s has bad ip/port: %r"
                        % (source, dcc_text),
                        kind="error"))
                return
            size = 0
            if len(parts) > 5:
                try:
                    size = int(parts[5])
                except ValueError:
                    size = 0
            # Passive (reverse) DCC uses port=0 and a token in
            # parts[6]. We don't implement reverse DCC yet, so tell
            # the user instead of silently dropping.
            if port == 0:
                w.sig_line.emit(
                    self._srv_name, "*server*",
                    IrcLine(
                        timestamp=time.time(), nick="*",
                        text="DCC SEND from %s uses passive/reverse "
                             "DCC (port=0); not yet supported. "
                             "Configure the bot for active DCC."
                        % source,
                        kind="error"))
                return
            host = "%d.%d.%d.%d" % (
                (ip_int >> 24) & 0xff, (ip_int >> 16) & 0xff,
                (ip_int >> 8) & 0xff, ip_int & 0xff)
            with w._dcc_lock:
                req_id = w._next_dcc_id
                w._next_dcc_id += 1
                w._dcc_offers[req_id] = {
                    "server": self._srv_name, "from": source,
                    "filename": filename, "host": host,
                    "port": port, "size": size}
            # Visible log line in the server buffer too - useful
            # when auto-receive accepts something quietly.
            w.sig_line.emit(
                self._srv_name, "*server*",
                IrcLine(
                    timestamp=time.time(), nick="*",
                    text="DCC SEND offer: %s from %s "
                         "(%s bytes, host=%s port=%d, req=%d)"
                    % (filename, source, f"{size:,}",
                       host, port, req_id),
                    kind="info"))
            w.sig_dcc_offer.emit(
                self._srv_name, source, filename, size, req_id)
        except Exception as e:
            w.sig_error.emit(
                self._srv_name, "Bad DCC offer: %s" % e)
            w.sig_line.emit(
                self._srv_name, "*server*",
                IrcLine(
                    timestamp=time.time(), nick="*",
                    text="DCC parse exception: %s for %r"
                    % (e, dcc_text),
                    kind="error"))

    @staticmethod
    def _split_dcc(s: str) -> list:
        """Tokenise a DCC line, respecting quoted filenames."""
        out, cur, in_q = [], [], False
        for ch in s:
            if ch == '"':
                in_q = not in_q
                continue
            if ch == " " and not in_q:
                if cur:
                    out.append("".join(cur)); cur = []
            else:
                cur.append(ch)
        if cur:
            out.append("".join(cur))
        return out


# ------------------------------------------------------------------
# UI - WeeChat-style three-column layout
# ------------------------------------------------------------------
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QTextCursor, QColor
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QTextEdit, QTextBrowser,
    QLineEdit, QPushButton, QLabel, QInputDialog, QFileDialog,
    QMessageBox, QFormLayout, QCheckBox, QSpinBox, QComboBox,
    QMenu, QDialogButtonBox, QProgressDialog,
)
from .palette import C
from .config import scaled_font_px


def _mono(px=11) -> QFont:
    f = QFont("Topaz", scaled_font_px(px))
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


# WeeChat-ish palette. Dark blue background, light fonts, with
# the characteristic green sender names and red event lines.
_BG = "#001640"            # very dark blue
_FG = "#d8d8d8"
_HEAD_BG = "#163880"       # status header bar
_HEAD_FG = "#f0f0f0"
_TIME_FG = "#88ccff"
_NICK_FG = "#62ff62"       # bright green - own & generic nick
_EVENT_FG = "#ff6464"      # red for joins/parts/quits
_NOTICE_FG = "#ffcc66"     # amber for notices
_ACTION_FG = "#cc88ff"     # purple for /me
_OWN_NICK_FG = "#ffff66"   # yellow for our own nick
_PIPE_FG = "#888888"


class IrcDialog(QDialog):
    """Embedded IRC client. WeeChat-style:
       [buffers] | [chat]      | [nicks]
                   [input]
    Each row in 'buffers' is a server or one of its channels/queries.
    The active buffer's lines show in the chat area; if it's a
    channel, its members fill the nick list."""

    def __init__(self, parent=None, lister=None):
        super().__init__(parent)
        self._lister = lister
        self._worker: Optional[_IrcWorker] = None
        # All open buffers keyed by "<server>/<name>".
        self._buffers: Dict[str, IrcBuffer] = {}
        # Currently shown buffer key, or None.
        self._active: Optional[str] = None
        # Per-server: our current nickname.
        self._own_nick: Dict[str, str] = {}
        # Central DCC transfer register: req_id -> TransferInfo.
        # Keeps active, finished and failed transfers so the
        # Transfers window shows the full session history.
        self._transfers: Dict[int, TransferInfo] = {}
        # Open Transfers dialog (or None) so we can refresh it.
        self._transfers_dlg: Optional[QDialog] = None
        # Chat logger - writes per-buffer .log files when enabled.
        self._logger = _IrcLogger()

        self.setWindowTitle("IRC - Quopus Commander")
        self.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(1100, 720)
        self._build_ui()
        QTimer.singleShot(50, self._auto_start)

    # -- UI ------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # Top header bar - WeeChat shows the server / channel /
        # topic banner here.
        self.lbl_head = QLabel("Not connected")
        self.lbl_head.setFont(_mono(11))
        self.lbl_head.setStyleSheet(
            f"QLabel {{ background-color: {_HEAD_BG}; "
            f"color: {_HEAD_FG}; padding: 3px 6px; }}")
        root.addWidget(self.lbl_head)

        # Toolbar row - the main actions live up here so they're
        # obvious and easy to reach. Left = connect / config /
        # transfers / nick; right = buffer-level actions.
        tb = QHBoxLayout()
        tb.setContentsMargins(0, 2, 0, 2)
        tb.setSpacing(4)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setFont(_mono(10))
        self.btn_connect.setToolTip(
            "Pick a saved server and connect to it")
        self.btn_connect.clicked.connect(self._show_connect_menu)
        tb.addWidget(self.btn_connect)
        self.btn_servers = QPushButton("Servers")
        self.btn_servers.setFont(_mono(10))
        self.btn_servers.setToolTip(
            "Edit server profiles, nicks, SASL, DCC, ...")
        self.btn_servers.clicked.connect(self._edit_servers)
        tb.addWidget(self.btn_servers)
        self.btn_transfers = QPushButton("Transfers")
        self.btn_transfers.setFont(_mono(10))
        self.btn_transfers.setToolTip(
            "Show DCC file transfers (incoming and outgoing)")
        self.btn_transfers.clicked.connect(self._show_transfers)
        tb.addWidget(self.btn_transfers)
        self.btn_nick = QPushButton("Nick")
        self.btn_nick.setFont(_mono(10))
        self.btn_nick.setToolTip(
            "Change your nickname on the active server")
        self.btn_nick.clicked.connect(self._prompt_change_nick)
        tb.addWidget(self.btn_nick)
        self.btn_logs = QPushButton("Logs")
        self.btn_logs.setFont(_mono(10))
        self.btn_logs.setToolTip(
            "Chat logging settings - on/off and storage folder")
        self.btn_logs.clicked.connect(self._show_log_settings)
        tb.addWidget(self.btn_logs)
        tb.addStretch(1)
        # Right-hand side: buffer-level actions
        self.btn_join = QPushButton("Join channel")
        self.btn_join.setFont(_mono(10))
        self.btn_join.setToolTip(
            "Join a channel on the active server")
        self.btn_join.clicked.connect(self._prompt_join)
        tb.addWidget(self.btn_join)
        self.btn_close_buf = QPushButton("Close buffer")
        self.btn_close_buf.setFont(_mono(10))
        self.btn_close_buf.setToolTip(
            "Close the currently active buffer")
        self.btn_close_buf.clicked.connect(self._close_active_buffer)
        tb.addWidget(self.btn_close_buf)
        root.addLayout(tb)

        # Main three-pane row
        split = QSplitter(Qt.Orientation.Horizontal)

        # Left: buffer list
        self.list_buffers = QListWidget()
        self.list_buffers.setFont(_mono(11))
        self.list_buffers.setMinimumWidth(160)
        self.list_buffers.setStyleSheet(
            f"QListWidget {{ background-color: {_BG}; "
            f"color: {_FG}; border: none; }} "
            f"QListWidget::item:selected {{ "
            f"background-color: #b22222; color: white; }}")
        self.list_buffers.itemClicked.connect(self._on_buffer_clicked)
        self.list_buffers.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_buffers.customContextMenuRequested.connect(
            self._on_buffer_context)
        split.addWidget(self.list_buffers)

        # Middle: chat
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(2)
        self.view = QTextBrowser()
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.setFont(_mono(11))
        self.view.setStyleSheet(
            f"QTextBrowser {{ background-color: {_BG}; "
            f"color: {_FG}; border: none; }}")
        mv.addWidget(self.view, 1)
        # Input + nick label
        bottom = QHBoxLayout()
        self.lbl_nick = QLabel("[?]")
        self.lbl_nick.setFont(_mono(11))
        self.lbl_nick.setStyleSheet(
            f"QLabel {{ color: {_OWN_NICK_FG}; "
            f"background-color: {_BG}; padding: 2px 6px; }}")
        bottom.addWidget(self.lbl_nick)
        self.edit = QLineEdit()
        self.edit.setFont(_mono(11))
        self.edit.setPlaceholderText(
            "Type message or /command (/help for list)")
        self.edit.setStyleSheet(
            f"QLineEdit {{ background-color: {_BG}; "
            f"color: {_FG}; border: 1px solid #444; padding: 3px; }}")
        self.edit.returnPressed.connect(self._on_send)
        bottom.addWidget(self.edit, 1)
        mv.addLayout(bottom)
        split.addWidget(mid)

        # Right: nick list
        self.list_nicks = QListWidget()
        self.list_nicks.setFont(_mono(11))
        self.list_nicks.setMinimumWidth(140)
        self.list_nicks.setStyleSheet(
            f"QListWidget {{ background-color: {_BG}; "
            f"color: {_FG}; border: none; }}")
        self.list_nicks.itemDoubleClicked.connect(
            self._on_nick_double_clicked)
        split.addWidget(self.list_nicks)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 5)
        split.setStretchFactor(2, 1)
        root.addWidget(split, 1)

        # Bottom status strip - shows network / transfer totals.
        # Small, unobtrusive. The main buttons are now in the
        # toolbar at the top.
        self.lbl_foot = QLabel("Ready.")
        self.lbl_foot.setFont(_mono(10))
        self.lbl_foot.setStyleSheet(
            f"QLabel {{ background-color: {_HEAD_BG}; "
            f"color: {_HEAD_FG}; padding: 2px 6px; }}")
        root.addWidget(self.lbl_foot)

    # -- start-up -----------------------------------------------
    def _auto_start(self):
        if not pydle_available():
            QMessageBox.warning(
                self, "IRC",
                "The 'pydle' package is not installed.\n\n"
                "Install it with:\n    pip install pydle\n\n"
                "then reopen this window.")
            self.lbl_head.setText("pydle not installed")
            return
        self._worker = _IrcWorker()
        self._wire()
        self._worker.start()
        # Autoconnect any servers flagged as such.
        for srv in load_irc_config().values():
            if srv.autoconnect:
                self._worker.connect_server(srv)

    def _wire(self):
        w = self._worker
        w.sig_status.connect(self._on_status)
        w.sig_error.connect(self._on_error)
        w.sig_connected.connect(self._on_connected)
        w.sig_disconnected.connect(self._on_disconnected)
        w.sig_buffer_added.connect(self._on_buffer_added)
        w.sig_buffer_removed.connect(self._on_buffer_removed)
        w.sig_line.connect(self._on_line)
        w.sig_nicks.connect(self._on_nicks)
        w.sig_topic.connect(self._on_topic)
        w.sig_nick_changed.connect(self._on_nick_changed)
        w.sig_dcc_offer.connect(self._on_dcc_offer)
        w.sig_dcc_started_send.connect(self._on_dcc_started_send)
        w.sig_dcc_progress.connect(self._on_dcc_progress)
        w.sig_dcc_done.connect(self._on_dcc_done)
        w.sig_dcc_failed.connect(self._on_dcc_failed)

    # -- signal handlers ----------------------------------------
    def _on_status(self, server: str, text: str):
        # Always show in the header. If we have a buffer for this
        # server, also drop an info line into the server buffer.
        if server:
            self.lbl_head.setText("[%s] %s" % (server, text))
            self._append_system(server, "*server*", text)
        else:
            self.lbl_head.setText(text)

    def _on_error(self, server: str, text: str):
        if server:
            self._append_system(server, "*server*",
                                "ERROR: " + text, kind="error")
        # Header always shows the latest issue.
        self.lbl_head.setText(
            ("[%s] " % server if server else "") + text)

    def _on_connected(self, server: str):
        # Nick label refreshes once on_nick_change fires too.
        pass

    def _on_disconnected(self, server: str):
        # Mark the server buffer with a system line.
        self._append_system(server, "*server*",
                            "Disconnected.", kind="error")

    def _on_buffer_added(self, buf: IrcBuffer):
        key = _buffer_key(buf.server, buf.name)
        if key in self._buffers:
            return
        self._buffers[key] = buf
        item = QListWidgetItem(self._buffer_label(buf))
        item.setData(Qt.ItemDataRole.UserRole, key)
        self.list_buffers.addItem(item)
        # If nothing is currently active, switch to this buffer.
        if self._active is None:
            self.list_buffers.setCurrentItem(item)
            self._switch_to(key)

    def _on_buffer_removed(self, server: str, name: str):
        key = _buffer_key(server, name)
        self._buffers.pop(key, None)
        for i in range(self.list_buffers.count()):
            it = self.list_buffers.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                self.list_buffers.takeItem(i)
                break
        if self._active == key:
            self._active = None
            self.view.clear()
            self.list_nicks.clear()
            self.lbl_head.setText("(no buffer)")

    def _on_line(self, server: str, name: str, line: IrcLine):
        key = _buffer_key(server, name)
        buf = self._buffers.get(key)
        if buf is None:
            # If a line arrives for a buffer we don't have, open
            # one - happens for incoming queries.
            buf = IrcBuffer(server=server, name=name,
                            is_query=(not name.startswith("#")
                                      and name != "*server*"))
            self._on_buffer_added(buf)
        buf.lines.append(line)
        # Persist to disk if logging is enabled for this buffer.
        self._logger.write_line(
            server, name, _format_log_line(line))
        if self._active == key:
            self._append_line_to_view(line, server)
        else:
            buf.unread += 1
            self._refresh_buffer_label(key)

    def _on_nicks(self, server: str, channel: str, nicks: list):
        key = _buffer_key(server, channel)
        buf = self._buffers.get(key)
        if buf is None:
            return
        buf.nicks = nicks
        if self._active == key:
            self._refresh_nicks(nicks)

    def _on_topic(self, server: str, channel: str, topic: str):
        key = _buffer_key(server, channel)
        buf = self._buffers.get(key)
        if buf is None:
            return
        buf.topic = topic
        if self._active == key:
            self._refresh_header()

    def _on_nick_changed(self, server: str, new: str):
        self._own_nick[server] = new
        if self._active and self._active.startswith(server + "/"):
            self.lbl_nick.setText("[%s]" % new)

    # -- buffer switching ---------------------------------------
    def _on_buffer_clicked(self, item: QListWidgetItem):
        key = item.data(Qt.ItemDataRole.UserRole)
        self._switch_to(key)

    def _switch_to(self, key: str):
        buf = self._buffers.get(key)
        if buf is None:
            return
        self._active = key
        buf.unread = 0
        self._refresh_buffer_label(key)
        self.view.clear()
        for line in buf.lines[-500:]:  # cap render to last 500
            self._append_line_to_view(line, buf.server)
        self._refresh_nicks(buf.nicks if buf.is_channel else [])
        self._refresh_header()
        # Update own nick label
        nk = self._own_nick.get(buf.server, "?")
        self.lbl_nick.setText("[%s]" % nk)
        # Focus input.
        self.edit.setFocus()

    def _refresh_header(self):
        if self._active is None:
            self.lbl_head.setText("(no buffer)")
            return
        buf = self._buffers[self._active]
        parts = [buf.server, buf.name]
        if buf.topic:
            parts.append("- %s" % buf.topic)
        self.lbl_head.setText(" | ".join(parts))

    def _refresh_nicks(self, nicks: list):
        self.list_nicks.clear()
        for n in nicks:
            self.list_nicks.addItem(n)

    def _buffer_label(self, buf: IrcBuffer) -> str:
        # WeeChat-style "server.#channel" style names; we just show
        # the leaf and decorate with unread count.
        leaf = buf.name
        if buf.is_server:
            leaf = "(%s)" % buf.server
        if buf.unread:
            leaf = "%s [%d]" % (leaf, buf.unread)
        return leaf

    def _refresh_buffer_label(self, key: str):
        buf = self._buffers.get(key)
        if buf is None:
            return
        for i in range(self.list_buffers.count()):
            it = self.list_buffers.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                it.setText(self._buffer_label(buf))
                break

    # -- rendering one chat line in the WeeChat look -----------
    def _append_line_to_view(self, line: IrcLine, server: str):
        import html as _html
        when = time.strftime("%H:%M:%S",
                             time.localtime(line.timestamp))
        nick = line.nick
        text = line.text
        own_nick = self._own_nick.get(server, "")
        # Colour selection
        if line.kind == "event":
            nick_col = _EVENT_FG
            body = ('<span style="color:%s;">&lt;-- %s %s</span>'
                    % (_EVENT_FG, _html.escape(nick),
                       _html.escape(text)))
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">|</span> '
                    % (_TIME_FG, when, _PIPE_FG))
            html = head + body
        elif line.kind == "action":
            nick_col = _ACTION_FG
            body = ('<span style="color:%s;">* %s %s</span>'
                    % (_ACTION_FG, _html.escape(nick),
                       _html.escape(text)))
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">|</span> '
                    % (_TIME_FG, when, _PIPE_FG))
            html = head + body
        elif line.kind == "notice":
            nick_col = _NOTICE_FG
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">|</span> '
                    % (_TIME_FG, when, _PIPE_FG))
            body = ('<span style="color:%s;">-%s-</span> %s'
                    % (_NOTICE_FG, _html.escape(nick),
                       _html.escape(text)))
            html = head + body
        elif line.kind in ("info", "system"):
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">|</span> '
                    % (_TIME_FG, when, _PIPE_FG))
            body = ('<span style="color:%s;">-- %s</span>'
                    % (_NOTICE_FG, _html.escape(text)))
            html = head + body
        elif line.kind == "error":
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">|</span> '
                    % (_TIME_FG, when, _PIPE_FG))
            body = ('<span style="color:%s;">!! %s</span>'
                    % (_EVENT_FG, _html.escape(text)))
            html = head + body
        else:
            # Regular PRIVMSG
            nick_col = (_OWN_NICK_FG if (line.own or nick == own_nick)
                        else _NICK_FG)
            head = ('<span style="color:%s;">%s</span> '
                    '<span style="color:%s;">%s</span>'
                    '<span style="color:%s;"> | </span>'
                    % (_TIME_FG, when, nick_col,
                       _html.escape(nick).rjust(14),
                       _PIPE_FG))
            html = head + _html.escape(text)
        # Append. We keep the cursor at end and let the scroll
        # follow.
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.view.setTextCursor(cur)
        self.view.insertHtml("<div>" + html + "</div>")
        # Force a newline between lines.
        self.view.insertHtml("<br>")
        sb = self.view.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def _append_system(self, server: str, name: str, text: str,
                       kind: str = "info"):
        line = IrcLine(timestamp=time.time(), nick="*",
                       text=text, kind=kind)
        # Make sure a buffer exists.
        key = _buffer_key(server, name)
        if key not in self._buffers:
            self._buffers[key] = IrcBuffer(
                server=server, name=name,
                is_server=(name == "*server*"))
            item = QListWidgetItem(self._buffer_label(
                self._buffers[key]))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list_buffers.addItem(item)
        self._buffers[key].lines.append(line)
        self._logger.write_line(
            server, name, _format_log_line(line))
        if self._active == key:
            self._append_line_to_view(line, server)

    # -- input / commands ---------------------------------------
    def _on_send(self):
        text = self.edit.text()
        if not text:
            return
        self.edit.clear()
        if self._active is None:
            self._set_head_error("No buffer selected")
            return
        buf = self._buffers[self._active]
        server = buf.server
        if text.startswith("/"):
            self._handle_command(server, buf, text)
        else:
            # Plain message to current buffer (must be channel/query)
            if buf.is_server:
                self._set_head_error(
                    "This is the server buffer - use /msg or /join")
                return
            self._worker.send_message(server, buf.name, text)

    def _handle_command(self, server: str, buf: IrcBuffer,
                        text: str):
        # /command [args...]
        parts = text[1:].split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "help":
            self._show_help()
        elif cmd == "join":
            if not arg:
                self._set_head_error("/join #channel")
                return
            self._worker.join_channel(server, arg.split()[0])
        elif cmd == "part":
            ch = arg.split(" ", 1)[0] if arg else buf.name
            reason = arg.split(" ", 1)[1] if " " in arg else ""
            if not ch.startswith("#"):
                self._set_head_error("/part needs a channel")
                return
            self._worker.part_channel(server, ch, reason)
        elif cmd in ("msg", "query"):
            sp = arg.split(" ", 1)
            if len(sp) < 2:
                self._set_head_error("/msg <nick> <text>")
                return
            target, msg = sp[0], sp[1]
            self._worker.send_message(server, target, msg)
            # Open query buffer so the conversation continues.
            self._on_buffer_added(IrcBuffer(
                server=server, name=target, is_query=True))
        elif cmd == "me":
            if buf.is_server:
                self._set_head_error("/me needs a channel or query")
                return
            self._worker.send_action(server, buf.name, arg)
        elif cmd == "nick":
            if not arg:
                self._set_head_error("/nick <newname>")
                return
            self._worker.change_nick(server, arg.split()[0])
        elif cmd == "quit":
            self._worker.disconnect_server(server)
        elif cmd == "quote":
            self._worker.send_raw(server, arg)
        elif cmd == "topic":
            # /topic           = read topic
            # /topic new text  = set topic
            if not buf.is_channel:
                self._set_head_error("/topic needs a channel")
                return
            if not arg:
                self._set_head_error(
                    "Topic: " + (buf.topic or "(none)"))
            else:
                self._worker.send_raw(
                    server, "TOPIC %s :%s" % (buf.name, arg))
        elif cmd == "dcc":
            # /dcc send <nick> <path>  -> send a file via DCC SEND
            sp = arg.split(" ", 2)
            if len(sp) >= 3 and sp[0].lower() == "send":
                target = sp[1]
                filepath = sp[2].strip().strip('"').strip("'")
                if not filepath:
                    self._set_head_error(
                        "/dcc send <nick> <filepath>")
                    return
                self._worker.send_file(server, target, filepath)
                self._append_system(
                    server, buf.name,
                    "Offering DCC SEND of %s to %s..."
                    % (filepath, target))
            else:
                self._set_head_error(
                    "/dcc send <nick> <filepath>")
        elif cmd == "close":
            self._close_active_buffer()
        else:
            # Unknown - send as raw IRC command.
            self._worker.send_raw(server, text[1:])

    def _show_help(self):
        msg = (
            "Commands:\n"
            "  /join #channel             join a channel\n"
            "  /part [#chan] [reason]     leave a channel\n"
            "  /msg <nick> <text>         private message\n"
            "  /me <text>                 /me action\n"
            "  /nick <newname>            change your nickname\n"
            "  /topic [new text]          show or set channel topic\n"
            "  /dcc send <nick> <file>    send a file via DCC SEND\n"
            "  /quit                      disconnect this server\n"
            "  /quote <raw IRC line>      send a raw IRC command\n"
            "  /close                     close this buffer\n"
            "  /help                      this list\n"
        )
        QMessageBox.information(self, "IRC commands", msg)

    def _set_head_error(self, text: str):
        self.lbl_head.setText("! " + text)

    # -- buffer-list context menu -------------------------------
    def _on_buffer_context(self, pos):
        it = self.list_buffers.itemAt(pos)
        if it is None:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        buf = self._buffers.get(key)
        if buf is None:
            return
        menu = QMenu(self.list_buffers)
        act_switch = menu.addAction("Switch to")
        act_close = menu.addAction("Close")
        act_disconnect = None
        if buf.is_server:
            act_disconnect = menu.addAction("Disconnect server")
        # Logging submenu
        menu.addSeparator()
        log_menu = menu.addMenu("Logging")
        # Show the effective decision next to each option.
        eff = "ON" if self._logger.should_log(
            buf.server, buf.name) else "off"
        ov = self._logger.override_for(buf.server, buf.name)
        glob = "on" if self._logger.is_global_enabled() else "off"
        act_log_default = log_menu.addAction(
            "Use global (%s) %s"
            % (glob, "  *" if ov is None else ""))
        act_log_on = log_menu.addAction(
            "Force ON for this buffer%s"
            % ("  *" if ov == "on" else ""))
        act_log_off = log_menu.addAction(
            "Force OFF for this buffer%s"
            % ("  *" if ov == "off" else ""))
        log_menu.addSeparator()
        act_log_open = log_menu.addAction(
            "Open log file in lister")
        log_menu.setTitle("Logging  [%s]" % eff)

        chosen = menu.exec(
            self.list_buffers.viewport().mapToGlobal(pos))
        if chosen is act_switch:
            self.list_buffers.setCurrentItem(it)
            self._switch_to(key)
        elif chosen is act_close:
            self._close_buffer(key)
        elif act_disconnect is not None and chosen is act_disconnect:
            self._worker.disconnect_server(buf.server)
        elif chosen is act_log_default:
            self._logger.set_override(buf.server, buf.name, None)
            self._set_head_error(
                "Logging for %s/%s: follow global setting"
                % (buf.server, buf.name))
        elif chosen is act_log_on:
            self._logger.set_override(buf.server, buf.name, "on")
            self._set_head_error(
                "Logging for %s/%s: ON" % (buf.server, buf.name))
        elif chosen is act_log_off:
            self._logger.set_override(buf.server, buf.name, "off")
            self._set_head_error(
                "Logging for %s/%s: OFF" % (buf.server, buf.name))
        elif chosen is act_log_open:
            self._open_log_for(buf.server, buf.name)

    def _close_active_buffer(self):
        if self._active:
            self._close_buffer(self._active)

    def _close_buffer(self, key: str):
        buf = self._buffers.get(key)
        if buf is None:
            return
        if buf.is_channel and self._worker is not None:
            self._worker.part_channel(buf.server, buf.name)
        elif buf.is_server and self._worker is not None:
            self._worker.disconnect_server(buf.server)
        # For query / closed-channel buffers we just drop the UI.
        self._on_buffer_removed(buf.server, buf.name)

    def _on_nick_double_clicked(self, item: QListWidgetItem):
        if self._active is None:
            return
        buf = self._buffers[self._active]
        nick = item.text().lstrip("@+%")
        # Open a query buffer for that nick.
        self._on_buffer_added(IrcBuffer(
            server=buf.server, name=nick, is_query=True))
        key = _buffer_key(buf.server, nick)
        # Select it in the list
        for i in range(self.list_buffers.count()):
            it = self.list_buffers.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                self.list_buffers.setCurrentItem(it)
                break
        self._switch_to(key)

    # -- connect menu / server editor ---------------------------
    def _show_connect_menu(self):
        """Build a submenu tree:
              [Server A]
                  Connect (no channel)
                  ----------------
                  My bookmarks
                    #foo - my note
                    #bar
                  Popular channels (for the template)
                    #python
                    #linux
                    ...
              [Server B]
                  ...
        Clicking the server header just connects; clicking a
        channel connects AND joins that channel. The autojoin
        list from the profile still kicks in independently."""
        servers = load_irc_config()
        if not servers:
            QMessageBox.information(
                self, "IRC",
                "No saved servers yet.\nUse 'Servers...' to add one.")
            return
        menu = QMenu(self)
        for name in sorted(servers.keys()):
            srv = servers[name]
            sub = menu.addMenu(name)
            act_bare = sub.addAction("Connect (no channel)")
            act_bare.setData(("connect", name, None))
            # User bookmarks
            if srv.bookmarks:
                sub.addSeparator()
                hdr = sub.addAction("My bookmarks")
                hdr.setEnabled(False)
                for chan, note in srv.bookmarks:
                    label = chan if not note else \
                        "%s   -   %s" % (chan, note)
                    a = sub.addAction(label)
                    a.setData(("join", name, chan))
            # Curated channels for the template
            curated = template_channels(srv.template_origin) \
                if srv.template_origin else []
            if curated:
                sub.addSeparator()
                hdr2 = sub.addAction("Popular channels")
                hdr2.setEnabled(False)
                for chan, descr in curated:
                    label = chan if not descr else \
                        "%s   -   %s" % (chan, descr)
                    a = sub.addAction(label)
                    a.setData(("join", name, chan))
        chosen = menu.exec(self.btn_connect.mapToGlobal(
            self.btn_connect.rect().bottomLeft()))
        if chosen is None or chosen.data() is None:
            return
        kind, srv_name, chan = chosen.data()
        srv = servers[srv_name]
        already = (self._worker is not None
                   and srv_name in self._worker._clients)
        if not already and self._worker is not None:
            self._worker.connect_server(srv)
        if kind == "join" and chan:
            # If we were already connected, join immediately;
            # otherwise wait a bit for the registration handshake
            # before sending JOIN.
            delay = 0 if already else 1500
            QTimer.singleShot(
                delay,
                lambda: self._deferred_join(srv_name, chan))

    def _deferred_join(self, server_name: str, channel: str):
        """Helper for connect+join from the bookmark menu - waits
        until we're actually registered before sending JOIN."""
        if self._worker is None:
            return
        self._worker.join_channel(server_name, channel)

    def _edit_servers(self):
        dlg = _ServerEditor(self)
        dlg.exec()

    # -- DCC handlers ------------------------------------------
    def _on_dcc_offer(self, server: str, from_nick: str,
                      filename: str, size: int, req_id: int):
        import os as _os
        safe = _os.path.basename(filename.replace("\\", "/")) \
            or ("dcc_%d.bin" % req_id)

        # Check the server's auto-receive policy.
        srv = load_irc_config().get(server)
        auto_dest = None
        if srv is not None:
            if srv.dcc_auto == "all":
                auto_dest = self._dcc_auto_dest_for(srv, safe)
            elif srv.dcc_auto == "trusted":
                trusted = [n.lower()
                           for n in srv.dcc_trusted_nicks]
                if from_nick.lower() in trusted:
                    auto_dest = self._dcc_auto_dest_for(srv, safe)
        if auto_dest is not None:
            # Skip the prompt entirely - log to the chat instead so
            # the user knows what happened.
            self._append_system(
                server, "*server*",
                "Auto-receiving DCC '%s' from %s (%s bytes) -> %s"
                % (safe, from_nick, f"{size:,}", auto_dest))
            self._start_dcc_receive(req_id, auto_dest, safe, size,
                                    server=server, peer=from_nick)
            return

        # Manual path: ask the user.
        text = (
            "%s on %s is offering a file via DCC:\n\n"
            "  %s\n"
            "  size: %s bytes\n\n"
            "Accept? It will be saved to the lister's folder."
            % (from_nick, server, filename, f"{size:,}"))
        r = QMessageBox.question(
            self, "DCC SEND offer", text,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            self._worker.reject_dcc(req_id)
            return
        default_dir = self._guess_save_dir()
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save DCC file",
            str(Path(default_dir) / safe))
        if not dest:
            self._worker.reject_dcc(req_id)
            return
        self._start_dcc_receive(req_id, dest, safe, size,
                                server=server, peer=from_nick)

    def _dcc_auto_dest_for(self, srv: IrcServer,
                           safe_name: str) -> str:
        """Build a destination path for an auto-received DCC file."""
        target_dir = srv.dcc_auto_dir or self._guess_save_dir()
        try:
            Path(target_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            target_dir = self._guess_save_dir()
        return self._unique_path(Path(target_dir) / safe_name)

    @staticmethod
    def _unique_path(p: Path) -> str:
        """Avoid clobbering an existing file by appending a counter
        (file.zip -> file (1).zip)."""
        if not p.exists():
            return str(p)
        stem = p.stem
        suf = p.suffix
        n = 1
        while True:
            cand = p.with_name(f"{stem} ({n}){suf}")
            if not cand.exists():
                return str(cand)
            n += 1

    def _start_dcc_receive(self, req_id: int, dest: str,
                           safe: str, size: int,
                           server: str = "",
                           peer: str = ""):
        """Common path used by both manual and auto receive. Adds
        a TransferInfo to the central register and kicks off the
        actual receive on the worker; no per-transfer popup - the
        Transfers window shows progress."""
        import time as _t
        self._transfers[req_id] = TransferInfo(
            req_id=req_id, server=server, peer=peer,
            filename=safe, size=size, direction="recv",
            path=dest, started_at=_t.time())
        self._worker.accept_dcc(req_id, dest)
        self._refresh_transfers_view()
        self._update_status_strip()

    def _on_dcc_progress(self, req_id: int, done: int,
                         total: int):
        ti = self._transfers.get(req_id)
        if ti is None:
            return
        ti.bytes_done = done
        if total > 0:
            ti.size = total
        self._refresh_transfers_view()

    def _on_dcc_done(self, req_id: int, path: str):
        import time as _t
        ti = self._transfers.get(req_id)
        if ti is not None:
            ti.state = "done"
            ti.path = path
            ti.bytes_done = ti.size
            ti.finished_at = _t.time()
        # Refresh lister so file appears in the active folder.
        try:
            if self._lister is not None and \
                    hasattr(self._lister, "refresh"):
                self._lister.refresh()
        except Exception:
            pass
        self._refresh_transfers_view()
        self._update_status_strip()
        # Quiet status-line message instead of a popup that
        # interrupts everything when an auto-receive finishes.
        self.lbl_foot.setText("Transfer done: %s" % path)

    def _on_dcc_failed(self, req_id: int, error: str):
        import time as _t
        ti = self._transfers.get(req_id)
        if ti is not None:
            ti.state = "failed"
            ti.error = error
            ti.finished_at = _t.time()
        self._refresh_transfers_view()
        self._update_status_strip()
        self.lbl_foot.setText("Transfer failed: %s" % error)

    def _guess_save_dir(self) -> str:
        try:
            cur = getattr(self._lister, "current_path", None)
            if cur is not None and Path(cur).is_dir():
                return str(cur)
        except Exception:
            pass
        return str(Path.home())

    # -- toolbar handlers --------------------------------------
    def _prompt_join(self):
        """Ask for a channel name and join it on the active
        server. If there's no active buffer (no server connected
        yet) we say so."""
        if self._active is None or self._worker is None:
            self._set_head_error("Connect to a server first")
            return
        buf = self._buffers[self._active]
        ch, ok = QInputDialog.getText(
            self, "Join channel",
            "Channel name (e.g. #python):")
        if not ok or not ch.strip():
            return
        ch = ch.strip().split()[0]
        if not ch.startswith("#"):
            ch = "#" + ch
        self._worker.join_channel(buf.server, ch)

    def _prompt_change_nick(self):
        """Change our nickname on the active server."""
        if self._active is None or self._worker is None:
            self._set_head_error("Connect to a server first")
            return
        buf = self._buffers[self._active]
        cur = self._own_nick.get(buf.server, "")
        new, ok = QInputDialog.getText(
            self, "Change nickname",
            "New nickname on %s:" % buf.server, text=cur)
        if not ok or not new.strip():
            return
        self._worker.change_nick(buf.server, new.strip().split()[0])

    # -- transfers ---------------------------------------------
    def _on_dcc_started_send(self, server: str, peer: str,
                             filename: str, size: int,
                             req_id: int):
        """Outbound DCC SEND has been advertised - register the
        transfer so the Transfers window can track progress."""
        import time as _t
        self._transfers[req_id] = TransferInfo(
            req_id=req_id, server=server, peer=peer,
            filename=filename, size=size, direction="send",
            started_at=_t.time())
        self._refresh_transfers_view()
        self._update_status_strip()

    def _show_transfers(self):
        """Open (or raise) the DCC transfers window."""
        if self._transfers_dlg is not None:
            try:
                if self._transfers_dlg.isVisible():
                    self._transfers_dlg.raise_()
                    self._transfers_dlg.activateWindow()
                    return
            except RuntimeError:
                self._transfers_dlg = None
        self._transfers_dlg = _TransfersDialog(self)
        self._transfers_dlg.show()
        self._refresh_transfers_view()

    def _refresh_transfers_view(self):
        """Tell the open Transfers dialog (if any) to redraw."""
        if self._transfers_dlg is None:
            return
        try:
            self._transfers_dlg.refresh()
        except RuntimeError:
            # Dialog was closed underneath us.
            self._transfers_dlg = None

    def _update_status_strip(self):
        """Refresh the small footer with active-transfer count."""
        active = sum(1 for t in self._transfers.values()
                     if t.state == "active")
        total = len(self._transfers)
        if active:
            self.lbl_foot.setText(
                "%d transfer%s active   (%d total this session)"
                % (active, "" if active == 1 else "s", total))
        elif total:
            self.lbl_foot.setText(
                "Idle.   %d transfer%s this session"
                % (total, "" if total == 1 else "s"))
        else:
            self.lbl_foot.setText("Ready.")

    # -- chat logging -----------------------------------------
    def _show_log_settings(self):
        """Global logging settings: on/off + storage folder."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Chat logging - global settings")
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        dlg.resize(640, 240)
        v = QVBoxLayout(dlg)
        info = QLabel(
            "When global logging is ON, every IRC buffer is "
            "written to disk\nunless explicitly disabled via "
            "right-click on a buffer.\nIndividual buffers can also "
            "be enabled while the global toggle is off.")
        info.setFont(_mono(11))
        info.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        v.addWidget(info)

        cb = QCheckBox("Log all IRC buffers by default (global)")
        cb.setFont(_mono(11))
        cb.setChecked(self._logger.is_global_enabled())
        v.addWidget(cb)

        # Folder picker row
        from PyQt6.QtWidgets import QFileDialog as _FD
        row = QHBoxLayout()
        row.addWidget(QLabel("Log folder:"))
        e_dir = QLineEdit()
        e_dir.setFont(_mono(11))
        e_dir.setText(str(self._logger.log_dir()))
        row.addWidget(e_dir, 1)
        b_browse = QPushButton("Browse...")
        b_browse.setFont(_mono(10))

        def _pick():
            d = _FD.getExistingDirectory(
                dlg, "Pick log folder", e_dir.text() or "")
            if d:
                e_dir.setText(d)
        b_browse.clicked.connect(_pick)
        row.addWidget(b_browse)
        v.addLayout(row)

        hint = QLabel(
            "Files are stored as  <folder>/<server>/<buffer>.log")
        hint.setFont(_mono(10))
        hint.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        v.addWidget(hint)

        v.addStretch(1)
        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(dlg.accept)
        bbox.rejected.connect(dlg.reject)
        v.addWidget(bbox)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._logger.set_global(
            cb.isChecked(), e_dir.text().strip())
        self._set_head_error(
            "Chat logging: global=%s, folder=%s"
            % ("ON" if cb.isChecked() else "off",
               self._logger.log_dir()))

    def _open_log_for(self, server: str, buffer: str):
        """Show the log file in the active lister (or open the
        containing folder if the file doesn't exist yet)."""
        p = self._logger._path_for(server, buffer)
        target = p if p.is_file() else p.parent
        try:
            if self._lister is not None and hasattr(
                    self._lister, "goto"):
                self._lister.goto(
                    target if target.is_dir()
                    else target.parent)
            else:
                # Fall back to the OS shell.
                import platform as _pl, subprocess as _sp, os
                if _pl.system() == "Windows":
                    os.startfile(str(target))
                elif _pl.system() == "Darwin":
                    _sp.Popen(["open", str(target)])
                else:
                    _sp.Popen(["xdg-open", str(target)])
        except Exception as e:
            QMessageBox.warning(
                self, "Open log",
                "Couldn't open log location:\n%s" % e)

    # -- lifecycle ---------------------------------------------
    def closeEvent(self, ev):
        try:
            if self._worker is not None:
                self._worker.stop()
        except Exception:
            pass
        try:
            self._logger.close_all()
        except Exception:
            pass
        super().closeEvent(ev)


# ------------------------------------------------------------------
# Server profile editor
# ------------------------------------------------------------------
class _ServerEditor(QDialog):
    """A simple list+form editor for saved server profiles."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("IRC servers")
        self.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(700, 500)
        self._servers = load_irc_config()
        self._current: Optional[str] = None
        self._build()

    def _build(self):
        root = QHBoxLayout(self)
        # Left: list + add/remove
        left = QVBoxLayout()
        self.lst = QListWidget()
        self.lst.setFont(_mono(11))
        self.lst.itemClicked.connect(self._on_pick)
        left.addWidget(self.lst, 1)
        row = QHBoxLayout()
        bt_add = QPushButton("Add")
        bt_tpl = QPushButton("From template...")
        bt_del = QPushButton("Delete")
        bt_add.clicked.connect(self._add)
        bt_tpl.clicked.connect(self._add_from_template)
        bt_del.clicked.connect(self._delete)
        row.addWidget(bt_add); row.addWidget(bt_tpl); row.addWidget(bt_del)
        left.addLayout(row)
        root.addLayout(left, 1)

        # Right: form
        self.form_w = QWidget()
        form = QFormLayout(self.form_w)
        self.e_name = QLineEdit()
        self.e_host = QLineEdit()
        self.e_port = QSpinBox(); self.e_port.setRange(1, 65535)
        self.e_port.setValue(6697)
        self.cb_tls = QCheckBox("Use TLS")
        self.cb_tls.setChecked(True)
        self.e_nick = QLineEdit()
        self.e_nick.setPlaceholderText("Primary nickname")
        self.e_alts = QLineEdit()
        self.e_alts.setPlaceholderText(
            "Alt nicks, e.g.  quopus_ quopus__ qntm")
        self.e_user = QLineEdit()
        self.e_user.setPlaceholderText(
            "IDENT / ident username (defaults to nickname)")
        self.e_real = QLineEdit()
        self.e_real.setText("Quopus IRC")
        self.e_srvpw = QLineEdit()
        self.e_srvpw.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_saslu = QLineEdit()
        self.e_saslp = QLineEdit()
        self.e_saslp.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_nspw = QLineEdit()
        self.e_nspw.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_nspw.setPlaceholderText(
            "NickServ password (auto-IDENTIFY, "
            "ignored if SASL is set)")
        self.e_autojoin = QLineEdit()
        self.e_autojoin.setPlaceholderText(
            "#chan1 #chan2 ...")
        self.cb_autoconnect = QCheckBox("Connect on open")
        self.cb_reconnect = QCheckBox(
            "Auto-reconnect on disconnect")
        self.cb_reconnect.setChecked(True)
        self.e_quit = QLineEdit()
        self.e_quit.setText("Quopus IRC")
        # DCC auto-receive controls.
        self.cb_dcc_auto = QComboBox()
        self.cb_dcc_auto.addItems([
            "off (always ask)",
            "trusted nicks only",
            "all (auto-accept everything)"])
        self.e_dcc_trusted = QLineEdit()
        self.e_dcc_trusted.setPlaceholderText(
            "nick1 nick2 ...  (for 'trusted nicks only')")
        self.e_dcc_dir = QLineEdit()
        self.e_dcc_dir.setPlaceholderText(
            "(empty = current lister folder)")
        form.addRow("Name:", self.e_name)
        form.addRow("Host:", self.e_host)
        form.addRow("Port:", self.e_port)
        form.addRow("", self.cb_tls)
        form.addRow("Nickname:", self.e_nick)
        form.addRow("Alt nicks:", self.e_alts)
        form.addRow("Username:", self.e_user)
        form.addRow("Realname:", self.e_real)
        form.addRow("Server pw:", self.e_srvpw)
        form.addRow("SASL user:", self.e_saslu)
        form.addRow("SASL pw:", self.e_saslp)
        form.addRow("NickServ pw:", self.e_nspw)
        form.addRow("Autojoin:", self.e_autojoin)
        form.addRow("", self.cb_autoconnect)
        form.addRow("", self.cb_reconnect)
        form.addRow("Quit message:", self.e_quit)
        form.addRow("DCC auto-receive:", self.cb_dcc_auto)
        form.addRow("DCC trusted:", self.e_dcc_trusted)
        form.addRow("DCC save dir:", self.e_dcc_dir)

        # Bookmarks pane - the user's own list of channels for
        # this server, plus a quick view of the curated channels
        # if this server came from a template.
        bm_label = QLabel("Channel bookmarks:")
        bm_label.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        form.addRow(bm_label)
        self.lst_bm = QListWidget()
        self.lst_bm.setFont(_mono(11))
        self.lst_bm.setMaximumHeight(140)
        form.addRow(self.lst_bm)
        bm_row = QHBoxLayout()
        bt_bm_add = QPushButton("Add")
        bt_bm_edit = QPushButton("Edit note")
        bt_bm_del = QPushButton("Remove")
        bt_bm_curated = QPushButton("Add from popular...")
        for b in (bt_bm_add, bt_bm_edit, bt_bm_del, bt_bm_curated):
            b.setFont(_mono(10))
        bt_bm_add.clicked.connect(self._bm_add)
        bt_bm_edit.clicked.connect(self._bm_edit)
        bt_bm_del.clicked.connect(self._bm_remove)
        bt_bm_curated.clicked.connect(self._bm_add_curated)
        bm_row.addWidget(bt_bm_add)
        bm_row.addWidget(bt_bm_edit)
        bm_row.addWidget(bt_bm_del)
        bm_row.addWidget(bt_bm_curated)
        bm_row.addStretch(1)
        form.addRow(bm_row)

        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close)
        bbox.button(QDialogButtonBox.StandardButton.Save)\
            .clicked.connect(self._save)
        bbox.button(QDialogButtonBox.StandardButton.Close)\
            .clicked.connect(self.accept)
        form.addRow(bbox)
        root.addWidget(self.form_w, 2)

        self._refresh_list()
        # Lock the form until something's selected
        self.form_w.setEnabled(False)

    def _refresh_list(self):
        self.lst.clear()
        for name in sorted(self._servers.keys()):
            self.lst.addItem(name)

    def _on_pick(self, item: QListWidgetItem):
        name = item.text()
        srv = self._servers.get(name)
        if srv is None:
            return
        self._current = name
        self.form_w.setEnabled(True)
        self.e_name.setText(srv.name)
        self.e_host.setText(srv.host)
        self.e_port.setValue(srv.port)
        self.cb_tls.setChecked(srv.tls)
        self.e_nick.setText(srv.nickname)
        self.e_alts.setText(" ".join(srv.alt_nicks))
        self.e_user.setText(srv.username)
        self.e_real.setText(srv.realname)
        self.e_srvpw.setText(srv.server_password)
        self.e_saslu.setText(srv.sasl_username)
        self.e_saslp.setText(srv.sasl_password)
        self.e_nspw.setText(srv.nickserv_password)
        self.e_autojoin.setText(" ".join(srv.autojoin))
        self.cb_autoconnect.setChecked(srv.autoconnect)
        self.cb_reconnect.setChecked(srv.auto_reconnect)
        self.e_quit.setText(srv.quit_message or "Quopus IRC")
        # Map DCC policy back to combo index.
        idx_map = {"off": 0, "trusted": 1, "all": 2}
        self.cb_dcc_auto.setCurrentIndex(
            idx_map.get(srv.dcc_auto, 0))
        self.e_dcc_trusted.setText(
            " ".join(srv.dcc_trusted_nicks))
        self.e_dcc_dir.setText(srv.dcc_auto_dir)
        self._refresh_bookmarks_list()

    def _refresh_bookmarks_list(self):
        """Repopulate the bookmark QListWidget for the currently
        selected server (the source of truth is the in-memory
        IrcServer.bookmarks)."""
        self.lst_bm.clear()
        if self._current is None:
            return
        srv = self._servers.get(self._current)
        if srv is None:
            return
        for chan, note in srv.bookmarks:
            text = chan if not note else "%s   -   %s" % (chan, note)
            self.lst_bm.addItem(text)

    def _save_bookmarks_now(self):
        """Persist server config immediately - used after each
        bookmark mutation so changes survive even if the user
        closes the editor without clicking Save on the form."""
        try:
            save_irc_config(self._servers)
        except Exception:
            pass

    # -- bookmark editor actions -----------------------------------
    def _bm_add(self):
        """Add a new bookmark by typing the channel name."""
        if self._current is None:
            return
        chan, ok = QInputDialog.getText(
            self, "Add bookmark",
            "Channel (e.g. #python):")
        if not ok or not chan.strip():
            return
        chan = chan.strip().split()[0]
        if not chan.startswith("#") and not chan.startswith("&"):
            chan = "#" + chan
        note, _ok = QInputDialog.getText(
            self, "Add bookmark",
            "Optional note for this channel:")
        srv = self._servers[self._current]
        # Reject duplicates - update the note instead.
        srv.bookmarks = [(c, n) for c, n in srv.bookmarks
                         if c.lower() != chan.lower()]
        srv.bookmarks.append((chan, note.strip()))
        self._refresh_bookmarks_list()
        self._save_bookmarks_now()

    def _bm_edit(self):
        """Edit the note on the currently selected bookmark."""
        if self._current is None:
            return
        row = self.lst_bm.currentRow()
        if row < 0:
            return
        srv = self._servers[self._current]
        chan, note = srv.bookmarks[row]
        new_note, ok = QInputDialog.getText(
            self, "Edit note",
            "Note for %s:" % chan, text=note)
        if not ok:
            return
        srv.bookmarks[row] = (chan, new_note.strip())
        self._refresh_bookmarks_list()
        self._save_bookmarks_now()

    def _bm_remove(self):
        """Remove the selected bookmark."""
        if self._current is None:
            return
        row = self.lst_bm.currentRow()
        if row < 0:
            return
        srv = self._servers[self._current]
        del srv.bookmarks[row]
        self._refresh_bookmarks_list()
        self._save_bookmarks_now()

    def _bm_add_curated(self):
        """Pick from the bundled popular-channel list for this
        server's template (if it has one). Lets the user
        multi-select and adds them in one shot."""
        if self._current is None:
            return
        srv = self._servers[self._current]
        curated = template_channels(srv.template_origin) \
            if srv.template_origin else []
        if not curated:
            QMessageBox.information(
                self, "Bookmarks",
                "No curated channels for this server."
                "\nUse 'Add' to enter one manually.")
            return
        # Multi-select dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Add popular channels")
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        dlg.resize(500, 400)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "Tick channels to add as bookmarks "
            "(existing ones will refresh their notes):"))
        lw = QListWidget()
        lw.setFont(_mono(11))
        lw.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection)
        existing = {c.lower() for c, _n in srv.bookmarks}
        for chan, descr in curated:
            label = chan if not descr else \
                "%s   -   %s" % (chan, descr)
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, (chan, descr))
            if chan.lower() in existing:
                it.setForeground(QColor("#777"))
            lw.addItem(it)
        v.addWidget(lw, 1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for it in lw.selectedItems():
            chan, descr = it.data(Qt.ItemDataRole.UserRole)
            srv.bookmarks = [(c, n) for c, n in srv.bookmarks
                             if c.lower() != chan.lower()]
            srv.bookmarks.append((chan, descr))
        self._refresh_bookmarks_list()
        self._save_bookmarks_now()

    def _add(self):
        name, ok = QInputDialog.getText(
            self, "New server",
            "Short name (e.g. Libera):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._servers:
            QMessageBox.warning(self, "IRC",
                                "A server with that name exists.")
            return
        self._servers[name] = IrcServer(
            name=name, host="", nickname="quopus",
            alt_nicks=["quopus_", "quopus__"])
        self._refresh_list()
        for i in range(self.lst.count()):
            if self.lst.item(i).text() == name:
                self.lst.setCurrentRow(i)
                self._on_pick(self.lst.item(i))
                break

    def _add_from_template(self):
        """Pick a network template (built-in or user-defined),
        ask for an alias + nickname, then seed the entry. The
        picker also has buttons to add/edit/delete user templates
        right there, so power users can curate their own list
        without leaving the dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Pick an IRC network")
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        dlg.resize(640, 480)
        v = QVBoxLayout(dlg)
        info = QLabel(
            "Pick a network. Host, port, and TLS will be filled\n"
            "in for you. You can edit anything afterwards.\n"
            "Use 'New user template' to add your own.")
        info.setFont(_mono(11))
        info.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        v.addWidget(info)
        lw = QListWidget()
        lw.setFont(_mono(11))

        def _populate():
            lw.clear()
            for n, host, port, tls, descr, is_user in all_templates():
                scheme = "ircs" if tls else "irc"
                tag = "[user] " if is_user else "       "
                line = "%s%-14s  %s://%s:%d   %s" % (
                    tag, n, scheme, host, port, descr)
                item = QListWidgetItem(line)
                item.setData(Qt.ItemDataRole.UserRole,
                             (n, is_user))
                lw.addItem(item)
            lw.setCurrentRow(0)

        _populate()
        v.addWidget(lw, 1)
        # Template-management buttons row
        tbtns = QHBoxLayout()
        bt_new = QPushButton("New user template...")
        bt_edit = QPushButton("Edit user template...")
        bt_del = QPushButton("Delete user template")
        for b in (bt_new, bt_edit, bt_del):
            b.setFont(_mono(10))
        tbtns.addWidget(bt_new)
        tbtns.addWidget(bt_edit)
        tbtns.addWidget(bt_del)
        tbtns.addStretch(1)
        v.addLayout(tbtns)

        def _selected_user_name():
            """Return the name of the selected entry IF it's a
            user template (so Edit/Delete only act on those)."""
            it = lw.currentItem()
            if it is None:
                return None
            name, is_user = it.data(Qt.ItemDataRole.UserRole)
            return name if is_user else None

        def _on_new():
            data = self._edit_user_template_dialog(None)
            if data is None:
                return
            name = data["name"]
            user_tpls = load_user_templates()
            user_tpls[name] = {k: v for k, v in data.items()
                               if k != "name"}
            save_user_templates(user_tpls)
            _populate()
            # Select what we just added.
            for i in range(lw.count()):
                tname, is_user = lw.item(i).data(
                    Qt.ItemDataRole.UserRole)
                if tname == name and is_user:
                    lw.setCurrentRow(i)
                    break

        def _on_edit():
            name = _selected_user_name()
            if name is None:
                QMessageBox.information(
                    dlg, "User templates",
                    "Only user-defined templates can be edited.\n"
                    "Pick a [user] entry first.")
                return
            user_tpls = load_user_templates()
            existing = dict(user_tpls.get(name, {}))
            existing["name"] = name
            data = self._edit_user_template_dialog(existing)
            if data is None:
                return
            new_name = data["name"]
            if new_name != name:
                user_tpls.pop(name, None)
            user_tpls[new_name] = {k: v for k, v in data.items()
                                   if k != "name"}
            save_user_templates(user_tpls)
            _populate()

        def _on_del():
            name = _selected_user_name()
            if name is None:
                QMessageBox.information(
                    dlg, "User templates",
                    "Only user-defined templates can be deleted.")
                return
            if QMessageBox.question(
                    dlg, "Delete user template",
                    "Delete template %s? "
                    "(Existing server profiles built from it stay.)"
                    % name) \
                    != QMessageBox.StandardButton.Yes:
                return
            user_tpls = load_user_templates()
            user_tpls.pop(name, None)
            save_user_templates(user_tpls)
            _populate()

        bt_new.clicked.connect(_on_new)
        bt_edit.clicked.connect(_on_edit)
        bt_del.clicked.connect(_on_del)

        # Nick row + OK/Cancel
        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("Nickname:"))
        e_nick = QLineEdit()
        e_nick.setPlaceholderText("quopus")
        nick_row.addWidget(e_nick, 1)
        v.addLayout(nick_row)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        it = lw.currentItem()
        if it is None:
            return
        tpl_name, _is_user = it.data(Qt.ItemDataRole.UserRole)
        nick = e_nick.text().strip() or "quopus"
        # Unique alias if a profile of the same name already exists.
        alias = tpl_name
        n = 2
        while alias in self._servers:
            alias = "%s_%d" % (tpl_name, n)
            n += 1
        srv = make_server_from_template_name(
            tpl_name, alias, nick)
        if srv is None:
            QMessageBox.warning(
                self, "Templates",
                "Couldn't build server from template %r"
                % tpl_name)
            return
        self._servers[alias] = srv
        self._refresh_list()
        for i in range(self.lst.count()):
            if self.lst.item(i).text() == alias:
                self.lst.setCurrentRow(i)
                self._on_pick(self.lst.item(i))
                break

    def _edit_user_template_dialog(
            self, existing: Optional[dict]) -> Optional[dict]:
        """Show an editor for one user template; return the new
        data on accept, None on cancel. `existing` is the previous
        record (when editing) or None for a fresh entry. The
        channel list is mutated through Add/Edit/Remove buttons."""
        from PyQt6.QtWidgets import (
            QFormLayout, QSpinBox, QCheckBox)
        dlg = QDialog(self)
        dlg.setWindowTitle(
            "Edit user template"
            if existing else "New user template")
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        dlg.resize(560, 540)
        v = QVBoxLayout(dlg)
        form = QFormLayout()
        e_name = QLineEdit()
        e_host = QLineEdit()
        e_port = QSpinBox()
        e_port.setRange(1, 65535)
        e_port.setValue(6697)
        cb_tls = QCheckBox("Use TLS")
        cb_tls.setChecked(True)
        e_descr = QLineEdit()
        e_descr.setPlaceholderText(
            "Short description shown in the picker")
        if existing:
            e_name.setText(existing.get("name", ""))
            e_host.setText(existing.get("host", ""))
            e_port.setValue(int(existing.get("port", 6697)))
            cb_tls.setChecked(bool(existing.get("tls", True)))
            e_descr.setText(existing.get("description", ""))
        form.addRow("Name:", e_name)
        form.addRow("Host:", e_host)
        form.addRow("Port:", e_port)
        form.addRow("", cb_tls)
        form.addRow("Description:", e_descr)
        v.addLayout(form)

        v.addWidget(QLabel("Popular channels for this template:"))
        chan_list = QListWidget()
        chan_list.setFont(_mono(11))
        chan_list.setMaximumHeight(180)
        # Channels are stored as (name, note) tuples in the list's
        # UserRole; the visible string is "#chan  -  note".
        for c, note in (existing or {}).get("channels", []) or []:
            text = c if not note else "%s   -   %s" % (c, note)
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, (c, note))
            chan_list.addItem(it)
        v.addWidget(chan_list)
        crow = QHBoxLayout()
        c_add = QPushButton("Add")
        c_edit = QPushButton("Edit note")
        c_del = QPushButton("Remove")
        for b in (c_add, c_edit, c_del):
            b.setFont(_mono(10))
        crow.addWidget(c_add)
        crow.addWidget(c_edit)
        crow.addWidget(c_del)
        crow.addStretch(1)
        v.addLayout(crow)

        def _ch_add():
            ch, ok = QInputDialog.getText(
                dlg, "Channel", "Channel name:")
            if not ok or not ch.strip():
                return
            ch = ch.strip().split()[0]
            if not ch.startswith("#") and not ch.startswith("&"):
                ch = "#" + ch
            note, _ok = QInputDialog.getText(
                dlg, "Channel", "Short description:")
            text = ch if not note else "%s   -   %s" % (ch, note)
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole,
                       (ch, note.strip()))
            chan_list.addItem(it)

        def _ch_edit():
            row = chan_list.currentRow()
            if row < 0:
                return
            ch, note = chan_list.item(row).data(
                Qt.ItemDataRole.UserRole)
            new_note, ok = QInputDialog.getText(
                dlg, "Edit note",
                "Note for %s:" % ch, text=note)
            if not ok:
                return
            text = ch if not new_note \
                else "%s   -   %s" % (ch, new_note)
            chan_list.item(row).setText(text)
            chan_list.item(row).setData(
                Qt.ItemDataRole.UserRole,
                (ch, new_note.strip()))

        def _ch_del():
            row = chan_list.currentRow()
            if row < 0:
                return
            chan_list.takeItem(row)

        c_add.clicked.connect(_ch_add)
        c_edit.clicked.connect(_ch_edit)
        c_del.clicked.connect(_ch_del)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        name = e_name.text().strip()
        host = e_host.text().strip()
        if not name or not host:
            QMessageBox.warning(
                dlg, "User templates",
                "Name and host are required.")
            return None
        # Reject collisions with built-in names so they're
        # uniquely identifiable in the picker.
        builtin_names = {t[0] for t in IRC_NETWORK_TEMPLATES}
        if name in builtin_names:
            QMessageBox.warning(
                dlg, "User templates",
                "%r is a built-in template name. "
                "Pick a different one." % name)
            return None
        channels = []
        for i in range(chan_list.count()):
            channels.append(
                chan_list.item(i).data(Qt.ItemDataRole.UserRole))
        return {
            "name": name, "host": host,
            "port": e_port.value(), "tls": cb_tls.isChecked(),
            "description": e_descr.text().strip(),
            "channels": channels,
        }

    def _delete(self):
        if self._current is None:
            return
        if QMessageBox.question(
                self, "Delete server",
                "Delete %s?" % self._current) \
                != QMessageBox.StandardButton.Yes:
            return
        self._servers.pop(self._current, None)
        self._current = None
        self.form_w.setEnabled(False)
        self._refresh_list()
        save_irc_config(self._servers)

    def _save(self):
        if self._current is None:
            return
        new_name = self.e_name.text().strip() or self._current
        # The existing entry may have bookmarks + a template origin
        # we want to keep across edits (they aren't on the form,
        # they live in the side-panel and are mutated directly).
        old = self._servers.get(self._current)
        old_bookmarks = list(old.bookmarks) if old else []
        old_template = old.template_origin if old else ""
        # If renamed, move the entry.
        if new_name != self._current:
            self._servers.pop(self._current, None)
        autojoin = [w for w in self.e_autojoin.text().split()
                    if w]
        alt_nicks = [w for w in self.e_alts.text().split() if w]
        self._servers[new_name] = IrcServer(
            name=new_name,
            host=self.e_host.text().strip(),
            port=self.e_port.value(),
            tls=self.cb_tls.isChecked(),
            nickname=self.e_nick.text().strip(),
            alt_nicks=alt_nicks,
            username=self.e_user.text().strip(),
            realname=self.e_real.text().strip() or "Quopus IRC",
            server_password=self.e_srvpw.text(),
            sasl_username=self.e_saslu.text().strip(),
            sasl_password=self.e_saslp.text(),
            autojoin=autojoin,
            autoconnect=self.cb_autoconnect.isChecked(),
            nickserv_password=self.e_nspw.text(),
            dcc_auto=["off", "trusted", "all"][
                self.cb_dcc_auto.currentIndex()],
            dcc_trusted_nicks=[w for w in
                               self.e_dcc_trusted.text().split()
                               if w],
            dcc_auto_dir=self.e_dcc_dir.text().strip(),
            auto_reconnect=self.cb_reconnect.isChecked(),
            quit_message=self.e_quit.text().strip()
                or "Quopus IRC",
            bookmarks=old_bookmarks,
            template_origin=old_template,
        )
        save_irc_config(self._servers)
        self._current = new_name
        self._refresh_list()
        for i in range(self.lst.count()):
            if self.lst.item(i).text() == new_name:
                self.lst.setCurrentRow(i)
                break


# ------------------------------------------------------------------
# DCC transfers window - shows active and completed transfers
# ------------------------------------------------------------------
from PyQt6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QAbstractItemView,
)


class _TransfersDialog(QDialog):
    """A live table of all DCC transfers in this session. Each row
    shows direction (in/out), peer nick, file name, progress bar,
    state, and a small actions menu (open / reveal / remove).

    The window pulls its data from the parent IrcDialog's
    self._transfers dict; refresh() is called whenever a transfer
    signal arrives."""

    def __init__(self, parent: "IrcDialog"):
        super().__init__(parent)
        self._irc = parent
        self.setWindowTitle("DCC Transfers - Quopus IRC")
        self.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        self.resize(820, 420)
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        info = QLabel(
            "DCC file transfers - both directions. Right-click a "
            "row for actions.")
        info.setFont(_mono(10))
        info.setStyleSheet(f"QLabel {{ color: {C.BLACK}; }}")
        v.addWidget(info)

        self.tbl = QTableWidget(0, 7)
        self.tbl.setHorizontalHeaderLabels([
            "Dir", "Server", "Peer", "File", "Progress",
            "State", "Size"])
        self.tbl.setFont(_mono(10))
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)         # File
        for i in (1, 2, 5, 6):
            hdr.setSectionResizeMode(
                i, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)           # Dir
        self.tbl.setColumnWidth(0, 80)
        hdr.setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed)           # Progress
        self.tbl.setColumnWidth(4, 220)
        self.tbl.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(
            self._on_context_menu)
        v.addWidget(self.tbl, 1)

        # Bottom buttons
        row = QHBoxLayout()
        bt_clear = QPushButton("Clear finished")
        bt_clear.setFont(_mono(10))
        bt_clear.clicked.connect(self._clear_finished)
        row.addWidget(bt_clear)
        row.addStretch(1)
        bt_close = QPushButton("Close")
        bt_close.setFont(_mono(10))
        bt_close.clicked.connect(self.close)
        row.addWidget(bt_close)
        v.addLayout(row)

    def refresh(self):
        """Rebuild the table from the parent's transfer register.
        We keep this simple - O(n) where n is the number of
        transfers - because the list rarely grows large."""
        transfers = list(self._irc._transfers.values())
        # Active first, then by most-recently started (newest top).
        transfers.sort(
            key=lambda t: (0 if t.state == "active" else 1,
                           -t.started_at))
        self.tbl.setRowCount(len(transfers))
        for row, t in enumerate(transfers):
            self.tbl.setItem(row, 0, QTableWidgetItem(
                "<= in" if t.direction == "recv" else "out =>"))
            self.tbl.setItem(row, 1, QTableWidgetItem(t.server))
            self.tbl.setItem(row, 2, QTableWidgetItem(t.peer))
            self.tbl.setItem(row, 3, QTableWidgetItem(t.filename))
            pb = QProgressBar()
            pb.setMaximum(max(t.size, 1))
            pb.setValue(min(t.bytes_done, max(t.size, 1)))
            if t.size > 0:
                pct = int(100 * t.bytes_done / t.size)
                pb.setFormat("%d%%  (%s / %s)" % (
                    pct,
                    self._human(t.bytes_done),
                    self._human(t.size)))
            else:
                pb.setFormat(self._human(t.bytes_done))
            self.tbl.setCellWidget(row, 4, pb)
            state = t.state
            if t.state == "failed" and t.error:
                state = "failed: " + t.error[:40]
            self.tbl.setItem(row, 5, QTableWidgetItem(state))
            self.tbl.setItem(row, 6, QTableWidgetItem(
                self._human(t.size)))
            # Stash the req_id on the first column for lookup.
            self.tbl.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, t.req_id)

    @staticmethod
    def _human(n: int) -> str:
        try:
            n = int(n)
        except Exception:
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" \
                    else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    def _selected_req_id(self):
        items = self.tbl.selectedItems()
        if not items:
            return None
        # Find the row-0 item (where we stashed the req_id).
        row = items[0].row()
        item0 = self.tbl.item(row, 0)
        if item0 is None:
            return None
        return item0.data(Qt.ItemDataRole.UserRole)

    def _on_context_menu(self, pos):
        req_id = self._selected_req_id()
        if req_id is None:
            return
        ti = self._irc._transfers.get(req_id)
        if ti is None:
            return
        menu = QMenu(self.tbl)
        act_open = act_reveal = act_remove = None
        if ti.state == "done" and ti.path:
            act_open = menu.addAction("Open file")
            act_reveal = menu.addAction("Reveal in lister")
        if ti.state != "active":
            act_remove = menu.addAction("Remove from list")
        if not menu.actions():
            return
        chosen = menu.exec(
            self.tbl.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            self._open_with_os(ti.path)
        elif chosen is act_reveal:
            self._reveal_in_lister(ti.path)
        elif chosen is act_remove:
            self._irc._transfers.pop(req_id, None)
            self.refresh()
            self._irc._update_status_strip()

    def _open_with_os(self, path: str):
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
                self, "Open", "Couldn't open:\n%s" % e)

    def _reveal_in_lister(self, path: str):
        lister = getattr(self._irc, "_lister", None)
        if lister is None:
            return
        try:
            p = Path(path).parent
            # Lister has a goto(path) method for navigation.
            if hasattr(lister, "goto"):
                lister.goto(p)
            elif hasattr(lister, "refresh"):
                lister.refresh()
        except Exception:
            pass

    def _clear_finished(self):
        keep = {k: v for k, v in self._irc._transfers.items()
                if v.state == "active"}
        self._irc._transfers = keep
        self.refresh()
        self._irc._update_status_strip()

    def closeEvent(self, ev):
        self._irc._transfers_dlg = None
        super().closeEvent(ev)
