# date_time: 2026-06-10 21:23
"""
YouTube Audio Streaming - integrated Quopus feature.

Provides the "YouTube Audio" player (Audio menu / action buttons).
Opens a non-modal window so you can keep using Quopus while music
plays. Features:

  * Channel search: type a name, see matching channels, save one as
    a bookmark.
  * Bookmarks: click a saved channel to list its tracks, sorted by
    upload date (newest first).
  * Playback: stream the audio of any track. A "played X of Y"
    read-out plus a draggable seek slider let you jump anywhere in
    the track, and a segmented-LED spectrum equalizer (the same
    widget the MOD player uses) bounces along.
  * Persistence: bookmarks and the last-played track are saved in
    Quopus's config, so they survive restarts.
  * Async: search, metadata fetch, and decoding all run in their
    own threads. The UI never blocks; Quopus stays responsive.

Entry point: open_youtube_audio(main_window) - called from
actions.Actions.act_youtube_audio.

Requirements:
  * yt-dlp      - pip install yt-dlp     (channel search + stream URL)
  * ffmpeg      - on PATH                 (decode any codec to PCM)
  * sounddevice - pip install sounddevice (audio output; same as MOD)
  * numpy       - pip install numpy

The feature degrades gracefully: if a dependency is missing it tells
you exactly what to install instead of crashing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize,
)
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QWidget,
    QSplitter, QMessageBox, QSlider, QFrame,
)


# =====================================================================
# Dependency checks
# =====================================================================
def _missing_deps() -> list[str]:
    """Return a list of human-readable missing-dependency messages.
    Empty list means everything is available."""
    missing = []
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        missing.append("yt-dlp  (pip install yt-dlp)")
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg  (install ffmpeg and put it on PATH)")
    try:
        import sounddevice  # noqa: F401
    except Exception:
        missing.append(
            "sounddevice  (pip install sounddevice)")
    return missing


# =====================================================================
# yt-dlp helpers (run inside worker threads, never on the UI thread)
# =====================================================================

# Which browser to pull cookies from. YouTube increasingly demands
# "sign in to confirm you're not a bot" for stream resolution; the
# fix is to let yt-dlp read your browser's YouTube cookies. This is
# set from the host config (key "youtube_cookies_browser") at
# runtime via set_cookie_browser(); default "auto" tries a list of
# common browsers until one works.
_COOKIE_BROWSER = "auto"

# Browsers yt-dlp's cookiesfrombrowser supports, in the order we
# try them under "auto".
_BROWSER_CANDIDATES = [
    "firefox", "chrome", "chromium", "edge", "brave",
    "opera", "vivaldi",
]


def set_cookie_browser(name: str):
    """Set which browser yt-dlp pulls YouTube cookies from. One of
    the names in _BROWSER_CANDIDATES, or 'auto' / '' for autodetect,
    or 'none' to disable cookies entirely."""
    global _COOKIE_BROWSER
    _COOKIE_BROWSER = (name or "auto").strip().lower()


# Path to an exported cookies.txt (Netscape format). This is the MOST
# reliable option on Windows: Chrome/Brave/Vivaldi cookies can no
# longer be read by yt-dlp because of Chrome's app-bound encryption
# (the "Failed to decrypt with DPAPI" error), so a cookies.txt
# exported with e.g. the "Get cookies.txt LOCALLY" extension - or
# Firefox cookies - is the dependable path. Set from host config key
# "youtube_cookies_file".
_COOKIE_FILE = None


# YouTube now forces SABR streaming + PO-token binding on the default
# "web" client, so it often returns formats WITHOUT a usable URL
# ("Requested format is not available"). Asking yt-dlp to try several
# player clients in one pass and merge their formats greatly raises
# the chance that at least one returns a plain audio URL. Order/choice
# evolves; this set works well as of mid-2026.
_PLAYER_CLIENTS = ["default", "tv", "web_safari", "mweb", "ios"]


def set_cookie_file(path):
    """Point yt-dlp at an exported cookies.txt, or clear it with ''."""
    global _COOKIE_FILE
    _COOKIE_FILE = (str(path).strip() or None) if path else None


def _cookie_browsers_to_try() -> list:
    """Return the list of browser names to attempt for cookies.
    'none' disables; a specific name tries only that; 'auto' (the
    default) tries the common browsers in turn."""
    b = _COOKIE_BROWSER
    if b in ("none", "off", "disabled"):
        return []
    if b in ("", "auto"):
        return list(_BROWSER_CANDIDATES)
    return [b]


def _add_cookies(opts: dict, browser: Optional[str]) -> dict:
    """Return a copy of `opts` with cookiesfrombrowser set for the
    given browser (or unchanged if browser is None)."""
    if not browser:
        return opts
    o = dict(opts)
    # yt-dlp expects a tuple: (browser, profile, keyring, container)
    o["cookiesfrombrowser"] = (browser, None, None, None)
    return o


def _extract_with_cookies(url, base_opts, download=False):
    """Run yt-dlp extract_info, trying cookie sources in order of
    reliability until one succeeds:

      1. an explicit cookies.txt  (set_cookie_file) - most reliable,
         and the only thing that works when Chrome-family cookies
         can't be decrypted (app-bound encryption / DPAPI error),
      2. no cookies at all        - fine for many public videos,
      3. each candidate browser   - Firefox first (it's the one that
         still works on Windows), then the Chromium family.

    Raises the last error if every attempt fails. This is the single
    choke-point that makes YouTube's "confirm you're not a bot" gate
    go away once a working cookie source is available.
    """
    import yt_dlp

    attempts = []
    cf = _COOKIE_FILE
    if cf and Path(cf).is_file():
        o = dict(base_opts)
        o["cookiefile"] = str(cf)
        attempts.append(o)
    attempts.append(dict(base_opts))                 # cookie-less
    for browser in _cookie_browsers_to_try():
        attempts.append(_add_cookies(base_opts, browser))

    last_err = None
    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("extract_info failed")
def _yt_try_handle(query: str) -> Optional[dict]:
    """If `query` looks like (or can be turned into) a channel
    handle, try resolving it directly. Returns a channel dict or
    None. This catches the common case where the user types a
    channel name that doesn't dominate video search results -
    e.g. "Nordischsound" resolves to youtube.com/@Nordischsound
    even though its videos may not rank highly for that word."""
    import yt_dlp
    # Build candidate handle URLs. Strip spaces (YouTube handles
    # have none) and try a couple of casings.
    raw = query.strip()
    handle = raw.lstrip("@").replace(" ", "")
    if not handle:
        return None
    candidates = [
        f"https://www.youtube.com/@{handle}",
    ]
    # If the user typed it with original casing, also try as-is and
    # lower-case (handles are case-insensitive on YouTube's side
    # but the URL resolver can be picky).
    if handle.lower() != handle:
        candidates.append(
            f"https://www.youtube.com/@{handle.lower()}")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
        "playlistend": 1,        # we only need channel metadata
    }
    for url in candidates:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            cid = (info.get("channel_id")
                   or info.get("uploader_id") or info.get("id"))
            cname = (info.get("channel")
                     or info.get("uploader")
                     or info.get("title") or handle)
            curl = (info.get("channel_url")
                    or info.get("uploader_url") or url)
            if cid or cname:
                return {"name": cname,
                        "channel_id": cid or curl,
                        "url": curl}
        except Exception:
            continue
    return None


def _yt_search_channels(query: str, limit: int = 15) -> list[dict]:
    """Search YouTube for channels matching `query`. Returns a list
    of {name, channel_id, url} dicts. Uses yt-dlp's flat extraction
    so it's fast (no per-video metadata).

    Strategy, in order:
      1. Direct handle resolution (youtube.com/@<query>) - catches
         channels whose videos don't rank for the query word, like
         "Nordischsound" -> @Nordischsound.
      2. YouTube's channel-search tab (search URL + the EgIQAQ
         params that filter results to channels only).
      3. A plain video search, collapsed to the unique channels
         behind the results, as a final fallback.
    The first channel from the direct-handle hit (if any) is put at
    the top of the list; the rest are de-duplicated by channel id.
    """
    import yt_dlp
    seen = {}

    # --- 1. direct handle ---
    direct = _yt_try_handle(query)
    if direct is not None:
        seen[direct["channel_id"]] = direct

    # --- 2. channel-filtered search ---
    # The EgIQAQ%3D%3D query parameter is YouTube's "filter:
    # channels" facet. yt-dlp follows it and returns channel
    # entries (_type == "url" with channel metadata).
    chan_search = (
        "https://www.youtube.com/results?search_query="
        + query.replace(" ", "+") + "&sp=EgIQAQ%253D%253D")
    opts_flat = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
        "playlistend": limit * 2,
    }
    try:
        with yt_dlp.YoutubeDL(opts_flat) as ydl:
            info = ydl.extract_info(chan_search, download=False)
        for entry in (info.get("entries") or []):
            cid = (entry.get("channel_id") or entry.get("id")
                   or entry.get("uploader_id"))
            cname = (entry.get("channel") or entry.get("title")
                     or entry.get("uploader") or "")
            if not cid or cid in seen:
                continue
            url = (entry.get("channel_url") or entry.get("url")
                   or entry.get("uploader_url")
                   or f"https://www.youtube.com/channel/{cid}")
            seen[cid] = {"name": cname or cid,
                         "channel_id": cid, "url": url}
            if len(seen) >= limit:
                break
    except Exception:
        pass

    # --- 3. video search, collapsed to channels ---
    if len(seen) < limit:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 20,
            "default_search": f"ytsearch{max(limit * 3, 20)}",
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            for entry in (info.get("entries") or []):
                cid = (entry.get("channel_id")
                       or entry.get("uploader_id"))
                cname = (entry.get("channel")
                         or entry.get("uploader") or "")
                if not cid or not cname or cid in seen:
                    continue
                url = (entry.get("channel_url")
                       or entry.get("uploader_url")
                       or f"https://www.youtube.com/channel/{cid}")
                seen[cid] = {"name": cname,
                             "channel_id": cid, "url": url}
                if len(seen) >= limit:
                    break
        except Exception:
            pass

    return list(seen.values())


def _yt_channel_tracks(channel_url: str,
                       limit: int = 60) -> list[dict]:
    """List a channel's uploads, newest first. Returns a list of
    {title, video_id, url, upload_date, duration} dicts.

    We point yt-dlp at the channel's /videos tab and use flat
    extraction (no per-video network hit) for speed. YouTube's
    /videos tab is already returned newest-first, but we re-sort by
    upload_date defensively in case the order ever changes.
    """
    import yt_dlp
    base = channel_url.rstrip("/")
    if not base.endswith("/videos"):
        base = base + "/videos"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
        "playlistend": limit,
    }
    tracks = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(base, download=False)
        for entry in (info.get("entries") or []):
            vid = entry.get("id")
            if not vid:
                continue
            # Pick a thumbnail. Flat extraction usually gives a
            # `thumbnails` list (smallest -> largest) and/or a
            # single `thumbnail`. We grab a mid-size one if the
            # list is there, else fall back to the YouTube
            # default thumbnail URL pattern for the video id.
            thumb = entry.get("thumbnail") or ""
            thumbs = entry.get("thumbnails") or []
            if thumbs:
                # Prefer something around 320px wide; thumbs are
                # usually sorted ascending, so take a middle one.
                mid = thumbs[min(len(thumbs) - 1, len(thumbs) // 2)]
                thumb = mid.get("url") or thumb
            if not thumb:
                thumb = (f"https://i.ytimg.com/vi/{vid}/"
                         "mqdefault.jpg")
            # Upload date. Flat channel extraction usually omits
            # `upload_date` but sometimes provides a `timestamp`
            # (Unix epoch). Derive YYYYMMDD from whatever we have;
            # otherwise leave it blank (the UI then shows no date
            # rather than a confusing "?").
            up = entry.get("upload_date") or ""
            if not up:
                ts = entry.get("timestamp")
                if ts:
                    try:
                        from datetime import datetime, timezone
                        up = datetime.fromtimestamp(
                            int(ts), tz=timezone.utc
                        ).strftime("%Y%m%d")
                    except Exception:
                        up = ""
            tracks.append({
                "title": entry.get("title") or vid,
                "video_id": vid,
                "url": (entry.get("url")
                        or f"https://www.youtube.com/watch?v={vid}"),
                "upload_date": up,
                "duration": entry.get("duration") or 0,
                "thumbnail": thumb,
            })
    # YouTube's /videos tab already returns newest-first. When
    # upload_date is present we sort by it (newest first); entries
    # without a date keep their original channel position via a
    # stable sort, so the list stays newest-first even if dates are
    # missing under flat extraction.
    for i, t in enumerate(tracks):
        t["_order"] = i
    def _key(t):
        d = t.get("upload_date") or ""
        # Tracks with a real date sort by it descending; dateless
        # ones fall back to their channel order. We invert the
        # index so that, combined with reverse=True below, channel
        # order is preserved among dateless entries.
        return (d if d else "00000000",
                -t["_order"])
    tracks.sort(key=_key, reverse=True)
    for t in tracks:
        t.pop("_order", None)
    return tracks


def _yt_audio_stream_url(video_url: str) -> tuple[str, str, int]:
    """Resolve the best audio-only stream URL for a video. Returns
    (stream_url, title, duration_seconds). Raises on failure.

    YouTube sometimes returns an info dict without a top-level
    `url` (e.g. when the chosen format is a merged A/V pair, or for
    certain restricted videos). We try, in order: the top-level
    url, requested_formats, then a manual scan of the full formats
    list for the best audio-only stream. If yt-dlp's default format
    selection fails entirely we retry with progressively looser
    format strings before giving up.
    """
    import yt_dlp

    def _pick_from_info(info):
        # 1. top-level url (audio-only format selected)
        u = info.get("url")
        if u:
            return u
        # 2. requested_formats (merged output components)
        for fmt in (info.get("requested_formats") or []):
            if (fmt.get("acodec") and fmt.get("acodec") != "none"
                    and fmt.get("url")):
                return fmt["url"]
        # 3. scan ALL formats for the best audio-only stream
        best = None
        best_abr = -1.0
        for fmt in (info.get("formats") or []):
            if (fmt.get("acodec") and fmt.get("acodec") != "none"
                    and (fmt.get("vcodec") in (None, "none"))
                    and fmt.get("url")):
                abr = fmt.get("abr") or fmt.get("tbr") or 0
                if abr > best_abr:
                    best_abr = abr
                    best = fmt["url"]
        if best:
            return best
        # 4. last resort: any format at all that has a url + audio
        for fmt in (info.get("formats") or []):
            if (fmt.get("url") and fmt.get("acodec")
                    and fmt.get("acodec") != "none"):
                return fmt["url"]
        return None

    last_err = None
    for fmt_str in ("bestaudio/best",
                    "bestaudio[ext=m4a]/bestaudio/best",
                    "worstaudio/worst",
                    "best"):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 20,
            "format": fmt_str,
            # Try multiple player clients and merge their formats so a
            # SABR-only "web" response doesn't kill resolution.
            "extractor_args": {
                "youtube": {"player_client": _PLAYER_CLIENTS}},
        }
        try:
            # Cookie-aware: retries with browser cookies if YouTube
            # demands "sign in to confirm you're not a bot".
            info = _extract_with_cookies(
                video_url, opts, download=False)
            url = _pick_from_info(info)
            if url:
                return (url,
                        info.get("title") or "",
                        int(info.get("duration") or 0))
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        msg = str(last_err)
        low = msg.lower()
        if ("sign in" in low or "not a bot" in low
                or "failed to decrypt" in low or "cookies" in low
                or "requested format" in low or "only images" in low
                or "sabr" in low):
            raise RuntimeError(
                "YouTube blocked this stream. Two things are going on "
                "right now:\n\n"
                "1) Bot gate: Chrome/Brave/Vivaldi cookies can't be "
                "read on Windows (DPAPI). Use the 'cookies.txt' button "
                "with an exported cookies.txt, or sign into YouTube in "
                "Firefox and set the cookie source to 'firefox'.\n"
                "2) YouTube is forcing SABR / PO-token on some clients, "
                "so formats come back without a URL ('Requested format "
                "is not available').\n\n"
                "The fix for both is almost always to UPDATE yt-dlp: "
                "click 'Update yt-dlp' (or run 'pip install -U yt-dlp') "
                "and restart Quopus. If it still fails after updating, "
                "the video needs a PO token provider - tell me and I'll "
                "wire that in.")
        raise RuntimeError(
            f"Could not resolve audio (tried several formats): "
            f"{last_err}")
    raise RuntimeError("No audio stream found for this video")


def _yt_fetch_dates(video_ids: list[str],
                    progress_cb=None) -> dict[str, str]:
    """Fetch the real upload_date for each video id. Flat channel
    extraction omits dates, so we query each video's metadata. To
    keep it reasonably fast we run several lookups in parallel via
    a thread pool. Returns {video_id: 'YYYYMMDD'}.

    `progress_cb(video_id, yyyymmdd)` is called as each date lands
    so the UI can update incrementally instead of waiting for the
    whole batch.

    We use the lightest extraction that still yields a date: a
    per-video extract_info with download disabled. yt-dlp caches
    the player response, so this is one cheap HTTP call per video.
    """
    import yt_dlp
    from concurrent.futures import ThreadPoolExecutor, as_completed

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        # We only want metadata, not format resolution - this
        # skips the expensive signature/format work.
        "extract_flat": False,
        "playlist_items": "0",
    }

    def _one(vid):
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            info = _extract_with_cookies(url, opts, download=False)
            up = info.get("upload_date") or ""
            if not up:
                ts = (info.get("timestamp")
                      or info.get("release_timestamp"))
                if ts:
                    from datetime import datetime, timezone
                    up = datetime.fromtimestamp(
                        int(ts), tz=timezone.utc).strftime("%Y%m%d")
            return vid, up
        except Exception:
            return vid, ""

    out = {}
    # 4 parallel lookups is a good balance: faster than serial,
    # without hammering YouTube hard enough to get throttled.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_one, v): v for v in video_ids}
        for fut in as_completed(futs):
            vid, up = fut.result()
            out[vid] = up
            if progress_cb is not None:
                try:
                    progress_cb(vid, up)
                except Exception:
                    pass
    return out


# =====================================================================
# Worker threads - keep all network / decode work off the UI thread
# =====================================================================
class _SearchThread(QThread):
    """Searches for channels matching a query."""
    done = pyqtSignal(list)         # list[dict]
    failed = pyqtSignal(str)

    def __init__(self, query, parent=None):
        super().__init__(parent)
        self._query = query

    def run(self):
        try:
            res = _yt_search_channels(self._query)
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class _TracksThread(QThread):
    """Fetches a channel's uploads, newest first."""
    done = pyqtSignal(list)         # list[dict]
    failed = pyqtSignal(str)

    def __init__(self, channel_url, parent=None):
        super().__init__(parent)
        self._url = channel_url

    def run(self):
        try:
            res = _yt_channel_tracks(self._url)
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class _DatesThread(QThread):
    """Fetches real upload dates for a list of videos in the
    background, emitting each as it arrives so the track list can
    fill in dates progressively and then re-sort newest-first."""
    one_date = pyqtSignal(str, str)   # video_id, 'YYYYMMDD'
    all_done = pyqtSignal()

    def __init__(self, video_ids, parent=None):
        super().__init__(parent)
        self._ids = list(video_ids)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        def cb(vid, up):
            if not self._cancel:
                self.one_date.emit(vid, up)
        try:
            _yt_fetch_dates(self._ids, progress_cb=cb)
        except Exception:
            pass
        self.all_done.emit()


class _ResolveThread(QThread):
    """Resolves a video's audio stream URL (a network call), then
    the player thread can start decoding."""
    done = pyqtSignal(str, str, int)   # url, title, duration_s
    failed = pyqtSignal(str)

    def __init__(self, video_url, parent=None):
        super().__init__(parent)
        self._url = video_url

    def run(self):
        try:
            url, title, dur = _yt_audio_stream_url(self._url)
            self.done.emit(url, title, dur)
        except Exception as e:
            self.failed.emit(str(e))


class _ThumbThread(QThread):
    """Downloads a track's thumbnail image (a small HTTP GET) so the
    player can show a preview without blocking the UI."""
    done = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, thumb_url, parent=None):
        super().__init__(parent)
        self._url = thumb_url

    def run(self):
        try:
            import urllib.request
            req = urllib.request.Request(
                self._url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            self.done.emit(data)
        except Exception as e:
            self.failed.emit(str(e))


class _AudioThread(QThread):
    """Decodes a stream URL to PCM via ffmpeg and plays it through
    sounddevice. Emits PCM blocks for the spectrum widget and a
    position read-out in seconds.

    The design mirrors mod_player.AudioThread: a blocking output
    stream pumped from a decode loop, with pause/stop/volume guarded
    by a lock. ffmpeg does the heavy lifting (any codec -> raw s16le
    stereo at 44.1 kHz), we just shovel its stdout into the speaker.

    Signals:
        block(np.ndarray)   - stereo int16 PCM block for the EQ
        position(float)     - seconds played so far
        finished()          - stream ended naturally
        failed(str)         - decode / playback error
        started(int)        - playback actually began; arg = total
                              duration in seconds (may be 0 if
                              unknown)
    """
    block = pyqtSignal(np.ndarray)
    position = pyqtSignal(float)
    finished = pyqtSignal()
    failed = pyqtSignal(str)
    started = pyqtSignal(int)

    SAMPLERATE = 44100
    CHANNELS = 2
    BLOCK_FRAMES = 2048           # frames per read/emit

    def __init__(self, stream_url, duration_s=0, parent=None):
        super().__init__(parent)
        self._url = stream_url
        self._duration = duration_s
        self._stop = False
        self._paused = False
        self._volume = 1.0
        self._lock = threading.Lock()
        self._proc = None
        # Seeking: when the user drags the slider we record the
        # target second here; the decode loop notices, kills the
        # current ffmpeg, and relaunches it with -ss <target> so
        # decoding resumes from that point. -1 means "no pending
        # seek".
        self._seek_to = -1.0
        self._start_offset = 0.0   # seconds the current ffmpeg's
                                   # output is offset by (-ss value)

    def stop(self):
        self._stop = True
        # Kill ffmpeg promptly so a blocking read returns.
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass

    def set_paused(self, paused: bool):
        self._paused = bool(paused)

    def set_volume(self, v: float):
        with self._lock:
            self._volume = max(0.0, min(1.0, float(v)))

    def seek(self, seconds: float):
        """Request a jump to `seconds`. The decode loop relaunches
        ffmpeg from that offset on its next iteration."""
        with self._lock:
            self._seek_to = max(0.0, float(seconds))
        # Kill the current ffmpeg so the blocking read returns
        # immediately and the loop can restart at the new offset.
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass

    def _spawn_ffmpeg(self, ffmpeg, offset_s: float):
        """Launch ffmpeg decoding from `offset_s` seconds in.
        Returns the Popen, or None on failure. The -ss before -i
        does a fast input seek (keyframe-accurate enough for audio
        and very quick even on long streams)."""
        cmd = [ffmpeg]
        if offset_s > 0.05:
            cmd += ["-ss", f"{offset_s:.2f}"]
        cmd += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", self._url,
            "-vn",                       # no video
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", str(self.CHANNELS),
            "-ar", str(self.SAMPLERATE),
            "-loglevel", "quiet",
            "pipe:1",
        ]
        try:
            return subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=64 * 1024)
        except Exception:
            return None

    def _read_exact(self, n):
        """Read EXACTLY n bytes from ffmpeg's stdout, looping until the
        block is full. Returns fewer bytes only at EOF or when ffmpeg
        is killed (seek/stop).

        This is the crackle fix: the pipe hands back short reads all the
        time, and the old code padded every short read with silence,
        injecting gaps into the MIDDLE of the stream. Filling the block
        first means we only ever play contiguous PCM."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        buf = bytearray()
        while len(buf) < n and not self._stop:
            try:
                chunk = proc.stdout.read(n - len(buf))
            except Exception:
                break
            if not chunk:
                break                      # EOF / ffmpeg killed
            buf += chunk
            if self._seek_to >= 0.0:       # seek pending: don't block
                break
        return bytes(buf)

    def run(self):
        try:
            import sounddevice as sd
        except Exception as e:
            self.failed.emit(
                f"sounddevice not available: {e}\n\n"
                "Install with:  pip install sounddevice")
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.failed.emit(
                "ffmpeg not found on PATH. Install ffmpeg to "
                "stream YouTube audio.")
            return
        self._start_offset = 0.0
        self._proc = self._spawn_ffmpeg(ffmpeg, 0.0)
        if self._proc is None:
            self.failed.emit("Failed to start ffmpeg")
            return
        try:
            stream = sd.OutputStream(
                samplerate=self.SAMPLERATE,
                channels=self.CHANNELS, dtype="int16",
                blocksize=self.BLOCK_FRAMES, latency="high")
            stream.start()
        except Exception as e:
            self.failed.emit(f"Failed to open audio output: {e}")
            try:
                self._proc.kill()
            except Exception:
                pass
            return

        self.started.emit(int(self._duration))
        bytes_per_block = self.BLOCK_FRAMES * self.CHANNELS * 2
        # frames decoded since the CURRENT ffmpeg started; absolute
        # position = _start_offset + frames_played / SR
        frames_played = 0
        silence = np.zeros((self.BLOCK_FRAMES, self.CHANNELS),
                           dtype=np.int16)
        emit_every = 0
        try:
            while not self._stop:
                # Handle a pending seek: relaunch ffmpeg at the
                # requested offset and reset the frame counter.
                with self._lock:
                    pending = self._seek_to
                    self._seek_to = -1.0
                if pending >= 0.0:
                    try:
                        if (self._proc
                                and self._proc.poll() is None):
                            self._proc.kill()
                    except Exception:
                        pass
                    self._start_offset = pending
                    frames_played = 0
                    self._proc = self._spawn_ffmpeg(ffmpeg, pending)
                    if self._proc is None:
                        self.failed.emit(
                            "Failed to restart ffmpeg after seek")
                        break
                    # Emit the new position immediately so the UI
                    # snaps to it even before audio resumes.
                    self.position.emit(pending)
                if self._paused:
                    stream.write(silence)
                    continue
                raw = self._read_exact(bytes_per_block)
                if not raw:
                    # Empty read can mean EOF, or that we just
                    # killed ffmpeg for a seek - only report
                    # finished when there's no pending seek.
                    with self._lock:
                        seeking = self._seek_to >= 0.0
                    if seeking or self._stop:
                        continue
                    self.finished.emit()
                    break
                # Normally a full block. At EOF (or right after a kill)
                # it can be short - just play what we have, trimmed to
                # whole frames. NO silence padding mid-stream: that was
                # the source of the crackle.
                frame_bytes = self.CHANNELS * 2
                usable = len(raw) - (len(raw) % frame_bytes)
                if usable <= 0:
                    continue
                chunk = np.frombuffer(
                    raw[:usable], dtype=np.int16).reshape(
                        -1, self.CHANNELS)
                with self._lock:
                    vol = self._volume
                out = chunk
                if vol != 1.0:
                    out = (chunk.astype(np.int32)
                           * int(vol * 256) // 256).astype(np.int16)
                stream.write(out)
                frames_played += chunk.shape[0]
                emit_every += 1
                if emit_every % 2 == 0:
                    self.block.emit(chunk.copy())
                    self.position.emit(
                        self._start_offset
                        + frames_played / float(self.SAMPLERATE))
        except Exception as e:
            if not self._stop:
                self.failed.emit(f"Playback error: {e}")
        finally:
            try:
                stream.stop(); stream.close()
            except Exception:
                pass
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.kill()
            except Exception:
                pass


# =====================================================================
# Helpers
# =====================================================================
def _fmt_mmss(seconds: float) -> str:
    """Format seconds as M:SS (or H:MM:SS for long tracks)."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_date(yyyymmdd: str) -> str:
    """Turn '20260131' into '2026-01-31'; pass through anything else."""
    if yyyymmdd and len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd or "?"


# =====================================================================
# The player window
# =====================================================================
class _PipUpdateThread(QThread):
    """Runs `pip install -U yt-dlp` off the UI thread. Emits
    done(ok, message)."""
    done = pyqtSignal(bool, str)

    def run(self):
        import sys as _sys
        import subprocess as _sp
        try:
            p = _sp.run(
                [_sys.executable, "-m", "pip", "install", "-U",
                 "yt-dlp"],
                capture_output=True, text=True, timeout=300)
            if p.returncode == 0:
                ver = ""
                try:
                    for line in (p.stdout or "").splitlines():
                        if "yt-dlp-" in line or "yt_dlp-" in line:
                            ver = line.strip()
                except Exception:
                    pass
                self.done.emit(True, ver or "updated")
            else:
                self.done.emit(
                    False, (p.stderr or p.stdout or "").strip()[:400])
        except Exception as e:
            self.done.emit(False, str(e)[:400])


class YouTubeAudioDialog(QDialog):
    """Non-modal YouTube audio player. Lives as long as the user
    keeps it open; Quopus stays fully usable behind it.

    Layout (top to bottom):
      * Search row: text field + Search button
      * A horizontal splitter:
          left  - search results / bookmarks (two stacked lists)
          right - track list for the selected bookmark
      * Now-playing strip: title, progress bar, played/total time
      * Spectrum EQ (segmented LED bars)
      * Transport: Play/Pause, Stop, volume
    """

    def __init__(self, main_window, parent=None):
        super().__init__(parent or main_window)
        # `main_window` is the QuopusMainWindow - we read/write its
        # config dict directly and use it as the dialog parent so
        # we inherit Quopus's window placement.
        self._mw = main_window
        self.setWindowTitle("YouTube Audio - Quopus Commander")
        self.resize(820, 600)
        # Non-modal so Quopus keeps working behind us.
        self.setModal(False)

        # State
        self._search_thread = None
        self._tracks_thread = None
        self._dates_thread = None
        self._resolve_thread = None
        self._thumb_thread = None
        self._audio_thread = None
        self._channels = []          # last search results
        self._tracks = []            # current bookmark's tracks
        self._cur_track = None       # dict of the playing track
        self._cur_channel_url = None
        self._cur_channel_name = ""
        self._total_secs = 0
        self._last_pos = 0.0          # most recent playback second
        self._last_pos_save = 0.0     # monotonic time of last save
        self._resume_pos = 0          # seconds to resume from

        self._build_ui()
        # Apply the saved cookie preferences (used to get past
        # YouTube's "confirm you're not a bot" gate).
        try:
            set_cookie_browser(
                self._cfg().get("youtube_cookies_browser", "auto"))
            set_cookie_file(
                self._cfg().get("youtube_cookies_file", ""))
        except Exception:
            pass
        self._load_bookmarks()
        self._restore_last_track()

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- search row ---
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Channel search:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            "Type an artist / channel name and press Enter")
        self._search_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self._search_edit, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search)
        search_row.addWidget(self._search_btn)
        root.addLayout(search_row)

        # --- main splitter ---
        split = QSplitter(Qt.Orientation.Horizontal)

        # left column: results + bookmarks
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("Search results (channels):"))
        self._results_list = QListWidget()
        self._results_list.itemDoubleClicked.connect(
            self._on_bookmark_add)
        left_lay.addWidget(self._results_list, 1)
        add_btn = QPushButton("Add selected channel to bookmarks")
        add_btn.clicked.connect(self._on_bookmark_add)
        left_lay.addWidget(add_btn)

        left_lay.addWidget(QLabel("Bookmarks:"))
        self._bm_list = QListWidget()
        self._bm_list.itemClicked.connect(self._on_bookmark_open)
        self._bm_list.itemDoubleClicked.connect(self._on_bookmark_open)
        left_lay.addWidget(self._bm_list, 1)
        rm_btn = QPushButton("Remove selected bookmark")
        rm_btn.clicked.connect(self._on_bookmark_remove)
        left_lay.addWidget(rm_btn)
        split.addWidget(left)

        # right column: tracks
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        self._tracks_label = QLabel("Tracks (newest first):")
        right_lay.addWidget(self._tracks_label)
        self._tracks_list = QListWidget()
        self._tracks_list.itemDoubleClicked.connect(
            self._on_track_play)
        right_lay.addWidget(self._tracks_list, 1)
        play_sel = QPushButton("Play selected track")
        play_sel.clicked.connect(self._on_track_play)
        right_lay.addWidget(play_sel)
        split.addWidget(right)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 420])
        root.addWidget(split, 1)

        # --- now-playing strip: thumbnail + (title / seek / time) ---
        np_row = QHBoxLayout()
        # Thumbnail preview. Fixed 16:9 box; we scale the downloaded
        # image into it keeping aspect ratio.
        self._thumb = QLabel()
        self._thumb.setFixedSize(160, 90)
        self._thumb.setStyleSheet(
            "QLabel { background: #111; border: 1px solid #444; }")
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setText("no\npreview")
        np_row.addWidget(self._thumb)

        np_right = QVBoxLayout()
        self._now_label = QLabel("Nothing playing")
        f = self._now_label.font()
        f.setBold(True)
        self._now_label.setFont(f)
        self._now_label.setWordWrap(True)
        # Allow the label to shrink below its content width so a
        # very long track title can't push the whole dialog wider
        # than the screen. The label takes whatever width the
        # layout gives it and wraps within that.
        from PyQt6.QtWidgets import QSizePolicy
        self._now_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred)
        self._now_label.setMinimumWidth(0)
        np_right.addWidget(self._now_label)

        prog_row = QHBoxLayout()
        # Seek slider: 0..1000 maps to 0..duration. The user can
        # click or drag it to jump within the track. We track
        # whether the user is actively dragging so the periodic
        # position updates from the audio thread don't fight the
        # handle while they're holding it.
        self._seeking = False
        self._seek = QSlider(Qt.Orientation.Horizontal)
        self._seek.setRange(0, 1000)
        self._seek.setValue(0)
        self._seek.setEnabled(False)
        self._seek.sliderPressed.connect(self._on_seek_pressed)
        self._seek.sliderReleased.connect(self._on_seek_released)
        # Click-to-seek on the groove (not just drag): jump when
        # the value changes from a click while not dragging.
        self._seek.actionTriggered.connect(self._on_seek_action)
        prog_row.addWidget(self._seek, 1)
        self._time_label = QLabel("0:00 of 0:00")
        self._time_label.setMinimumWidth(120)
        self._time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter)
        prog_row.addWidget(self._time_label)
        np_right.addLayout(prog_row)
        np_right.addStretch(1)
        np_row.addLayout(np_right, 1)
        root.addLayout(np_row)

        # --- spectrum EQ ---
        self._spectrum = _Spectrum()
        self._spectrum.setMinimumHeight(120)
        # Cap the EQ height and let it expand horizontally only
        # within the dialog. Without an explicit vertical policy
        # some spectrum widgets report a huge sizeHint and the
        # layout grows the window. Fixed-ish height, expanding
        # width, keeps it a tidy band.
        from PyQt6.QtWidgets import QSizePolicy
        self._spectrum.setMaximumHeight(160)
        self._spectrum.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed)
        root.addWidget(self._spectrum)

        # --- transport ---
        trans = QHBoxLayout()
        self._play_btn = QPushButton("Play")
        self._play_btn.clicked.connect(self._on_play_pause)
        self._play_btn.setEnabled(False)
        trans.addWidget(self._play_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        trans.addWidget(self._stop_btn)
        trans.addStretch(1)
        # Cookie source: which browser yt-dlp reads YouTube cookies
        # from, to satisfy the "confirm you're not a bot" gate.
        trans.addWidget(QLabel("Cookies:"))
        from PyQt6.QtWidgets import QComboBox
        self._cookie_combo = QComboBox()
        self._cookie_combo.addItem("Auto-detect", "auto")
        for b in _BROWSER_CANDIDATES:
            self._cookie_combo.addItem(b.capitalize(), b)
        self._cookie_combo.addItem("None", "none")
        # Reflect the saved setting.
        saved = (self._cfg().get("youtube_cookies_browser")
                 or "auto")
        idx = self._cookie_combo.findData(saved)
        if idx >= 0:
            self._cookie_combo.setCurrentIndex(idx)
        self._cookie_combo.setToolTip(
            "Which browser to read YouTube cookies from. If "
            "playback fails with a 'confirm you're not a bot' "
            "error, pick the browser you're signed into YouTube "
            "with.")
        self._cookie_combo.currentIndexChanged.connect(
            self._on_cookie_browser_changed)
        trans.addWidget(self._cookie_combo)
        self._cookie_file_btn = QPushButton("cookies.txt\u2026")
        self._cookie_file_btn.setToolTip(
            "Use an exported cookies.txt - the most reliable option on "
            "Windows, where Chrome/Brave/Vivaldi cookies can't be "
            "decrypted. Export it with the 'Get cookies.txt LOCALLY' "
            "extension while signed into YouTube, then pick it here.")
        self._cookie_file_btn.clicked.connect(self._on_pick_cookie_file)
        trans.addWidget(self._cookie_file_btn)
        self._yt_update_btn = QPushButton("Update yt-dlp")
        self._yt_update_btn.setToolTip(
            "Update yt-dlp to the latest version. YouTube changes its "
            "anti-bot / SABR handling constantly, and an outdated "
            "yt-dlp is the #1 reason playback that 'worked before' "
            "suddenly stops. Restart Quopus afterwards.")
        self._yt_update_btn.clicked.connect(self._on_update_ytdlp)
        trans.addWidget(self._yt_update_btn)
        trans.addWidget(QLabel("Volume:"))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setValue(80)
        self._vol.setMaximumWidth(160)
        self._vol.valueChanged.connect(self._on_volume)
        trans.addWidget(self._vol)
        root.addLayout(trans)

        # --- status line ---
        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")
        root.addWidget(self._status)

    # -----------------------------------------------------------------
    # Persistence (bookmarks + last track live in Quopus's config)
    # -----------------------------------------------------------------
    def _cfg(self):
        return getattr(self._mw, "config", {})

    def _save_state(self):
        """Write bookmarks + last track back to the config and
        persist to disk."""
        cfg = self._cfg()
        cfg["youtube_bookmarks"] = [
            {"name": self._bm_list.item(i).text(),
             "url": self._bm_list.item(i).data(
                 Qt.ItemDataRole.UserRole)}
            for i in range(self._bm_list.count())
        ]
        if self._cur_track:
            cfg["youtube_last_track"] = self._cur_track
        try:
            from .config import save_config
            save_config(cfg)
        except Exception:
            pass

    def _cache_get(self, channel_url):
        """Return the cached track list for a channel, or []."""
        cache = self._cfg().get("youtube_track_cache") or {}
        return cache.get(channel_url) or []

    def _cache_put(self, channel_url, tracks):
        """Store the track list for a channel and persist. We strip
        transient UI flags (_order, _date_pending) before saving so
        the cache stays clean JSON."""
        cfg = self._cfg()
        cache = cfg.get("youtube_track_cache")
        if not isinstance(cache, dict):
            cache = {}
        clean = []
        for t in tracks:
            ct = {k: v for k, v in t.items()
                  if not k.startswith("_")}
            clean.append(ct)
        cache[channel_url] = clean
        cfg["youtube_track_cache"] = cache
        try:
            from .config import save_config
            save_config(cfg)
        except Exception:
            pass

    def _load_bookmarks(self):
        cfg = self._cfg()
        self._bm_list.clear()
        for bm in (cfg.get("youtube_bookmarks") or []):
            it = QListWidgetItem(bm.get("name", "?"))
            it.setData(Qt.ItemDataRole.UserRole, bm.get("url", ""))
            self._bm_list.addItem(it)

    def _restore_last_track(self):
        cfg = self._cfg()
        last = cfg.get("youtube_last_track")
        if last and last.get("title"):
            self._cur_track = last
            pos = int(last.get("position", 0) or 0)
            self._resume_pos = pos
            dur = int(last.get("duration", 0) or 0)
            if pos > 0:
                where = _fmt_mmss(pos)
                if dur:
                    where += f" of {_fmt_mmss(dur)}"
                self._set_now(
                    f"Last played: {last['title']}  "
                    f"(resume at {where} - press Play)")
            else:
                self._set_now(
                    f"Last played: {last['title']}  (press Play)")
            self._play_btn.setEnabled(True)
            # Show its preview if we saved a thumbnail URL.
            self._load_thumb(last.get("thumbnail", ""))

    # -----------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------
    def _on_search(self):
        q = self._search_edit.text().strip()
        if not q:
            return
        if self._search_thread and self._search_thread.isRunning():
            return
        self._set_status(f"Searching for channels: {q} ...")
        self._search_btn.setEnabled(False)
        self._results_list.clear()
        self._search_thread = _SearchThread(q, self)
        self._search_thread.done.connect(self._on_search_done)
        self._search_thread.failed.connect(self._on_search_failed)
        self._search_thread.start()

    def _on_search_done(self, channels):
        self._search_btn.setEnabled(True)
        self._channels = channels
        self._results_list.clear()
        if not channels:
            self._set_status("No channels found.")
            return
        for ch in channels:
            it = QListWidgetItem(ch["name"])
            it.setData(Qt.ItemDataRole.UserRole, ch["url"])
            self._results_list.addItem(it)
        self._set_status(f"Found {len(channels)} channel(s). "
                         "Double-click one to bookmark it.")

    def _on_search_failed(self, err):
        self._search_btn.setEnabled(True)
        self._set_status(f"Search failed: {err}")

    # -----------------------------------------------------------------
    # Bookmarks
    # -----------------------------------------------------------------
    def _on_bookmark_add(self, *_):
        it = self._results_list.currentItem()
        if it is None:
            self._set_status("Select a channel in the results first.")
            return
        name = it.text()
        url = it.data(Qt.ItemDataRole.UserRole)
        # Avoid duplicates by URL.
        for i in range(self._bm_list.count()):
            if self._bm_list.item(i).data(
                    Qt.ItemDataRole.UserRole) == url:
                self._set_status(f"'{name}' is already bookmarked.")
                return
        bm = QListWidgetItem(name)
        bm.setData(Qt.ItemDataRole.UserRole, url)
        self._bm_list.addItem(bm)
        self._save_state()
        self._set_status(f"Bookmarked '{name}'.")

    def _on_bookmark_remove(self):
        it = self._bm_list.currentItem()
        if it is None:
            return
        row = self._bm_list.row(it)
        self._bm_list.takeItem(row)
        self._save_state()
        self._set_status("Bookmark removed.")

    def _on_bookmark_open(self, item):
        if item is None:
            return
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url:
            return
        if self._tracks_thread and self._tracks_thread.isRunning():
            return
        self._cur_channel_url = url
        self._cur_channel_name = item.text()
        # Show cached tracks immediately (with their saved dates),
        # so the list isn't blank while we hit the network.
        cached = self._cache_get(url)
        if cached:
            self._tracks = cached
            self._repopulate_tracks()
            self._tracks_label.setText(
                f"Tracks for '{item.text()}' "
                f"({len(cached)} cached, checking for new...)")
        else:
            self._tracks_list.clear()
            self._tracks_label.setText(
                f"Tracks for '{item.text()}' (loading...)")
        self._set_status(f"Loading uploads for {item.text()} ...")
        self._tracks_thread = _TracksThread(url, self)
        self._tracks_thread.done.connect(
            lambda tr, n=item.text(): self._on_tracks_done(tr, n))
        self._tracks_thread.failed.connect(self._on_tracks_failed)
        self._tracks_thread.start()

    @staticmethod
    def _track_label(t):
        """Build a track list label, dropping missing pieces so we
        never show '[?]' or '(?)'."""
        parts = []
        if t.get("upload_date"):
            parts.append(f"[{_fmt_date(t['upload_date'])}]")
        elif t.get("_date_pending"):
            parts.append("[...]")     # date is being fetched
        parts.append(t.get("title", "?"))
        if t.get("duration"):
            parts.append(f"({_fmt_mmss(t['duration'])})")
        return "  ".join(parts)

    def _repopulate_tracks(self):
        """Refill the track list widget from self._tracks, keeping
        the current selection on the same video if possible."""
        cur_vid = None
        cur = self._tracks_list.currentItem()
        if cur is not None:
            d = cur.data(Qt.ItemDataRole.UserRole)
            if d:
                cur_vid = d.get("video_id")
        self._tracks_list.clear()
        sel_row = -1
        for i, t in enumerate(self._tracks):
            it = QListWidgetItem(self._track_label(t))
            it.setData(Qt.ItemDataRole.UserRole, t)
            self._tracks_list.addItem(it)
            if cur_vid and t.get("video_id") == cur_vid:
                sel_row = i
        if sel_row >= 0:
            self._tracks_list.setCurrentRow(sel_row)

    def _on_tracks_done(self, tracks, channel_name):
        self._cur_channel_name = channel_name
        if not tracks:
            # Network gave nothing - keep whatever cache we showed.
            if not self._tracks:
                self._tracks_list.clear()
                self._tracks_label.setText(
                    f"Tracks for '{channel_name}' (none found)")
                self._set_status(
                    "No uploads found for that channel.")
            return
        # Merge: reuse upload_date (and any data) we already know
        # for tracks we've seen before, so only genuinely NEW
        # tracks need a date lookup. Build a lookup of known dates
        # from both the current list and the on-disk cache.
        known = {}
        url = getattr(self, "_cur_channel_url", None)
        for src in (self._tracks, self._cache_get(url) if url else []):
            for t in src:
                vid = t.get("video_id")
                if vid and t.get("upload_date"):
                    known[vid] = t["upload_date"]
        for t in tracks:
            if not t.get("upload_date"):
                kd = known.get(t.get("video_id"))
                if kd:
                    t["upload_date"] = kd
            if not t.get("upload_date"):
                t["_date_pending"] = True
        self._tracks = tracks
        self._repopulate_tracks()

        # Only NEW tracks (no date from cache either) need fetching.
        need = [t["video_id"] for t in tracks
                if t.get("_date_pending")]
        if need:
            self._tracks_label.setText(
                f"Tracks for '{channel_name}' "
                f"({len(tracks)}, {len(need)} new - fetching "
                "dates...):")
            self._set_status(
                f"Fetching upload dates for {len(need)} new "
                "tracks ...")
            if (self._dates_thread
                    and self._dates_thread.isRunning()):
                self._dates_thread.cancel()
                self._dates_thread.wait(500)
            self._dates_thread = _DatesThread(need, self)
            self._dates_thread.one_date.connect(self._on_one_date)
            self._dates_thread.all_done.connect(
                self._on_dates_done)
            self._dates_thread.start()
        else:
            # All dates known from cache - sort + persist now.
            self._on_dates_done()

    def _on_one_date(self, video_id, yyyymmdd):
        # Update the matching track dict; label refresh happens in
        # bulk on all_done to avoid churning the list per date.
        for t in self._tracks:
            if t.get("video_id") == video_id:
                t["_date_pending"] = False
                if yyyymmdd:
                    t["upload_date"] = yyyymmdd
                break

    def _on_dates_done(self):
        # Re-sort newest-first now that dates are in, then refill.
        for i, t in enumerate(self._tracks):
            t["_order"] = i
            t.pop("_date_pending", None)
        def _key(t):
            d = t.get("upload_date") or ""
            return (d if d else "00000000", -t["_order"])
        self._tracks.sort(key=_key, reverse=True)
        for t in self._tracks:
            t.pop("_order", None)
        self._repopulate_tracks()
        name = getattr(self, "_cur_channel_name", "")
        self._tracks_label.setText(
            f"Tracks for '{name}' "
            f"({len(self._tracks)}, newest first):")
        self._set_status("Double-click a track to stream its audio.")
        # Persist the now-dated, sorted list to the per-bookmark
        # cache so the next open is instant.
        url = getattr(self, "_cur_channel_url", None)
        if url:
            self._cache_put(url, self._tracks)

    def _on_tracks_failed(self, err):
        self._tracks_label.setText("Tracks (failed)")
        self._set_status(f"Failed to load uploads: {err}")

    # -----------------------------------------------------------------
    # Playback
    # -----------------------------------------------------------------
    def _on_track_play(self, *_):
        it = self._tracks_list.currentItem()
        if it is None:
            self._set_status("Select a track first.")
            return
        track = it.data(Qt.ItemDataRole.UserRole)
        self._start_track(track)

    def _start_track(self, track: dict, resume_at: int = 0):
        # Stop anything currently playing.
        self._stop_audio()
        self._cur_track = track
        # Remember where to jump once audio is rolling (0 = start).
        self._resume_pos = max(0, int(resume_at or 0))
        self._set_now(f"Resolving: {track['title']} ...")
        self._set_status("Resolving audio stream ...")
        self._play_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        # Load the preview thumbnail (separate, non-critical).
        self._load_thumb(track.get("thumbnail", ""))
        # Resolve the stream URL in a thread (network call), then
        # the resolve thread's `done` kicks off the audio thread.
        self._resolve_thread = _ResolveThread(track["url"], self)
        self._resolve_thread.done.connect(self._on_resolved)
        self._resolve_thread.failed.connect(self._on_resolve_failed)
        self._resolve_thread.start()

    def _load_thumb(self, thumb_url: str):
        """Fetch + show the track thumbnail. No-op if we have no
        URL; failure just leaves the 'no preview' placeholder."""
        self._thumb.setText("loading...")
        if not thumb_url:
            self._thumb.setText("no\npreview")
            return
        # Cancel a previous in-flight thumb load.
        if self._thumb_thread and self._thumb_thread.isRunning():
            try:
                self._thumb_thread.quit()
                self._thumb_thread.wait(500)
            except Exception:
                pass
        self._thumb_thread = _ThumbThread(thumb_url, self)
        self._thumb_thread.done.connect(self._on_thumb_loaded)
        self._thumb_thread.failed.connect(
            lambda _e: self._thumb.setText("no\npreview"))
        self._thumb_thread.start()

    def _on_thumb_loaded(self, data: bytes):
        from PyQt6.QtGui import QPixmap
        pm = QPixmap()
        if pm.loadFromData(data):
            scaled = pm.scaled(
                self._thumb.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._thumb.setPixmap(scaled)
        else:
            self._thumb.setText("no\npreview")

    def _on_resolved(self, stream_url, title, duration_s):
        # Persist this as the last-played track now that it worked.
        if self._cur_track is not None:
            self._cur_track["title"] = (title
                                        or self._cur_track["title"])
            if duration_s:
                self._cur_track["duration"] = duration_s
        self._save_state()
        self._total_secs = duration_s or (
            self._cur_track.get("duration", 0)
            if self._cur_track else 0)
        self._audio_thread = _AudioThread(
            stream_url, duration_s=self._total_secs, parent=self)
        self._audio_thread.started.connect(self._on_audio_started)
        self._audio_thread.block.connect(self._spectrum.feed_block)
        self._audio_thread.position.connect(self._on_position)
        self._audio_thread.finished.connect(self._on_audio_finished)
        self._audio_thread.failed.connect(self._on_audio_failed)
        self._audio_thread.set_volume(self._vol.value() / 100.0)
        self._audio_thread.start()

    def _on_resolve_failed(self, err):
        self._now_label.setText("Resolve failed")
        self._set_status(f"Could not resolve audio: {err}")
        self._play_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    def _on_audio_started(self, total_secs):
        if total_secs:
            self._total_secs = total_secs
        title = (self._cur_track.get("title", "")
                 if self._cur_track else "")
        self._set_now(f"Playing: {title}")
        self._play_btn.setText("Pause")
        self._play_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        # Seeking only makes sense if we know the duration.
        self._seek.setEnabled(self._total_secs > 0)
        # If we're resuming the last track, jump to where we left
        # off. Guard against a stale position past the track end.
        if self._resume_pos > 0 and self._audio_thread is not None:
            tgt = self._resume_pos
            if self._total_secs:
                # Leave a little room before the very end.
                tgt = min(tgt, max(0, self._total_secs - 3))
            if tgt > 0:
                self._audio_thread.seek(tgt)
                self._set_status(f"Resumed at {_fmt_mmss(tgt)}.")
        self._resume_pos = 0
        if not self._status.text():
            self._set_status("")

    def _on_position(self, secs):
        played = _fmt_mmss(secs)
        total = _fmt_mmss(self._total_secs) if self._total_secs else "?"
        # "played X of Y minutes" style read-out.
        self._time_label.setText(f"{played} of {total}")
        # Don't move the handle while the user is dragging it.
        if self._total_secs > 0 and not self._seeking:
            frac = max(0.0, min(1.0, secs / self._total_secs))
            # Update without firing valueChanged/actionTriggered.
            self._seek.blockSignals(True)
            self._seek.setValue(int(frac * 1000))
            self._seek.blockSignals(False)
        # Remember where we are so we can resume here next session.
        # Persisting every position tick (several per second) would
        # hammer the config file, so we only write to disk at most
        # once every few seconds.
        self._last_pos = secs
        if self._cur_track is not None:
            self._cur_track["position"] = int(secs)
        import time
        now = time.monotonic()
        if now - getattr(self, "_last_pos_save", 0.0) >= 5.0:
            self._last_pos_save = now
            self._save_state()

    # --- seek slider handlers ---
    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_released(self):
        self._seeking = False
        self._do_seek(self._seek.value())

    def _on_seek_action(self, action):
        # actionTriggered fires for groove clicks and arrow steps
        # too, not just drags. We only treat NON-drag actions here
        # (a click jumps immediately); drags are handled on release
        # so we don't relaunch ffmpeg on every pixel.
        from PyQt6.QtWidgets import QAbstractSlider
        move_actions = (
            QAbstractSlider.SliderAction.SliderPageStepAdd,
            QAbstractSlider.SliderAction.SliderPageStepSub,
            QAbstractSlider.SliderAction.SliderSingleStepAdd,
            QAbstractSlider.SliderAction.SliderSingleStepSub,
        )
        if action in move_actions and not self._seeking:
            # Qt updates sliderPosition for these; jump to it.
            self._do_seek(self._seek.sliderPosition())

    def _do_seek(self, slider_val):
        """Translate a 0..1000 slider value to a track position in
        seconds and tell the audio thread to jump there."""
        if (self._total_secs <= 0 or self._audio_thread is None
                or not self._audio_thread.isRunning()):
            return
        target = (slider_val / 1000.0) * self._total_secs
        self._audio_thread.seek(target)
        self._time_label.setText(
            f"{_fmt_mmss(target)} of {_fmt_mmss(self._total_secs)}")
        self._set_status(f"Seeking to {_fmt_mmss(target)} ...")

    def _on_audio_finished(self):
        self._play_btn.setText("Play")
        self._stop_btn.setEnabled(False)
        self._seek.setEnabled(False)
        self._spectrum.reset()
        self._now_label.setText(
            (self._cur_track.get("title", "")
             if self._cur_track else "") + "  (finished)")
        self._set_status("Track finished.")

    def _on_audio_failed(self, err):
        self._play_btn.setText("Play")
        self._play_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._seek.setEnabled(False)
        self._spectrum.reset()
        self._set_status(f"Playback failed: {err}")

    def _on_play_pause(self):
        # If we have a resolved/playing thread, toggle pause.
        if self._audio_thread and self._audio_thread.isRunning():
            if self._play_btn.text() == "Pause":
                self._audio_thread.set_paused(True)
                self._play_btn.setText("Play")
                self._set_status("Paused.")
            else:
                self._audio_thread.set_paused(False)
                self._play_btn.setText("Pause")
                self._set_status("")
            return
        # Otherwise (re)start the last/current track from scratch,
        # resuming at the saved position if we have one.
        if self._cur_track:
            resume = int(self._cur_track.get("position", 0) or 0)
            self._start_track(self._cur_track, resume_at=resume)

    def _on_stop(self):
        self._stop_audio()
        self._play_btn.setText("Play")
        self._play_btn.setEnabled(bool(self._cur_track))
        self._stop_btn.setEnabled(False)
        self._seek.setEnabled(False)
        self._seek.blockSignals(True)
        self._seek.setValue(0)
        self._seek.blockSignals(False)
        self._time_label.setText("0:00 of 0:00")
        self._spectrum.reset()
        self._set_status("Stopped.")

    def _on_volume(self, v):
        if self._audio_thread:
            self._audio_thread.set_volume(v / 100.0)

    def _stop_audio(self):
        if self._audio_thread:
            try:
                self._audio_thread.stop()
                self._audio_thread.wait(2000)
            except Exception:
                pass
            self._audio_thread = None

    # -----------------------------------------------------------------
    def _on_cookie_browser_changed(self, _idx):
        """Persist + apply the chosen cookie browser."""
        browser = self._cookie_combo.currentData() or "auto"
        set_cookie_browser(browser)
        self._cfg()["youtube_cookies_browser"] = browser
        try:
            from .config import save_config
            save_config(self._cfg())
        except Exception:
            pass
        self._set_status(
            f"Cookie source set to '{browser}'. "
            "Try playing a track again.")

    def _on_update_ytdlp(self):
        """Update yt-dlp via pip (best-effort, off the UI thread)."""
        import sys as _sys
        if getattr(_sys, "frozen", False):
            self._set_status(
                "Bundled build: pip can't update a frozen app. "
                "Update the bundled yt-dlp, or run Quopus from source "
                "to use 'Update yt-dlp'.")
            return
        self._yt_update_btn.setEnabled(False)
        self._set_status("Updating yt-dlp - this can take a moment...")
        self._pip_thread = _PipUpdateThread(self)
        self._pip_thread.done.connect(self._on_ytdlp_updated)
        self._pip_thread.start()

    def _on_ytdlp_updated(self, ok, text):
        self._yt_update_btn.setEnabled(True)
        if ok:
            self._set_status(
                "yt-dlp updated (%s). RESTART Quopus, then try a "
                "track again." % text)
        else:
            self._set_status("yt-dlp update failed: %s" % text)

    def _on_pick_cookie_file(self):
        """Choose (or clear) an exported cookies.txt for yt-dlp."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        cur = self._cfg().get("youtube_cookies_file", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select exported cookies.txt", cur,
            "Cookies (*.txt);;All files (*)")
        if not path:
            if cur and QMessageBox.question(
                    self, "Cookies",
                    "Clear the saved cookies.txt and fall back to the "
                    "browser cookie source?"
                    ) == QMessageBox.StandardButton.Yes:
                path = ""
            else:
                return
        self._cfg()["youtube_cookies_file"] = path
        set_cookie_file(path)
        try:
            from .config import save_config
            save_config(self._cfg())
        except Exception:
            pass
        self._set_status(
            "Using cookies.txt. Try a track again."
            if path else "cookies.txt cleared.")

    def _set_status(self, msg):
        self._status.setText(msg)

    def _set_now(self, text):
        """Set the now-playing label, hard-truncating absurdly long
        titles so a pathological title can't stretch the layout
        even before word-wrap kicks in."""
        if text and len(text) > 140:
            text = text[:137] + "..."
        self._now_label.setText(text)

    def closeEvent(self, ev):
        # Clean up threads so we don't leak ffmpeg processes or
        # crash on exit.
        self._stop_audio()
        for t in (self._search_thread, self._tracks_thread,
                  self._resolve_thread, self._thumb_thread,
                  self._dates_thread):
            try:
                if t and t.isRunning():
                    t.quit(); t.wait(1000)
            except Exception:
                pass
        self._save_state()
        super().closeEvent(ev)


# =====================================================================
# Spectrum widget - reuse Quopus's LED EQ if present, else a local
# minimal fallback so the module works standalone.
# =====================================================================
def _make_spectrum_class():
    try:
        from .spectrum import SpectrumAnalyzer
        return SpectrumAnalyzer
    except Exception:
        # Minimal fallback: a flat label. Keeps the module working
        # even if the host's spectrum module isn't importable.
        class _Fallback(QFrame):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setFrameShape(QFrame.Shape.Box)
                self._lbl = QLabel("(spectrum unavailable)", self)
            def feed_block(self, *a, **k):
                pass
            def reset(self):
                pass
        return _Fallback


_Spectrum = _make_spectrum_class()


# =====================================================================
# Integration entry points (called from actions.Actions)
# =====================================================================
# Keep a single window instance per Quopus session so re-triggering
# the action just raises the existing player instead of stacking
# windows.
_PLAYER_WINDOW = None


def check_available(parent=None) -> bool:
    """Return True if all dependencies are present. Otherwise pop a
    message box listing what to install and return False."""
    missing = _missing_deps()
    if not missing:
        return True
    QMessageBox.warning(
        parent, "YouTube Audio - missing dependencies",
        "This feature needs the following:\n\n  - "
        + "\n  - ".join(missing)
        + "\n\nInstall them and try again.")
    return False


def open_youtube_audio(main_window):
    """Open (or raise) the YouTube audio player window. Non-modal,
    so Quopus stays fully usable behind it. Called by
    actions.Actions.act_youtube_audio."""
    global _PLAYER_WINDOW
    if not check_available(main_window):
        return
    # Reuse an existing window if it's still open.
    if _PLAYER_WINDOW is not None:
        try:
            if _PLAYER_WINDOW.isVisible():
                _PLAYER_WINDOW.raise_()
                _PLAYER_WINDOW.activateWindow()
                return
        except Exception:
            pass
    _PLAYER_WINDOW = YouTubeAudioDialog(main_window)
    # show() (not exec()) keeps it non-modal so Quopus stays usable.
    _PLAYER_WINDOW.show()
    _PLAYER_WINDOW.raise_()
    _PLAYER_WINDOW.activateWindow()
