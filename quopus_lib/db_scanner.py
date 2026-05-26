"""Scanner: walks a directory tree, indexes files into the
Quopus database.

Pipeline per file:

  1. Plain file -> compute MD5, insert row, done
  2. ZIP/LHA/RAR/7z archive -> compute MD5, insert row, then
     iterate members, extract each to a temp buffer, recurse:
        - if member is a disk image -> parse it
        - if member is a C64 file (PRG/SEQ/USR/REL) -> insert row
        - if member is another archive -> recurse one more level
          (we cap recursion depth at 3 to avoid zip-bombs)
  3. D64/D71/D81 disk image -> compute MD5, insert row, then
     parse BAM, list directory, extract each entry, insert row
     per entry with its own MD5.

Why we don't extract every file from an archive permanently:
  - You can have GB-scale archives that would explode to TB
    on disk if fully extracted
  - The MD5 is the index; the user can extract on demand from
    the original archive if they need the actual bytes

We DO compute MD5 of each individual member by reading it into
memory once. For typical C64 PRGs that's 1-200KB so this is fine
even on huge collections.

Performance: a million files at ~50KB each on a fast SSD scans in
about 20-30 minutes. The bottleneck is filesystem I/O, not the
hashing.
"""
import io
import os
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import database
from . import cbmfiles


# Filename extensions we recognize. Stored lowercased without dot.
ARCHIVE_EXTS = {"zip", "lha", "lzh", "lzx", "rar", "7z"}
# Standard CBM-DOS disk images that we can BAM-parse (D64/D71/D81)
# plus raw-track formats (G64, NIB, NBZ) which usually don't have
# a parseable directory because they preserve copy-protected
# trackloader content. For raw-track formats we still index the
# file with its MD5 but record a "no_directory" issue so the user
# knows the contents aren't catalogued.
DISK_EXTS = {"d64", "d71", "d81", "d80", "d82",
             "g64", "g71", "g81",
             "nib", "nbz"}
# Subset of DISK_EXTS we can actually parse directories from.
# Used to decide between BAM-walk and "raw track image" handling.
DISK_BAM_EXTS = {"d64", "d71", "d81", "d80", "d82"}
# Plain C64 file types that are worth indexing on their own.
# We index extensions that are commonly used for raw C64 files;
# users have huge collections of these.
C64_FILE_EXTS = {"prg", "seq", "usr", "rel", "p00", "s00", "u00", "r00"}


# Cap on recursion depth into nested archives. ZIP-in-ZIP-in-ZIP
# is unusual; deeper than this is almost certainly a malicious
# zip-bomb. We log and skip rather than crash.
MAX_ARCHIVE_DEPTH = 3


class AsyncScanner:
    """Like Scanner but enqueues files into the shared ingest
    queue instead of processing them sequentially. The walker
    thread only does directory traversal and mtime checks; the
    actual hash + archive extraction + DB insert happens on
    the worker pool.

    Use this for bulk scans of large archive folders. For
    single-file ingestion (watcher events) use the queue
    directly via ingest_queue.get_queue().enqueue() - building
    an AsyncScanner per event would create one scan record per
    file in the scans table, which is noise.

    Architecture:

        UI thread        AsyncScanner       IngestQueue        Workers
        ─────────────────────────────────────────────────────────────
        run() ─────────► walk tree
                         │
                         ├ for each file:
                         │   if incremental
                         │     and indexed:
                         │     skip
                         │   else:
                         │     enqueue ─────► queue ─────────► worker
                         │                                       │
                         │                                       │ MD5
                         │                                       │ archive walk
                         │                                       │ DB insert
                         │                                       ▼
                         │                                    completed++
                         │
                         └ wait_idle() ─────► block until
                                              all workers done
                         │
        finishes ◄───────┘

    The walker is bounded by the queue size: when workers fall
    behind, enqueue() blocks, which throttles the walker. This
    is exactly the backpressure we want - no unbounded memory
    growth on huge folders.
    """

    def __init__(self,
                 root: Path,
                 progress_cb: Optional[Callable] = None,
                 cancel_cb: Optional[Callable] = None,
                 incremental: bool = True):
        self.root = Path(root).resolve()
        self.progress_cb = progress_cb or (lambda *a, **k: None)
        self.cancel_cb = cancel_cb or (lambda: False)
        self.incremental = incremental
        self.scan_id: Optional[int] = None
        self.files_walked = 0
        self.files_skipped = 0
        self.files_enqueued = 0

    def run(self) -> int:
        """Walk the tree, enqueue every file, wait for workers
        to drain. Returns the scan_id row for the scans table."""
        from . import ingest_queue
        database.init_db()
        self._start_scan()
        q = ingest_queue.get_queue()
        # Make sure the workers are running before we start
        # enqueueing - they're daemon threads so they'd be
        # auto-started on first enqueue() anyway, but explicitly
        # starting now makes profiling output cleaner.
        q.start()

        status = "done"
        try:
            self._scan_tree(q)
            # Wait for workers to finish everything we enqueued.
            # We poll cancel_cb during the wait so the user can
            # cancel even after the walker finished but workers
            # are still chewing through big archives.
            while True:
                if self.cancel_cb():
                    status = "aborted"
                    break
                if q.wait_idle(timeout=1.0):
                    break
        except Exception as e:
            print(f"  [async scan] crashed: {e}")
            status = "error"
        finally:
            self._finish_scan(q, status)
        return self.scan_id

    def _start_scan(self):
        with database.connection() as conn:
            cur = conn.execute("""
                INSERT INTO scans(started_at, root_path, status)
                VALUES(?, ?, 'running')
            """, (time.time(), str(self.root)))
            self.scan_id = cur.lastrowid
            conn.commit()

    def _finish_scan(self, q, status: str):
        """Read the final counts from the workers + the DB and
        update the scan record. We can't reliably attribute
        worker errors to this specific scan since workers
        process jobs from multiple scans interleaved, so the
        error_count here is best-effort based on what we saw
        during the walk.

        Robust against DB unavailability: if the sqlite connection
        fails (e.g. fd exhausted by the bulk ingest itself, disk
        full, permissions), we log to stderr and return rather
        than crash the worker thread. The scan record stays in
        'running' status; on next launch the recovery pass picks
        it up.
        """
        # Count files + disks attributed to this scan
        try:
            with database.connection() as conn:
                files = conn.execute(
                    "SELECT COUNT(*) as n FROM files WHERE scan_id = ?",
                    (self.scan_id,)).fetchone()
                disks = conn.execute("""
                    SELECT COUNT(*) as n FROM disk_images d
                    JOIN files f ON f.id = d.file_id
                    WHERE f.scan_id = ?
                """, (self.scan_id,)).fetchone()
                conn.execute("""
                    UPDATE scans
                    SET finished_at = ?, file_count = ?, disk_count = ?,
                        status = ?
                    WHERE id = ?
                """, (time.time(), files["n"], disks["n"],
                      status, self.scan_id))
                conn.commit()
        except Exception as e:
            # DB unreachable - log and bail rather than re-raise.
            # The whole async scan thread otherwise crashes and the
            # crash traceback hides the real cause (which is usually
            # fd exhaustion or disk full).
            print(f"  [scan] _finish_scan: cannot update scan "
                  f"record (db error: {e}). Scan #{self.scan_id} "
                  f"left in 'running' status for recovery on next "
                  f"launch.")

    def _scan_tree(self, q):
        """Walk and enqueue. The os.walk pulls one directory
        listing at a time, so memory stays bounded even on
        million-file trees."""
        from . import ingest_queue
        for dirpath, _, filenames in os.walk(self.root):
            if self.cancel_cb():
                return
            self.progress_cb(dirpath, self.files_walked, None)
            for fname in filenames:
                if self.cancel_cb():
                    return
                full = Path(dirpath) / fname
                self.files_walked += 1
                # Early extension filter: skip files that the
                # worker would discard anyway (TXT, EXE, JPG etc).
                # Doing this here avoids the stat() and enqueue
                # overhead per file - on huge mixed folders this
                # is the difference between hours and minutes.
                ext = Path(fname).suffix.lower().lstrip(".")
                if ext not in C64_FILE_EXTS \
                        and ext not in DISK_EXTS \
                        and ext not in ARCHIVE_EXTS:
                    self.files_skipped += 1
                    continue
                # Incremental skip: if we already indexed this
                # exact path + mtime, don't bother enqueuing.
                # The check is cheap (single indexed DB lookup)
                # so doing it here saves a worker round-trip.
                if self.incremental:
                    try:
                        mtime = full.stat().st_mtime
                    except OSError:
                        continue
                    if database.file_already_indexed(
                            str(full), mtime):
                        self.files_skipped += 1
                        continue
                # Enqueue with backpressure. block=True means
                # we wait when workers fall behind, which is
                # the desired behavior on huge scans.
                q.enqueue(ingest_queue.IngestJob(
                    path=full,
                    scan_id=self.scan_id))
                self.files_enqueued += 1
                # Progress callback every 100 files - throttle
                # so UI doesn't redraw on every enqueue.
                if self.files_walked % 100 == 0:
                    qstats = q.stats()
                    self.progress_cb(
                        str(full),
                        qstats["completed"],
                        self.files_walked)



class Scanner:
    """Walks a directory and ingests every file into the database.

    Construct one Scanner per scan operation. The progress_cb gets
    called periodically with (current_path, files_done, total_or_None)
    so the UI can show a progress bar; it can be None for headless
    use.

    The cancel_cb is checked between files; return True to abort
    the scan partway through. We finalize the scan record as
    'aborted' so the UI can show it cleanly.
    """

    def __init__(self,
                 root: Path,
                 progress_cb: Optional[Callable] = None,
                 cancel_cb: Optional[Callable] = None,
                 incremental: bool = True):
        self.root = Path(root).resolve()
        self.progress_cb = progress_cb or (lambda *a, **k: None)
        self.cancel_cb = cancel_cb or (lambda: False)
        self.incremental = incremental
        self.scan_id: Optional[int] = None
        self.files_done = 0
        self.files_added = 0
        self.disks_added = 0
        self.errors = 0

    # --------------------------------------------------------
    # Scan lifecycle
    # --------------------------------------------------------

    def run(self) -> int:
        """Execute the scan. Returns the scan_id row for the
        scans table, even on failure - so the UI can show the
        result either way."""
        database.init_db()
        self._start_scan()
        status = "done"
        try:
            self._scan_tree()
        except Exception as e:
            self._log_error(f"Scan crashed: {e}")
            status = "error"
        else:
            if self.cancel_cb():
                status = "aborted"
        finally:
            self._finish_scan(status)
        return self.scan_id

    def _start_scan(self):
        with database.connection() as conn:
            cur = conn.execute("""
                INSERT INTO scans(started_at, root_path, status)
                VALUES(?, ?, 'running')
            """, (time.time(), str(self.root)))
            self.scan_id = cur.lastrowid
            conn.commit()

    def _finish_scan(self, status: str):
        # Robust against DB unavailability (fd exhaustion / disk
        # full): log and bail rather than crashing the calling
        # thread. Scan record stays in 'running' status; the
        # crash-recovery pass on next launch picks it up.
        try:
            with database.connection() as conn:
                conn.execute("""
                    UPDATE scans
                    SET finished_at = ?, file_count = ?, disk_count = ?,
                        error_count = ?, status = ?
                    WHERE id = ?
                """, (time.time(), self.files_added, self.disks_added,
                      self.errors, status, self.scan_id))
                conn.commit()
        except Exception as e:
            print(f"  [scan] _finish_scan: cannot update scan "
                  f"record (db error: {e}). Scan #{self.scan_id} "
                  f"left in 'running' status for recovery.")

    def _log_error(self, msg: str):
        # Errors don't have their own table - we just bump the
        # counter and print. If users want detailed error logs
        # later, we can add an scan_errors table.
        self.errors += 1
        print(f"  [scan error] {msg}")

    # --------------------------------------------------------
    # Tree walk
    # --------------------------------------------------------

    def _scan_tree(self):
        """Walk the tree, handing each file to _ingest_file."""
        for dirpath, _, filenames in os.walk(self.root):
            if self.cancel_cb():
                return
            self.progress_cb(dirpath, self.files_done, None)
            for fname in filenames:
                if self.cancel_cb():
                    return
                full = Path(dirpath) / fname
                try:
                    self._ingest_file(full)
                except Exception as e:
                    self._log_error(f"{full}: {e}")
                self.files_done += 1
                if self.files_done % 50 == 0:
                    self.progress_cb(str(full), self.files_done, None)

    # --------------------------------------------------------
    # Per-file ingest
    # --------------------------------------------------------

    def _ingest_file(self, path: Path, container_id: Optional[int] = None,
                     depth: int = 0):
        """Top-level: insert the file row, dispatch to specialized
        handler if it's an archive or disk image.

        Filter policy: only index files that are interesting to a
        C64 archive catalog. That means:
          - C64 file types (PRG / SEQ / USR / REL plus their .Pxx
            variants from P00-format dumps)
          - Disk images (D64 / D71 / D81 / D80 / D82)
          - Archives (ZIP / LHA / LZX / RAR / 7Z) since they
            may contain the above

        Everything else (TXT, NFO, EXE, JPG, PDF, ...) gets skipped
        even at the top level. The user explicitly doesn't want a
        general file index, only a C64-format catalog."""
        if not path.is_file():
            return
        ext = path.suffix.lower().lstrip(".")

        is_archive = ext in ARCHIVE_EXTS
        is_disk = ext in DISK_EXTS
        is_c64_file = ext in C64_FILE_EXTS

        # Skip uninteresting file types up front so we don't waste
        # time hashing TXT, EXE, JPG etc. on a top-level scan.
        # Inside an archive we already filter via _ingest_member,
        # so the recursion can't sneak past this check either.
        if not (is_archive or is_disk or is_c64_file):
            return

        try:
            mtime = path.stat().st_mtime
            size = path.stat().st_size
        except OSError as e:
            self._log_error(f"stat failed {path}: {e}")
            return

        # Skip in incremental mode if we've seen this exact path+mtime
        # before. Container files (extracted from archives) don't have
        # mtimes that match - they're always re-ingested.
        if (self.incremental and container_id is None
                and database.file_already_indexed(str(path), mtime)):
            return

        # Hash. For huge files this is the slowest step.
        try:
            md5 = database.file_md5(path)
        except OSError as e:
            self._log_error(f"hash failed {path}: {e}")
            return

        file_id = self._insert_file(
            path=str(path), name=path.name, extension=ext,
            size=size, md5=md5, mtime=mtime,
            container_id=container_id,
            is_archive=is_archive, is_disk=is_disk)
        self.files_added += 1

        # Dispatch into archive / disk handlers. These may
        # insert many more rows (archive members, disk entries).
        # If we crash anywhere in here, this file's row stays
        # 'pending' and the next launch's recovery pass will
        # re-enqueue it.
        try:
            if is_archive and depth < MAX_ARCHIVE_DEPTH:
                self._ingest_archive(path, file_id, depth + 1)
            elif is_disk:
                self._ingest_disk_image(path, file_id)
        except Exception as e:
            # Mark failed so we don't keep retrying a broken
            # file every launch. The user can re-scan to retry.
            self._log_error(
                f"ingest dispatch failed for {path}: {e}")
            database.mark_file_failed(file_id, str(e))
            return

        # All followup work committed - it's safe to flip the
        # status to 'done'. Doing this AFTER the dispatch is
        # the entire point of the scan_status mechanism: a crash
        # before this point leaves us 'pending' and recoverable.
        database.mark_file_done(file_id)

    def _insert_file(self, **kw) -> int:
        """Insert a row into files and return its id. The row
        starts with scan_status='pending'; the caller must
        invoke database.mark_file_done(file_id) once all
        followup work (archive members, disk entries) is
        committed. If the scanner crashes between these two
        steps the row stays 'pending', which causes the next
        Quopus launch to re-enqueue this file rather than
        skipping it as already-indexed."""
        with database.connection() as conn:
            cur = conn.execute("""
                INSERT INTO files(
                    scan_id, path, name, extension, size, md5, mtime,
                    container_id, is_archive, is_disk, scan_status)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (self.scan_id, kw["path"], kw["name"], kw["extension"],
                  kw["size"], kw["md5"], kw["mtime"],
                  kw["container_id"], int(kw["is_archive"]),
                  int(kw["is_disk"])))
            conn.commit()
            return cur.lastrowid

    def _insert_archive_member(self, *, virtual_path: str, name: str,
                                extension: str, size: int, md5: str,
                                container_id: int,
                                is_archive: bool = False,
                                is_disk: bool = False) -> int:
        """Insert a row for a file extracted from inside an archive.
        It has no mtime (archives don't track per-member modification
        times consistently) and its 'path' is a virtual one like
        'foo.zip!member.prg' so we can still display where it came
        from in the UI.

        Archive members start in 'done' state because the INSERT
        for them is atomic - by the time this returns, all their
        data is committed. Only their parent (the archive itself)
        stays 'pending' until ALL its members are inserted, so a
        crash mid-archive still leaves the parent visibly incomplete
        even though some members already have 'done' rows."""
        with database.connection() as conn:
            cur = conn.execute("""
                INSERT INTO files(
                    scan_id, path, name, extension, size, md5, mtime,
                    container_id, is_archive, is_disk, scan_status)
                VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'done')
            """, (self.scan_id, virtual_path, name, extension,
                  size, md5, container_id,
                  int(is_archive), int(is_disk)))
            conn.commit()
            return cur.lastrowid

    # --------------------------------------------------------
    # Archive handling
    # --------------------------------------------------------

    def _ingest_archive(self, path: Path, file_id: int, depth: int):
        """Open the archive, extract each member to a buffer,
        index it. Dispatches to handler per file type."""
        ext = path.suffix.lower().lstrip(".")
        if ext == "zip":
            self._ingest_zip(path, file_id, depth)
        elif ext in ("lha", "lzh"):
            self._ingest_lha(path, file_id, depth)
        elif ext == "lzx":
            self._ingest_lzx(path, file_id, depth)
        elif ext == "rar":
            self._ingest_rar(path, file_id, depth)
        elif ext == "7z":
            self._ingest_7z(path, file_id, depth)
        # else: unknown archive type, skip silently

    def _ingest_zip(self, path: Path, file_id: int, depth: int):
        try:
            zf = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, OSError) as e:
            self._log_error(f"can't open zip {path}: {e}")
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_EXTRACT_FAILED,
                f"Cannot open: {e}")
            return
        with zf:
            # ZIP supports per-entry encryption. The high bit
            # of an entry's flag_bits indicates encryption. We
            # check this upfront so we can log ONE password
            # issue per archive instead of one per encrypted
            # member.
            encrypted_count = 0
            total_count = 0
            for info in zf.infolist():
                if self.cancel_cb():
                    return
                if info.is_dir():
                    continue
                total_count += 1
                # bit 0 of flag_bits = encrypted
                if info.flag_bits & 0x1:
                    encrypted_count += 1
                    continue
                try:
                    data = zf.read(info)
                except RuntimeError as e:
                    # zipfile raises RuntimeError for stuff like
                    # "Bad password" when we try to read despite
                    # the flag check above. Treat as encrypted.
                    if "password" in str(e).lower() or \
                            "encrypted" in str(e).lower():
                        encrypted_count += 1
                    else:
                        self._log_error(
                            f"can't read {info.filename} from "
                            f"{path}: {e}")
                    continue
                except (zipfile.BadZipFile, OSError) as e:
                    self._log_error(
                        f"can't read {info.filename} from "
                        f"{path}: {e}")
                    continue
                self._ingest_member(
                    parent_path=path,
                    member_name=info.filename,
                    data=data,
                    container_id=file_id,
                    depth=depth)
            # If the archive has encrypted entries, log a single
            # password issue summarizing how many we skipped.
            if encrypted_count > 0:
                database.log_scan_issue(
                    self.scan_id, str(path),
                    database.ISSUE_PASSWORD,
                    f"{encrypted_count} of {total_count} entries "
                    f"are password-protected and were skipped")

    def _ingest_lha(self, path: Path, file_id: int, depth: int):
        try:
            import lhafile
        except ImportError:
            self._log_error(
                f"lhafile not installed - skipping {path}")
            return
        try:
            lh = lhafile.LhaFile(str(path))
        except Exception as e:
            self._log_error(f"can't open lha {path}: {e}")
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_EXTRACT_FAILED,
                f"Cannot open: {e}")
            return
        # lhafile.LhaFile is not a context manager (no __enter__),
        # so we close explicitly in a finally. Without this, every
        # scanned LHA leaks one file descriptor and a multi-minute
        # bulk scan hits the per-process fd limit (default 1024
        # on Linux) within a few thousand archives.
        try:
            for info in lh.infolist():
                if self.cancel_cb():
                    return
                if info.directory:
                    continue
                try:
                    data = lh.read(info.filename)
                except Exception as e:
                    # LHA doesn't typically use passwords but old
                    # Amiga archives can have weird formats. Log as
                    # extract_failed.
                    self._log_error(
                        f"can't read {info.filename} from {path}: {e}")
                    continue
                self._ingest_member(
                    parent_path=path,
                    member_name=info.filename,
                    data=data,
                    container_id=file_id,
                    depth=depth)
        finally:
            try:
                lh.close()
            except Exception:
                pass

    def _ingest_lzx(self, path: Path, file_id: int, depth: int):
        """Extract LZX archive contents using the external 'unlzx'
        binary. LZX is Amiga-specific and there's no Python library
        for it, so we fall back to a CLI tool that ships in /external
        with Quopus (or is installed system-wide).

        We extract to a temp dir, walk it, ingest each file
        individually. The temp dir gets cleaned up after."""
        import shutil
        import subprocess
        import tempfile

        # Try to locate unlzx: bundled in external/, then PATH
        unlzx = shutil.which("unlzx")
        if unlzx is None:
            # Check bundled external folder (Quopus ships these
            # for retro-archive support that has no Python lib)
            from .config import BUNDLE_DIR
            for candidate in (
                BUNDLE_DIR / "external" / "unlzx.exe",
                BUNDLE_DIR / "external" / "unlzx",
            ):
                if candidate.is_file():
                    unlzx = str(candidate)
                    break
        if unlzx is None:
            self._log_error(
                f"unlzx not installed - skipping {path}. "
                f"Install from "
                f"http://aminet.net/util/arc/unlzx.lha or place "
                f"the binary in external/")
            return

        # Extract to temp dir. unlzx extracts to current working
        # directory by default; we cd into the temp dir for the
        # subprocess so we have a known clean location.
        tmpdir = tempfile.mkdtemp(prefix="quopus_lzx_")
        try:
            try:
                # unlzx -x = extract, -X = preserve dirs, %s = file
                # Different unlzx versions take different syntax;
                # the safest portable invocation is just "unlzx
                # <file>" run from the target dir.
                result = subprocess.run(
                    [unlzx, "-x", str(path.resolve())],
                    cwd=tmpdir,
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    self._log_error(
                        f"unlzx failed on {path}: "
                        f"rc={result.returncode}, "
                        f"stderr={result.stderr[:200]!r}")
                    return
            except subprocess.TimeoutExpired:
                self._log_error(
                    f"unlzx timed out on {path} (60s limit)")
                return
            except OSError as e:
                self._log_error(f"unlzx exec failed: {e}")
                return

            # Walk the extracted tree and ingest each file
            for root, _, files in os.walk(tmpdir):
                if self.cancel_cb():
                    return
                for f in files:
                    extracted = Path(root) / f
                    try:
                        rel_name = str(extracted.relative_to(tmpdir))
                    except ValueError:
                        rel_name = extracted.name
                    try:
                        data = extracted.read_bytes()
                    except OSError as e:
                        self._log_error(
                            f"can't read extracted {rel_name}: {e}")
                        continue
                    self._ingest_member(
                        parent_path=path,
                        member_name=rel_name,
                        data=data,
                        container_id=file_id,
                        depth=depth)
        finally:
            # Always clean up the temp dir, even if we hit an error
            # halfway through
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _ingest_rar(self, path: Path, file_id: int, depth: int):
        try:
            import rarfile
        except ImportError:
            self._log_error(
                f"rarfile not installed - skipping {path}")
            return
        try:
            rf = rarfile.RarFile(str(path))
        except rarfile.PasswordRequired:
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_PASSWORD,
                "Entire archive is password-protected")
            return
        except Exception as e:
            self._log_error(f"can't open rar {path}: {e}")
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_EXTRACT_FAILED,
                f"Cannot open: {e}")
            return
        encrypted_count = 0
        total_count = 0
        with rf:
            # RAR archives can have ALL entries encrypted (above
            # case) OR just SOME (we handle here). Check each
            # entry's password-required flag.
            for info in rf.infolist():
                if self.cancel_cb():
                    return
                if info.is_dir():
                    continue
                total_count += 1
                # rarfile sets .password_required (or
                # .needs_password) on entries. Different versions
                # expose it differently, so we check both.
                needs_pw = (getattr(info, "needs_password", False)
                            or getattr(info, "password_required",
                                       False))
                if needs_pw:
                    encrypted_count += 1
                    continue
                try:
                    data = rf.read(info.filename)
                except rarfile.PasswordRequired:
                    encrypted_count += 1
                    continue
                except Exception as e:
                    self._log_error(
                        f"can't read {info.filename} from "
                        f"{path}: {e}")
                    continue
                self._ingest_member(
                    parent_path=path,
                    member_name=info.filename,
                    data=data,
                    container_id=file_id,
                    depth=depth)
            if encrypted_count > 0:
                database.log_scan_issue(
                    self.scan_id, str(path),
                    database.ISSUE_PASSWORD,
                    f"{encrypted_count} of {total_count} entries "
                    f"are password-protected")

    def _ingest_7z(self, path: Path, file_id: int, depth: int):
        # 7z isn't a built-in Python lib. We try py7zr if it's
        # available; otherwise skip. Not worth the dependency
        # burden for users who don't have 7z archives.
        try:
            import py7zr
        except ImportError:
            self._log_error(
                f"py7zr not installed - skipping {path}")
            return
        try:
            sf = py7zr.SevenZipFile(str(path), mode="r")
        except py7zr.exceptions.PasswordRequired:
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_PASSWORD,
                "Archive header is encrypted - cannot list "
                "contents without password")
            return
        except Exception as e:
            self._log_error(f"can't open 7z {path}: {e}")
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_EXTRACT_FAILED,
                f"Cannot open: {e}")
            return

        # py7zr API has shifted across versions:
        #   <  0.20: sf.readall() returned dict[name, BytesIO]
        #   >= 0.20: removed readall(); use BytesIOFactory or
        #            extractall(path=tempdir) and read from disk
        # We try BytesIOFactory first (efficient, no temp files)
        # and fall back to a temp-directory extraction so even
        # very old or unusual py7zr builds work.
        contents = self._extract_7z_to_memory(sf, path)
        if contents is None:
            # Either crashed or auth failed - error already logged
            return
        for fname, data in contents.items():
            if self.cancel_cb():
                return
            self._ingest_member(
                parent_path=path,
                member_name=fname,
                data=data,
                container_id=file_id,
                depth=depth)

    def _extract_7z_to_memory(self, sf, path: Path):
        """Pull all members of an open SevenZipFile into a dict
        {name: bytes}. Tries the modern BytesIOFactory path
        first; falls back to a temp-directory extraction.

        Returns None on unrecoverable error (password protection
        discovered mid-extraction, IO failure). The caller stops
        the ingest in that case.

        fd-leak guard: track whether `with sf:` has actually
        entered (which auto-closes on exit). If the path-1 setup
        fails BEFORE the with-block (e.g. BytesIOFactory ctor
        raises) we must close `sf` ourselves before falling
        through to path 2's reopen - otherwise every 7z that
        triggers the fallback leaks one fd, and on a big retro
        catalog with many oddball 7z's that exhausts the process
        fd budget within minutes.
        """
        import py7zr

        sf_consumed = False  # True once `with sf:` has entered

        # ---- Path 1: BytesIOFactory (py7zr 0.20+) ----
        # Modern py7zr exposes BytesIOFactory which extracts into
        # in-memory buffers without touching the disk. We pass
        # a generous limit (1 GiB per member) - anti-zipbomb is
        # already handled by MAX_ARCHIVE_DEPTH.
        try:
            from py7zr.io import BytesIOFactory
            factory = BytesIOFactory(limit=1024 * 1024 * 1024)
            try:
                with sf:
                    sf_consumed = True
                    if sf.needs_password():
                        database.log_scan_issue(
                            self.scan_id, str(path),
                            database.ISSUE_PASSWORD,
                            "Archive contents are encrypted")
                        return None
                    sf.extractall(factory=factory)
            except py7zr.exceptions.PasswordRequired:
                database.log_scan_issue(
                    self.scan_id, str(path),
                    database.ISSUE_PASSWORD,
                    "Archive contents are encrypted")
                return None
            # factory.products is dict[str, Py7zBytesIO]; each
            # supports read() like a normal BytesIO.
            result = {}
            for fname, bio in factory.products.items():
                try:
                    # Reset to start in case py7zr left position
                    # at EOF after writing.
                    if hasattr(bio, "seek"):
                        bio.seek(0)
                    result[fname] = bio.read()
                except Exception as e:
                    self._log_error(
                        f"7z member {fname} read failed: {e}")
            return result
        except ImportError:
            # No BytesIOFactory in this py7zr version. sf has not
            # been entered yet - it's still open. Path 2 below
            # uses `with sf:`, which closes it cleanly.
            pass
        except Exception as e:
            # Some BytesIOFactory unexpected failure - try the
            # fallback path instead of giving up entirely.
            self._log_error(
                f"7z BytesIOFactory failed on {path}: {e} "
                f"- trying temp-dir fallback")
            # If `with sf:` was entered above, sf is already
            # closed by the context manager - reopen for path 2.
            # If we never entered the with-block (e.g. factory
            # ctor itself raised), close sf explicitly first to
            # avoid leaking a fd on every fallback.
            if not sf_consumed:
                try:
                    sf.close()
                except Exception:
                    pass
            try:
                sf = py7zr.SevenZipFile(str(path), mode="r")
            except Exception as e2:
                self._log_error(
                    f"reopen 7z {path} failed: {e2}")
                return None
            sf_consumed = False  # fresh handle, path 2 will use with

        # ---- Path 2: temp-directory extraction (fallback) ----
        # Old py7zr or BytesIOFactory failure: extract to a temp
        # directory and read each file back. Slower but compatible.
        import shutil
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="quopus_7z_")
        try:
            try:
                with sf:
                    if sf.needs_password():
                        database.log_scan_issue(
                            self.scan_id, str(path),
                            database.ISSUE_PASSWORD,
                            "Archive contents are encrypted")
                        return None
                    sf.extractall(path=tmpdir)
            except py7zr.exceptions.PasswordRequired:
                database.log_scan_issue(
                    self.scan_id, str(path),
                    database.ISSUE_PASSWORD,
                    "Archive contents are encrypted")
                return None
            except Exception as e:
                self._log_error(f"7z extractall failed: {e}")
                return None
            result = {}
            for root, _, files in os.walk(tmpdir):
                for f in files:
                    full = Path(root) / f
                    try:
                        rel = str(full.relative_to(tmpdir))
                    except ValueError:
                        rel = full.name
                    try:
                        result[rel] = full.read_bytes()
                    except OSError as e:
                        self._log_error(
                            f"7z temp read {rel}: {e}")
            return result
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _ingest_member(self, *, parent_path: Path, member_name: str,
                       data: bytes, container_id: int, depth: int):
        """A single file extracted from inside an archive. Decide
        what to do based on the extension."""
        mname = Path(member_name)
        ext = mname.suffix.lower().lstrip(".")
        # Build a virtual path that shows the archive origin:
        #   /home/user/scene.zip!releases/intro.prg
        virtual_path = f"{parent_path}!{member_name}"
        md5 = database.bytes_md5(data)

        is_disk = ext in DISK_EXTS
        is_archive = ext in ARCHIVE_EXTS
        is_c64_file = ext in C64_FILE_EXTS

        # Index this member only if it's something we care about.
        # Random text files, READMEs, JPEGs inside scene packs etc
        # are ignored - they bloat the DB without being searchable
        # in a meaningful way.
        if not (is_disk or is_archive or is_c64_file):
            return

        member_file_id = self._insert_archive_member(
            virtual_path=virtual_path,
            name=mname.name,
            extension=ext,
            size=len(data),
            md5=md5,
            container_id=container_id,
            is_archive=is_archive,
            is_disk=is_disk)
        self.files_added += 1

        # Nested archives
        if is_archive and depth < MAX_ARCHIVE_DEPTH:
            # We need a path-like object for our nested handlers.
            # Write to a tempfile so the lha/rar/7z libs can re-open
            # them (most don't accept BytesIO).
            import tempfile
            with tempfile.NamedTemporaryFile(
                    suffix="." + ext, delete=False) as tf:
                tf.write(data)
                tf_path = Path(tf.name)
            try:
                self._ingest_archive(tf_path, member_file_id,
                                     depth + 1)
            finally:
                try:
                    tf_path.unlink()
                except OSError:
                    pass

        # Disk image inside archive - parse via in-memory buffer
        elif is_disk:
            self._ingest_disk_image_data(
                data, member_file_id,
                source_label=virtual_path,
                ext=ext)

    # --------------------------------------------------------
    # Disk image handling
    # --------------------------------------------------------

    def _ingest_disk_image(self, path: Path, file_id: int):
        """Parse a CBM disk image from disk and ingest its entries.

        For BAM-parseable formats (D64/D71/D81) we walk the
        directory and index each PRG/SEQ/USR/REL.

        For raw-track formats (G64/NIB/NBZ) or files whose
        directory walk fails (corrupt BAM, trackloader without
        standard CBM-DOS structure), we still index the file
        itself with its MD5 but log an issue so the user knows
        the contents aren't catalogued. The disk_image row gets
        inserted with file_count=0 - so it shows up in disk
        searches with the filename at least, just no entries
        inside."""
        ext = path.suffix.lower().lstrip(".")
        try:
            data = path.read_bytes()
        except OSError as e:
            self._log_error(f"can't read disk image {path}: {e}")
            database.log_scan_issue(
                self.scan_id, str(path),
                database.ISSUE_CORRUPT_DISK,
                f"Read error: {e}")
            return
        self._ingest_disk_image_data(data, file_id, str(path), ext)

    def _ingest_disk_image_data(self, data: bytes, file_id: int,
                                source_label: str = "",
                                ext: str = ""):
        """Parse a disk image from bytes. Shared between the
        on-disk path and the in-archive path.

        source_label is the path string for issue logging - we
        pass it through because the in-archive code already has
        the virtual path like 'pack.zip!file.d64' and we want
        that to appear in the issues list, not just '(memory)'.

        Trial-tier license gates disk-image cataloging at
        TRIAL_DB_DISK_LIMIT total disks (default 1000). When
        the cap is hit we still keep the disk's MD5 / size /
        filename in the `files` table (so file-name search and
        dedup still work), but we don't walk the directory or
        insert disk_entries. The user can keep adding to their
        catalog up to that disk count; once they cross it they
        get a one-line issue per blocked disk explaining why.
        Pro license bypasses the gate entirely.
        """
        # Trial gate: cap disk-image cataloging
        try:
            from . import license as _lic
            if not _lic.has_feature(_lic.FEATURE_DB_UNLIMITED):
                if database.disk_count() >= _lic.TRIAL_DB_DISK_LIMIT:
                    # Cache "we already warned about this scan"
                    # so we don't spam the issues log with one
                    # row per disk in a 50k-file rescan.
                    if not getattr(self, "_trial_cap_logged",
                                   False):
                        database.log_scan_issue(
                            self.scan_id, source_label,
                            "trial_disk_limit",
                            detail=(f"Trial limit of "
                                    f"{_lic.TRIAL_DB_DISK_LIMIT} "
                                    f"disk images reached. "
                                    f"Disk content not catalogued. "
                                    f"Register to unlock unlimited "
                                    f"cataloging."))
                        self._trial_cap_logged = True
                    return
        except Exception:
            # License lookup transiently failed - behave
            # permissively (no gate), same pattern used by the
            # other trial gates in the codebase.
            pass

        # Format-specific path: raw-track formats (G64/NIB/NBZ)
        # use the external 'nibconv' tool to convert them to D64
        # in memory, then fall through to the regular parser.
        # If nibconv isn't available, log a no_directory issue
        # so the user knows the file is registered by MD5 but
        # contents aren't catalogued.
        if ext in ("g64", "g71", "g81", "nib", "nbz"):
            converted = self._try_convert_to_d64(
                data, ext, source_label)
            if converted is not None:
                data = converted
                ext = "d64"  # fall through to D64 parser path
            else:
                self._insert_disk_no_directory(
                    file_id, source_label, ext,
                    detail=(f"Install 'nibconv' (from nibtools, "
                            f"https://c64preservation.com) into "
                            f"external/ to catalog "
                            f"{ext.upper()} contents"))
                return

        try:
            info = cbmfiles.parse_disk_image(data)
        except Exception as e:
            # cbmfiles raised - log and record the issue but
            # still create a disk_images row with no entries so
            # the file is searchable by name/MD5.
            self._log_error(
                f"parse failed for disk image {source_label}: {e}")
            self._insert_disk_no_directory(
                file_id, source_label, ext,
                detail=f"Directory walk failed: {e}")
            return

        if info is None:
            # parse_disk_image returns None for unrecognized
            # sizes (e.g. G64 with unusual track count, partial
            # downloads). Log so the user can investigate.
            self._insert_disk_no_directory(
                file_id, source_label, ext,
                detail=("Image size doesn't match a known D64/"
                        "D71/D81 layout - might be truncated, "
                        "extended, or a non-standard format"))
            return

        # Successful directory walk. Insert disk_images row and
        # all the entries.
        with database.connection() as conn:
            cur = conn.execute("""
                INSERT INTO disk_images(
                    file_id, disk_name, disk_id, dos_type,
                    image_type, track_count, file_count)
                VALUES(?, ?, ?, ?, ?, ?, ?)
            """, (file_id, info.disk_name, info.disk_id,
                  info.dos_type, info.image_type,
                  info.track_count, len(info.entries)))
            disk_image_id = cur.lastrowid
            # Insert disk_entries rows, one per directory entry
            for ent in info.entries:
                # Only PRG/SEQ/USR/REL are interesting per Mario's
                # request. Skip DEL etc.
                if ent.file_type not in ("prg", "seq", "usr", "rel"):
                    continue
                conn.execute("""
                    INSERT INTO disk_entries(
                        disk_image_id, name, file_type, size_blocks,
                        size_bytes, md5, track, sector)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """, (disk_image_id, ent.name, ent.file_type,
                      ent.size_blocks, ent.size_bytes, ent.md5,
                      ent.track, ent.sector))
            conn.commit()
        self.disks_added += 1

        # Even on success, some entries inside the disk may have
        # had extraction errors (corrupt sectors mid-file). If
        # any entries have NULL md5, that's the marker. Don't
        # spam an issue per file - one summary per disk is
        # plenty.
        bad_entries = sum(1 for ent in info.entries
                          if ent.file_type in ("prg", "seq", "usr", "rel")
                          and ent.md5 is None)
        if bad_entries:
            database.log_scan_issue(
                self.scan_id, source_label,
                database.ISSUE_CORRUPT_DISK,
                f"Disk indexed but {bad_entries} file(s) failed "
                f"to extract (corrupt sectors?)")

    def _try_convert_to_d64(self, data: bytes, ext: str,
                            source_label: str) -> Optional[bytes]:
        """Convert a G64/NIB/NBZ buffer to D64 bytes using the
        external 'nibconv' tool from the nibtools package.

        Returns the D64 bytes on success, None if conversion
        failed for any reason (tool missing, conversion error,
        result not a valid D64). The caller logs an appropriate
        issue based on which case happened.

        We write the input to a temp file, run nibconv, read
        back the resulting .d64. nibconv has slightly different
        invocation per version but the common form is:
            nibconv input.nib output.d64
        which works across all 4.x versions of nibtools."""
        import shutil
        import subprocess
        import tempfile

        # Locate nibconv: PATH first, then our bundled external/
        nibconv = shutil.which("nibconv")
        if nibconv is None:
            from .config import BUNDLE_DIR
            for candidate in (
                BUNDLE_DIR / "external" / "nibconv.exe",
                BUNDLE_DIR / "external" / "nibconv",
            ):
                if candidate.is_file():
                    nibconv = str(candidate)
                    break
        if nibconv is None:
            return None

        tmpdir = tempfile.mkdtemp(prefix="quopus_nibconv_")
        try:
            # Write source to a real file - nibconv needs a path
            src_path = Path(tmpdir) / f"input.{ext}"
            src_path.write_bytes(data)
            dst_path = Path(tmpdir) / "output.d64"
            try:
                result = subprocess.run(
                    [nibconv, str(src_path), str(dst_path)],
                    capture_output=True,
                    timeout=120,  # raw-track conversion is slow
                )
            except subprocess.TimeoutExpired:
                self._log_error(
                    f"nibconv timed out on {source_label} "
                    f"(120s limit)")
                database.log_scan_issue(
                    self.scan_id, source_label,
                    database.ISSUE_EXTRACT_FAILED,
                    "nibconv exceeded 120s timeout - possibly "
                    "corrupt or extremely large image")
                return None
            except OSError as e:
                self._log_error(f"nibconv exec failed: {e}")
                return None

            # nibconv often reports problems via stderr but
            # returns 0 anyway, or warns and still produces a
            # valid output. Trust the output file existing as
            # the success criterion.
            if not dst_path.is_file() or dst_path.stat().st_size == 0:
                stderr_summary = (result.stderr or b"").decode(
                    errors="replace")[:200]
                self._log_error(
                    f"nibconv produced no output for "
                    f"{source_label}: {stderr_summary!r}")
                database.log_scan_issue(
                    self.scan_id, source_label,
                    database.ISSUE_EXTRACT_FAILED,
                    f"nibconv failed: {stderr_summary[:120]}")
                return None

            converted = dst_path.read_bytes()
            return converted
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _insert_disk_no_directory(self, file_id: int,
                                  source_label: str,
                                  ext: str,
                                  detail: str = ""):
        """Insert a disk_images row marking the disk as 'no
        directory walked'. Used for raw-track formats and for
        BAM-parse failures."""
        with database.connection() as conn:
            conn.execute("""
                INSERT INTO disk_images(
                    file_id, disk_name, disk_id, dos_type,
                    image_type, track_count, file_count)
                VALUES(?, '(no directory)', '', '', ?, 0, 0)
            """, (file_id, ext or "raw"))
            conn.commit()
        self.disks_added += 1
        database.log_scan_issue(
            self.scan_id, source_label,
            database.ISSUE_NO_DIRECTORY,
            detail)
