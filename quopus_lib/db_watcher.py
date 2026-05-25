"""Filesystem watcher: monitors registered folders and updates
the Quopus database when files appear, change, or get deleted.

Design:
  - Uses the `watchdog` package if available (~1MB, pip install)
  - Falls back to manual polling every 60s if watchdog isn't there
  - Tracks one or more "watched roots" persistently (saved in
    config/watched_folders.json) so the watcher survives Quopus
    restarts and resumes monitoring on the next launch
  - Debounces rapid-fire events: file copies often produce a
    burst of write events for one file, so we wait 2s after the
    last event before ingesting

Public API:
  start_watcher() - launch the background thread if not already
  stop_watcher()  - request the watcher to shut down
  add_watched_folder(path)
  remove_watched_folder(path)
  list_watched_folders() -> [str]
  is_running() -> bool

The watcher runs in its own Python thread and feeds work into the
existing Scanner class. SQLite handles concurrent reads + one
writer fine in WAL mode (which we set in database.py), so this
plays well with the UI doing searches while the watcher is
busy ingesting.
"""
import json
import threading
import time
from pathlib import Path
from typing import Optional

from .config import CONFIG_DIR
from . import database
from . import db_scanner


_WATCHED_FILE = CONFIG_DIR / "watched_folders.json"


# Singleton watcher thread, started lazily on first use
_watcher_thread: Optional["_WatcherThread"] = None
_watcher_lock = threading.Lock()


# ============================================================
# Config persistence
# ============================================================


def list_watched_folders() -> list[str]:
    """Return the list of folder paths currently being watched."""
    if not _WATCHED_FILE.is_file():
        return []
    try:
        with open(_WATCHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(p) for p in data]
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _save_watched_folders(folders: list[str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_WATCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(folders)), f, indent=2)


def add_watched_folder(path) -> bool:
    """Add a folder to the watch list. Returns True if added,
    False if already watched. Triggers an initial scan of the
    folder if Quopus hasn't indexed it before."""
    path = str(Path(path).resolve())
    current = list_watched_folders()
    if path in current:
        return False
    current.append(path)
    _save_watched_folders(current)
    # If a watcher is running, tell it about the new folder so it
    # starts monitoring immediately instead of waiting for restart
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_running():
        _watcher_thread.refresh_watches()
    return True


def remove_watched_folder(path) -> bool:
    """Remove a folder from the watch list. Returns True if
    removed, False if it wasn't watched."""
    path = str(Path(path).resolve())
    current = list_watched_folders()
    if path not in current:
        return False
    current.remove(path)
    _save_watched_folders(current)
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_running():
        _watcher_thread.refresh_watches()
    return True


# ============================================================
# Watcher thread
# ============================================================


class _WatcherThread(threading.Thread):
    """The background thread that maintains FS watchers and
    processes events. Singleton - never instantiate directly,
    use start_watcher().

    Two implementations side-by-side:
      - With watchdog: native FS notifications (inotify on
        Linux, FSEvents on macOS, ReadDirectoryChangesW on
        Windows). Near-zero latency.
      - Without watchdog: polls every 60s for new/changed files
        (uses the same incremental-scan logic Scanner has).
    """

    def __init__(self):
        super().__init__(daemon=True, name="QuopusFSWatcher")
        # Use _stop_event rather than _stop because threading.Thread
        # has an internal _stop() method we'd otherwise shadow,
        # which breaks join() with a "Event not callable" error.
        self._stop_event = threading.Event()
        self._refresh = threading.Event()
        # Queue of (path, action) tuples to process. We use a
        # plain dict-of-set keyed by path so duplicate events for
        # the same file collapse into one ingest.
        self._pending: dict[str, float] = {}
        self._pending_lock = threading.Lock()
        # Debounce: don't ingest a file until 2s after the last
        # event for it. Catches file-copy bursts that produce
        # many write events.
        self._debounce_seconds = 2.0
        # watchdog observer, set up in run() if available
        self._observer = None
        self._handler = None
        # Shared scan_id across all files indexed by this
        # watcher session. Created lazily on first ingest so
        # an idle watcher doesn't litter the scans table.
        self._watcher_scan_id: Optional[int] = None

    def is_running(self) -> bool:
        return self.is_alive() and not self._stop_event.is_set()

    def stop(self):
        """Request a clean shutdown. The thread will exit on its
        next poll cycle (within a couple seconds)."""
        self._stop_event.set()
        # Wake up the wait() so we exit promptly
        self._refresh.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
        # Mark this watcher session's scan record as 'done' so
        # the UI shows a clean status. If we never enqueued
        # anything there's no scan record to update.
        if self._watcher_scan_id is not None:
            try:
                with database.connection() as conn:
                    conn.execute("""
                        UPDATE scans
                        SET finished_at = ?, status = 'done'
                        WHERE id = ? AND status = 'running'
                    """, (time.time(), self._watcher_scan_id))
                    conn.commit()
            except Exception:
                pass

    def refresh_watches(self):
        """Tell the thread to re-read watched_folders.json and
        adjust its observers. Used when the user adds/removes a
        watched folder."""
        self._refresh.set()

    def queue_path(self, path: str):
        """Mark a path as needing ingestion. Called by the FS
        event handler (watchdog) or the polling loop.

        Early-rejects file types that the scanner would skip
        anyway (TXT, EXE, JPG etc). The watcher fires events
        for every write in a watched folder; without this
        filter we'd debounce + re-stat every random temp file
        the user touches."""
        from .db_scanner import (
            C64_FILE_EXTS, DISK_EXTS, ARCHIVE_EXTS)
        ext = Path(path).suffix.lower().lstrip(".")
        if ext not in C64_FILE_EXTS \
                and ext not in DISK_EXTS \
                and ext not in ARCHIVE_EXTS:
            return
        with self._pending_lock:
            self._pending[path] = time.monotonic()

    def run(self):
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            use_native = True
        except ImportError:
            use_native = False

        if use_native:
            self._run_native(Observer, FileSystemEventHandler)
        else:
            self._run_polling()

    def _run_native(self, Observer, FileSystemEventHandler):
        """Native FS notifications via watchdog."""
        outer = self  # for the handler closure

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    outer.queue_path(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    outer.queue_path(event.src_path)

            def on_moved(self, event):
                # Treat the destination as new
                if not event.is_directory:
                    outer.queue_path(event.dest_path)
                # We don't bother removing old entries on rename -
                # they have outdated paths but the MD5 is still
                # valid, so the user gets a stale entry. A "Clean
                # stale entries" button in the UI handles this.

        self._handler = _Handler()
        self._observer = Observer()

        # Initial watch setup
        self._sync_watches()

        # Start watching
        self._observer.start()

        # Process events in a loop. We don't sleep on the
        # observer's queue (watchdog handles that); we sleep on
        # our own debounce timer.
        while not self._stop_event.is_set():
            self._process_pending()
            # Wait up to 1 second, or until refresh is signaled
            if self._refresh.wait(timeout=1.0):
                self._refresh.clear()
                self._sync_watches()

        # Cleanup
        try:
            self._observer.stop()
            self._observer.join(timeout=3)
        except Exception:
            pass

    def _run_polling(self):
        """Fallback: just walk every watched folder every 60
        seconds. Way less efficient but works without any
        external dependency."""
        last_poll = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now - last_poll >= 60.0:
                self._poll_walk()
                last_poll = now
            self._process_pending()
            # Wait 5s or refresh signal
            if self._refresh.wait(timeout=5.0):
                self._refresh.clear()
                last_poll = 0.0  # force re-walk after change

    def _poll_walk(self):
        """Polling fallback: walk each watched root looking for
        files with mtime newer than what's in the DB."""
        import os
        folders = list_watched_folders()
        for folder in folders:
            if self._stop_event.is_set():
                return
            for root, _, files in os.walk(folder):
                if self._stop_event.is_set():
                    return
                for f in files:
                    full = os.path.join(root, f)
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        continue
                    if not database.file_already_indexed(full, mtime):
                        self.queue_path(full)

    def _sync_watches(self):
        """Reconcile the observer's watch list with the
        watched_folders.json file. Adds new ones, removes old."""
        if self._observer is None:
            return
        try:
            # The cleanest way to fully resync is to unwatch
            # everything and re-add. The list of watched folders
            # is small (a few entries typically) so this is fine.
            # Internal watchdog API to enumerate isn't stable, so
            # we just stop+restart the observer.
            self._observer.unschedule_all()
            for folder in list_watched_folders():
                p = Path(folder)
                if p.is_dir():
                    self._observer.schedule(
                        self._handler, str(p), recursive=True)
        except Exception as e:
            print(f"  [watcher] sync_watches failed: {e}")

    def _process_pending(self):
        """Walk the pending queue, ingest files whose debounce
        timer has expired."""
        cutoff = time.monotonic() - self._debounce_seconds
        ready = []
        with self._pending_lock:
            for path, ts in list(self._pending.items()):
                if ts <= cutoff:
                    ready.append(path)
                    del self._pending[path]
        # Process each ready file. We do this OUTSIDE the lock
        # so we don't block other event-handler threads.
        for path in ready:
            self._ingest_one(path)

    def _ingest_one(self, path: str):
        """Enqueue a single file for async ingestion. The worker
        pool in ingest_queue picks it up and runs the actual
        hash/parse/insert. We don't do any heavy work in this
        thread because we want to stay responsive to fast bursts
        of FS events.

        We share one scan_id across all files ingested in this
        watcher session so the scans table doesn't accumulate a
        row per file. The scan record is created lazily on first
        enqueue."""
        from . import ingest_queue
        try:
            p = Path(path)
            if not p.is_file():
                return
            # Establish the watcher's scan_id on first ingest.
            # All subsequent files from this watcher session
            # share it.
            if self._watcher_scan_id is None:
                database.init_db()
                with database.connection() as conn:
                    cur = conn.execute("""
                        INSERT INTO scans(
                            started_at, root_path, status)
                        VALUES(?, ?, 'running')
                    """, (time.time(), "[live watcher]"))
                    self._watcher_scan_id = cur.lastrowid
                    conn.commit()
            ingest_queue.get_queue().enqueue(
                ingest_queue.IngestJob(
                    path=p,
                    scan_id=self._watcher_scan_id))
        except Exception as e:
            print(f"  [watcher] enqueue failed for {path}: {e}")


# ============================================================
# Public API
# ============================================================


def start_watcher() -> bool:
    """Start the FS watcher thread if not already running.
    Returns True if started, False if already running."""
    global _watcher_thread
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return False
        _watcher_thread = _WatcherThread()
        _watcher_thread.start()
        return True


def stop_watcher() -> bool:
    """Request a clean shutdown of the watcher. Returns True if
    it was running, False if not."""
    global _watcher_thread
    with _watcher_lock:
        if _watcher_thread is None or not _watcher_thread.is_alive():
            return False
        _watcher_thread.stop()
        _watcher_thread.join(timeout=5)
        _watcher_thread = None
        return True


def is_running() -> bool:
    """True if the background watcher thread is currently active."""
    return (_watcher_thread is not None
            and _watcher_thread.is_alive())


def has_native_support() -> bool:
    """Returns True if the watchdog package is installed (native
    FS notifications). Returns False if we'd fall back to polling.
    The UI shows different status text for each case."""
    try:
        import watchdog  # noqa
        return True
    except ImportError:
        return False
