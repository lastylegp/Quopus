#!/usr/bin/env python3
# date_time: 2026-05-27 16:20
"""
Quopus Commander - PC file manager inspired by Directory Opus 4

The original Directory Opus 4 was an Amiga workbench replacement / file
manager by Jonathan Potter / GPSoftware (1990s). Quopus Commander is a
modern PyQt6 reimagining of that workflow on Windows / Linux / macOS,
with retro-computing tooling bolted on (CBM / Amiga / U64 support).

Usage:
    pip install PyQt6 psutil
    python quopus.py

Drop C64 Pro Mono TTF into ./fonts/ for pixel-perfect PETSCII rendering.
"""
import os
import sys
import traceback


def _crash_log_path():
    """Where to dump a startup crash trace.

    In a PyInstaller bundle the .exe is the natural anchor - same
    directory the user double-clicked from, so they can find the
    log without hunting. In source mode we use the script's
    directory. Never raises - falls back to the temp dir if even
    those are unwriteable."""
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "quopus_crash.log")
    except Exception:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "quopus_crash.log")


def _install_crash_handler():
    """Catch every uncaught exception in main thread and dump it
    to a file the user can actually find. Without this, a frozen
    Windows build with console=False just silently disappears -
    nothing on screen, nothing in Event Viewer, no trace.

    The handler logs to quopus_crash.log next to the .exe and
    additionally tries to pop a Windows MessageBox so the user
    sees there *was* a crash, not just empty silence."""

    def excepthook(exc_type, exc, tb):
        msg = "".join(
            traceback.format_exception(exc_type, exc, tb))
        log = _crash_log_path()
        try:
            with open(log, "w", encoding="utf-8") as f:
                f.write("Quopus Commander startup crash\n")
                f.write("=" * 50 + "\n")
                f.write(f"sys.executable: {sys.executable}\n")
                f.write(f"sys.argv: {sys.argv}\n")
                f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
                f.write(f"_MEIPASS: "
                          f"{getattr(sys, '_MEIPASS', 'n/a')}\n")
                f.write(f"cwd: {os.getcwd()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write("=" * 50 + "\n\n")
                f.write(msg)
        except OSError:
            pass
        # Windows: pop a MessageBox so the user knows. ctypes.windll
        # only exists on Windows - elsewhere we fall back to print.
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Quopus Commander couldn't start.\n\n"
                    f"A crash log was written to:\n{log}\n\n"
                    f"Error:\n{exc_type.__name__}: {exc}",
                    "Quopus Commander - startup error",
                    0x10)  # MB_ICONERROR
            else:
                sys.stderr.write(msg)
        except Exception:
            sys.stderr.write(msg)

    sys.excepthook = excepthook


_install_crash_handler()

try:
    from PyQt6.QtWidgets import QApplication, QStyleFactory, QSplashScreen
    from PyQt6.QtGui import QPalette, QColor, QPixmap, QPainter, QFont
    from PyQt6.QtCore import Qt
except ImportError:
    print("ERROR: PyQt6 not installed. Run: pip install PyQt6")
    sys.exit(1)

try:
    import psutil  # noqa
except ImportError:
    print("ERROR: psutil not installed. Run: pip install psutil")
    sys.exit(1)

from quopus_lib.palette import C, load_bundled_fonts
from quopus_lib.config import FONTS_DIR
from quopus_lib.main_window import QuopusMain


# Version banner shown on the splash screen. Bumping the version
# number is a one-line change here - everywhere else reads it from
# this constant.
QUOPUS_VERSION = "v1.0"
QUOPUS_TAGLINE = "by lA-sTYLe/Quantum"


def _build_splash_pixmap(icon_path, width=420, height=520):
    """Compose the splash pixmap: a grey rounded backdrop, logo
    centered on it, title text underneath.

    The backdrop is essential when the desktop wallpaper is busy
    (dark photo, fiery red gradient, video preview) - against a
    plain transparent pixmap the dark-blue title text disappears.
    A neutral grey panel gives every part of the splash a known
    contrast, regardless of what's behind the window.

    Why build it ourselves instead of letting QSplashScreen draw
    showMessage() text on top of a static PNG: we want the layout
    to be deterministic (icon centered, fixed margins, version
    rendered with the bundled C64 Pro Mono font where available)
    rather than depending on the underlying QSplashScreen text
    placement logic which varies by platform.

    Returns a QPixmap or None if the icon couldn't be loaded.
    """
    if not icon_path or not icon_path.exists():
        return None
    src = QPixmap(str(icon_path))
    if src.isNull():
        return None

    # Scale the icon to a reasonable size. The Q-diamond looks best
    # at around 300px square - bigger and the antialiasing artifacts
    # become visible, smaller and the detail in the rays vanishes.
    target_icon_size = 320
    icon = src.scaled(
        target_icon_size, target_icon_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation)

    # Canvas starts transparent so the rounded corners of the
    # backdrop blend smoothly with whatever's behind the window
    # (we keep WA_TranslucentBackground on the QSplashScreen).
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # Grey rounded panel as the splash "card". Inset a few pixels
    # from the pixmap edges so the rounded corners + 1px border
    # render cleanly without clipping. Light enough to read the
    # dark blue title text against, dark enough to make the white
    # parts of the logo pop.
    from PyQt6.QtCore import QRectF
    backdrop = QRectF(8, 8, width - 16, height - 16)
    # Soft shadow underneath - a slightly larger rect at low alpha
    # gives the splash a sense of "floating" against the desktop
    # instead of looking like it was pasted in flat.
    shadow_color = QColor(0, 0, 0, 60)
    shadow_rect = backdrop.adjusted(4, 6, 4, 6)
    p.setBrush(shadow_color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(shadow_rect, 24, 24)
    # The panel itself: neutral medium-light grey with a slightly
    # darker edge so it has a clear boundary on either a black or
    # white desktop.
    p.setBrush(QColor("#dcdcdc"))
    p.setPen(QColor("#a0a0a0"))
    p.drawRoundedRect(backdrop, 24, 24)

    # Center the icon horizontally, leave some breathing room at
    # the top so the title text below the icon has its own space.
    icon_x = (width - icon.width()) // 2
    icon_y = 30
    p.drawPixmap(icon_x, icon_y, icon)

    # Title and version text below the icon
    text_y = icon_y + icon.height() + 28

    # Title: "Quopus Commander". Big, bold, centered.
    title_font = QFont("Arial", 26, QFont.Weight.Bold)
    p.setFont(title_font)
    p.setPen(QColor("#1a3a8a"))   # blue from the icon palette
    title_rect = pix.rect()
    title_rect.setTop(text_y)
    title_rect.setHeight(48)
    p.drawText(title_rect,
               Qt.AlignmentFlag.AlignHCenter
               | Qt.AlignmentFlag.AlignTop,
               "Quopus Commander")

    # Version line: "v1.0  by lA-sTYLe/Quantum". Smaller, dimmer.
    # For registered users we replace the tagline with the
    # license holder's name as a personal touch.
    try:
        from quopus_lib import license as _lic
        if _lic.is_registered():
            info = _lic.load_license()
            subtitle = f"{QUOPUS_VERSION}   Registered to {info.name}"
        else:
            subtitle = (f"{QUOPUS_VERSION}   {QUOPUS_TAGLINE}   "
                          f"(TRIAL)")
    except Exception:
        subtitle = f"{QUOPUS_VERSION}   {QUOPUS_TAGLINE}"
    version_font = QFont("Arial", 13)
    p.setFont(version_font)
    p.setPen(QColor("#666666"))
    version_rect = pix.rect()
    version_rect.setTop(text_y + 50)
    version_rect.setHeight(28)
    p.drawText(version_rect,
               Qt.AlignmentFlag.AlignHCenter
               | Qt.AlignmentFlag.AlignTop,
               subtitle)

    # Loading hint at the bottom - the user gets QSplashScreen's
    # showMessage() drawn here later, so we just leave whitespace
    # for it. The native paint method handles its own area below
    # this point.
    p.end()
    return pix


def _show_splash(app):
    """Create and show the splash screen. Returns the splash widget
    so the caller can finish() it when the main window is ready.

    Returns None if the icon couldn't be loaded - we silently fall
    back to no splash rather than crashing the startup."""
    try:
        from quopus_lib.config import BUNDLE_DIR
        icon_path = (BUNDLE_DIR / "quopus_lib"
                     / "icons" / "quopus.png")
        pix = _build_splash_pixmap(icon_path)
        if pix is None:
            return None
        splash = QSplashScreen(
            pix,
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint)
        splash.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Stamp the show time on the splash widget itself so the
        # caller can enforce a minimum visible duration regardless
        # of how fast the rest of startup runs.
        import time
        splash._shown_at = time.monotonic()
        splash.show()
        # Pump the event loop so the window actually paints before
        # we head into the slow font/config init. Without this the
        # splash appears as a blank grey rectangle until main() is
        # nearly done.
        app.processEvents()
        return splash
    except Exception:
        return None


# How long the splash stays visible after startup completes.
# Even if the rest of init takes <100ms (fast machine, warm cache),
# the user gets at least this long to read the logo.
SPLASH_MIN_DISPLAY_SECONDS = 4.0


def _finish_splash(app, splash, main_window):
    """Close the splash after enforcing the minimum display time.

    If startup beat the minimum (typical on a fast box) we sleep
    out the remainder while still keeping the UI responsive via
    processEvents() in 50ms ticks. If startup already ran longer
    than the minimum (slow box, cold cache, antivirus scan) we
    close immediately."""
    if splash is None:
        return
    import time
    started = getattr(splash, "_shown_at", time.monotonic())
    remaining = SPLASH_MIN_DISPLAY_SECONDS - (
        time.monotonic() - started)
    # Polite spin - keep the splash painted, repaint message, let
    # the event loop run for window-server keep-alive pings.
    while remaining > 0:
        app.processEvents()
        # Sleep no longer than 50ms at a time so the wait stays
        # interruptible by user clicks/keys.
        time.sleep(min(0.05, remaining))
        remaining = SPLASH_MIN_DISPLAY_SECONDS - (
            time.monotonic() - started)
    splash.finish(main_window)


def _run_migration_with_progress(parent_window, database, current_v):
    """Run a heavy DB migration with a modal progress dialog.

    Called only when needs_migration() reports True. Does three
    things in sequence:

      1. Show a "preparing backup..." dialog while we make a
         full SQLite-backup-API copy of the existing DB. The
         user can find this at <db>.pre_v<N>_backup_<ts> if
         anything goes wrong with the migration.

      2. Show a progress bar dialog while the migration runs
         on a background QThread. The bar reflects the re-index
         pass (the slow part - O(N) over files + disk_entries).

      3. Dismiss the dialog when the migration finishes.

    The Qt event loop keeps spinning during all of this so the
    progress dialog can repaint and the user can see what's
    happening. The migration thread posts progress updates via
    a queued signal so updates land on the main thread cleanly.
    """
    from PyQt6.QtCore import QThread, pyqtSignal, Qt
    from PyQt6.QtWidgets import (
        QProgressDialog, QApplication, QMessageBox)

    # Step 1: Backup. Show a small wait message - on a 12 GB DB
    # the SQLite backup API takes ~10-30 seconds depending on
    # disk speed.
    wait = QProgressDialog(
        "Backing up database before schema upgrade...\n"
        "(this is automatic - the backup file lets you roll "
        "back if anything goes wrong)",
        None,  # No cancel button - backup must complete
        0, 0,  # 0/0 = indeterminate spinner
        parent_window)
    wait.setWindowTitle(
        f"Quopus database upgrade v{current_v} → "
        f"v{database.SCHEMA_VERSION}")
    wait.setWindowModality(Qt.WindowModality.ApplicationModal)
    wait.setMinimumDuration(0)
    wait.show()
    QApplication.processEvents()

    backup_path = None
    try:
        backup_path = database.backup_db_before_migration()
    except Exception as e:
        print(f"  [migration] backup failed: {e}")
    wait.close()

    # Step 2: Run migration in a background thread. We use a
    # QThread + signals so progress updates land on the main
    # thread (QProgressDialog isn't thread-safe).
    class _MigWorker(QThread):
        progress = pyqtSignal(str, int, int)  # stage, cur, total
        done = pyqtSignal(bool, str)           # ok, error_msg

        def run(self):
            try:
                def cb(stage, cur, total):
                    self.progress.emit(stage, cur, total)
                database.init_db(progress_cb=cb)
                self.done.emit(True, "")
            except Exception as e:
                self.done.emit(False, str(e))

    worker = _MigWorker()
    prog = QProgressDialog(
        "Upgrading database schema...\n"
        "This is a one-time operation on this version of Quopus.\n"
        "Larger databases (millions of files) can take several "
        "minutes.",
        None,  # No cancel - half-migration would corrupt the DB
        0, 100,
        parent_window)
    prog.setWindowTitle(
        f"Quopus database upgrade v{current_v} → "
        f"v{database.SCHEMA_VERSION}")
    prog.setWindowModality(Qt.WindowModality.ApplicationModal)
    prog.setMinimumDuration(0)
    prog.setAutoClose(False)
    prog.setAutoReset(False)

    state = {"ok": True, "err": "", "finished": False}

    def on_progress(stage, cur, total):
        # Map stage labels to user-friendly text
        stage_text = {
            "reindex": "Preparing to re-index search...",
            "reindex_files":
                f"Re-indexing files: {cur:,} / {total:,}",
            "reindex_entries":
                f"Re-indexing disk entries: "
                f"{cur:,} / {total:,}",
            "reindex_done":
                f"Re-index complete: {total:,} rows",
        }.get(stage, f"{stage}: {cur:,} / {total:,}")
        prog.setLabelText(
            f"Upgrading database schema...\n\n{stage_text}")
        if total > 0:
            prog.setRange(0, total)
            prog.setValue(min(cur, total))
        else:
            prog.setRange(0, 0)

    def on_done(ok, err):
        state["ok"] = ok
        state["err"] = err
        state["finished"] = True

    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    worker.start()
    prog.show()

    # Spin the event loop until the worker signals done. We
    # could use worker.wait() instead but that blocks the GUI
    # thread - this way progress updates render.
    while not state["finished"]:
        QApplication.processEvents()
        QThread.msleep(50)
    worker.wait()
    prog.close()

    # Step 3: Report outcome.
    if state["ok"]:
        msg = (f"Database upgraded to schema v{database.SCHEMA_VERSION}.")
        if backup_path:
            msg += (f"\n\nBackup of the old version was saved to:\n"
                    f"{backup_path}\n\n"
                    "You can delete this file once you're sure "
                    "everything works.")
        QMessageBox.information(
            parent_window, "Database upgrade complete", msg)
    else:
        roll_msg = ""
        if backup_path:
            roll_msg = (f"\n\nA backup of the original database "
                        f"is at:\n{backup_path}\n\n"
                        "You can rename it back to "
                        f"{database.DB_PATH.name} if you want "
                        "to revert.")
        QMessageBox.critical(
            parent_window, "Database upgrade failed",
            f"The migration encountered an error:\n\n"
            f"{state['err']}{roll_msg}")


def _raise_fd_limit():
    """Raise the soft file-descriptor limit to the hard limit
    available to this process.

    Why: Quopus opens a lot of fds during bulk archive scans -
    one per archive being inspected, plus inotify watches per
    watched directory (recursive watcher), plus sqlite cache
    files (-wal/-shm sidecars), plus the rolling psutil
    /proc/meminfo reads from the status bar. On a big retro
    catalog (Mario's setup hits ~50K archives, watcher running
    on /mnt/nas), the default Linux soft limit of 1024 is
    exhausted within minutes, and you get a cascade of
    OSError: [Errno 24] failures: "unable to open database
    file", "Zu viele offene Dateien: /proc/meminfo", and 7z /
    archive readers silently failing to open inputs.

    The hard limit is typically 4096 or 524288 depending on
    distro; raising the soft cap to it doesn't allocate
    anything, it just unlocks the headroom. Best-effort - on
    Windows the resource module doesn't exist; we just skip.
    """
    try:
        import resource
    except ImportError:
        return  # Windows: SetHandleCount equivalent not needed
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Aim high but cap at 65536 - going beyond on a default
        # Linux causes audit-log noise and brings diminishing
        # returns. If the hard limit is lower, use that.
        target = min(hard, 65536) if hard != resource.RLIM_INFINITY \
            else 65536
        if soft < target:
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (target, hard))
            print(f"[startup] fd soft limit raised "
                  f"{soft} -> {target} (hard={hard})")
    except (ValueError, OSError) as e:
        print(f"[startup] could not raise fd limit: {e}")


def main():
    _raise_fd_limit()
    # Load user-installed custom modules BEFORE constructing the
    # main window. Plugins might want to register themselves under
    # action_names that the action-button grid references right
    # after construction (e.g. a user-defined "my_dev_shortcut"
    # bound to F2). Discovery is best-effort: any module that
    # fails to import is logged and skipped, never blocks startup.
    try:
        from quopus_lib import custom_modules
        custom_modules.load_all()
        n = len(custom_modules.all_modules())
        if n:
            print(f"[startup] loaded {n} custom module(s)")
        errs = custom_modules.load_errors()
        if errs:
            print(f"[startup] {len(errs)} custom module(s) failed "
                  f"to load - see Config -> Reload custom modules "
                  f"for details")
    except Exception as e:
        print(f"[startup] custom_modules subsystem unavailable: {e}")
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))

    # Apply persisted global font (family + pointsize) BEFORE the
    # main window is constructed so its widgets pick up the right
    # metrics on first paint. If the user later changes the font
    # via Settings, the same helper gets called again to live-
    # update the running app.
    try:
        from quopus_lib.config import load_config, apply_app_font
        _early_cfg = load_config()
        apply_app_font(_early_cfg, app)
    except Exception as e:
        print(f"[startup] could not apply app font: {e}")

    # App-Identity: Name, organization, plus Window-Icon. Das Icon
    # zeigt sich in der Taskleiste, dem Alt-Tab-Switcher und im
    # Window-Decoration-Frame.
    app.setApplicationName("Quopus")
    app.setApplicationDisplayName("Quopus Commander")
    app.setOrganizationName("lA-sTYLe")
    # setDesktopFileName matched die XDG-.desktop-Datei (Linux) damit
    # WM und Taskbar das richtige Icon ziehen statt eines generischen
    # Python-Icons. Auf Windows/macOS ist das ein No-op.
    app.setDesktopFileName("quopus")
    try:
        from PyQt6.QtGui import QIcon
        from quopus_lib.config import BUNDLE_DIR
        # In source-mode BUNDLE_DIR is the project root; in a
        # PyInstaller bundle it's _MEIPASS (onefile) or the exe
        # dir (onedir) - both have the icon shipped under the
        # same relative path.
        icon_path = BUNDLE_DIR / "quopus_lib" / "icons" / "quopus.png"
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
    except Exception:
        pass

    # Splash screen BEFORE the slow stuff (font scanning, config
    # parse, button grid build). The user sees the logo within a
    # few hundred ms of double-clicking the exe instead of staring
    # at nothing for 2-5 seconds.
    splash = _show_splash(app)
    if splash:
        splash.showMessage(
            "Loading fonts...",
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignBottom,
            QColor("#444444"))
        app.processEvents()

    loaded = load_bundled_fonts(FONTS_DIR)
    if loaded:
        print(f"Loaded fonts from {FONTS_DIR}:")
        for name in loaded:
            print(f"  - {name}")
    else:
        print(f"No fonts in {FONTS_DIR}.")
        print("  Drop C64_Pro_Mono.ttf + Topaz.ttf there for proper rendering.")

    if splash:
        splash.showMessage(
            "Loading palette...",
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignBottom,
            QColor("#444444"))
        app.processEvents()

    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(C.WB_GREY))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(C.BLACK))
    pal.setColor(QPalette.ColorRole.Base, QColor(C.LISTER_BG))
    pal.setColor(QPalette.ColorRole.Text, QColor(C.LISTER_FG))
    pal.setColor(QPalette.ColorRole.Button, QColor(C.BTN_BLUE))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(C.WHITE))
    app.setPalette(pal)

    if splash:
        splash.showMessage(
            "Building UI...",
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignBottom,
            QColor("#444444"))
        app.processEvents()

    w = QuopusMain()
    # Apply trial watermark to the main window title. Registered
    # users see their name; trial users see [TRIAL].
    try:
        from quopus_lib.license_ui import apply_watermark
        apply_watermark(w)
    except Exception:
        pass
    # Build the main window but DON'T show it yet. We want the
    # splash to be the only thing visible for its full display
    # duration; popping the main window early would cover or
    # flicker over the splash on multi-monitor setups and any WM
    # that respects WindowStaysOnTopHint inconsistently.

    # Splash close, enforcing minimum display time. The helper
    # paints the splash and pumps events until the threshold is
    # reached, then calls finish() which fades the splash out.
    # The main window is shown AFTERWARDS - so there's a single
    # clean visual transition: splash visible -> splash gone ->
    # main window appears, never both on screen simultaneously.
    _finish_splash(app, splash, w)

    # Show the nag dialog AFTER the splash but BEFORE the main
    # window appears. The dialog is modal and parented to None
    # so it gets its own taskbar entry while it's up. Trial
    # users see this each launch; registered users skip it.
    try:
        from quopus_lib.license_ui import show_nag_if_needed
        show_nag_if_needed(None)
    except Exception:
        # Don't let license UI bugs prevent Quopus from starting -
        # always show the main window even if the nag screen
        # crashes.
        pass

    # Now bring up the main window.
    w.show()

    # Database init - this needs special care on first launch
    # after a schema upgrade. A v3 -> v4 migration on a 12 GB
    # catalog can take 5+ minutes because we have to rebuild
    # the FTS5 trigram indexes from scratch. We can't just
    # do that synchronously - the user would think Quopus
    # had hung.
    #
    # Strategy:
    #   1. Check if migration needed - cheap, one SQL query
    #   2. If yes: backup the DB, then run migration in a
    #      background thread with a modal progress dialog
    #      showing live "X / Y rows re-indexed" status
    #   3. If no (fresh DB or already current): init_db()
    #      directly, fast
    #
    # The watcher must NOT start before init_db() completes,
    # otherwise it would try to write to a half-migrated DB.
    try:
        from quopus_lib import database
        needs_mig, current_v = database.needs_migration()
        if needs_mig:
            _run_migration_with_progress(w, database, current_v)
        else:
            database.init_db()
    except Exception as e:
        print(f"  [database init] failed: {e}")

    # Start FS-watcher only AFTER db init/migration finishes.
    # Restoring previously-watched folders is best-effort;
    # if the watcher backend is missing or a path is gone we
    # log and continue.
    try:
        from quopus_lib import db_watcher
        if db_watcher.list_watched_folders():
            db_watcher.start_watcher()
    except Exception:
        pass

    # Crash-recovery pass: find files left in 'pending' state
    # by a previous Quopus session that didn't finish indexing
    # them (crash, kill, power loss). We delete the partial row
    # so the file is treated as never-indexed, then re-enqueue
    # the path. The ingest queue's workers will redo it from
    # scratch.
    #
    # Runs in a background thread so a DB with millions of rows
    # doesn't block app startup even if the SELECT is slow.
    try:
        from quopus_lib import database, ingest_queue
        import threading
        def _do_crash_recovery():
            try:
                from pathlib import Path as _Path
                pending = database.list_pending_files()
                if not pending:
                    return
                print(f"  [crash recovery] found {len(pending)} "
                      f"file(s) left pending from a previous "
                      f"session, re-queueing...")
                q = ingest_queue.get_queue()
                requeued = 0
                for row in pending:
                    path = _Path(row["path"])
                    # Skip virtual archive-member paths (they
                    # contain '!' from foo.zip!member.prg) - those
                    # aren't real files on disk. They'll get redone
                    # when their container is re-ingested.
                    if "!" in str(path) or not path.is_file():
                        database.clear_pending_status(row["id"])
                        continue
                    database.clear_pending_status(row["id"])
                    q.enqueue(ingest_queue.IngestJob(path=path))
                    requeued += 1
                if requeued:
                    print(f"  [crash recovery] {requeued} file(s) "
                          f"re-queued for indexing")
            except Exception as e:
                print(f"  [crash recovery] skipped: {e}")
        threading.Thread(
            target=_do_crash_recovery,
            name="quopus-crash-recovery",
            daemon=True).start()
    except Exception as e:
        print(f"  [crash recovery] skipped: {e}")

    # Register a clean shutdown hook for the ingest queue so any
    # in-flight worker writes get to finish before the SQLite
    # WAL is checkpointed at process exit. The workers are daemon
    # threads, so without this they'd just die mid-transaction
    # (which IS safe with WAL, just leaves stuff to recover next
    # launch - this is the polite version).
    import atexit
    def _shutdown_quopus():
        try:
            from quopus_lib import db_watcher
            if db_watcher.is_running():
                db_watcher.stop_watcher()
        except Exception:
            pass
        try:
            from quopus_lib import ingest_queue
            ingest_queue.shutdown()
        except Exception:
            pass
    atexit.register(_shutdown_quopus)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
