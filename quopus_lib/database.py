"""Quopus Archive Database.

A SQLite catalog of large C64 archives. Lets you search huge
collections by filename or disk header without unpacking the
archives every time.

Three tables:

  files          Physical files on disk (PRG, SEQ, ZIP, D64, etc).
                 Each gets an MD5. If a file came from inside an
                 archive, container_id points to the parent.

  disk_images    For each D64/D71/D81 found, the parsed BAM
                 metadata: disk name, disk ID, DOS type, file
                 count.

  disk_entries   For each PRG/SEQ/USR/REL inside a disk image,
                 its filename (PETSCII-decoded), file type, size
                 in blocks, MD5 of the extracted content.

The DB file lives in CONFIG_DIR/quopus_db.sqlite. It's an ordinary
SQLite file - you can browse it with any sqlite3 tool if needed.

Why SQLite and not a custom flat-file format:
  - Built into Python, no extra dependency
  - WAL mode lets the UI search concurrently while the scanner
    is writing new entries
  - Full-text search (FTS5) for fast filename lookup across
    millions of rows
  - The DB lives in one file you can back up or move easily

Why MD5 and not SHA256:
  - C64 archive scene uses MD5 as the de-facto fingerprint
  - 32 hex chars is half the storage of SHA256 across millions
    of rows
  - Collisions don't matter here; we use MD5 for matching,
    not for cryptographic verification
"""
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Iterator

from .config import CONFIG_DIR


# Where the DB lives. The DEFAULT path is in CONFIG_DIR but the
# user can switch to a different file via set_db_path() to view
# a DB shared by another sysop. switch_to_default() reverts.
#
# Module-level variable so all helpers (connection(), init_db()
# etc) automatically see the current target. We keep a separate
# DEFAULT_DB_PATH constant so we can always get back home.
DEFAULT_DB_PATH = CONFIG_DIR / "quopus_db.sqlite"
DB_PATH = DEFAULT_DB_PATH

# When loading a shared DB from another user, we mark it
# read-only so a stray Scan or Watcher event can't write back
# to it. The flag is consulted by connection() to add the
# SQLite ?mode=ro URI parameter.
_db_readonly = False


def set_db_path(path, readonly: bool = False) -> None:
    """Switch the active database to a different file.

    Used for loading DBs shared by other Quopus users. The
    file should exist and be a valid Quopus catalog; we don't
    validate beforehand - any errors surface as the first
    query against the new DB raises.

    When readonly=True, future connection() calls open the file
    in SQLite's read-only URI mode so scanners and watchers
    can't accidentally modify the friend's DB.
    """
    global DB_PATH, _db_readonly
    DB_PATH = Path(path).expanduser().resolve()
    _db_readonly = readonly


def switch_to_default() -> None:
    """Revert to the user's own DB at the standard CONFIG_DIR
    location, with full read-write access."""
    global DB_PATH, _db_readonly
    DB_PATH = DEFAULT_DB_PATH
    _db_readonly = False


def is_default_db() -> bool:
    """True if the current DB_PATH is the standard one. The UI
    uses this to decide whether to show 'switch back to default'
    options and whether to allow scanning."""
    return DB_PATH == DEFAULT_DB_PATH


def is_readonly() -> bool:
    """True if the current DB is loaded in read-only mode. The
    UI uses this to disable Scan/Reset/Vacuum buttons that
    would modify the file."""
    return _db_readonly


# Schema versioning - when we add or restructure tables, bump
# this. The init code applies migrations to bring old DBs up
# to current.
#
# v4 (2026-05-18): split the unified fts_names FTS5 table into
# two separate ones (fts_names for files, fts_entries for disk
# entries) using rowid identity with the source table. This
# eliminates the slow "DELETE FROM fts_names WHERE kind = ?
# AND ref_id = ?" trigger that did a full FTS scan per delete
# (catastrophic at 40M entries). Now deletes are O(log N)
# rowid lookups instead of O(N) FTS scans.
SCHEMA_VERSION = 4


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Scan-Sessions: each call to "Scan folder" creates a row here
-- so we can show progress, count entries per scan, optionally
-- remove all entries from a specific scan.
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    started_at REAL,
    finished_at REAL,
    root_path TEXT,
    file_count INTEGER DEFAULT 0,
    disk_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'   -- 'running', 'done', 'aborted', 'error'
);

-- Physical files on disk or extracted from an archive.
-- 'container_id' refers to another row in this same table -
-- the ZIP/LHA/etc the file was found inside. Top-level files
-- have container_id = NULL.
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER REFERENCES scans(id) ON DELETE CASCADE,
    path TEXT NOT NULL,             -- absolute path (or virtual for archive children)
    name TEXT NOT NULL,             -- basename
    extension TEXT,                 -- lowercased, without dot
    size INTEGER,
    md5 TEXT,                       -- 32 hex chars
    mtime REAL,                     -- for incremental re-scan
    container_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    is_archive INTEGER DEFAULT 0,   -- 1 if zip/lha/etc
    is_disk INTEGER DEFAULT 0,      -- 1 if d64/d71/d81 (parsed in disk_images)
    -- Crash-recovery state. 'pending' means we started indexing
    -- this file (hash done, row inserted) but didn't finish all
    -- the followup work (archive members, disk entries). On the
    -- next Quopus launch, all pending rows get re-enqueued so
    -- archives interrupted halfway through get retried instead
    -- of being skipped as "already indexed".
    -- 'done' means everything is committed.
    -- 'failed' means we tried and gave up (e.g. unreadable).
    scan_status TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(scan_status);

CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_md5  ON files(md5);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_container ON files(container_id);

-- Disk images: D64/D71/D81 with their BAM metadata
CREATE TABLE IF NOT EXISTS disk_images (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    disk_name TEXT,         -- 16 chars, PETSCII-decoded
    disk_id TEXT,           -- 5 chars typically (2 ID + space + 2 DOS), trimmed
    dos_type TEXT,          -- e.g. '2A'
    image_type TEXT,        -- 'd64' / 'd71' / 'd81'
    track_count INTEGER,
    file_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_disks_name ON disk_images(disk_name);
CREATE INDEX IF NOT EXISTS idx_disks_id   ON disk_images(disk_id);

-- Individual files inside a disk image (PRG / SEQ / USR / REL)
CREATE TABLE IF NOT EXISTS disk_entries (
    id INTEGER PRIMARY KEY,
    disk_image_id INTEGER REFERENCES disk_images(id) ON DELETE CASCADE,
    name TEXT,              -- PETSCII filename, decoded to ASCII
    file_type TEXT,         -- 'prg', 'seq', 'usr', 'rel', 'del'
    size_blocks INTEGER,
    size_bytes INTEGER,     -- if we extracted the file, else NULL
    md5 TEXT,               -- MD5 of extracted content, NULL if we couldn't extract
    track INTEGER,
    sector INTEGER
);

CREATE INDEX IF NOT EXISTS idx_entries_name ON disk_entries(name);
CREATE INDEX IF NOT EXISTS idx_entries_md5  ON disk_entries(md5);
CREATE INDEX IF NOT EXISTS idx_entries_disk ON disk_entries(disk_image_id);

-- Issues found during scanning that need user attention.
-- Password-protected archives, corrupt disks, trackloaded
-- images without standard directories - things we couldn't
-- index normally and the user might want to know about.
CREATE TABLE IF NOT EXISTS scan_issues (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER REFERENCES scans(id) ON DELETE CASCADE,
    path TEXT NOT NULL,         -- file that had the problem
    issue_type TEXT NOT NULL,   -- 'password' / 'corrupt_disk' /
                                -- 'no_directory' / 'extract_failed' /
                                -- 'unknown_format'
    detail TEXT,                -- human-readable explanation
    occurred_at REAL            -- unix timestamp
);

CREATE INDEX IF NOT EXISTS idx_issues_type ON scan_issues(issue_type);
CREATE INDEX IF NOT EXISTS idx_issues_scan ON scan_issues(scan_id);

-- FTS5 virtual table for fast filename search across files +
-- disk entries. We populate it via triggers from the main
-- tables so the FTS index stays in sync automatically.
-- Two separate FTS5 trigram indexes - one per source table.
-- The rowid is set explicitly to match the source row's id,
-- so deletes can use the fast WHERE rowid = ? path instead of
-- a full scan over the FTS index (which was the v3 bug: a
-- combined "fts_names" with UNINDEXED kind+ref_id columns
-- made every DELETE on disk_entries scan the entire FTS5
-- contents at O(N), turning a "remove one disk image" op
-- into a 2.7s nightmare at 700k rows and a multi-hour stall
-- at projected scale).
CREATE VIRTUAL TABLE IF NOT EXISTS fts_names USING fts5(
    name,
    tokenize='trigram'   -- substring matching: "blas" finds "klausenburg"
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_entries USING fts5(
    name,
    tokenize='trigram'
);

-- Triggers to keep FTS in sync with the source tables.
-- All four are O(log N) because they use the rowid identity.
CREATE TRIGGER IF NOT EXISTS files_ai
AFTER INSERT ON files BEGIN
    INSERT INTO fts_names(rowid, name) VALUES (new.id, new.name);
END;

CREATE TRIGGER IF NOT EXISTS files_ad
AFTER DELETE ON files BEGIN
    DELETE FROM fts_names WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS files_au
AFTER UPDATE OF name ON files BEGIN
    UPDATE fts_names SET name = new.name WHERE rowid = new.id;
END;

CREATE TRIGGER IF NOT EXISTS entries_ai
AFTER INSERT ON disk_entries BEGIN
    INSERT INTO fts_entries(rowid, name) VALUES (new.id, new.name);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad
AFTER DELETE ON disk_entries BEGIN
    DELETE FROM fts_entries WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS entries_au
AFTER UPDATE OF name ON disk_entries BEGIN
    UPDATE fts_entries SET name = new.name WHERE rowid = new.id;
END;
"""


def _connect() -> sqlite3.Connection:
    """Open a connection with sensible defaults. The caller is
    responsible for closing it - use the connection() context
    manager below for the common case.

    When the active DB is in read-only mode (a shared catalog
    from another sysop), we open it with SQLite's URI ?mode=ro
    parameter so no accidental writes can corrupt the file.
    WAL mode isn't applied in that case because WAL needs write
    access to create the -wal/-shm sidecar files.

    Lock-contention robustness: the bulk scanner runs 2 worker
    threads + the watcher + the UI thread on the same DB. WAL
    permits one writer + many readers concurrently, but two
    writers meeting at BEGIN IMMEDIATE will race. Python's
    sqlite3 default timeout is 5s, which is often not enough
    during big batch commits. We raise it to 60s here and also
    set PRAGMA busy_timeout to match - both are needed because
    they cover slightly different code paths in the C bindings.
    """
    if _db_readonly:
        # SQLite URI form: file:/path?mode=ro
        # uri=True tells the connector to parse the string as
        # a URI; without it the '?' is just part of the filename.
        # We use absolute path with file: prefix.
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60.0)
        conn.row_factory = sqlite3.Row
        # Foreign keys are advisory in read-only mode but harmless
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
        except sqlite3.OperationalError:
            pass
        _apply_scale_pragmas(conn)
        _register_helpers(conn)
        return conn
    # Read-write: ensure dir exists then connect normally
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    # WAL + foreign keys must be set per-connection (PRAGMA in
    # the schema only affects the first connection that runs it).
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # busy_timeout = how long sqlite waits on SQLITE_BUSY before
    # returning the error to the caller. 60s gives bulk commits
    # enough breathing room even on a slow disk.
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
    except sqlite3.OperationalError:
        pass
    _apply_scale_pragmas(conn)
    _register_helpers(conn)
    return conn


def _apply_scale_pragmas(conn: sqlite3.Connection) -> None:
    """Apply PRAGMAs tuned for large catalogs (~2M files,
    ~40M disk entries → ~12 GB DB).

    These are all per-connection settings that aren't persisted
    in the DB file itself, so they have to be set every time.

    - cache_size = -262144 (256 MB) keeps recently-used pages
      hot in RAM. Default is ~2 MB which is way too small for
      a 12 GB catalog - every query hits disk. 256 MB covers
      typical "I'm searching for various things" working sets.
      Negative value = kibibytes, positive = pages.

    - mmap_size = 256 MB lets SQLite memory-map a chunk of the
      DB file for read access, avoiding read() syscall overhead
      for the index pages. Especially helps the FTS5 trigram
      index which has ~1.6 GB of seek-heavy pages at full
      scale. Linux/Windows/macOS all support this.

    - temp_store = MEMORY keeps GROUP BY / ORDER BY / DISTINCT
      scratch space in RAM instead of /tmp. The dedupe query
      (GROUP BY md5 across 2M rows) writes ~50 MB temp data,
      which on a slow USB stick or network mount would be
      painful. RAM-resident is always fast.

    - synchronous = NORMAL trades a tiny bit of crash safety
      for a 2-3x speedup on writes. With WAL+NORMAL, only a
      power loss during a checkpoint can corrupt the DB; a
      regular OS crash leaves it consistent. For a file
      catalog that's re-scannable from the source filesystem,
      this is an easy trade. (FULL is for banks.)
    """
    try:
        conn.execute("PRAGMA cache_size = -262144")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA temp_store = MEMORY")
        if not _db_readonly:
            conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        # Some PRAGMAs fail silently if not supported in the
        # build; we don't care - they're just optimizations.
        pass


def _register_helpers(conn: sqlite3.Connection) -> None:
    """Register SQL helper functions used by our queries.

    Per-connection registration is unavoidable for sqlite3.connect
    (custom functions don't persist across connections). We do
    this at the end of _connect() so every code path picks them
    up uniformly.

    Currently provides:
      ws_match(haystack, needle)
        Whitespace-tolerant substring match. Treats every run of
        whitespace in `needle` as equivalent to ANY run of
        whitespace in `haystack`. Used by search_filenames() so
        a query like 'end 1' (one space) matches files named
        'end  1' or 'end\\t1' (tab) - the user has no way of
        knowing what kind of whitespace was used in the original
        filename, so we don't make them guess. Case-insensitive
        via .lower().
    """
    import re

    def ws_match(haystack, needle):
        if haystack is None or needle is None:
            return False
        # Pattern: each whitespace run in needle becomes \s+ in
        # the regex; everything else gets re.escape'd literal.
        # Empty needle matches everything (defensive - the caller
        # should have already short-circuited).
        if not needle:
            return True
        parts = needle.split()
        if not parts:
            return False
        pattern = r"\s+".join(re.escape(p) for p in parts)
        return re.search(pattern, haystack, re.IGNORECASE) is not None

    try:
        conn.create_function("ws_match", 2, ws_match,
                              deterministic=True)
    except TypeError:
        # Older Python < 3.8 doesn't support deterministic kwarg
        conn.create_function("ws_match", 2, ws_match)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Context manager that opens, yields, and closes a connection."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def needs_migration() -> tuple[bool, int]:
    """Check whether the DB on disk needs a schema migration.

    Returns (needs_it, current_version). If the DB doesn't
    exist yet, returns (False, 0) - init_db() will create it
    fresh at the current version, no migration needed.

    Used by quopus.py at startup to decide whether to show a
    "migrating, please wait..." progress dialog. Cheap - just
    one SQL query against schema_meta.
    """
    if not DB_PATH.is_file():
        return (False, 0)
    try:
        # Use a fresh standalone connection - don't go through
        # _connect() since that might trigger init_db side
        # effects on the schema_meta table.
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_meta'")
            if cur.fetchone() is None:
                return (False, 0)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row is None:
                return (False, 0)
            current = int(row[0])
            return (current < SCHEMA_VERSION, current)
        finally:
            conn.close()
    except Exception:
        # If we can't even open it, the migration code will
        # surface a real error; don't pre-empt with a false yes
        return (False, 0)


def backup_db_before_migration() -> Optional[Path]:
    """Make a sibling copy of the DB before we touch it.

    Returns the path of the backup, or None if backup failed
    (we keep going anyway - some backup is better than blocking
    a migration that the user actually wants). The backup uses
    a timestamp suffix so multiple runs don't clobber each
    other:  quopus_db.sqlite.pre_v4_backup_20260518_120000

    Uses SQLite's online backup API rather than a raw file copy
    so it works even if a watcher is in the middle of writing
    to the source DB.
    """
    if not DB_PATH.is_file():
        return None
    import time as _time
    ts = _time.strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.with_name(
        f"{DB_PATH.name}.pre_v{SCHEMA_VERSION}_backup_{ts}")
    try:
        src = sqlite3.connect(str(DB_PATH))
        try:
            dst = sqlite3.connect(str(backup_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return backup_path
    except Exception:
        # Don't fail the migration just because the backup
        # didn't work. The user can always make their own
        # copy of the .sqlite file outside Quopus.
        return None


def init_db(progress_cb=None) -> None:
    """Create the schema if it doesn't exist, run any migrations
    needed for an older schema. Safe to call on every startup.

    No-op when the DB is loaded read-only - we can't write to
    schema_meta in that case, and a shared DB from another user
    should already have its schema set up. If the friend's DB
    is on an old schema version Quopus can't migrate it without
    write access; queries will still work for compatible columns
    but features added in later schemas (like the issues tab)
    may show empty results."""
    if _db_readonly:
        return
    with connection() as conn:
        # First check if we're on an older schema. If so, run
        # the migration BEFORE we apply the current SCHEMA_SQL -
        # otherwise CREATE IF NOT EXISTS would add v4 tables on
        # top of a v3 layout and the migration's DROP+CREATE
        # logic would end up dealing with a mixed state.
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='schema_meta'"
        ).fetchone() is not None
        old = None
        if has_meta:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row is not None:
                old = int(row["value"])

        if old is not None and old < SCHEMA_VERSION:
            _migrate(conn, old, SCHEMA_VERSION, progress_cb)

        # Apply schema. CREATE IF NOT EXISTS makes this idempotent
        # so we can re-run on every Quopus start without worry.
        # Runs after migration so it can fill any gaps but won't
        # clash with already-created v4 objects.
        conn.executescript(SCHEMA_SQL)

        # Record the version we're on. Future schema bumps trigger
        # migrations here.
        if old is None:
            conn.execute(
                "INSERT INTO schema_meta(key, value) "
                "VALUES('version', ?)",
                (str(SCHEMA_VERSION),))
        elif old < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_meta SET value = ? "
                "WHERE key = 'version'",
                (str(SCHEMA_VERSION),))
        conn.commit()


def _migrate(conn: sqlite3.Connection, from_v: int, to_v: int,
             progress_cb=None) -> None:
    """Apply schema migrations. Each `if from_v < N` block runs
    on its way up to version N, in order.

    progress_cb, if given, is called periodically during the
    long-running parts (v3->v4 re-index is O(N) over a possibly-
    huge table). Signature:
        progress_cb(stage: str, current: int, total: int)
    where stage is a short label like 'reindex_files' and
    current/total let the UI render a progress bar. The migration
    code calls it at least once per stage so the UI has something
    to show even for fast migrations.

    Migration pattern:
        if from_v < 2:
            conn.execute("ALTER TABLE files ADD COLUMN foo TEXT")
        if from_v < 3:
            ...
    """
    def _say(stage, cur=0, total=0):
        if progress_cb is not None:
            try:
                progress_cb(stage, cur, total)
            except Exception:
                pass
    if from_v < 2:
        # v1 -> v2: add scan_issues table. Use CREATE IF NOT
        # EXISTS so a re-run of init_db on an already-migrated
        # DB doesn't blow up.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scan_issues (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER REFERENCES scans(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                detail TEXT,
                occurred_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_issues_type
                ON scan_issues(issue_type);
            CREATE INDEX IF NOT EXISTS idx_issues_scan
                ON scan_issues(scan_id);
        """)
    if from_v < 3:
        # v2 -> v3: add scan_status column for crash recovery.
        # Existing rows are marked 'done' since we can't know
        # retroactively whether they were interrupted; the user
        # can run a full rescan if they suspect data was lost
        # to an earlier crash before this version landed.
        try:
            conn.execute(
                "ALTER TABLE files ADD COLUMN "
                "scan_status TEXT DEFAULT 'done'")
        except sqlite3.OperationalError:
            # Column already exists - probably a partial
            # migration that didn't bump the version. Idempotent.
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_status "
            "ON files(scan_status)")

    if from_v < 4:
        # v3 -> v4: rebuild FTS5 layout. The v3 schema had ONE
        # combined fts_names with kind/ref_id UNINDEXED, which
        # made every DELETE on disk_entries do an O(N) scan of
        # the FTS index (slow at 700k rows, catastrophic at the
        # projected 40M-entry scale). v4 splits into two tables
        # with rowid identity to the source, turning deletes
        # into O(log N) operations.
        #
        # The old fts_names is dropped (along with its content/
        # docsize/idx shadow tables) and rebuilt from scratch
        # by re-indexing every file and disk_entry. On a 12 GB
        # DB this could take a minute or two but only happens
        # once per upgrade.
        #
        # The old triggers are dropped explicitly because they
        # reference the now-incompatible column layout.
        conn.executescript("""
            DROP TRIGGER IF EXISTS files_ai;
            DROP TRIGGER IF EXISTS files_ad;
            DROP TRIGGER IF EXISTS files_au;
            DROP TRIGGER IF EXISTS entries_ai;
            DROP TRIGGER IF EXISTS entries_ad;
            DROP TRIGGER IF EXISTS entries_au;
            DROP TABLE IF EXISTS fts_names;
            DROP TABLE IF EXISTS fts_entries;

            CREATE VIRTUAL TABLE fts_names USING fts5(
                name,
                tokenize='trigram'
            );
            CREATE VIRTUAL TABLE fts_entries USING fts5(
                name,
                tokenize='trigram'
            );

            CREATE TRIGGER files_ai
            AFTER INSERT ON files BEGIN
                INSERT INTO fts_names(rowid, name) VALUES (new.id, new.name);
            END;
            CREATE TRIGGER files_ad
            AFTER DELETE ON files BEGIN
                DELETE FROM fts_names WHERE rowid = old.id;
            END;
            CREATE TRIGGER files_au
            AFTER UPDATE OF name ON files BEGIN
                UPDATE fts_names SET name = new.name WHERE rowid = new.id;
            END;
            CREATE TRIGGER entries_ai
            AFTER INSERT ON disk_entries BEGIN
                INSERT INTO fts_entries(rowid, name) VALUES (new.id, new.name);
            END;
            CREATE TRIGGER entries_ad
            AFTER DELETE ON disk_entries BEGIN
                DELETE FROM fts_entries WHERE rowid = old.id;
            END;
            CREATE TRIGGER entries_au
            AFTER UPDATE OF name ON disk_entries BEGIN
                UPDATE fts_entries SET name = new.name WHERE rowid = new.id;
            END;
        """)
        # Re-index everything. On a large DB (40M entries) this
        # is the slow part of the migration - we do it in 50k-row
        # chunks so the UI can report progress and the user knows
        # it isn't frozen.
        #
        # Two passes: one for files (smaller, ~2M rows), one for
        # disk_entries (~40M). Total is reported across both.
        n_files = conn.execute(
            "SELECT COUNT(*) FROM files").fetchone()[0]
        n_entries = conn.execute(
            "SELECT COUNT(*) FROM disk_entries").fetchone()[0]
        grand_total = n_files + n_entries
        done = 0
        _say("reindex", 0, grand_total)
        CHUNK = 50000
        # Files
        max_id = conn.execute(
            "SELECT MAX(id) FROM files").fetchone()[0] or 0
        cur_id = 0
        while cur_id < max_id:
            next_id = cur_id + CHUNK
            conn.execute(
                "INSERT INTO fts_names(rowid, name) "
                "SELECT id, name FROM files "
                "WHERE id > ? AND id <= ?",
                (cur_id, next_id))
            cur_id = next_id
            # Estimate completed rows - the actual count varies
            # if IDs are sparse, but for progress display this is
            # close enough.
            done = min(n_files, done + CHUNK)
            _say("reindex_files", done, grand_total)
        done = n_files
        _say("reindex_files", done, grand_total)
        # Disk entries
        max_id = conn.execute(
            "SELECT MAX(id) FROM disk_entries").fetchone()[0] or 0
        cur_id = 0
        while cur_id < max_id:
            next_id = cur_id + CHUNK
            conn.execute(
                "INSERT INTO fts_entries(rowid, name) "
                "SELECT id, name FROM disk_entries "
                "WHERE id > ? AND id <= ?",
                (cur_id, next_id))
            cur_id = next_id
            done = min(grand_total, done + CHUNK)
            _say("reindex_entries", done, grand_total)
        _say("reindex_done", grand_total, grand_total)


def file_md5(path: Path, chunk_size: int = 256 * 1024) -> str:
    """Compute the MD5 of a file in streaming chunks so we don't
    blow up memory on huge ZIPs."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def bytes_md5(data: bytes) -> str:
    """Compute the MD5 of an in-memory bytes object. Used for
    files extracted from inside archives or disk images that we
    have as bytes rather than on-disk paths."""
    return hashlib.md5(data).hexdigest()


# ============================================================
# High-level query helpers used by the UI
# ============================================================


def search_filenames(query: str, limit: int = 500,
                     dedupe_md5: bool = False) -> list[dict]:
    """Find files and disk entries whose name matches the query.

    For queries of 3+ characters: uses FTS5 trigram tokenizer
    for fast substring matching across millions of rows.

    For queries of 1-2 characters: falls back to plain LIKE
    '%q%' scan. The trigram tokenizer can't match queries
    shorter than its trigram size (3), so without this fallback
    'aa' would return nothing even with millions of matching
    rows. LIKE is slower (full table scan) but the user
    typically only types 1-2 chars in transient cases, and
    even at 1M rows a LIKE scan completes in under a second.

    When dedupe_md5 is True, results are reduced so that each
    unique MD5 appears only once. The first hit wins (in FTS
    order or LIKE order, which is usually scan order). Hits
    with a NULL MD5 are passed through unchanged - we can't
    tell them apart. The result dict gets two extra keys:
        dup_count:  total number of rows sharing this MD5
                    (always >= 1; 1 means the file is unique)
        dup_kept:   True for the row that was kept after
                    deduplication (always True in returned
                    results; useful for callers that want to
                    re-merge later)

    Returns a list of dicts with keys:
        kind:    'file' or 'entry'
        name:    matched name
        path:    full path (for files) or "disk_name:entry_name"
                 (for entries)
        size:    file size in bytes
        md5:     MD5 hex string
        ref_id:  the row ID in the underlying table
    """
    q = query.strip()
    if not q:
        return []
    # If we're going to dedupe, fetch more rows than the user
    # asked for - otherwise N duplicates of the same MD5 collapse
    # into 1 result and we hand back a near-empty list. We can't
    # cheaply do GROUP BY md5 in SQL because of the FTS5 -> files
    # join and the multi-table union, so over-fetch and dedupe
    # in Python.  5x is a heuristic that works well: a typical
    # archive that triggers dedupe (Scenebase mirrors) has dup
    # factors of 2-10x; 5x catches the common case without
    # blowing up memory for non-dup'd queries.
    fetch_limit = limit * 5 if dedupe_md5 else limit
    # Whitespace in the query is tricky: FTS5's trigram tokenizer
    # collapses whitespace runs, so a phrase search for "end 1"
    # (one space) won't match "end  1" (two spaces) in the index.
    # The user has no way to tell what kind of whitespace was used
    # in a filename, so we route ANY whitespace-containing query
    # through the LIKE path with whitespace-tolerant matching
    # (each whitespace run in the query matches one-or-more
    # whitespace chars in the target). LIKE on the indexed name
    # column is still fast enough for typical scenebase sizes -
    # we trade strict-substring matching for a search that
    # actually does what the user expects.
    has_whitespace = any(c.isspace() for c in q)
    if has_whitespace or len(q) < 3:
        rows = _search_filenames_like(q, fetch_limit)
    else:
        rows = _search_filenames_fts(q, fetch_limit)
    if dedupe_md5:
        rows = _apply_md5_dedup(rows)
        # Apply the user's actual limit after dedup
        if len(rows) > limit:
            rows = rows[:limit]
    return rows


def _apply_md5_dedup(rows: list[dict]) -> list[dict]:
    """Collapse rows that share an MD5 into a single representative.

    Pre-condition: rows is a flat list of result dicts from
    _search_filenames_fts or _like, each with a "md5" key
    (possibly None).

    The first occurrence wins. Subsequent rows with the same MD5
    are dropped, but we increment dup_count on the kept row so
    the UI can show a "(+N more)" badge. Rows without an MD5
    are always kept - we can't deduplicate them safely.
    """
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        md5 = r.get("md5")
        if not md5:
            # No MD5 -> pass through, but flag as singleton
            r["dup_count"] = 1
            r["dup_kept"] = True
            out.append(r)
            continue
        if md5 in seen:
            # Duplicate -> increment counter on the kept row
            seen[md5]["dup_count"] += 1
        else:
            # First sighting of this MD5 -> keep it
            r["dup_count"] = 1
            r["dup_kept"] = True
            seen[md5] = r
            out.append(r)
    return out


def find_by_md5(md5: str) -> list[dict]:
    """Return every file and disk entry that has the given MD5.

    Used by the DB browser's "show duplicates" action when the
    user wants to see all copies of a file after the result
    list was deduplicated. Returns the same dict shape as
    search_filenames() so the browser can render them with the
    existing row builder.

    The MD5 must be a lowercase 32-char hex string.
    """
    if not md5:
        return []
    results = []
    with connection() as conn:
        # Files matching this MD5
        cur = conn.execute("""
            SELECT f.id, f.name, f.path, f.size, f.md5,
                   f.is_disk, f.is_archive,
                   c.path as container_path
            FROM files f
            LEFT JOIN files c ON c.id = f.container_id
            WHERE f.md5 = ?
        """, (md5,))
        for info in cur.fetchall():
            results.append({
                "kind": "file",
                "name": info["name"],
                "path": info["path"],
                "container": info["container_path"],
                "size": info["size"],
                "md5": info["md5"],
                "is_disk": bool(info["is_disk"]),
                "is_archive": bool(info["is_archive"]),
                "ref_id": info["id"],
                "dup_count": 1,
                "dup_kept": True,
            })
        # Disk entries matching this MD5
        cur = conn.execute("""
            SELECT e.id, e.name, e.file_type, e.size_blocks,
                   e.size_bytes, e.md5,
                   d.disk_name, d.image_type,
                   f.id as disk_file_id, f.path as disk_path
            FROM disk_entries e
            JOIN disk_images d ON d.id = e.disk_image_id
            JOIN files f ON f.id = d.file_id
            WHERE e.md5 = ?
        """, (md5,))
        for info in cur.fetchall():
            results.append({
                "kind": "entry",
                "name": info["name"],
                "path": (f"{info['disk_path']}:"
                         f"{info['disk_name'] or '?'}/"
                         f"{info['name']}"),
                "disk_name": info["disk_name"],
                "disk_path": info["disk_path"], "disk_file_id": info["disk_file_id"],
                "file_type": info["file_type"],
                "size_blocks": info["size_blocks"],
                "size_bytes": info["size_bytes"],
                "md5": info["md5"],
                "ref_id": info["id"],
                "dup_count": 1,
                "dup_kept": True,
            })
    return results


def _search_filenames_fts(query: str, limit: int) -> list[dict]:
    """Fast path: trigram FTS5 search. Used for queries with 3+
    characters where the index can give us substring matches
    in O(log n)."""
    # FTS5 trigram tokenizer: wrap the query in quotes to make
    # it a phrase, escape any embedded quotes.
    safe = query.replace('"', '""')
    fts_query = f'"{safe}"'
    return _resolve_filename_rows("fts", fts_query, limit)


def _search_filenames_like(query: str, limit: int) -> list[dict]:
    """Slow path: LIKE / ws_match scan.

    Used for two cases:
      - Queries under 3 chars (FTS5 trigram index can't help)
      - Queries containing whitespace (FTS5's tokenizer collapses
        whitespace runs, which makes phrase matches unpredictable
        across "end 1" vs "end  1" vs "end\\t1")

    For plain queries (no whitespace) we use SQL LIKE with a
    bracketing %% so it can use a prefix index hit if present.
    For whitespace queries we fall back to ws_match() - a custom
    SQL function registered at connection time - which treats
    every run of whitespace in the needle as equivalent to any
    run of whitespace in the target name. The user typing
    'end 1' finds 'end  1', 'end\\t1', 'end\\n1' etc - they
    shouldn't have to know whether the file's author used
    one space, two spaces, or a tab.
    """
    has_whitespace = any(c.isspace() for c in query)
    results = []
    with connection() as conn:
        if has_whitespace:
            # Whitespace-tolerant path via ws_match()
            cur = conn.execute("""
                SELECT f.id, f.name, f.path, f.size, f.md5,
                       f.is_disk, f.is_archive,
                       c.path as container_path
                FROM files f
                LEFT JOIN files c ON c.id = f.container_id
                WHERE ws_match(f.name, ?)
                LIMIT ?
            """, (query, limit))
        else:
            # Plain LIKE path - faster than ws_match in the
            # common case
            like_pattern = f"%{query}%"
            cur = conn.execute("""
                SELECT f.id, f.name, f.path, f.size, f.md5,
                       f.is_disk, f.is_archive,
                       c.path as container_path
                FROM files f
                LEFT JOIN files c ON c.id = f.container_id
                WHERE f.name LIKE ? COLLATE NOCASE
                LIMIT ?
            """, (like_pattern, limit))
        for info in cur.fetchall():
            results.append({
                "kind": "file",
                "name": info["name"],
                "path": info["path"],
                "container": info["container_path"],
                "size": info["size"],
                "md5": info["md5"],
                "is_disk": bool(info["is_disk"]),
                "is_archive": bool(info["is_archive"]),
                "ref_id": info["id"],
            })
            if len(results) >= limit:
                return results
        # Disk entries
        remaining = limit - len(results)
        if remaining > 0:
            if has_whitespace:
                cur = conn.execute("""
                    SELECT e.id, e.name, e.file_type, e.size_blocks,
                           e.size_bytes, e.md5,
                           d.disk_name, d.image_type,
                           f.id as disk_file_id, f.path as disk_path
                    FROM disk_entries e
                    JOIN disk_images d ON d.id = e.disk_image_id
                    JOIN files f ON f.id = d.file_id
                    WHERE ws_match(e.name, ?)
                    LIMIT ?
                """, (query, remaining))
            else:
                cur = conn.execute("""
                    SELECT e.id, e.name, e.file_type, e.size_blocks,
                           e.size_bytes, e.md5,
                           d.disk_name, d.image_type,
                           f.id as disk_file_id, f.path as disk_path
                    FROM disk_entries e
                    JOIN disk_images d ON d.id = e.disk_image_id
                    JOIN files f ON f.id = d.file_id
                    WHERE e.name LIKE ? COLLATE NOCASE
                    LIMIT ?
                """, (f"%{query}%", remaining))
            for info in cur.fetchall():
                results.append({
                    "kind": "entry",
                    "name": info["name"],
                    "path": (f"{info['disk_path']}:"
                             f"{info['disk_name'] or '?'}/"
                             f"{info['name']}"),
                    "disk_name": info["disk_name"],
                    "disk_path": info["disk_path"], "disk_file_id": info["disk_file_id"],
                    "file_type": info["file_type"],
                    "size_blocks": info["size_blocks"],
                    "size_bytes": info["size_bytes"],
                    "md5": info["md5"],
                    "ref_id": info["id"],
                })
    return results


def _resolve_filename_rows(mode: str, fts_query: str,
                           limit: int) -> list[dict]:
    """Run an FTS5 query against the split file + entry indexes
    and resolve each matched row to its full info (file path,
    MD5, size etc).

    Schema v4+ keeps two separate FTS5 tables - fts_names for
    files, fts_entries for disk entries - each with rowid
    identical to the source table's id. This means resolving
    a hit back to its full info is a pure rowid lookup (no FTS
    join), and deletes on the source tables propagate via
    trigger in O(log N) time instead of the v3 O(N) full scan.

    We fetch up to `limit` hits from each table, then merge.
    Could be smarter (e.g. interleave by relevance score) but
    in practice both index types are equally interesting and
    the UI sorts by name anyway.
    """
    results = []
    half = max(1, limit // 2)
    with connection() as conn:
        # Files match
        cur = conn.execute("""
            SELECT f.id, f.name, f.path, f.size, f.md5,
                   f.is_disk, f.is_archive,
                   c.path as container_path
            FROM fts_names
            JOIN files f ON f.id = fts_names.rowid
            LEFT JOIN files c ON c.id = f.container_id
            WHERE fts_names.name MATCH ?
            LIMIT ?
        """, (fts_query, limit))
        for info in cur.fetchall():
            results.append({
                "kind": "file",
                "name": info["name"],
                "path": info["path"],
                "container": info["container_path"],
                "size": info["size"],
                "md5": info["md5"],
                "is_disk": bool(info["is_disk"]),
                "is_archive": bool(info["is_archive"]),
                "ref_id": info["id"],
            })
        # Disk entry match
        remaining = limit - len(results)
        if remaining > 0:
            cur = conn.execute("""
                SELECT e.id, e.name, e.file_type, e.size_blocks,
                       e.size_bytes, e.md5,
                       d.disk_name, d.image_type,
                       f.id as disk_file_id, f.path as disk_path
                FROM fts_entries
                JOIN disk_entries e ON e.id = fts_entries.rowid
                JOIN disk_images d ON d.id = e.disk_image_id
                JOIN files f ON f.id = d.file_id
                WHERE fts_entries.name MATCH ?
                LIMIT ?
            """, (fts_query, remaining))
            for info in cur.fetchall():
                results.append({
                    "kind": "entry",
                    "name": info["name"],
                    "path": (f"{info['disk_path']}:"
                             f"{info['disk_name'] or '?'}/"
                             f"{info['name']}"),
                    "disk_name": info["disk_name"],
                    "disk_path": info["disk_path"], "disk_file_id": info["disk_file_id"],
                    "file_type": info["file_type"],
                    "size_blocks": info["size_blocks"],
                    "size_bytes": info["size_bytes"],
                    "md5": info["md5"],
                    "ref_id": info["id"],
                })
    return results


def search_disk_headers(query: str, limit: int = 200) -> list[dict]:
    """Find disk images whose disk_name or disk_id matches.

    Plain LIKE with COLLATE NOCASE so 'SIDOLOGY' and 'sidology'
    match interchangeably. Disk names are usually short and
    there aren't that many of them (a giant scene archive has
    thousands of disks, not millions), so LIKE is fast enough
    without an FTS index.
    """
    if not query.strip():
        return []
    like_pattern = f"%{query.strip()}%"
    results = []
    with connection() as conn:
        cur = conn.execute("""
            SELECT d.id, d.disk_name, d.disk_id, d.image_type,
                   d.file_count, f.path
            FROM disk_images d
            JOIN files f ON f.id = d.file_id
            WHERE d.disk_name LIKE ? COLLATE NOCASE
               OR d.disk_id LIKE ? COLLATE NOCASE
            ORDER BY d.disk_name
            LIMIT ?
        """, (like_pattern, like_pattern, limit))
        for row in cur.fetchall():
            results.append({
                "id": row["id"],
                "disk_name": row["disk_name"],
                "disk_id": row["disk_id"],
                "image_type": row["image_type"],
                "file_count": row["file_count"],
                "path": row["path"],
            })
    return results


def find_duplicates_by_md5(limit: int = 200) -> list[dict]:
    """Return groups of files / disk entries that share an MD5.
    Useful for finding duplicate downloads in a giant archive."""
    results = []
    with connection() as conn:
        # Files
        cur = conn.execute("""
            SELECT md5, COUNT(*) as n, GROUP_CONCAT(path, '|') as paths
            FROM files
            WHERE md5 IS NOT NULL
            GROUP BY md5
            HAVING n > 1
            ORDER BY n DESC
            LIMIT ?
        """, (limit,))
        for row in cur.fetchall():
            results.append({
                "kind": "file",
                "md5": row["md5"],
                "count": row["n"],
                "paths": row["paths"].split("|") if row["paths"] else [],
            })
        # Disk entries
        cur = conn.execute("""
            SELECT md5, COUNT(*) as n,
                   GROUP_CONCAT(name, '|') as names
            FROM disk_entries
            WHERE md5 IS NOT NULL
            GROUP BY md5
            HAVING n > 1
            ORDER BY n DESC
            LIMIT ?
        """, (limit,))
        for row in cur.fetchall():
            results.append({
                "kind": "entry",
                "md5": row["md5"],
                "count": row["n"],
                "names": row["names"].split("|") if row["names"] else [],
            })
    return results


def disk_count() -> int:
    """Cheap count of all disk_images rows. Used by the trial
    gate in the scanner - we don't want to compute the full
    stats() dict on every disk insertion.

    Counted on disk_images (the actual disk-image rows we
    catalog: D64, D71, D81, G64, TAP, etc), not on the parent
    `files` table or on disk_entries (which would count the
    contents of each disk separately and balloon the number).
    """
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM disk_images").fetchone()
        return int(row["n"])


def stats() -> dict:
    """Counts and sizes for the DB overview UI."""
    with connection() as conn:
        files = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(size), 0) as bytes "
            "FROM files").fetchone()
        disks = conn.execute(
            "SELECT COUNT(*) as n FROM disk_images").fetchone()
        entries = conn.execute(
            "SELECT COUNT(*) as n FROM disk_entries").fetchone()
        scans = conn.execute(
            "SELECT COUNT(*) as n FROM scans").fetchone()
        # Pending and failed rows are useful diagnostics - the
        # user can see at a glance if the previous session was
        # interrupted (pending > 0) or hit unrecoverable files
        # (failed > 0). Both numbers should normally be 0.
        pending = conn.execute(
            "SELECT COUNT(*) as n FROM files "
            "WHERE scan_status = 'pending'").fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) as n FROM files "
            "WHERE scan_status = 'failed'").fetchone()
        return {
            "files": files["n"],
            "bytes": files["bytes"],
            "disks": disks["n"],
            "entries": entries["n"],
            "scans": scans["n"],
            "pending": pending["n"],
            "failed": failed["n"],
        }


def latest_scan() -> Optional[dict]:
    """Return info on the most recent scan, or None if no scans
    have ever been run."""
    with connection() as conn:
        row = conn.execute("""
            SELECT id, started_at, finished_at, root_path,
                   file_count, disk_count, error_count, status
            FROM scans
            ORDER BY started_at DESC
            LIMIT 1
        """).fetchone()
        return dict(row) if row else None


def file_already_indexed(path: str, mtime: float) -> bool:
    """Check whether a file at this path with this mtime is
    already fully indexed.

    Only matches rows with status='done'. A 'pending' row means
    the previous scan was interrupted partway through (Quopus
    crashed, killed mid-archive-walk, etc.) - those need to be
    redone. A 'failed' row means we already gave up; the user
    must explicitly retry via re-scan to give it another chance.

    This is the function that incremental scanning relies on to
    avoid re-hashing every file every time, so getting the
    status filter right is critical for crash recovery."""
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM files "
            "WHERE path = ? AND mtime = ? AND scan_status = 'done'",
            (path, mtime)).fetchone()
        return row is not None


def get_file_container_chain(file_id: int) -> list[dict]:
    """Walk a file's container hierarchy and return the chain
    of rows from the outermost real-on-disk file inward to the
    target.

    For a regular on-disk file the chain has one entry: just
    that file. For a member nested inside archives the chain
    is [outer_archive, ..., parent, file] where parent might
    itself be inside another container.

    Each entry is a dict with: id, name, path, container_id,
    is_archive, is_disk, extension.

    This exists because the scanner stores nested-member paths
    as 'parent_path!member_name', which works for display but
    can't be cleanly parsed back when filenames themselves
    contain '!' (common in scene releases like '!Read.me' or
    'Group!Title.d64'). Walking container_id is unambiguous.
    """
    chain = []
    current_id = file_id
    # Guard against pathological cycles - SQL FK should prevent
    # them but a corrupt DB might still have one.
    seen = set()
    with connection() as conn:
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            row = conn.execute(
                "SELECT id, name, path, container_id, "
                "is_archive, is_disk, extension "
                "FROM files WHERE id = ?",
                (current_id,)).fetchone()
            if row is None:
                break
            chain.append({
                "id": row["id"],
                "name": row["name"],
                "path": row["path"],
                "container_id": row["container_id"],
                "is_archive": bool(row["is_archive"]),
                "is_disk": bool(row["is_disk"]),
                "extension": row["extension"] or "",
            })
            current_id = row["container_id"]
    # We walked inside-out, the caller wants outside-in
    chain.reverse()
    return chain


def mark_file_done(file_id: int) -> None:
    """Mark a file as fully processed. Call this only AFTER all
    archive members / disk entries have been inserted and the
    enclosing transaction has committed; if you call it earlier
    a mid-archive crash will leave the file marked 'done' with
    only partial children indexed."""
    with connection() as conn:
        conn.execute(
            "UPDATE files SET scan_status = 'done' WHERE id = ?",
            (file_id,))
        conn.commit()


def mark_file_failed(file_id: int, reason: str = "") -> None:
    """Mark a file as failed. Used when the scanner gives up on
    a file (corrupt archive, unreadable disk image). Failed
    files don't get retried on the next incremental scan - the
    user needs to fix the underlying problem and run an
    explicit re-scan to clear the state."""
    with connection() as conn:
        conn.execute(
            "UPDATE files SET scan_status = 'failed' WHERE id = ?",
            (file_id,))
        conn.commit()


def list_pending_files() -> list[dict]:
    """Find all files left in 'pending' state - the ones that
    were being processed when Quopus crashed or was killed.

    Called once at startup by quopus.py to re-enqueue them
    through the ingest queue so the interrupted work finishes."""
    with connection() as conn:
        cur = conn.execute("""
            SELECT id, path, mtime, scan_id
            FROM files
            WHERE scan_status = 'pending'
            ORDER BY id
        """)
        return [dict(row) for row in cur.fetchall()]


def clear_pending_status(file_id: int) -> None:
    """Reset a file's row to no-row state - used by recovery
    when we want to fully re-do a pending file. Removes
    associated disk_images and disk_entries via the foreign
    key cascade. The file will be re-ingested from scratch."""
    with connection() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()


# ============================================================
# Scan issues - things the user might want to know about
# ============================================================


# Recognized issue types. Used as enum-like values in the
# issue_type column. The UI maps each to a friendly icon/color.
ISSUE_PASSWORD = "password"           # archive needs a key
ISSUE_CORRUPT_DISK = "corrupt_disk"   # BAM/dir walk failed
ISSUE_NO_DIRECTORY = "no_directory"   # trackloader / no CBM-DOS
ISSUE_EXTRACT_FAILED = "extract_failed"  # archive lib gave up
ISSUE_UNKNOWN_FORMAT = "unknown_format"  # extension matched but
                                          # bytes don't


def log_scan_issue(scan_id: Optional[int], path: str,
                   issue_type: str, detail: str = "") -> None:
    """Record an issue for later display in the issues tab.
    Designed to be cheap - scanners can call this from inside
    tight loops without slowing the index down. Returns silently
    on DB errors (the scan should continue even if the issue
    log itself has problems).

    scan_id can be None for ad-hoc events (watcher-triggered
    ingests that aren't part of a named scan).

    No-op when the DB is read-only (viewing a shared catalog).
    The scanner shouldn't run on a read-only DB at all but if
    something slips through we don't want to crash."""
    if _db_readonly:
        return
    try:
        with connection() as conn:
            conn.execute("""
                INSERT INTO scan_issues(
                    scan_id, path, issue_type, detail, occurred_at)
                VALUES(?, ?, ?, ?, ?)
            """, (scan_id, path, issue_type, detail,
                  __import__("time").time()))
            conn.commit()
    except Exception:
        pass


def list_scan_issues(scan_id: Optional[int] = None,
                     issue_type: Optional[str] = None,
                     limit: int = 1000) -> list[dict]:
    """List recorded issues, optionally filtered by scan or type."""
    sql = ("SELECT id, scan_id, path, issue_type, detail, "
           "occurred_at FROM scan_issues WHERE 1=1 ")
    args: list = []
    if scan_id is not None:
        sql += "AND scan_id = ? "
        args.append(scan_id)
    if issue_type is not None:
        sql += "AND issue_type = ? "
        args.append(issue_type)
    sql += "ORDER BY occurred_at DESC LIMIT ?"
    args.append(limit)
    with connection() as conn:
        cur = conn.execute(sql, tuple(args))
        return [dict(row) for row in cur.fetchall()]


def clear_scan_issues(scan_id: Optional[int] = None) -> int:
    """Remove issues. With no scan_id: removes everything.
    Returns number of rows deleted."""
    with connection() as conn:
        if scan_id is None:
            cur = conn.execute("DELETE FROM scan_issues")
        else:
            cur = conn.execute(
                "DELETE FROM scan_issues WHERE scan_id = ?",
                (scan_id,))
        conn.commit()
        return cur.rowcount


def count_issues_by_type() -> dict:
    """Counts grouped by issue_type, for the stats panel."""
    with connection() as conn:
        cur = conn.execute("""
            SELECT issue_type, COUNT(*) as n
            FROM scan_issues
            GROUP BY issue_type
        """)
        return {row["issue_type"]: row["n"] for row in cur.fetchall()}
