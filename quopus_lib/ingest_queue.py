"""Async ingest queue for the Quopus database.

Both the FS watcher (db_watcher) and the bulk scanner (db_scanner)
push files into this queue. A pool of N worker threads pulls
items off and runs the actual MD5 + archive-walk + DB-insert
sequence in parallel.

Why a shared queue instead of one queue per producer:
  - The user might add new files (watcher) while a bulk scan
    is running. Two separate worker pools would compete for
    disk bandwidth.
  - SQLite in WAL mode allows multiple concurrent readers but
    only one writer at a time. A shared queue lets us
    serialize writes through a single DB connection per worker,
    which is what SQLite likes.

Why bounded:
  - On a 1TB archive scan, naively enqueueing every file path
    upfront uses ~100MB of RAM in path strings. A bounded queue
    causes the producer (os.walk) to block when workers fall
    behind, which is exactly the backpressure we want.

Why threads, not asyncio:
  - The actual work is CPU-bound (MD5) + blocking I/O (file
    reads, sqlite). asyncio gives us neither - we'd need
    `loop.run_in_executor` for every step, which IS threads.
  - The existing Scanner / Watcher code is already thread-based.
    Mixing asyncio adds complexity for zero benefit here.
"""
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import database


@dataclass
class IngestJob:
    """One file to ingest. The fields are minimal because the
    actual work (hashing, archive walk, BAM parse) happens
    inside the worker - we don't want producer threads doing
    any of that."""
    path: Path
    # If this file was extracted from an archive, container_id
    # is the row id of that container. Top-level scans pass
    # None here.
    container_id: Optional[int] = None
    # Recursion depth into nested archives. Workers won't
    # recurse past db_scanner.MAX_ARCHIVE_DEPTH.
    depth: int = 0
    # Tag for tracing - which scan / watcher session enqueued
    # this. None for ad-hoc watcher events.
    scan_id: Optional[int] = None
    # Set when the job is finished (success or error). Lets
    # producers wait for completion of their batch.
    done_event: Optional[threading.Event] = None


class IngestQueue:
    """Singleton job queue + worker pool. Lazily started on
    first enqueue. Use the module-level helpers below rather
    than instantiating this directly.

    Lifecycle:
      - First enqueue() call spawns the workers
      - Workers block on the internal queue.Queue
      - shutdown() cleanly drains and exits workers
      - Workers each hold their own sqlite connection so writes
        across workers don't fight a shared connection's locks
    """

    # Cap how many items we hold in memory between producer and
    # workers. ~1000 small strings is ~100KB; the backpressure
    # this creates is correct.
    QUEUE_MAXSIZE = 1024

    def __init__(self, num_workers: int = 2):
        self.num_workers = max(1, num_workers)
        self._queue: queue.Queue[Optional[IngestJob]] = queue.Queue(
            maxsize=self.QUEUE_MAXSIZE)
        self._workers: list[threading.Thread] = []
        self._stop_event = threading.Event()
        # Stats - updated by workers, read by the UI for the
        # status banner. The lock guards every read+write so
        # tracker numbers don't tear on 32-bit systems.
        self._lock = threading.Lock()
        self._stats = {
            "queued": 0,
            "in_flight": 0,
            "completed": 0,
            "errored": 0,
            "total_bytes_hashed": 0,
        }
        self._started = False

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    def start(self):
        """Spawn the worker pool if not already running. Safe to
        call multiple times - subsequent calls are no-ops."""
        if self._started:
            return
        self._started = True
        for i in range(self.num_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"QuopusIngestWorker-{i}",
                daemon=True)
            t.start()
            self._workers.append(t)

    def shutdown(self, wait: bool = True, timeout: float = 10.0):
        """Stop the worker pool. If wait=True, blocks until all
        queued jobs are processed and workers exit. If
        timeout expires we return anyway - workers are daemon
        threads so they'll die with the process."""
        if not self._started:
            return
        # Signal stop and inject sentinels so any blocked worker
        # wakes up immediately.
        self._stop_event.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Queue is jammed. The workers will see the stop
                # event on their next get_nowait check.
                break
        if wait:
            deadline = time.monotonic() + timeout
            for t in self._workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
        self._workers = []
        self._started = False
        self._stop_event.clear()

    def is_running(self) -> bool:
        return self._started and any(
            t.is_alive() for t in self._workers)

    # --------------------------------------------------------
    # Producer API
    # --------------------------------------------------------

    def enqueue(self, job: IngestJob, block: bool = True,
                timeout: Optional[float] = None) -> bool:
        """Add a job to the queue. Returns True if accepted,
        False if the queue was full and block=False (or timeout
        was reached). Automatically starts workers on first call.

        block=True (default) gives natural backpressure - the
        producer blocks when workers fall behind. For
        os.walk-based bulk scanning this is exactly right; the
        walker pauses, workers catch up, walker resumes.
        """
        if not self._started:
            self.start()
        try:
            self._queue.put(job, block=block, timeout=timeout)
            with self._lock:
                self._stats["queued"] += 1
            return True
        except queue.Full:
            return False

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until the queue is empty AND no workers are
        actively processing a job. Returns True if idle within
        the timeout, False if it expired. Used by the UI to
        know when a batch scan is fully done."""
        deadline = (time.monotonic() + timeout
                    if timeout is not None else None)
        while True:
            with self._lock:
                idle = (self._stats["queued"] ==
                        self._stats["completed"] +
                        self._stats["errored"]
                        and self._stats["in_flight"] == 0)
            if idle:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def stats(self) -> dict:
        """Snapshot of current queue stats. Cheap to call from
        the UI for a status banner."""
        with self._lock:
            return dict(self._stats)

    # --------------------------------------------------------
    # Worker loop
    # --------------------------------------------------------

    def _worker_loop(self):
        """One worker thread's main loop. Pulls jobs from the
        queue, processes each via the heavy-lifting code in
        db_scanner.

        Each worker holds its own sqlite connection internally
        via the per-call database.connection() helper - that's
        a fresh connection per query, which keeps things simple
        at the cost of some open/close overhead. The overhead
        is dwarfed by the actual ingestion cost (MD5 + archive
        walk), so it's fine.
        """
        # Import inside the worker to avoid circular imports at
        # module-load time. db_scanner imports nothing from
        # ingest_queue, so this is safe.
        from . import db_scanner

        while True:
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue

            # Sentinel = shutdown signal
            if job is None:
                self._queue.task_done()
                return

            with self._lock:
                self._stats["in_flight"] += 1

            try:
                # Retry transient DB-locked errors. With our 60s
                # busy_timeout in database._connect(), a real lock
                # collision is rare - but on a slow NAS where the
                # WAL checkpoint takes ages we can still trip the
                # OperationalError. Two retries with backoff catch
                # almost all of these without spamming.
                import sqlite3 as _sqlite3
                import time as _time
                attempts = 0
                while True:
                    attempts += 1
                    try:
                        self._process(job, db_scanner)
                        if job.done_event is not None:
                            job.done_event.set()
                        with self._lock:
                            self._stats["completed"] += 1
                        break
                    except _sqlite3.OperationalError as e:
                        # "database is locked", "database is busy",
                        # and similar transient ops errors - retry
                        # with a short backoff up to 3 attempts
                        # before giving up.
                        msg = str(e).lower()
                        is_transient = (
                            "lock" in msg or "busy" in msg
                            or "timeout" in msg)
                        if is_transient and attempts < 3:
                            # Exponential backoff: 0.5s, 2s.
                            _time.sleep(0.5 * (4 ** (attempts - 1)))
                            continue
                        with self._lock:
                            self._stats["errored"] += 1
                        if job.done_event is not None:
                            job.done_event.set()
                        print(
                            f"  [ingest worker] failed for {job.path}: "
                            f"{e} (after {attempts} attempt(s))")
                        break
                    except Exception as e:
                        with self._lock:
                            self._stats["errored"] += 1
                        if job.done_event is not None:
                            job.done_event.set()
                        print(
                            f"  [ingest worker] failed for "
                            f"{job.path}: {e}")
                        break
            finally:
                with self._lock:
                    self._stats["in_flight"] -= 1
                self._queue.task_done()

    def _process(self, job: IngestJob, db_scanner):
        """The actual heavy lifting. Builds a one-shot Scanner
        instance per job. We avoid sharing a Scanner across
        workers because Scanner accumulates state (files_added,
        errors etc) we don't want to make thread-safe.

        For watcher single-file ingest, container_id and depth
        are None/0 - the Scanner walks the file as a top-level
        file. For nested-archive members enqueued by a worker
        processing the parent archive, container_id and depth
        are inherited from the parent job.
        """
        if not job.path.is_file():
            return

        # Tiny ad-hoc scanner just for this one file. We
        # construct it with incremental=True so watcher events
        # for an already-indexed file (same path+mtime) get
        # skipped automatically.
        scanner = db_scanner.Scanner(
            job.path.parent,
            incremental=True)
        scanner.scan_id = job.scan_id
        # If the scan_id was passed (watcher creates one per
        # session), don't start/finish a new scan record.
        # Otherwise the watcher would create one "scan" row per
        # file in the scans table which is noise.
        if scanner.scan_id is None:
            scanner._start_scan()
            owns_scan = True
        else:
            owns_scan = False
        try:
            scanner._ingest_file(
                job.path,
                container_id=job.container_id,
                depth=job.depth)
            with self._lock:
                self._stats["total_bytes_hashed"] += (
                    job.path.stat().st_size
                    if job.path.is_file() else 0)
        finally:
            if owns_scan:
                scanner._finish_scan("done")


# ============================================================
# Singleton
# ============================================================
#
# We expose a module-level singleton because there's no good
# reason to have more than one ingest queue per process. Both
# the watcher and the bulk scanner UI feed into it.


_singleton: Optional[IngestQueue] = None
_singleton_lock = threading.Lock()


def get_queue() -> IngestQueue:
    """Return the process-wide ingest queue, creating it on
    first call. Worker count is taken from the QUOPUS_INGEST_WORKERS
    env var if set, otherwise defaults to 2 (safe for SSDs and
    most laptop CPUs; users with NVMe + 8+ cores can bump it)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            n = 2
            env = os.environ.get("QUOPUS_INGEST_WORKERS", "")
            try:
                env_n = int(env)
                if 1 <= env_n <= 16:
                    n = env_n
            except ValueError:
                pass
            _singleton = IngestQueue(num_workers=n)
        return _singleton


def shutdown():
    """Module-level shutdown for clean app exit."""
    global _singleton
    if _singleton is not None:
        _singleton.shutdown()
        _singleton = None
