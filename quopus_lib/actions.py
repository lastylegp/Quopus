# date_time: 2026-06-02 13:17
"""Action dispatcher - uses selected_or_tagged for all operations."""
import os
import platform
import shlex
import shutil
import stat as stmod
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPlainTextEdit, QInputDialog, QMessageBox
)
from PyQt6.QtGui import QPixmap

from .palette import C, SCROLLBAR_QSS, fmt_size
from .dialogs import DirReverseDialog, BuffersDialog
from .config import save_config
from .config import scaled_font_px


class TransferCancelled(Exception):
    """Raised inside chunked copy/tree walker when the user hits Cancel."""
    pass


class ActionDispatcher:
    def __init__(self, main_window):
        self.w = main_window
        # Per-button options passed via dispatch(); reset every call.
        # Used by act_external_script and act_execute_command to
        # decide whether to capture output and/or refresh panels.
        self._current_opts = {}

    def _active(self):
        return self.w._active_lister()

    def _status(self, msg):
        self.w._status(msg)

    def _show_text(self, title, text):
        dlg = QDialog(self.w); dlg.setWindowTitle(title); dlg.resize(800, 600)
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QVBoxLayout(dlg)
        te = QPlainTextEdit(); te.setReadOnly(True); te.setPlainText(text)
        te.setStyleSheet(f"""
            background-color: {C.BLACK}; color: {C.WHITE};
            font-family: "Topaz-8","Topaz","Courier New",monospace;
            font-size: {scaled_font_px(12)}px; border: 1px solid {C.BLACK};
        """ + SCROLLBAR_QSS)
        lay.addWidget(te); dlg.exec()

    def dispatch(self, action, param=None, opts=None):
        """Dispatch a button action. `opts` is an optional dict with
        per-button settings (currently 'show_output' and
        'refresh_after') that some actions inspect. Stored on
        self._current_opts so action methods that DON'T take an
        explicit opts parameter can still find them via getattr.

        Lookup order:
          1. Built-in act_<name> method on this class
          2. User custom module from custom_modules/ directory
          3. "Unknown" status message

        Param extension filter: if the param contains a token of
        the form `{file|ext1,ext2,...}`, we first check that
        every selected file has one of the allowed extensions
        before running the action. If any file doesn't match,
        we show a warning and skip the action entirely. After
        the check the token is rewritten to `{file}` so action
        handlers that don't know about the filter syntax see
        the canonical form.

        Examples:
          {file|crt,prg}   -> only run if all selected files
                              end with .crt or .prg
          {file|nfo}       -> only run on .nfo files
          {file}           -> no filter (legacy behavior)
        """
        src, dst = self._active()
        # ----- Extension filter check ---------------------------
        # Parse `{file|ext1,ext2}` tokens out of param. We only
        # need the first match for the gate - if there are
        # multiple {file|...} tokens with conflicting filters,
        # the FIRST one wins (consistent failure mode).
        param, extfilter_ok = self._check_param_ext_filter(
            action, param, src)
        if not extfilter_ok:
            # User got a warning popup; abort the dispatch.
            return
        # --------------------------------------------------------
        # Stash on self so action methods can pull these out without
        # us having to change every act_*'s signature. Cleared after
        # the dispatch finishes so a later button click that doesn't
        # set opts doesn't accidentally inherit the previous state.
        self._current_opts = opts or {}
        try:
            fn = getattr(self, f"act_{action}", None)
            if fn is not None:
                fn(src, dst, param)
                return
            # No built-in handler - try the custom-module registry.
            # Plugins can either expose a fresh action name or
            # shadow a non-existent built-in; either way works.
            try:
                from . import custom_modules
                if custom_modules.dispatch(action, self, param or ""):
                    return
            except Exception as e:
                # Plugin-loader failure must not break the
                # built-in dispatch path - if custom_modules
                # itself is broken, we still want unknown built-ins
                # to fall through to the normal "Unknown" status.
                print(f"  [actions] custom_modules.dispatch raised: {e}")
            self._status(f"Unknown: {action}")
        except Exception as e:
            QMessageBox.critical(self.w, "Quopus", f"Action failed: {e}")
        finally:
            self._current_opts = {}

    def _check_param_ext_filter(self, action, param, src):
        """Parse `{file|ext1,ext2,...}` tokens out of param.

        Returns (rewritten_param, ok_to_proceed).

        Behavior:
          - param is None or no `{file|...}` token present:
            returns (param, True) unchanged
          - selection is empty: returns (param, True) - the
            action itself will deal with empty selection (some
            actions handle the no-selection case via a file
            picker dialog, others are no-ops, we don't second-
            guess them here)
          - any selected file doesn't match: shows a warning
            message with the offending filename + allowed
            extensions, returns (param, False)
          - all selected files match: returns the param with
            `{file|ext1,ext2}` rewritten to `%f` so down-stream
            handlers (act_run, act_shell, act_external_script,
            act_execute_command) substitute it correctly through
            _substitute_tokens. We rewrite to `%f` not `{file}`
            because the existing token system only knows
            `%`-style tokens.

        The token is case-INSENSITIVE in the keyword: both
        `{file|...}` and `{FILE|...}` are recognized. Extensions
        inside are case-insensitive too and the leading dot is
        optional, so all of these match a .crt file:
            {file|crt}
            {FILE|.crt}
            {File|CRT}
            {file|crt,prg}
        """
        if not param or not isinstance(param, str):
            return param, True
        # Quick reject if no extended-token syntax is present
        # (case-insensitive). The lower-case `in` check is the
        # cheap path; we only run the regex when needed.
        if "{file|" not in param.lower():
            return param, True
        import re
        # Match {file|<csv of extensions>}. Allowed chars in an
        # extension: dot, letters, digits. Anything weirder will
        # fail to match and the original {file|...} stays in
        # the param literally - which is a hint to the user that
        # they have a typo.
        # The (?i) flag makes the whole pattern case-insensitive
        # so {FILE|...} works the same as {file|...}.
        pattern = re.compile(
            r"(?i)\{file\|([A-Za-z0-9.,]+)\}")
        match = pattern.search(param)
        if not match:
            return param, True
        # Parse the comma list into a normalized set of
        # extensions, all lowercase, all with leading dot.
        raw = match.group(1)
        allowed = set()
        for piece in raw.split(","):
            piece = piece.strip().lower()
            if not piece:
                continue
            if not piece.startswith("."):
                piece = "." + piece
            allowed.add(piece)
        if not allowed:
            # Empty filter `{file|}` - treat as no filter to
            # avoid bricking actions on user typos. We rewrite
            # to %f so the existing token system substitutes
            # the first selected file.
            new_param = pattern.sub("%f", param)
            return new_param, True

        # Gather current selection. We use selected_or_tagged()
        # because that's what most action handlers consume - we
        # want our gate check to be consistent with what the
        # action would actually iterate over.
        try:
            sel = list(src.selected_or_tagged() or [])
        except Exception:
            sel = []
        if not sel:
            # No files selected - let the action handle it
            # (file picker, no-op, etc.). Just normalize the
            # token and pass through.
            new_param = pattern.sub("%f", param)
            return new_param, True

        # Check every selected file. We use a generator so the
        # check stops at the first failure.
        from pathlib import Path
        bad = []
        for p in sel:
            name = Path(str(p)).name
            ext = Path(name).suffix.lower()
            if ext not in allowed:
                bad.append(name)

        if bad:
            # Build a helpful message that names the offender(s)
            # AND the allowed list. We cap the displayed list of
            # bad files at 5 - more than that and the message
            # becomes unwieldy.
            allowed_display = ", ".join(sorted(allowed))
            if len(bad) <= 5:
                bad_display = "\n".join(f"  - {b}" for b in bad)
            else:
                bad_display = (
                    "\n".join(f"  - {b}" for b in bad[:5])
                    + f"\n  ... and {len(bad) - 5} more")
            QMessageBox.warning(
                self.w, "File not allowed",
                f"Action '{action}' is restricted to files with "
                f"extension:\n  {allowed_display}\n\n"
                f"The following selected file(s) don't match:\n"
                f"{bad_display}\n\n"
                f"Either change the selection, or remove "
                f"'|{raw}' from the Param to lift the restriction.")
            return param, False

        # All good - rewrite the filter token to plain %f so the
        # existing token substituter (which only knows %-style
        # tokens) replaces it with the actual filename. If the
        # user already had a separate %f in the param, that's
        # fine - the rewrite just creates a second %f, both
        # expand to the same first-selected-file path.
        new_param = pattern.sub("%f", param)
        return new_param, True

    def _spawn_in_terminal(self, args, cwd=None, hold=True):
        """Launch a command in a real interactive terminal window.

        Used for buttons with the "In Terminal" option set, which is
        the right choice for INTERACTIVE programs (telnet, ssh, vim,
        REPLs) - they need a real TTY for keyboard input. The Quopus
        capture-output dialog can show stdout but can't pipe stdin
        back, so commands like `telnet` would just close immediately.

        `args` may be a string (treated as a shell command line) or
        a list of program+arguments. `hold=True` makes the terminal
        stay open after the program exits so the user can read any
        final messages (telnet's "Connection closed by foreign host"
        etc.) before the window disappears.
        """
        # Normalise args into a single command string for terminal
        # emulators that take "-e cmd ..." syntax. Quoting matters
        # less here because we hand the string to the shell inside
        # the terminal anyway.
        if isinstance(args, list):
            cmd_str = ' '.join(shlex.quote(a) for a in args)
        else:
            cmd_str = args

        sysname = platform.system()
        if sysname == "Windows":
            # cmd /K runs the command then keeps the window open.
            # We deliberately bypass _spawn_detached() because we
            # WANT a visible console here.
            full = f'start "" cmd /K "{cmd_str}"'
            subprocess.Popen(full, shell=True, cwd=cwd)
            return

        if sysname == "Darwin":
            # Open Terminal.app with our command.
            applescript = (
                f'tell application "Terminal" to do script '
                f'"cd {shlex.quote(str(cwd) if cwd else os.getcwd())}; '
                f'{cmd_str}; '
                f'echo; echo \\"[press enter]\\"; read"'
            )
            subprocess.Popen(["osascript", "-e", applescript])
            return

        # Linux / BSD: try common terminal emulators in order.
        # The trailing "; read" (or "-hold" for xterm) keeps the
        # window open after the program exits.
        if hold:
            held_cmd = f'{cmd_str}; echo; echo "[press enter to close]"; read'
        else:
            held_cmd = cmd_str

        # Each entry: (binary, args-builder lambda).  args-builder
        # gets the held_cmd as input and returns the full argv list.
        candidates = [
            # gnome-terminal: -- separates terminal flags from cmd.
            ("gnome-terminal",
             lambda c: ["gnome-terminal", "--", "bash", "-c", c]),
            # konsole: -e takes a single string.
            ("konsole",
             lambda c: ["konsole", "-e", "bash", "-c", c]),
            # xfce4-terminal: --command takes a single string.
            ("xfce4-terminal",
             lambda c: ["xfce4-terminal", f"--command=bash -c {shlex.quote(c)}"]),
            # mate-terminal: same pattern as gnome.
            ("mate-terminal",
             lambda c: ["mate-terminal", "--", "bash", "-c", c]),
            # tilix: similar.
            ("tilix",
             lambda c: ["tilix", "-e", "bash", "-c", c]),
            # alacritty: -e takes a program + args.
            ("alacritty",
             lambda c: ["alacritty", "-e", "bash", "-c", c]),
            # kitty: same pattern.
            ("kitty",
             lambda c: ["kitty", "bash", "-c", c]),
            # xterm: built-in -hold makes the window stay; we still
            # use the read-trick for consistency.
            ("xterm",
             lambda c: ["xterm", "-e", "bash", "-c", c]),
            # x-terminal-emulator: Debian alternative system pointer.
            ("x-terminal-emulator",
             lambda c: ["x-terminal-emulator", "-e", "bash", "-c", c]),
        ]
        for binary, build in candidates:
            if shutil.which(binary):
                argv = build(held_cmd)
                kwargs = dict(cwd=cwd, close_fds=True,
                                start_new_session=True)
                subprocess.Popen(argv, **kwargs)
                return
        # No terminal found - fall back to a captured-output dialog
        # so the user at least sees what happened, and tell them.
        QMessageBox.warning(
            self.w, "In Terminal",
            "No terminal emulator found on PATH.\n\n"
            "Tried: gnome-terminal, konsole, xfce4-terminal, "
            "mate-terminal, tilix, alacritty, kitty, xterm, "
            "x-terminal-emulator.\n\n"
            "Install one of the above or use 'Show output' instead.")

    @staticmethod
    def _spawn_detached(args, cwd=None, shell=False):
        """Launch a child process that fully detaches from Quopus.
        - Quopus is never blocked, regardless of how long the child runs
        - Closing Quopus does NOT kill the child (Windows: DETACHED_PROCESS
          + new process group; Linux: setsid via start_new_session)
        - stdin/stdout/stderr are wired to /dev/null so the child never
          tries to read from or write to Quopus's console
        Returns the Popen object (caller can ignore it).
        """
        kwargs = {
            "cwd": cwd,
            "shell": shell,
            "stdin":  subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == 'nt':
            # Windows: detach completely + open new console group so
            # Ctrl+C in Quopus doesn't kill the child
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = (DETACHED_PROCESS |
                                        CREATE_NEW_PROCESS_GROUP)
        else:
            # POSIX: new session so child becomes its own process-group
            # leader and survives the parent
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True
        return subprocess.Popen(args, **kwargs)

    def _run_with_output_dialog(self, args, cwd=None, shell=False,
                                  title="Command output",
                                  on_finished=None):
        """Run a child process while displaying its stdout/stderr in
        a non-modal Quopus dialog. Used when a button is configured
        with 'show_output' = True so the user can see diagnostic
        info from tools like unp64, exomizer, etc.

        - args / cwd / shell are passed to subprocess.Popen
        - The dialog stays open after the process finishes so the
          user can scroll back through the output. There's a Close
          button + Cancel button (terminates the child).
        - on_finished, if given, is called with the process's exit
          code once the child is done. Used by callers that want
          to refresh panels after the command completes.
        """
        from PyQt6.QtCore import QThread, pyqtSignal, Qt as _Qt
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                       QPlainTextEdit, QPushButton, QLabel)

        # Worker thread that reads the child's combined stdout+stderr
        # line by line and emits each line as a signal. Combining the
        # two streams keeps interleaving correct (a tool that prints
        # progress to stderr and results to stdout looks right).
        class _OutputWorker(QThread):
            line = pyqtSignal(str)
            finished_with_code = pyqtSignal(int)

            def __init__(self, args, cwd, shell):
                super().__init__()
                self.args = args
                self.cwd = cwd
                self.shell = shell
                self.proc = None
                self._cancel = False

            def cancel(self):
                self._cancel = True
                if self.proc and self.proc.poll() is None:
                    try:
                        # Kill the whole process group so child shells
                        # don't leave orphan grandchildren behind.
                        if os.name == 'nt':
                            self.proc.terminate()
                        else:
                            import signal
                            os.killpg(os.getpgid(self.proc.pid),
                                       signal.SIGTERM)
                    except Exception:
                        pass

            def run(self):
                kwargs = dict(
                    cwd=self.cwd, shell=self.shell,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,   # merge into stdout
                    bufsize=1,                   # line-buffered
                    universal_newlines=True,
                    encoding='utf-8',
                    errors='replace',
                )
                if os.name == 'nt':
                    # Hide the cmd.exe console that 'shell=True' on
                    # Windows would otherwise pop up briefly.
                    CREATE_NO_WINDOW = 0x08000000
                    kwargs["creationflags"] = CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                    kwargs["close_fds"] = True
                try:
                    self.proc = subprocess.Popen(self.args, **kwargs)
                except Exception as e:
                    self.line.emit(f"[error launching: {e}]")
                    self.finished_with_code.emit(-1)
                    return
                try:
                    for ln in self.proc.stdout:
                        if self._cancel:
                            break
                        # Strip the trailing newline; QPlainTextEdit
                        # adds its own.
                        self.line.emit(ln.rstrip('\r\n'))
                except Exception as e:
                    self.line.emit(f"[read error: {e}]")
                rc = self.proc.wait()
                self.finished_with_code.emit(rc)

        worker = _OutputWorker(args, cwd, shell)
        # Hold on the main window so the worker isn't GC'd mid-run.
        if not hasattr(self.w, '_cmd_workers'):
            self.w._cmd_workers = []
        self.w._cmd_workers.append(worker)

        dlg = QDialog(self.w)
        dlg.setWindowTitle(title)
        dlg.resize(900, 600)
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QVBoxLayout(dlg)
        # Show the actual command at the top so the user knows what
        # ran (helpful for debugging buttons with token substitution).
        cmd_str = (args if isinstance(args, str)
                    else " ".join(args))
        cmd_label = QLabel(f"Running:  {cmd_str}")
        cmd_label.setStyleSheet(
            f"color: #444; font-family: 'Topaz','Courier New',monospace; "
            f"font-size: {scaled_font_px(11)}px; padding: 4px;")
        cmd_label.setWordWrap(True)
        lay.addWidget(cmd_label)
        # Output area: black background, monospaced, read-only.
        te = QPlainTextEdit()
        te.setReadOnly(True)
        te.setMaximumBlockCount(10000)   # cap at 10k lines
        te.setStyleSheet(f"""
            background-color: {C.BLACK}; color: {C.WHITE};
            font-family: "Topaz-8","Topaz","Courier New",monospace;
            font-size: {scaled_font_px(12)}px; border: 1px solid {C.BLACK};
        """ + SCROLLBAR_QSS)
        lay.addWidget(te, 1)
        # Status line shows running / finished + exit code.
        status_label = QLabel("Running...")
        status_label.setStyleSheet(
            f"padding: 4px; font-size: {scaled_font_px(11)}px;")
        lay.addWidget(status_label)
        # Buttons: Cancel (terminates), Close (only enabled when done).
        bbar = QHBoxLayout()
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(worker.cancel)
        bbar.addWidget(b_cancel)
        bbar.addStretch(1)
        b_close = QPushButton("Close")
        b_close.clicked.connect(dlg.close)
        bbar.addWidget(b_close)
        lay.addLayout(bbar)

        def _on_line(ln):
            te.appendPlainText(ln)

        def _on_finished(rc):
            status_label.setText(
                f"Finished with exit code {rc}"
                if rc == 0 else
                f"Finished with exit code {rc}  (non-zero)")
            b_cancel.setEnabled(False)
            # Drop the worker from our tracking list so it can be GC'd.
            try:
                self.w._cmd_workers.remove(worker)
            except (ValueError, AttributeError):
                pass
            if on_finished is not None:
                try: on_finished(rc)
                except Exception: pass

        worker.line.connect(_on_line)
        worker.finished_with_code.connect(_on_finished)
        worker.start()
        # Non-modal so Quopus stays interactive. The dialog tracks
        # the worker on its own.
        dlg.show()
        return dlg

    def _refresh_both_panels(self):
        """Re-read both panels - used after a button command finishes
        when refresh_after was enabled."""
        try:
            for lister in (self.w.left_lister, self.w.right_lister):
                try: lister.refresh()
                except Exception: pass
        except Exception:
            pass

    @staticmethod
    def _spawn_visible(args, cwd=None):
        """Launch a child with a visible console window on Windows
        (e.g. cmd.exe shells, batch files the user wants to watch).
        Same detachment guarantees as _spawn_detached otherwise."""
        if os.name == 'nt':
            CREATE_NEW_CONSOLE = 0x00000010
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            return subprocess.Popen(
                args,
                cwd=cwd,
                creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
            )
        # On POSIX, fall back to detached
        return subprocess.Popen(
            args, cwd=cwd, start_new_session=True, close_fds=True)

    def _ask_overwrite(self, target, remember_decision, source_entry=None,
                         target_info=None):
        """
        Ask user what to do when target already exists.
        Returns one of: 'yes', 'yes_all', 'no', 'no_all', 'rename:<newpath>', 'cancel'.
        `remember_decision` holds the previously-chosen 'yes_all'/'no_all' if any,
        so we can short-circuit.
        `source_entry` is the FsEntry being copied; used to show size/date
        of the source so the user can compare against the existing target.
        `target_info` is an optional (size, mtime) tuple for the existing
        target. Pass this when the target lives on a filesystem that
        Path.stat() can't handle (FTP/SFTP) - the caller already has the
        info from its directory listing. mtime may be a datetime, a unix
        timestamp, or None.
        """
        if remember_decision.get('yes_all'):
            return 'yes'
        if remember_decision.get('no_all'):
            return 'no'

        from PyQt6.QtWidgets import QMessageBox, QPushButton, QInputDialog
        from datetime import datetime as _dt

        def _fmt_size(n):
            if n < 1024: return f"{n:,} bytes"
            if n < 1024*1024: return f"{n:,} bytes  ({n/1024:.1f} KB)"
            if n < 1024*1024*1024: return f"{n:,} bytes  ({n/(1024*1024):.2f} MB)"
            return f"{n:,} bytes  ({n/(1024*1024*1024):.2f} GB)"

        def _fmt_time(t):
            if t is None: return "?"
            if isinstance(t, (int, float)):
                try: t = _dt.fromtimestamp(t)
                except Exception: return "?"
            if hasattr(t, 'strftime'):
                return t.strftime('%Y-%m-%d %H:%M:%S')
            return str(t)

        # Existing target file info. If the caller already knows
        # the target's size/mtime (typically because the target is
        # on a remote FS where Path.stat() doesn't apply), use that.
        # Otherwise fall back to a local stat() call.
        tgt_size = None
        tgt_mtime = None
        if target_info is not None:
            try:
                tgt_size, tgt_mtime = target_info
                if hasattr(tgt_mtime, 'timestamp'):
                    tgt_mtime_ts = tgt_mtime.timestamp()
                else:
                    tgt_mtime_ts = tgt_mtime
                tgt_info = (f"Size:  {_fmt_size(tgt_size or 0)}\n"
                            f"Date:  {_fmt_time(tgt_mtime)}")
            except Exception:
                tgt_info = "(target info unavailable)"
                tgt_mtime_ts = None
        else:
            try:
                st = target.stat()
                tgt_size = st.st_size
                tgt_mtime = st.st_mtime
                tgt_mtime_ts = tgt_mtime
                tgt_info = (f"Size:  {_fmt_size(tgt_size)}\n"
                            f"Date:  {_fmt_time(tgt_mtime)}")
            except Exception:
                tgt_info = "(could not stat target)"
                tgt_mtime_ts = None

        # Source file info from the entry
        if source_entry is not None:
            src_size = getattr(source_entry, 'size', None) or 0
            src_mtime = getattr(source_entry, 'mtime', None)
            src_info = (f"Size:  {_fmt_size(src_size)}\n"
                        f"Date:  {_fmt_time(src_mtime)}")
            # Compare hint
            hint_lines = []
            try:
                if tgt_size is not None:
                    if src_size > tgt_size:
                        hint_lines.append("Source is LARGER than target")
                    elif src_size < tgt_size:
                        hint_lines.append("Source is SMALLER than target")
                    else:
                        hint_lines.append("Same size")
                # Compare timestamps if both present (use the unix-ts
                # variant of the target since tgt_mtime might be a
                # datetime when it came from an FTP listing).
                src_ts = (src_mtime.timestamp() if hasattr(src_mtime, 'timestamp')
                          else src_mtime)
                if isinstance(src_ts, (int, float)) \
                        and isinstance(tgt_mtime_ts, (int, float)):
                    if src_ts > tgt_mtime_ts + 1:
                        hint_lines.append("Source is NEWER than target")
                    elif src_ts < tgt_mtime_ts - 1:
                        hint_lines.append("Source is OLDER than target")
                    else:
                        hint_lines.append("Same date")
            except Exception:
                pass
            hint = "  |  ".join(hint_lines) if hint_lines else ""
        else:
            src_info = "(unknown - cross-fs transfer)"
            hint = ""

        box = QMessageBox(self.w)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Overwrite?")
        text = (f"Target already exists:\n{target}\n\n"
                f"--- Existing (target) ---\n{tgt_info}\n\n"
                f"--- New (source) ---\n{src_info}")
        if hint:
            text += f"\n\n[ {hint} ]"
        text += "\n\nOverwrite?"
        box.setText(text)
        # Use a monospaced informative text style so size/date columns line up
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        b_yes = box.addButton("Yes", QMessageBox.ButtonRole.YesRole)
        b_yes_all = box.addButton("Yes to all", QMessageBox.ButtonRole.YesRole)
        b_no = box.addButton("No (skip)", QMessageBox.ButtonRole.NoRole)
        b_no_all = box.addButton("No to all", QMessageBox.ButtonRole.NoRole)
        b_rename = box.addButton("Rename...", QMessageBox.ButtonRole.ActionRole)
        b_cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_no)
        box.exec()

        clicked = box.clickedButton()
        if clicked is b_yes: return 'yes'
        if clicked is b_yes_all:
            remember_decision['yes_all'] = True; return 'yes'
        if clicked is b_no: return 'no'
        if clicked is b_no_all:
            remember_decision['no_all'] = True; return 'no'
        if clicked is b_rename:
            new, ok = QInputDialog.getText(
                self.w, "Rename", "New name:", text=target.name)
            if not ok or not new.strip():
                return 'cancel'
            return 'rename:' + str(target.parent / new.strip())
        return 'cancel'

    def act_copy(self, src, dst, param):
        """Copy files from src lister to dst lister.
        Handles all four combinations of local/remote source/dest."""
        self._transfer(src, dst, move=False)

    def act_move(self, src, dst, param):
        """Move files from src lister to dst lister."""
        self._transfer(src, dst, move=True)

    def _transfer(self, src, dst, move=False):
        """Unified copy/move that handles local<->remote file transfers."""
        entries = src.selected_entries()
        if not entries:
            self._status("Nothing selected"); return

        src_kind = src.fs.kind
        dst_kind = dst.fs.kind

        # Same side check
        if src_kind == 'local' and dst_kind == 'local' \
                and src.current_path == dst.current_path:
            QMessageBox.information(self.w,
                "Move" if move else "Copy", "Source = destination")
            return

        # Progress dialog for any non-trivial transfer.
        # For single small local file skip it to avoid flicker.
        total_files = len(entries)
        remote_involved = (src_kind == 'remote' or dst_kind == 'remote')
        show_progress = True
        if not remote_involved and total_files == 1 and not entries[0].is_dir \
           and entries[0].size < 512 * 1024:
            show_progress = False

        progress_dlg = None
        speed_state = {'last_bytes': 0, 'last_time': 0, 'current_name': '',
                       'last_bps': 0, 'file_idx': 0,
                       'file_total': total_files}
        if show_progress:
            verb = "Moving" if move else "Copying"
            # Pick a more specific label than "FTP" when the
            # remote backend is actually a Quopus Drive mount.
            # QDriveFs.label starts with "qdrive://..." while
            # FTP backends use bookmark names or "ftp://...".
            # Both have kind=='remote' so we have to inspect
            # the label to disambiguate.
            def _backend_name(fs):
                lab = getattr(fs, 'label', '') or ''
                if lab.startswith('qdrive://'):
                    return "Quopus Drive"
                return "FTP"
            if remote_involved:
                # If both ends are remote, pick the source's
                # backend name (it's the one streaming bytes
                # OUT; from the user's mental model that's the
                # "via" channel). If only one end is remote,
                # use whichever it is.
                if src_kind == 'remote':
                    backend = _backend_name(src.fs)
                else:
                    backend = _backend_name(dst.fs)
                title = f"{verb} via {backend}"
            else:
                title = verb
            progress_dlg = self._make_transfer_progress(f"{title}...")

        n_ok = 0; n_skipped = 0; errors = []
        decision = {}
        cancelled = False

        def _update_progress(done, total, name):
            """Returns False if the user has pressed Cancel.
            Returns True (or None) otherwise. The chunked copy uses this
            return value to abort mid-file."""
            if not progress_dlg:
                return True
            # Check cancel BEFORE updating UI - cheap fast-path
            if progress_dlg.wasCanceled():
                return False
            import time
            now = time.monotonic()
            if speed_state['current_name'] != name:
                speed_state['current_name'] = name
                speed_state['last_bytes'] = 0
                speed_state['last_time'] = now
                speed_state['last_bps'] = 0
            dt = now - speed_state['last_time']
            if dt >= 0.25:
                delta_bytes = done - speed_state['last_bytes']
                speed_state['last_bps'] = delta_bytes / dt if dt > 0 else 0
                speed_state['last_bytes'] = done
                speed_state['last_time'] = now
            bps = speed_state['last_bps']
            speed_text = ""
            eta_text = ""
            if bps > 0:
                speed_text = f" @ {fmt_size(int(bps))}/s"
                if total > 0 and done < total:
                    remaining = (total - done) / bps
                    if remaining < 3600:
                        eta_text = f" - ETA {int(remaining//60):d}:{int(remaining%60):02d}"
                    else:
                        eta_text = f" - ETA {int(remaining//3600)}h{int((remaining%3600)//60):02d}m"

            file_info = f"File {speed_state['file_idx']} of {speed_state['file_total']}"

            if total > 0:
                pct = int(100 * done / total)
                label = (f"{file_info}: {name}\n"
                         f"{fmt_size(done)} / {fmt_size(total)}"
                         f" ({pct}%)"
                         f"{speed_text}{eta_text}")
                # QProgressDialog uses 32-bit signed ints; scale down for
                # files / totals larger than ~2 GiB
                MAX_INT = 2_000_000_000
                if total > MAX_INT:
                    scale = MAX_INT / total
                    progress_dlg.setRange(0, MAX_INT)
                    progress_dlg.setValue(int(done * scale))
                else:
                    progress_dlg.setRange(0, total)
                    progress_dlg.setValue(done)
            else:
                label = (f"{file_info}: {name}\n"
                         f"{fmt_size(done)} transferred"
                         f"{speed_text}")
                progress_dlg.setRange(0, 0)
            progress_dlg.setLabelText(label)
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()
            # Re-check after processing events; the user may have just clicked
            return not progress_dlg.wasCanceled()

        try:
            for e in entries:
                if cancelled or (progress_dlg and progress_dlg.wasCanceled()):
                    cancelled = True
                    break
                speed_state['file_idx'] += 1
                try:
                    ok = self._transfer_one(e, src, dst, move,
                                             decision, _update_progress)
                    if ok == 'done':   n_ok += 1
                    elif ok == 'skip': n_skipped += 1
                    elif ok == 'cancel':
                        cancelled = True; break
                except TransferCancelled:
                    cancelled = True
                    break
                except Exception as ex:
                    # Check for lost-connection in either lister
                    handled = False
                    for side in (src, dst):
                        if side.fs.kind == 'remote' and \
                           side._is_connection_lost_error(ex):
                            if progress_dlg: progress_dlg.close()
                            side._handle_remote_error(ex, "Transfer failed")
                            handled = True
                            cancelled = True
                            break
                    if handled: break
                    errors.append(f"{e.name}: {ex}")
        finally:
            if progress_dlg:
                progress_dlg.close()

        src.refresh(); dst.refresh()
        msg = f"{'Moved' if move else 'Copied'} {n_ok}"
        if n_skipped: msg += f", skipped {n_skipped}"
        if cancelled: msg += " (cancelled)"
        if errors:
            msg += f"; {len(errors)} error(s)"
            QMessageBox.warning(self.w, "Transfer", "\n".join(errors[:10]))
        self._status(msg)

    def _make_transfer_progress(self, title):
        from PyQt6.QtWidgets import QProgressDialog
        dlg = QProgressDialog(title, "Cancel", 0, 100, self.w)
        dlg.setWindowTitle("Transfer")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        return dlg

    def _transfer_one(self, entry, src, dst, move, decision, progress_cb):
        """Transfer a single entry. Returns 'done', 'skip', or 'cancel'."""
        src_kind = src.fs.kind
        dst_kind = dst.fs.kind

        # Helper: does the destination already have a file/dir of this name?
        # Returns one of:
        #   None                -> no existing target
        #   ('local', None)     -> exists locally (size/date via stat())
        #   ('remote', (sz, mt))-> exists remotely; size+mtime from listing
        # We need the second value so the overwrite dialog can display
        # target size/date for FTP destinations where Path.stat() doesn't
        # work.
        def dest_has(name):
            if dst_kind == 'local':
                if (dst.current_path / name).exists():
                    return ('local', None)
                return None
            # remote: check via fresh listing (cheap for small dirs)
            try:
                for re in dst.fs.list():
                    if re.name == name:
                        return ('remote',
                                 (getattr(re, 'size', None),
                                  getattr(re, 'mtime', None)))
            except Exception:
                pass
            return None

        target_name = entry.name
        existing = dest_has(target_name)

        if existing is not None:
            from pathlib import Path
            kind_, info_ = existing
            fake_target = (dst.current_path / target_name) \
                if dst_kind == 'local' \
                else Path(dst.fs.pwd()) / target_name
            choice = self._ask_overwrite(fake_target, decision,
                                          source_entry=entry,
                                          target_info=info_)
            if choice == 'cancel': return 'cancel'
            if choice == 'no':     return 'skip'
            if choice.startswith('rename:'):
                target_name = Path(choice.split(':', 1)[1]).name

        # Dispatch by source/dest kind
        if src_kind == 'local' and dst_kind == 'local':
            self._copy_local_to_local(entry, dst.current_path / target_name,
                                       move, progress_cb)
        elif src_kind == 'local' and dst_kind == 'remote':
            self._upload_local(entry, dst, target_name, move, progress_cb)
        elif src_kind == 'remote' and dst_kind == 'local':
            self._download_remote(entry, dst.current_path / target_name,
                                  src, move, progress_cb)
        else:  # remote -> remote
            self._copy_remote_to_remote(entry, src, dst, target_name,
                                        move, progress_cb)
        return 'done'

    def _copy_local_to_local(self, entry, target, move, progress_cb=None):
        import shutil
        import os
        from pathlib import Path
        src_path = Path(entry.path)
        if target.exists():
            if target.is_dir(): shutil.rmtree(target)
            else:               target.unlink()

        # FAST PATH for same-volume move: os.replace is essentially a
        # filesystem-level rename and completes in milliseconds even on
        # multi-gigabyte files / directories. Only the directory entry
        # gets re-linked, no data is copied. This is what Total
        # Commander does for same-volume moves - and what the previous
        # _chunked_copy + unlink path missed.
        #
        # On cross-volume (different mount, different network share,
        # different UNC server) os.replace raises OSError with errno
        # EXDEV (POSIX) or WinError 17 (Windows). We catch that and
        # fall through to the chunked-copy fallback below.
        #
        # We only attempt this for `move=True`. For copy we always
        # want a real new file - rename would just move the source.
        if move:
            try:
                os.replace(str(src_path), str(target))
                # Done in one syscall - report 100% so the progress
                # dialog ticks past this entry instantly.
                if progress_cb:
                    progress_cb(entry.size or 0, entry.size or 0,
                                  entry.name)
                return
            except OSError:
                # Cross-volume or similar - fall through to copy+delete
                pass

        if entry.is_dir:
            if move:
                shutil.move(str(src_path), str(target))
                # Can't report per-file progress when shutil.move handles it
                if progress_cb:
                    progress_cb(entry.size or 0, entry.size or 0, entry.name)
            else:
                # Recursive copy with progress
                self._copy_tree_with_progress(src_path, target, progress_cb,
                                              entry.name)
        else:
            # Chunked single-file copy so the dialog ticks
            if progress_cb:
                total = entry.size
                try:
                    total = src_path.stat().st_size
                except Exception: pass
                self._chunked_copy(src_path, target, total,
                                   progress_cb, entry.name)
                if move:
                    src_path.unlink()
            else:
                if move:
                    shutil.move(str(src_path), str(target))
                else:
                    shutil.copy2(src_path, target)

    def _chunked_copy(self, src, dst, total, progress_cb, display_name):
        """Copy a file in chunks, calling progress_cb(done, total, name).
        If progress_cb returns False, abort and raise TransferCancelled."""
        CHUNK = 1 << 20   # 1 MiB
        done = 0
        try:
            with open(src, 'rb') as r, open(dst, 'wb') as w:
                while True:
                    buf = r.read(CHUNK)
                    if not buf: break
                    w.write(buf)
                    done += len(buf)
                    if progress_cb:
                        # progress_cb returns False -> user pressed Cancel
                        if progress_cb(done, total, display_name) is False:
                            raise TransferCancelled()
        except TransferCancelled:
            # Best-effort: remove the partial dst file so we don't leave
            # corrupted half-copies behind
            try: dst.unlink()
            except Exception: pass
            raise
        # Preserve mtime/perms (best-effort)
        try:
            import shutil as _sh
            _sh.copystat(str(src), str(dst))
        except Exception:
            pass

    def _copy_tree_with_progress(self, src, dst, progress_cb, display_name):
        """Recursive directory copy with per-file progress.
        Honours cancellation from progress_cb."""
        from pathlib import Path
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    total = item.stat().st_size
                except Exception:
                    total = 0
                self._chunked_copy(item, target, total, progress_cb,
                                   f"{display_name}/{rel}")
        try:
            import shutil as _sh
            _sh.copystat(str(src), str(dst))
        except Exception:
            pass

    def _upload_local(self, entry, dst, target_name, move, progress_cb):
        from pathlib import Path

        def _cb(d, t, n):
            if progress_cb(d, t, n) is False:
                raise TransferCancelled()

        if entry.is_dir:
            base = Path(entry.path)
            for child in base.rglob("*"):
                if child.is_file():
                    rel = child.relative_to(base)
                    remote_rel = target_name + "/" + str(rel).replace("\\", "/")
                    parts = remote_rel.split("/")[:-1]
                    cur = ""
                    for part in parts:
                        cur = cur + "/" + part if cur else part
                        try: dst.fs.make_dir(cur)
                        except Exception: pass
                    dst.fs.upload_from(child, remote_rel,
                        progress=lambda d, t, n=str(rel): _cb(d, t, n))
        else:
            dst.fs.upload_from(entry.path, target_name,
                progress=lambda d, t, n=entry.name: _cb(d, t, n))
        if move:
            src_path = Path(entry.path)
            if src_path.is_dir():
                import shutil; shutil.rmtree(src_path)
            else:
                src_path.unlink()

    def _download_remote(self, entry, target, src, move, progress_cb):
        if entry.is_dir:
            raise NotImplementedError(
                "Recursive directory download not yet supported. "
                "Please download files individually.")
        target.parent.mkdir(parents=True, exist_ok=True)

        def _cb(d, t, n=entry.name):
            if progress_cb(d, t, n) is False:
                raise TransferCancelled()

        try:
            src.fs.download_to(entry.name, target, progress=_cb,
                                size=entry.size)
        except TransferCancelled:
            try: target.unlink()
            except Exception: pass
            raise
        if move:
            src.fs.delete(entry.path)

    def _copy_remote_to_remote(self, entry, src, dst, target_name, move,
                               progress_cb):
        """Round-trip via local temp file (servers rarely allow direct)."""
        import tempfile, os as _os
        from pathlib import Path
        tmp = Path(tempfile.gettempdir()) / \
              f"dopus_xfer_{_os.getpid()}_{entry.name}"

        def _cb(d, t, n=entry.name):
            if progress_cb(d, t, n) is False:
                raise TransferCancelled()

        try:
            src.fs.download_to(entry.name, tmp, progress=_cb,
                                size=entry.size)
            dst.fs.upload_from(tmp, target_name, progress=_cb)
            if move:
                src.fs.delete(entry.path)
        except TransferCancelled:
            # tmp will be cleaned up in finally
            raise
        finally:
            try: tmp.unlink()
            except Exception: pass

    def act_delete(self, src, dst, param):
        src._delete_selected()

    def act_makedir(self, src, dst, param):
        label = "[REMOTE] " if src.fs.kind == 'remote' else ""
        name, ok = QInputDialog.getText(self.w, "Makedir",
            f"{label}New directory in:\n{src.fs.display_path()}")
        if ok and name:
            try:
                src.fs.make_dir(name)
                src.refresh(); self._status(f"Created {name}")
            except Exception as e:
                QMessageBox.warning(self.w, "Makedir", str(e))

    def act_rename(self, src, dst, param):
        """F6/Shift+F6: rename selected file(s).
        - Single file selected: simple inline rename dialog
        - Multiple files or tagged: open multi-rename tool"""
        paths = src.selected_or_tagged()
        if not paths: return
        if len(paths) == 1:
            src._rename_selected()
        else:
            from .multi_rename import MultiRenameDialog
            dlg = MultiRenameDialog(paths, self.w)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                src.refresh()
                self._status(f"Renamed {len(paths)} file(s)")

    def act_multi_rename(self, src, dst, param):
        """Ctrl+M: always open multi-rename tool (even for single file).
        Useful for templated renames where user wants the full dialog."""
        paths = src.selected_or_tagged()
        if not paths:
            self._status("No files selected")
            return
        from .multi_rename import MultiRenameDialog
        dlg = MultiRenameDialog(paths, self.w)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src.refresh()
            self._status(f"Renamed {len(paths)} file(s)")
    def act_parent(self, src, dst, param):  src.parent_dir()
    def act_root(self, src, dst, param):    src.root_dir()

    def act_goto_dir(self, src, dst, param):
        """Navigate the active lister to a specific directory.
        Param holds the path; if empty, a folder picker opens."""
        if not param:
            from PyQt6.QtWidgets import QFileDialog
            path = QFileDialog.getExistingDirectory(
                self.w, "Pick a folder", str(src.current_path))
            if not path: return
        else:
            path = param
        from pathlib import Path
        p = Path(path).expanduser()
        if not p.is_dir():
            QMessageBox.warning(self.w, "Goto",
                f"Not a directory:\n{p}")
            return
        src.goto(str(p))
        self._status(f"-> {p}")
    def act_read(self, src, dst, param):    src._read_selected()
    def act_hexread(self, src, dst, param): src._hex_selected()
    def act_info(self, src, dst, param):    src._info_selected()
    def act_select_all(self, src, dst, p):  src.select_all()
    def act_select_none(self, src, dst, p): src.select_none()
    def act_reread(self, src, dst, param):  src.refresh(); dst.refresh()
    def act_back(self, src, dst, param):    src.go_back()
    def act_forward(self, src, dst, param): src.go_forward()
    # Shuffle player actions - param is the kind ('sid' or 'mod').
    # If unset, falls back to 'sid' since SID is the more common
    # shuffle use case (HVSC archive).
    def act_shuffle_sids(self, src, dst, param):
        src._shuffle_play_sids()
    def act_shuffle_mods(self, src, dst, param):
        src._shuffle_play_mods()

    def act_compare(self, src, dst, param):
        """Compare two files side-by-side, with text or hex diff.

        Resolution rules for picking the two files (in order):
          1. EXACTLY two rows are MOUSE-SELECTED in the active panel.
             This wins even if there are unrelated tags - the user
             pointed at two specific files, that's the most explicit
             signal possible.
          2. EXACTLY two files are tagged (Insert/Space) in the
             active panel.
          3. One in active panel + one in the other panel (selected
             OR tagged in either, exactly one each).
          4. Two in the OTHER panel only (mirror of rule 1+2).
          5. Otherwise: hint dialog.

        We deliberately don't fall back to selected_or_tagged()
        because that prioritises tagged over selected, which is the
        wrong choice here: a stale set of tags from earlier should
        NOT override the user's current click-select on two rows.

        Opens a non-modal CompareDialog so the user can keep working
        in Quopus while reviewing the diff.
        """
        from .compare_dialog import CompareDialog

        def picks_for(lister):
            """Return (selected_files, tagged_files) for one panel.
            Both filtered to regular files only (skips dirs)."""
            sel = []
            try:
                for p in lister.selected_paths():
                    pp = Path(p)
                    if pp.is_file(): sel.append(pp)
            except Exception:
                pass
            tagged = []
            try:
                for p in lister.model.tagged_paths():
                    pp = Path(p)
                    if pp.is_file(): tagged.append(pp)
            except Exception:
                pass
            return sel, tagged

        src_sel, src_tag = picks_for(src)
        dst_sel, dst_tag = picks_for(dst)

        a = b = None

        # Rule 1: two mouse-selected files in the active panel win.
        if len(src_sel) == 2:
            a, b = src_sel
        # Rule 2: exactly two tagged in the active panel.
        elif len(src_tag) == 2:
            a, b = src_tag
        # Rule 3: one from each panel. We accept either selected or
        # tagged on each side, as long as each side has exactly one
        # file picked one way or the other.
        else:
            def one_pick(sel, tag):
                """Return the single picked file for a panel, or None
                if the panel doesn't have exactly one pick."""
                if len(sel) == 1: return sel[0]
                if len(tag) == 1: return tag[0]
                # Cursor row as last-resort - only if nothing else
                # is going on there.
                if len(sel) == 0 and len(tag) == 0:
                    return None
                return None  # ambiguous (>1 selected or >1 tagged)
            a_pick = one_pick(src_sel, src_tag)
            b_pick = one_pick(dst_sel, dst_tag)
            if a_pick is not None and b_pick is not None:
                a, b = a_pick, b_pick
            else:
                # Rule 4: maybe the user did everything in the OTHER
                # panel and the active panel is empty.
                if not src_sel and not src_tag:
                    if len(dst_sel) == 2:
                        a, b = dst_sel
                    elif len(dst_tag) == 2:
                        a, b = dst_tag

        if a is None or b is None:
            # Build a helpful diagnostic. Tell the user what we saw
            # so they can fix the situation rather than guess.
            counts = []
            if src_sel:  counts.append(f"{len(src_sel)} selected (active)")
            if src_tag:  counts.append(f"{len(src_tag)} tagged (active)")
            if dst_sel:  counts.append(f"{len(dst_sel)} selected (other)")
            if dst_tag:  counts.append(f"{len(dst_tag)} tagged (other)")
            seen = ", ".join(counts) if counts else "nothing picked"
            QMessageBox.information(
                self.w, "Compare",
                "Compare needs exactly two files.\n\n"
                "Pick them either way:\n"
                "  - Click + Ctrl/Shift-click to select two files in "
                "one panel\n"
                "  - Tag two files (Insert / Space) in one panel\n"
                "  - Or one file in each panel (selected or tagged)\n\n"
                f"Right now: {seen}.")
            return

        # Both must exist as regular files (filtered above but
        # double-check in case a tag is stale).
        for p in (a, b):
            if not p.is_file():
                QMessageBox.warning(
                    self.w, "Compare",
                    f"Not a regular file:\n{p}")
                return
        # Open non-modal so Quopus stays usable while the user
        # browses the diff.
        dlg = CompareDialog(a, b, parent=self.w)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def act_run_emu(self, src, dst, param):
        """Run the selected file in the configured C64 emulator
        (VICE / x64sc / Hoxs64 / ...). Same path the file-assoc
        'c64emu' type uses, but as an explicit action so it can
        be bound to an action button directly.

        Takes the first selected/tagged file. If no emulator is
        configured yet, run_in_c64_emulator() opens the config
        dialog first. Works on .prg, .crt, .d64, .t64, .tap and
        anything else the emulator's autostart accepts - we
        don't restrict by extension here, the emulator decides.
        """
        from .c64_disasm import run_in_c64_emulator
        from .config import save_config
        paths = src.selected_or_tagged()
        if not paths:
            self._status("Run in emulator: nothing selected")
            return
        cfg = self.w.config
        # Launch each selected file. For a single file this is
        # the common case; for multiple, we start them in
        # sequence (each detached) so e.g. a batch of demos can
        # be fired off - though most emulators will just stack
        # windows. We stop on the first hard failure (e.g. user
        # cancelled the emulator-path config dialog) since that
        # implies the rest would fail the same way.
        for p in paths:
            ok = run_in_c64_emulator(
                p, self.w, cfg,
                lambda: save_config(cfg) if cfg else None)
            if not ok:
                break

    def act_run_u64(self, src, dst, param):
        """Push the selected file to the active Ultimate-64 and
        run it over HTTP (no temp file). Routes by extension:
          .prg            -> runners:run_prg  (DMA load + run)
          .crt            -> runners:run_crt  (cartridge reset)
          .sid            -> runners:sidplay
          .mod            -> runners:modplay
          .d64/.d71/.d81/.g64 -> mount on drive A (readonly)
        Anything else gets a "don't know how to run this on U64"
        warning.

        Uses the multi-device picker so the user chooses which
        U64 when several are configured. Takes the first
        selected/tagged file (running a whole batch on real
        hardware doesn't make sense - the U64 can only run one
        thing at a time).
        """
        from pathlib import Path
        paths = src.selected_or_tagged()
        if not paths:
            self._status("Run on U64: nothing selected")
            return
        p = Path(str(paths[0]))
        ext = p.suffix.lower()
        # Device picker (multi-device aware, with Config button)
        from .u64_devices import pick_device
        device = pick_device(
            self.w, self.w.config,
            title="Run on U64",
            prompt=f"Which Ultimate-64 should run "
                   f"'{p.name}'?")
        if device is None:
            return
        host = (device.get('host', '') or '').strip()
        if not host:
            QMessageBox.warning(
                self.w, "Run on U64",
                "The selected device has no host/IP set.")
            return
        password = device.get('password', '') or ''
        http_port = int(device.get('http_port', 80))
        try:
            data = p.read_bytes()
        except OSError as e:
            QMessageBox.warning(
                self.w, "Run on U64",
                f"Could not read file:\n{e}")
            return
        if not data:
            QMessageBox.warning(
                self.w, "Run on U64",
                f"'{p.name}' is empty - nothing to run.")
            return
        from .u64_streamer import (
            u64_run_prg, u64_run_crt, u64_play_sid,
            u64_play_mod, u64_mount_disk)
        ok, msg = False, "Unsupported file type"
        if ext == ".prg":
            ok, msg = u64_run_prg(host, data,
                                    password=password,
                                    port=http_port)
        elif ext == ".crt":
            ok, msg = u64_run_crt(host, data,
                                    password=password,
                                    port=http_port)
        elif ext == ".sid":
            ok, msg = u64_play_sid(host, data,
                                     password=password,
                                     port=http_port)
        elif ext == ".mod":
            ok, msg = u64_play_mod(host, data,
                                     password=password,
                                     port=http_port)
        elif ext in (".d64", ".d71", ".d81", ".g64"):
            ok, msg = u64_mount_disk(host, data,
                                       drive="a",
                                       mode="readonly",
                                       password=password,
                                       port=http_port)
        else:
            QMessageBox.warning(
                self.w, "Run on U64",
                f"Don't know how to run a '{ext}' file on the "
                f"U64.\n\nSupported: .prg, .crt, .sid, .mod, "
                f".d64/.d71/.d81/.g64")
            return
        if ok:
            self._status(f"Sent '{p.name}' to U64 at {host}")
        else:
            QMessageBox.warning(
                self.w, "Run on U64",
                f"U64 at {host} rejected the request:\n{msg}")

    def act_u64view(self, src, dst, param):
        """Open the Ultimate 64 video stream viewer.

        Non-modal so Quopus stays usable while the streamer is up.
        Re-using the same dialog when triggered twice would be
        nicer but we don't track it here - if the user opens it
        twice they'll get two windows; closing either cleanly
        shuts down its workers via closeEvent.

        Reads host/IP, the four port numbers, and the optional
        network password from the main Quopus config. Defaults
        match the U64 firmware:
          video  UDP 11000
          audio  UDP 11001
          telnet TCP 23
          http   TCP 80    (REST API for run/mount drag-drop)
        """
        from .u64_streamer import (
            U64Streamer,
            PORT_VIDEO, PORT_AUDIO, PORT_TELNET, PORT_HTTP,
        )
        host = ""
        video_port = PORT_VIDEO
        audio_port = PORT_AUDIO
        telnet_port = PORT_TELNET
        http_port = PORT_HTTP
        password = ""
        video_only = False
        always_on_top = False
        try:
            cfg = self.w.config
            # Multi-device: ask the user which U64 to use when
            # more than one is configured. pick_device() returns
            # immediately if there's only one (no dialog), warns
            # the user if none are configured, and returns None
            # if the user cancels the picker.
            from .u64_devices import pick_device
            device = pick_device(
                self.w, cfg,
                title="U64 Streamer",
                prompt="Which Ultimate-64 should the streamer "
                       "connect to?")
            if device is None:
                # User declined or no device configured. Either
                # way, abort - opening a streamer pointed at an
                # empty host isn't useful.
                return
            host = device.get('host', '') or ''
            video_port = int(device.get('video_port', PORT_VIDEO))
            audio_port = int(device.get('audio_port', PORT_AUDIO))
            telnet_port = int(device.get('telnet_port', PORT_TELNET))
            http_port = int(device.get('http_port', PORT_HTTP))
            password = device.get('password', '') or ''
            video_only = bool(device.get('video_only', False))
            always_on_top = bool(device.get('always_on_top', False))
        except Exception:
            pass
        dlg = U64Streamer(
            default_host=host,
            video_port=video_port,
            audio_port=audio_port,
            telnet_port=telnet_port,
            http_port=http_port,
            password=password,
            video_only=video_only,
            always_on_top=always_on_top,
            parent=self.w)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def act_u64_config(self, src, dst, param):
        """Open the Ultimate 64 device configuration dialog
        without launching the streamer.

        Useful when you just want to add / edit / remove a U64
        device entry: maybe you got a new U64, moved one to a
        different IP, or want to mark a different device as the
        active default. The streamer doesn't need to be open
        for any of this.

        Delegates to the module-level helper at the bottom of
        this file so other code paths (u64_devices.pick_device)
        can reuse the same dialog logic without needing access
        to a dispatcher instance.
        """
        open_u64_config_dialog(self.w, self.w.config)
        # Status-bar feedback after the dialog closes - the
        # helper is silent because it doesn't know about the
        # main-window status bar.
        try:
            n = len(self.w.config.get('u64_devices', []))
            self.w.statusBar().showMessage(
                f"U64 config saved ({n} device(s) configured)",
                3000)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Direct module launchers - these open dialogs that normally come
    # up only via double-click on a matching filetype. Useful for
    # putting them on dedicated buttons so the user doesn't have to
    # navigate to a SID/MOD/D64 file first.
    #
    # Most of the inner viewer/player dialogs require a path as their
    # first constructor arg. For the launcher actions we resolve a
    # path in this order:
    #   1. Currently-selected file in the active pane (if extension
    #      matches the tool's accepted set)
    #   2. File-picker dialog rooted at the active pane's cwd
    #   3. None/abort if the user cancels the picker
    # That way the button works both with "I already picked the file"
    # and "let me browse to it" flows.
    # ------------------------------------------------------------------

    def _pick_file_for_module(self, src, extensions, dialog_title,
                              filter_str):
        """Helper. Returns a Path or None.

        Resolution order:
        1. Any extension-matching file in the active pane's
           selection. Picks the FIRST match so users with multiple
           items tagged get the obviously-relevant one.
        2. Any file the user has highlighted (focused row) or
           tagged, regardless of extension. The inner dialog gets
           a chance to handle weirdly-named files - many viewers
           sniff content rather than trusting the suffix, and
           "user clicked it" is a strong signal of intent.
        3. QFileDialog rooted at the pane's cwd, filter set to
           the matching extensions but with an All-Files fallback.

        Cancelling the picker returns None and the caller bails
        silently. The point of this layered approach is to never
        prompt when the user has already given us a strong hint
        about which file they want - even if that file's suffix
        doesn't match the typical set."""
        from pathlib import Path as _P
        from PyQt6.QtWidgets import QFileDialog
        ext_set = {e.lower() for e in extensions}

        # 1) Extension-matching selection wins outright
        try:
            sel = list(src.selected_paths())
        except Exception:
            sel = []
        for p in sel:
            if p.suffix.lower() in ext_set:
                return _P(p)

        # 2) Any selected/highlighted/tagged file - trust the user
        try:
            any_sel = src.selected_or_tagged()
            if any_sel:
                # If the first item is a directory, skip it - the
                # module dialogs all want files. Then take the first
                # actual file.
                for p in any_sel:
                    try:
                        if p.is_file():
                            return _P(p)
                    except OSError:
                        # Symlinks pointing nowhere, permission
                        # issues etc - just try the next.
                        continue
        except Exception:
            pass

        # 3) Picker as fallback
        cwd = ""
        try:
            cwd = str(src.cwd) if src.cwd else ""
        except Exception:
            pass
        path, _ = QFileDialog.getOpenFileName(
            self.w, dialog_title, cwd, filter_str)
        if not path:
            return None
        return _P(path)

    def act_sidplayer(self, src, dst, param):
        """SID Player. Uses the selected .sid if present, otherwise
        prompts for one."""
        from .sid_player import SIDPlayerDialog
        if not SIDPlayerDialog.check_audio_available(self.w):
            return
        path = self._pick_file_for_module(
            src, (".sid", ".psid", ".rsid"),
            "Open SID file",
            "SID tunes (*.sid *.psid *.rsid);;All files (*)")
        if path is None:
            return
        try:
            dlg = SIDPlayerDialog(path, self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "SID Player", str(e))

    def act_multi_sid(self, src, dst, param):
        """Multi-SID parallel player. Plays all selected/tagged
        SID files in parallel - the screen splits horizontally
        with one tune per row and synchronized playback.

        This is a PRO feature - trial users see a "buy pro" dialog
        instead. We gate it here at the action dispatcher rather
        than inside the dialog so the trial user gets the message
        before the audio engine even tries to spin up.
        """
        # Trial gate: Multi-SID requires PRO_MULTI feature
        try:
            from quopus_lib import license
            if not license.has_feature(license.FEATURE_MULTI_SID):
                QMessageBox.information(
                    self.w, "Multi-SID - Pro Feature",
                    "Multi-SID parallel playback is a Pro feature.\n\n"
                    "Trial users can play SID tunes one at a time\n"
                    "with the regular SID Player. Register Quopus\n"
                    "to unlock Multi-SID mode and mix several tunes\n"
                    "simultaneously.\n\n"
                    "See BUYING.md or click 'Enter License File...'\n"
                    "on the trial nag screen to register.")
                return
        except Exception:
            # License lookup failed - err on the permissive side
            # so Pro users with transient license errors don't get
            # locked out of what they paid for.
            pass
        from .sid_player import SIDPlayerDialog
        if not SIDPlayerDialog.check_audio_available(self.w):
            return
        from pathlib import Path as _P
        sids = []
        try:
            for p in src.selected_paths():
                if p.suffix.lower() in (".sid", ".psid", ".rsid"):
                    sids.append(_P(p))
        except Exception:
            pass
        if len(sids) < 2:
            QMessageBox.information(self.w, "Multi-SID Player",
                "Select 2 or more SID files first.\n\n"
                "Multi-SID plays several tunes in parallel with\n"
                "synchronized timing - one selected file isn't\n"
                "enough.")
            return
        sids.sort(key=lambda x: x.name.lower())
        try:
            dlg = SIDPlayerDialog(sids[0], self.w, multi_files=sids)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "Multi-SID Player", str(e))

    def act_sidplayer_playlist(self, src, dst, param):
        """SID Player in PLAYLIST/BROWSE MODE - lets the user step
        through a hand-picked list of SIDs with Prev/Next buttons.

        Unlike Shuffle SIDs (which recursively scans the current
        directory) this only uses the files the user has explicitly
        selected/tagged. Useful when you want to listen through a
        few specific tracks without scanning a whole tree.

        Behavior:
        - 0 selected SIDs -> nothing to play, show a hint
        - 1 selected SID  -> open in regular single-tune mode
        - 2+ selected SIDs -> open with shuffle_files set to the
          full list, starting from the first selection. Player
          shows Prev/Next buttons; advancing past the end stops."""
        from .sid_player import SIDPlayerDialog
        if not SIDPlayerDialog.check_audio_available(self.w):
            return
        from pathlib import Path as _P
        sids = []
        try:
            for p in src.selected_paths():
                if p.suffix.lower() in (".sid", ".psid", ".rsid"):
                    sids.append(_P(p))
        except Exception:
            pass
        if not sids:
            QMessageBox.information(self.w, "SID Playlist",
                "Select one or more SID files first.\n\n"
                "Tip: tag files with Space, then click this\n"
                "button to browse them with Prev/Next.")
            return
        # Sort by filename so playback order is predictable.
        # Original selection order isn't reliable anyway because
        # tagged_paths() returns whatever order the model has.
        sids.sort(key=lambda x: x.name.lower())
        try:
            if len(sids) == 1:
                # Single file: just open normally - no point in
                # playlist mode with one entry.
                dlg = SIDPlayerDialog(sids[0], self.w)
            else:
                dlg = SIDPlayerDialog(sids[0], self.w,
                                          shuffle_files=sids)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "SID Playlist", str(e))

    def act_youtube_audio(self, src, dst, param):
        """YouTube Audio player. Search channels, bookmark them,
        browse uploads newest-first, stream audio with a seek
        slider and an LED spectrum EQ. Runs async (its own threads)
        so Quopus stays usable while music plays."""
        from . import youtube_audio
        try:
            youtube_audio.open_youtube_audio(self.w)
        except Exception as e:
            QMessageBox.warning(self.w, "YouTube Audio", str(e))

    def act_modplayer(self, src, dst, param):
        """MOD Player. Uses the selected .mod/.it/.s3m/.xm if any."""
        from .mod_player import ModPlayerDialog
        path = self._pick_file_for_module(
            src, (".mod", ".it", ".s3m", ".xm", ".mptm", ".stm"),
            "Open MOD file",
            "Tracker modules (*.mod *.it *.s3m *.xm *.mptm *.stm);;"
            "All files (*)")
        if path is None:
            return
        try:
            dlg = ModPlayerDialog(path, self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "MOD Player", str(e))

    def act_modplayer_playlist(self, src, dst, param):
        """MOD Player in PLAYLIST/BROWSE MODE - same idea as the
        SID variant but for tracker modules. Steps through the
        user's selection with Prev/Next instead of scanning a
        whole directory tree.

        Single selection just opens that file; 2+ selections get
        the playlist treatment."""
        from .mod_player import ModPlayerDialog
        if not ModPlayerDialog.check_audio_available(self.w):
            return
        from pathlib import Path as _P
        mods = []
        try:
            for p in src.selected_paths():
                if p.suffix.lower() in (".mod", ".it", ".s3m",
                                         ".xm", ".mptm", ".stm"):
                    mods.append(_P(p))
        except Exception:
            pass
        if not mods:
            QMessageBox.information(self.w, "MOD Playlist",
                "Select one or more tracker module files first.\n\n"
                "Tip: tag files with Space, then click this\n"
                "button to browse them with Prev/Next.")
            return
        mods.sort(key=lambda x: x.name.lower())
        try:
            if len(mods) == 1:
                dlg = ModPlayerDialog(mods[0], self.w)
            else:
                dlg = ModPlayerDialog(mods[0], self.w,
                                          shuffle_files=mods)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "MOD Playlist", str(e))

    def act_asm64(self, src, dst, param):
        """Open the Assembly64 search browser as a standalone dialog.
        The browser auto-restores the last search session, so this
        is effectively 'resume Asm64 browsing'."""
        from .asm64_browser import make_browser_dialog
        dlg = make_browser_dialog(parent=self.w)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def act_telnet(self, src, dst, param):
        """Open the Telnet/Raw-TCP terminal client.

        If `param` is non-empty it's treated as a saved-session
        name - we look it up in the sessions file and apply it
        before opening. Otherwise the dialog starts empty with
        whatever fields the user last had. Either way the dialog
        is non-modal so Quopus stays usable while connected."""
        from .telnet_client import (
            TelnetClientDialog, load_sessions, TelnetSession,
        )
        chosen = None
        if param:
            for s in load_sessions():
                if s.name == param:
                    chosen = s
                    break
        dlg = TelnetClientDialog(parent=self.w, session=chosen)
        dlg.show()

    def act_telegram(self, src, dst, param):
        """Open the Telegram client (MTProto / Telethon).

        A full user client: lists your chats, reads and sends
        messages, sends/receives files. The active lister (src) is
        passed in so the 'Send tagged' button can upload the files
        you've tagged there. Non-modal; we keep a reference on self
        so the GC doesn't sweep it while it's open.
        """
        from .telegram_client import TelegramDialog
        existing = getattr(self, "_telegram_dlg", None)
        if existing is not None:
            try:
                from PyQt6 import sip
                alive = not sip.isdeleted(existing)
            except (ImportError, TypeError):
                alive = True
            if alive:
                try:
                    if existing.isVisible():
                        existing.raise_()
                        existing.activateWindow()
                        return
                except RuntimeError:
                    pass
        self._telegram_dlg = TelegramDialog(parent=self.w, lister=src)
        self._telegram_dlg.show()

    def act_database(self, src, dst, param):
        """Open the Quopus Database Browser - a catalog of indexed
        C64 / Amiga / scene archives.

        Lets you search huge archive folders by filename or disk
        header without unpacking the archives. Indexing scans
        folder trees, computes MD5 of every PRG/SEQ/USR/REL and
        every disk image's content, stores the results in
        config/quopus_db.sqlite for fast lookup.

        The browser is non-modal - Quopus stays usable while
        scans run in the background. We keep a reference to the
        dialog on self so Python's GC doesn't sweep it away
        before the user closes it (WA_DeleteOnClose handles the
        eventual cleanup).

        Lifetime tricky bit: the dialog uses WA_DeleteOnClose, so
        after the user clicks X the underlying C++ object is gone
        but our Python attribute still holds a dangling sip
        wrapper. Touching ANY method on that wrapper raises
        "wrapped C/C++ object of type DatabaseBrowserDialog has
        been deleted". We guard the isVisible() check so a second
        click re-opens cleanly instead of crashing.
        """
        from .db_browser import show_database_browser
        existing = getattr(self, "_db_browser", None)
        if existing is not None:
            # Probe whether the underlying C++ widget still exists.
            # Two ways the wrapper can be "alive but dead":
            #   1. sip.isdeleted() returns True - Qt deleted it
            #      after closeEvent because WA_DeleteOnClose
            #   2. The wrapper raises RuntimeError on any access
            # We catch both paths and treat them the same:
            # forget the stale reference and create a fresh one.
            try:
                from PyQt6 import sip
                still_alive = not sip.isdeleted(existing)
            except (ImportError, TypeError):
                still_alive = True  # No sip helper, fall through
            if still_alive:
                try:
                    if existing.isVisible():
                        existing.raise_()
                        existing.activateWindow()
                        return
                except RuntimeError:
                    # "wrapped C/C++ object has been deleted"
                    # despite sip claiming it's alive - the
                    # wrapper got disconnected somewhere. Forget
                    # it and open a fresh dialog below.
                    pass
        self._db_browser = show_database_browser(parent=self.w)


    def act_rclone(self, src, dst, param):
        """Open the Rclone Browser dialog - a unified UI for any
        cloud storage supported by rclone (Google Drive, OneDrive,
        Dropbox, Box, S3, B2, Mega, Yandex, pCloud, Jottacloud,
        Mail.ru, WebDAV, SFTP and 60+ more).

        The user must have rclone installed and at least one
        remote configured via `rclone config` in a terminal (we
        don't bundle the OAuth/auth flows ourselves - rclone's
        own wizard handles all 70 backends and we'd just be
        reimplementing it).

        Same lifetime pattern as act_database: we hold a single
        non-modal dialog reference and re-focus it if the user
        clicks the button a second time."""
        from .rclone_browser import show_rclone_browser
        existing = getattr(self, "_rclone_browser", None)
        if existing is not None:
            try:
                from PyQt6 import sip
                still_alive = not sip.isdeleted(existing)
            except (ImportError, TypeError):
                still_alive = True
            if still_alive:
                try:
                    if existing.isVisible():
                        existing.raise_()
                        existing.activateWindow()
                        return
                except RuntimeError:
                    pass
        self._rclone_browser = show_rclone_browser(
            parent=self.w, config=self.w.config)


    def act_rclone_setup(self, src, dst, param):
        """Open `rclone config` in a terminal window so the user
        can add / edit / remove cloud accounts.

        Rclone's config wizard is fully interactive (multi-step
        prompts, OAuth browser hand-off, paste-the-token flows).
        It works best in a real terminal - embedding it in a
        QProcess + QTextEdit loses the OAuth browser opening
        and the colored output. So we just spawn a fresh
        terminal window with `rclone config` and let the user
        drive it. When they're done and close the terminal,
        they can come back to the Rclone browser and the new
        remotes will show up after a Reload.

        On each OS we use the native terminal emulator:
          Windows:  cmd.exe /K  in a new window
          macOS:    open -a Terminal with a temp shell script
          Linux:    try gnome-terminal, konsole, xterm in that
                    order until one starts
        """
        from . import rclone_backend
        mgr = rclone_backend.get_manager(self.w.config)
        rclone_path = mgr.rclone_path
        # Sanity check: if rclone isn't there don't even bother
        # spawning a terminal that'll just error out
        if not mgr.is_available():
            QMessageBox.warning(
                self.w, "Rclone not found",
                f"Couldn't find a working rclone binary at:\n"
                f"  {rclone_path}\n\n"
                f"Download rclone from https://rclone.org/downloads/\n"
                f"and drop rclone.exe into the external/ folder "
                f"next to Quopus, then try again.")
            return

        import subprocess, sys as _sys, shlex as _shlex
        try:
            if _sys.platform == "win32":
                # cmd /K runs the command and keeps the window
                # open after it exits (so the user can read any
                # final messages). start opens a new console
                # window detached from Quopus's.
                quoted = f'"{rclone_path}" config'
                subprocess.Popen(
                    f'start "rclone config" cmd /K {quoted}',
                    shell=True)
            elif _sys.platform == "darwin":
                # Write a tiny shell script that runs rclone
                # config then waits for a keypress - keeps the
                # Terminal window open at the end so the user
                # sees the outcome.
                import tempfile, os as _os
                script = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".sh", delete=False,
                    prefix="quopus_rclone_")
                script.write(
                    f'#!/bin/bash\n'
                    f'{_shlex.quote(rclone_path)} config\n'
                    f'echo\n'
                    f'echo "Press Enter to close..."\n'
                    f'read\n')
                script.close()
                _os.chmod(script.name, 0o755)
                subprocess.Popen(
                    ["open", "-a", "Terminal", script.name])
            else:
                # Linux: try the common terminal emulators in
                # order. Each takes -- before the command to
                # run, but the exact flag varies a bit.
                terminals = [
                    ["gnome-terminal", "--",
                     rclone_path, "config"],
                    ["konsole", "-e",
                     rclone_path, "config"],
                    ["xfce4-terminal", "-e",
                     f"{_shlex.quote(rclone_path)} config"],
                    ["xterm", "-e",
                     rclone_path, "config"],
                ]
                spawned = False
                for cmd in terminals:
                    try:
                        subprocess.Popen(cmd)
                        spawned = True
                        break
                    except FileNotFoundError:
                        continue
                if not spawned:
                    QMessageBox.warning(
                        self.w, "Rclone setup",
                        "Could not find a terminal emulator "
                        "(tried gnome-terminal, konsole, "
                        "xfce4-terminal, xterm).\n\n"
                        "Run manually in any terminal:\n"
                        f"  {rclone_path} config")
                    return
            QMessageBox.information(
                self.w, "Rclone setup",
                "Rclone configuration started in a new terminal "
                "window.\n\n"
                "Follow the prompts to add cloud accounts. When "
                "you're done, click 'Reload remotes' in the "
                "Rclone browser to see the new entries.")
        except Exception as e:
            QMessageBox.warning(
                self.w, "Rclone setup failed",
                f"Couldn't spawn rclone config:\n\n{e}")


    def act_d64editor(self, src, dst, param):
        """CBM disk editor. Uses selected D64/D71/D81 if any,
        otherwise prompts."""
        from .cbmfiles import CbmDiskDialog
        path = self._pick_file_for_module(
            src, (".d64", ".d71", ".d81", ".g64", ".g71"),
            "Open CBM disk image",
            "CBM disk images (*.d64 *.d71 *.d81 *.g64 *.g71);;"
            "All files (*)")
        if path is None:
            return
        try:
            dlg = CbmDiskDialog(str(path), self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "D64 Editor", str(e))

    def act_adf_viewer(self, src, dst, param):
        """Open the Amiga ADF disk image viewer/editor. Browse,
        preview, extract, add files, rename, delete, change
        disk label, set boot flags, validate bitmap. Uses the
        currently-selected .adf file if any; otherwise prompts.

        Backed by quopus_lib/adf.py - pure Python, no external
        dependencies, supports OFS and FFS, both DD and HD
        floppies.
        """
        from .adf_viewer import ADFDiskDialog
        from .adf import ADFError
        path = self._pick_file_for_module(
            src, (".adf",),
            "Open Amiga ADF disk image",
            "Amiga disk images (*.adf);;All files (*)")
        if path is None:
            return
        try:
            dlg = ADFDiskDialog(str(path), self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except ADFError as e:
            QMessageBox.warning(
                self.w, "ADF Viewer",
                f"Cannot open ADF:\n{e}")
        except Exception as e:
            QMessageBox.warning(
                self.w, "ADF Viewer", str(e))

    def act_adf_new(self, src, dst, param):
        """Create a blank Amiga ADF disk image. Prompts for the
        label, OFS/FFS, DD/HD, then opens the viewer/editor on
        the new disk so files can be added immediately."""
        from .adf_viewer import ADFDiskDialog
        try:
            dlg = ADFDiskDialog.new_disk(self.w)
        except Exception as e:
            QMessageBox.warning(self.w, "New ADF", str(e))
            return
        if dlg is None:
            return
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def act_basic_editor(self, src, dst, param):
        """Open the BASIC v2 editor. Empty by default - the user
        loads/types code inside the dialog."""
        from .basic_editor import BasicEditorDialog
        dlg = BasicEditorDialog(parent=self.w)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def act_image_viewer(self, src, dst, param):
        """Image viewer. Handles regular PNG/JPG/GIF/BMP plus a few
        retro formats. Uses selected file or prompts."""
        from .image_viewer import ImageViewer
        path = self._pick_file_for_module(
            src,
            (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
             ".tiff", ".tif", ".ico"),
            "Open image",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp "
            "*.tiff *.tif *.ico);;All files (*)")
        if path is None:
            return
        try:
            dlg = ImageViewer(path, self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "Image Viewer", str(e))

    def act_archive_viewer(self, src, dst, param):
        """Archive viewer (ZIP/LHA/LZX/ADF/DMS/RAR/7Z). Uses selected
        archive or prompts."""
        from .archive_viewer import ArchiveViewer
        path = self._pick_file_for_module(
            src,
            (".zip", ".lha", ".lzx", ".adf", ".dms", ".rar",
             ".7z", ".tar", ".gz", ".bz2"),
            "Open archive",
            "Archives (*.zip *.lha *.lzx *.adf *.dms *.rar "
            "*.7z *.tar *.gz *.bz2);;All files (*)")
        if path is None:
            return
        try:
            dlg = ArchiveViewer(path, self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "Archive Viewer", str(e))

    def act_disasm(self, src, dst, param):
        """C64 6502/6510 disassembler. Uses selected PRG/BIN/CRT
        or prompts."""
        from .c64_disasm import C64DisasmViewer
        path = self._pick_file_for_module(
            src, (".prg", ".bin", ".p00", ".crt"),
            "Open binary for disassembly",
            "C64 binaries (*.prg *.bin *.p00 *.crt);;All files (*)")
        if path is None:
            return
        try:
            dlg = C64DisasmViewer(path, self.w)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
        except Exception as e:
            QMessageBox.warning(self.w, "Disassembler", str(e))

    def act_tap_toolkit(self, src, dst, param):
        """C64 .tap cassette image toolkit. Parses the TAP
        container, decodes the pulse stream into CBM-standard and
        turbo-loader byte blocks, lists the reconstructed files,
        shows hex / pulse-histogram / waveform views, extracts
        blocks as .prg, and can run the tape in an emulator or a
        reconstructed file on the U64.

        Multi-file support mirrors the CRT toolkit: if several
        .tap files are selected in the lister, the first opens
        with a Prev/Next navigation bar to step through the rest.
        """
        from .tap_toolkit import open_tap_toolkit
        sel_paths = []
        try:
            sel_paths = [
                p for p in src.selected_or_tagged()
                if str(p).lower().endswith(".tap")]
        except Exception:
            sel_paths = []

        if sel_paths:
            playlist = [str(p) for p in sel_paths]
            try:
                open_tap_toolkit(
                    playlist[0], parent=self.w,
                    config=self.w.config,
                    playlist=playlist if len(playlist) > 1
                                          else None,
                    playlist_index=0)
            except Exception as e:
                QMessageBox.warning(
                    self.w, "TAP Toolkit", str(e))
            return

        path = self._pick_file_for_module(
            src, (".tap",),
            "Open C64 tape image",
            "C64 tape image (*.tap);;All files (*)")
        if path is None:
            return
        try:
            open_tap_toolkit(path, parent=self.w,
                             config=self.w.config)
        except Exception as e:
            QMessageBox.warning(self.w, "TAP Toolkit", str(e))

    def act_crt_toolkit(self, src, dst, param):
        """C64 .crt cartridge image inspector. Parses the VICE-format
        header, lists CHIP packets/banks, allows hex / disasm view
        of individual banks, raw bank extraction, EAPI / EasyFS /
        Yeti file-table detection, embedded-blob scan, GMod2 EEPROM
        read/write. Uses the selected .crt file or prompts.

        Multi-file support: if multiple .crt files are selected
        in the lister, the toolkit opens the FIRST one and gets
        a Prev/Next navigation bar so the user can step through
        all selected cartridges without re-picking. The list of
        carts to navigate is built from selected_or_tagged(),
        filtered to .crt extensions only - non-CRT files in the
        selection are silently skipped from the playlist (you
        wouldn't want a Markdown README mixed in).
        """
        from .crt_toolkit import open_crt_toolkit, CrtParseError
        # Build the playlist first: every .crt in the active
        # selection, ordered as they appear in the lister. We
        # only fall back to the file picker if NOTHING relevant
        # is selected.
        sel_paths = []
        try:
            sel_paths = [
                p for p in src.selected_or_tagged()
                if str(p).lower().endswith(".crt")]
        except Exception:
            sel_paths = []

        if sel_paths:
            # Multi-file path - first opens, others queued.
            playlist = [str(p) for p in sel_paths]
            try:
                open_crt_toolkit(
                    playlist[0], self.w,
                    playlist=playlist if len(playlist) > 1
                                            else None,
                    playlist_index=0)
            except CrtParseError as e:
                QMessageBox.warning(
                    self.w, "CRT Toolkit",
                    f"Not a valid VICE CRT file:\n{e}")
            except Exception as e:
                QMessageBox.warning(
                    self.w, "CRT Toolkit", str(e))
            return

        # Fallback: nothing selected -> open file picker as before
        path = self._pick_file_for_module(
            src, (".crt",),
            "Open C64 cartridge image",
            "C64 cartridge (*.crt);;All files (*)")
        if path is None:
            return
        try:
            open_crt_toolkit(path, self.w)
        except CrtParseError as e:
            QMessageBox.warning(
                self.w, "CRT Toolkit",
                f"Not a valid VICE CRT file:\n{e}")
        except Exception as e:
            QMessageBox.warning(self.w, "CRT Toolkit", str(e))

    def act_retrogfx_file(self, src, dst, param):
        """Retro GFX viewer. Distinct from retrogfx_browser (which is
        the standalone launcher with a recent-files list) and from
        retrogfx (which silently skips when nothing's selected).

        Behavior: prefers any selected/highlighted file regardless
        of extension - the retro_gfx viewer sniffs the format from
        the bytes itself, so a cracked-PRG with embedded charset
        works just like a proper .kla. Only pops a picker when
        there's nothing on screen to use."""
        from .retro_gfx_viewer import show_retro_gfx_viewer
        path = self._pick_file_for_module(
            src,
            (".kla", ".koa", ".koala", ".art", ".hpi",
             ".bin", ".prg", ".raw", ".chr"),
            "Open retro graphics file",
            "Retro graphics (*.kla *.koa *.koala *.art *.hpi "
            "*.bin *.prg *.raw *.chr);;Images (*.png *.gif);;"
            "All files (*)")
        if path is None:
            return
        try:
            show_retro_gfx_viewer(str(path), self.w)
        except Exception as e:
            QMessageBox.warning(self.w, "Retro GFX", str(e))

    def act_vice_memory(self, src, dst, param):
        """Open the memory view/edit dialog talking to a running VICE
        emulator via the binary monitor protocol (TCP).

        VICE must be started with -binarymonitor (default port 6502).
        We persist host + port in the Quopus config (vice_host,
        vice_port) - first-time use prompts for them once, after that
        it's a single click.
        """
        from .u64_streamer import (
            _ViceMemoryBackend,
            MemoryViewDialog,
            _parse_c64_address,
        )

        cfg = self.w.config
        host = cfg.get('vice_host', '') or ""
        try:
            port = int(cfg.get('vice_port', 6502))
        except (ValueError, TypeError):
            port = 6502

        # Erste Verwendung -> einmalig nach Host/Port fragen und in
        # der Config persistieren. Spaeter aenderbar ueber den Quopus
        # Config-Editor.
        if not host:
            host, ok = QInputDialog.getText(
                self.w, "VICE memory - host",
                "VICE binary monitor host:\n"
                "(start VICE with -binarymonitor first)",
                text="127.0.0.1")
            if not ok or not host.strip():
                return
            host = host.strip()
            port_str, ok = QInputDialog.getText(
                self.w, "VICE memory - port",
                "VICE binary monitor port:",
                text="6502")
            if not ok or not port_str.strip():
                return
            try:
                port = int(port_str.strip())
            except ValueError:
                QMessageBox.warning(
                    self.w, "VICE memory",
                    f"Invalid port: {port_str!r}")
                return
            cfg['vice_host'] = host
            cfg['vice_port'] = port
            save_config(cfg)

        # Start- und End-Adresse abfragen (gleiche Defaults wie der
        # Streamer-interne Memory-Button: $0800 .. $FFFF, also die
        # ueblichen Programm-Adressen).
        addr_str, ok = QInputDialog.getText(
            self.w, "VICE memory - start address",
            "Start address (hex, e.g. $0800 or 0800):",
            text="$0800")
        if not ok or not addr_str.strip():
            return
        try:
            address = _parse_c64_address(addr_str)
        except ValueError as e:
            QMessageBox.warning(self.w, "VICE memory", str(e))
            return

        end_str, ok = QInputDialog.getText(
            self.w, "VICE memory - end address",
            f"End address (inclusive, must be >= ${address:04X}):",
            text="$FFFF")
        if not ok or not end_str.strip():
            return
        try:
            end_addr = _parse_c64_address(end_str)
        except ValueError as e:
            QMessageBox.warning(self.w, "VICE memory", str(e))
            return
        if end_addr < address:
            QMessageBox.warning(
                self.w, "VICE memory",
                f"End address ${end_addr:04X} is before "
                f"start address ${address:04X}.")
            return
        length = end_addr - address + 1

        backend = _ViceMemoryBackend(host, port)
        dlg = MemoryViewDialog(
            self.w, backend=backend,
            address=address, length=length)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Der Dialog hat parent=None (siehe MemoryViewDialog.__init__)
        # damit er beim Quopus-Minimieren NICHT mitgeht. Konsequenz:
        # wir muessen ihn anderweitig am Leben halten - sonst wuerde
        # der Garbage-Collector ihn killen sobald `dlg` aus dem Scope
        # faellt. Wir merken ihn an einer Liste am Mainwindow vor.
        # destroyed-Signal entfernt ihn wieder beim Schliessen, damit
        # die Liste nicht waechst.
        if not hasattr(self.w, '_detached_dialogs'):
            self.w._detached_dialogs = []
        self.w._detached_dialogs.append(dlg)
        dlg.destroyed.connect(
            lambda _obj=None, d=dlg: (
                self.w._detached_dialogs.remove(d)
                if d in self.w._detached_dialogs else None))
        dlg.show()

    def act_c64_emu_config(self, src, dst, param):
        """Open the C64 emulator configuration dialog.

        Lets the user set the emulator executable path and the
        command-line argument template (with {file}/{name}/{dir}
        tokens). Stored in quopus.cfg as c64_emulator and
        c64_emulator_args - same keys read by:
          * The Disasm Viewer's F5 'Run in emulator' button
          * The 'c64emu' file-association type (for .prg / .crt etc)

        Typical setting for working with the VICE memory dialog:
          Executable: C:\\VICE\\x64sc.exe
          Arguments:  -binarymonitor -autostart {file}
        """
        from .c64_disasm import show_c64_emu_config_dialog
        show_c64_emu_config_dialog(
            self.w, self.w.config,
            lambda: save_config(self.w.config))

    def act_toggle_non_dos83(self, src, dst, param):
        """Toggle den 'Hide 8+3 filenames' Filter im SOURCE-Lister.

        Verwendet src statt dst weil der Filter den AKTIVEN Lister
        betrifft - der Filter ist pro Lister konfiguriert (kein
        globaler Toggle), damit der User links 8+3 versteckt halten
        und rechts alle Files sehen kann.
        """
        try:
            src.toggle_non_dos83()
        except AttributeError:
            # Lister kennt die Methode nicht (alter Code, oder ein
            # Shim-Lister wie in branch-view). Still ignorieren.
            pass

    def act_retrogfx(self, src, dst, param):
        """C64 Graphics Viewer (charset, Koala, Hi-Res) auf die
        selektierte Datei. Erkennt das Format anhand der Datei-
        groesse; bei Mehrfachselektion zeigt alle nacheinander an
        (jeder Viewer ist ein eigenes nicht-modales Fenster).
        """
        paths = src.selected_or_tagged()
        if not paths:
            self._status("Nothing selected")
            return
        from .retro_gfx_viewer import show_retro_gfx_viewer
        for p in paths:
            show_retro_gfx_viewer(str(p), src)

    def act_retrogfx_browser(self, src, dst, param):
        """C64 Graphics Browser - standalone Launcher ohne Pfad.

        Oeffnet den RetroGfxLauncherDialog. Der hat eine 'Open file'-
        Funktion plus eine History der zuletzt geoeffneten Charsets/
        Koalas/Hi-Res. Praktisch fuer Demo-Coding wenn der User mit
        mehreren Charsets vergleichen will - Launcher bleibt offen.

        Wir halten den Dialog am Quopus-Mainwindow am Leben (sonst
        wuerde GC ihn killen weil parent=None gesetzt ist - aehnliches
        Muster wie bei MemoryViewDialog).
        """
        from .retro_gfx_viewer import show_retro_gfx_launcher
        dlg = show_retro_gfx_launcher(self.w)
        if not hasattr(self.w, '_detached_dialogs'):
            self.w._detached_dialogs = []
        self.w._detached_dialogs.append(dlg)
        dlg.destroyed.connect(
            lambda _obj=None, d=dlg: (
                self.w._detached_dialogs.remove(d)
                if d in self.w._detached_dialogs else None))

    def act_hotkey(self, src, dst, param):
        """Fire one of Quopus's built-in hotkey handlers. `param` is
        the key combo string ("Alt+U", "Ctrl+B", "F1", ...) that
        was bound at startup in main_window._setup_hotkeys.

        Used by the Button-Config dialog: when the user picks a
        built-in hotkey from the dropdown, we record action="hotkey"
        and param=<combo>. Clicking the button then runs the same
        handler that pressing the keyboard combo would.
        """
        combo = (param or "").strip()
        if not combo:
            return
        try:
            self.w._fire_builtin_hotkey(combo)
        except Exception as e:
            self._status(f"hotkey {combo!r} failed: {e}")

    def act_swap(self, src, dst, param):
        lp = self.w.left_lister.current_path
        self.w.left_lister.goto(str(self.w.right_lister.current_path))
        self.w.right_lister.goto(str(lp))
        self._status("Swapped")

    def act_show(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: return
        p = paths[0]
        if p.suffix.lower() in (".jpg",".jpeg",".png",".bmp",".gif",".webp",".ico",".iff"):
            dlg = QDialog(self.w); dlg.setWindowTitle(f"Show: {p.name}")
            dlg.setStyleSheet(f"background-color: {C.WB_GREY};")
            pix = QPixmap(str(p))
            lbl = QLabel(); lbl.setPixmap(pix)
            lay = QVBoxLayout(dlg); lay.addWidget(lbl); dlg.exec()
        else:
            src._open_file(p)

    def act_play(self, src, dst, param): self.act_show(src, dst, param)

    def act_print(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: return
        try:
            if platform.system() == "Windows":
                os.startfile(str(paths[0]), "print")
            else:
                self._spawn_detached(["lp", str(paths[0])])
            self._status(f"Printing {paths[0].name}")
        except Exception as e:
            QMessageBox.warning(self.w, "Print", str(e))

    def act_run(self, src, dst, param):
        """Run / Open / Execute the selected file.
        On Windows uses os.startfile() which respects the system's
        file associations (.prg -> WinVICE, .txt -> Notepad, etc).
        On Linux uses xdg-open. Otherwise tries to execute directly.

        Per-button options (set in the button edit dialog):
            Show output     - capture stdout/stderr in a Quopus window
                                if the param contains a token like %f
                                or %F, treats it as a real command and
                                runs WITH output captured instead of
                                via the OS file-association launcher.
            Refresh panels  - re-read both panels after the command
                                finishes (only when Show output is
                                on; otherwise the launch is detached
                                and we don't know when it's done).
        """
        opts = self._current_opts or {}
        show_output = bool(opts.get("show_output"))
        refresh_after = bool(opts.get("refresh_after"))
        in_terminal = bool(opts.get("in_terminal"))

        # If the user gave us a param with token substitutions
        # (e.g. 'unp64 %f' or 'telnet 192.168.1.99') AND wants
        # special handling, route through the matching helper.
        # Without one of these flags, fall through to the OS
        # file-association launcher so .prg etc. open in their
        # registered apps.
        if param and (in_terminal or show_output):
            ivalue = self._maybe_prompt_for_input(param)
            if ivalue is None:
                # User cancelled the prompt - abort the command.
                self._status("Cancelled")
                return
            substituted = self._substitute_tokens(
                param, src, dst, input_value=ivalue)
            try:
                parts = shlex.split(substituted,
                                      posix=(os.name != 'nt'))
            except ValueError as e:
                QMessageBox.warning(self.w, "Run",
                                      f"Cannot parse command:\n{e}")
                return
            # Windows quoting fix: shlex.split(posix=False) leaves
            # surrounding "..." quotes attached to the token. Our
            # _substitute_tokens always wraps paths in quotes so
            # that they survive the split as one argument, but the
            # result is then literally `"C:\path\file.txt"` as an
            # argv entry - with the quote chars as part of the
            # string. Popen passes that straight to the child app
            # which sees a filename starting with `"`, and we get
            # "Die Syntax fuer den Dateinamen ist falsch" (Notepad)
            # or similar errors.
            # Strip surrounding quotes here, after the split has
            # done its job of identifying argument boundaries.
            if os.name == 'nt':
                parts = [
                    (p[1:-1] if len(p) >= 2
                                 and p[0] == '"' and p[-1] == '"'
                              else p)
                    for p in parts]
            if not parts:
                self._status("Run: empty command")
                return
            cwd = str(src.current_path) if src else None
            if in_terminal:
                self._spawn_in_terminal(parts, cwd=cwd)
                self._status(f"Launched in terminal: {parts[0]}")
                if refresh_after:
                    self._refresh_both_panels()
            else:  # show_output
                on_done = (self._refresh_both_panels
                            if refresh_after else None)
                self._run_with_output_dialog(
                    parts, cwd=cwd,
                    title=f"Run: {parts[0]}",
                    on_finished=on_done)
                self._status(f"Running: {parts[0]}")
            return

        paths = src.selected_or_tagged()
        if not paths:
            # If a param was provided (button param), run that instead
            if param:
                target = Path(param)
                if target.exists():
                    paths = [target]
                else:
                    QMessageBox.warning(self.w, "Run",
                        f"Path does not exist:\n{param}")
                    return
            else:
                QMessageBox.information(self.w, "Run",
                    "Nothing selected to run.\n\n"
                    "Select a file first, or configure the button "
                    "with a command/path in the Param field.")
                return
        target = paths[0]
        # Run in the directory containing the target so relative paths resolve
        work_dir = str(target.parent) if target.parent.is_dir() \
                   else str(src.current_path)
        try:
            if platform.system() == "Windows":
                # Use `start` via cmd so the shell handles file associations
                # AND we can set cwd. The cmd window itself is invisible
                # (CREATE_NO_WINDOW) but the launched program shows normally.
                self._spawn_detached(
                    ["cmd.exe", "/C", "start", "", "/D", work_dir,
                     str(target)],
                    cwd=work_dir,
                )
            elif platform.system() == "Darwin":
                self._spawn_detached(["open", str(target)], cwd=work_dir)
            else:
                # Linux: try xdg-open first, fall back to direct exec
                try:
                    self._spawn_detached(["xdg-open", str(target)],
                                          cwd=work_dir)
                except FileNotFoundError:
                    self._spawn_detached([str(target)], cwd=work_dir)
            self._status(f"Running {target.name} (cwd={work_dir})")
            # Refresh-after on detached launch fires immediately - we
            # have no signal for "external GUI program closed".
            if refresh_after:
                self._refresh_both_panels()
        except Exception as e:
            QMessageBox.warning(self.w, "Run",
                f"Cannot run {target.name}:\n{e}")

    def act_shell(self, src, dst, param):
        """Open a terminal at the current directory.
        On Windows: spawns a detached cmd.exe in a new console window.
        On Linux: tries common terminal emulators."""
        try:
            if platform.system() == "Windows":
                self._spawn_visible(
                    ["cmd.exe", "/K",
                     f'cd /d "{src.current_path}" && echo Quopus shell in {src.current_path}'],
                )
            elif platform.system() == "Darwin":
                self._spawn_detached([
                    "osascript", "-e",
                    f'tell app "Terminal" to do script "cd {src.current_path}"'
                ])
            else:
                for term in ("x-terminal-emulator","gnome-terminal","konsole","xterm"):
                    if shutil.which(term):
                        self._spawn_detached([term],
                                              cwd=str(src.current_path))
                        break
                else:
                    QMessageBox.warning(self.w, "Shell",
                        "No terminal emulator found on PATH")
                    return
            self._status(f"Shell in {src.current_path}")
        except Exception as e:
            QMessageBox.warning(self.w, "Shell", str(e))

    def act_archive(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths:
            QMessageBox.information(self.w, "Archive",
                "Nothing selected to archive.")
            return
        if dst.fs.kind == 'remote':
            QMessageBox.information(self.w, "Archive",
                "Archiving directly into FTP not supported. "
                "Move the destination to a local folder first.")
            return

        # Pick the format first so the right extension can be suggested
        formats = [
            ("ZIP (.zip)",        "zip"),
            ("TAR + GZIP (.tar.gz)", "tar.gz"),
            ("TAR + BZIP2 (.tar.bz2)", "tar.bz2"),
            ("TAR + XZ (.tar.xz)", "tar.xz"),
            ("TAR plain (.tar)",  "tar"),
            ("GZIP single file (.gz)", "gz"),
            ("LHA (.lha) — needs lha.exe in PATH", "lha"),
            ("RAR (.rar) — needs rar.exe in PATH", "rar"),
        ]
        labels = [f[0] for f in formats]
        choice, ok = QInputDialog.getItem(self.w, "Archive",
            "Format:", labels, 0, False)
        if not ok: return
        kind = formats[labels.index(choice)][1]

        ext_map = {
            "zip":     ".zip",
            "tar":     ".tar",
            "tar.gz":  ".tar.gz",
            "tar.bz2": ".tar.bz2",
            "tar.xz":  ".tar.xz",
            "gz":      ".gz",
            "lha":     ".lha",
            "rar":     ".rar",
        }
        ext = ext_map[kind]

        # Bare GZIP packs only one file
        if kind == "gz":
            files = [p for p in paths if p.is_file()]
            if not files:
                QMessageBox.warning(self.w, "Archive",
                    "GZIP packs a single file. Select a regular file "
                    "(use .tar.gz for multiple files / directories).")
                return
            if len(files) > 1:
                QMessageBox.information(self.w, "Archive",
                    "GZIP packs only ONE file. The first selected file "
                    f"will be used:\n  {files[0].name}\n\n"
                    "Cancel and pick TAR.GZ for multi-file archives.")
            target = files[0]
            default_name = target.name + ".gz"
        else:
            default_name = (paths[0].stem if len(paths) == 1
                            else "archive") + ext

        name, ok = QInputDialog.getText(self.w, "Archive",
            f"Archive name ({ext}):", text=default_name)
        if not ok or not name: return
        if not name.lower().endswith(ext.lower()):
            name += ext
        out_path = dst.current_path / name

        # Dispatch by kind
        if kind == "zip":
            self._run_zip_in_thread(paths, out_path, dst)
        elif kind in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
            self._run_tar_in_thread(paths, out_path, kind, dst)
        elif kind == "gz":
            self._run_gz_in_thread(target, out_path, dst)
        elif kind == "lha":
            self._run_lha_in_thread(paths, out_path, dst)
        elif kind == "rar":
            self._run_rar_in_thread(paths, out_path, dst)

    def _run_zip_in_thread(self, paths, out_path, dst_lister):
        """Build a ZIP file in a worker thread with a visible progress
        dialog. Quopus stays interactive throughout; Cancel aborts and
        deletes the partial ZIP."""
        from PyQt6.QtCore import QThread, pyqtSignal
        from PyQt6.QtWidgets import QProgressDialog
        from pathlib import Path

        # Phase 1: gather the file list synchronously (this is fast, just
        # walks the trees) so we know how many files / bytes we have.
        file_jobs = []   # list of (source_abs, arc_name, size)
        for p in paths:
            if p.is_file():
                try: sz = p.stat().st_size
                except Exception: sz = 0
                file_jobs.append((p, p.name, sz))
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: sz = sub.stat().st_size
                        except Exception: sz = 0
                        arc = sub.relative_to(p.parent)
                        file_jobs.append((sub, str(arc), sz))
        total_files = len(file_jobs)
        total_bytes = sum(s for _, _, s in file_jobs)

        if total_files == 0:
            QMessageBox.information(self.w, "Archive",
                "Nothing to archive (empty selection).")
            return

        # Worker thread
        class _ZipWorker(QThread):
            progress  = pyqtSignal(int, int, int, int, str)   # done_files, total_files, done_bytes, total_bytes, current_name
            done      = pyqtSignal(bool, str)                  # ok, error_message

            def __init__(self, jobs, out, total_b):
                super().__init__()
                self.jobs = jobs
                self.out = out
                self.total_b = total_b
                self._cancel = False

            def cancel(self):
                self._cancel = True

            def run(self):
                import zipfile
                done_b = 0
                CHUNK = 1 << 20
                try:
                    with zipfile.ZipFile(self.out, "w",
                                          zipfile.ZIP_DEFLATED) as zf:
                        for i, (src, arc, sz) in enumerate(self.jobs, 1):
                            if self._cancel:
                                raise InterruptedError("cancelled")
                            self.progress.emit(i, len(self.jobs),
                                                done_b, self.total_b, arc)
                            # For small files just write directly; for big ones
                            # stream in chunks to allow cancel + progress
                            if sz <= 4 * CHUNK:
                                zf.write(src, arc)
                                done_b += sz
                            else:
                                with open(src, 'rb') as fr, \
                                     zf.open(arc, 'w', force_zip64=True) as fw:
                                    while True:
                                        if self._cancel:
                                            raise InterruptedError("cancelled")
                                        buf = fr.read(CHUNK)
                                        if not buf: break
                                        fw.write(buf)
                                        done_b += len(buf)
                                        self.progress.emit(
                                            i, len(self.jobs),
                                            done_b, self.total_b, arc)
                    self.done.emit(True, "")
                except InterruptedError:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, "cancelled")
                except Exception as e:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, str(e))

        worker = _ZipWorker(file_jobs, out_path, total_bytes)

        # Visible progress dialog. Non-modal so Quopus stays interactive.
        # QProgressDialog uses 32-bit signed ints internally, so for archives
        # larger than 2 GiB we have to scale our byte counters down to fit.
        # 0..10000 ticks gives us 0.01% precision which is plenty.
        PROGRESS_TICKS = 10000

        def _scale(n):
            if total_bytes <= 0: return 0
            return min(PROGRESS_TICKS,
                       int(n * PROGRESS_TICKS / total_bytes))

        dlg = QProgressDialog(
            f"Creating {out_path.name}...", "Cancel",
            0, PROGRESS_TICKS, self.w)
        dlg.setWindowTitle("Archive")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        from PyQt6.QtCore import Qt as _Qt
        dlg.setWindowModality(_Qt.WindowModality.NonModal)
        dlg.show()
        dlg.canceled.connect(worker.cancel)

        speed_state = {'last_b': 0, 'last_t': 0, 'bps': 0}

        def _on_progress(done_f, total_f, done_b, total_b, name):
            import time as _t
            now = _t.monotonic()
            if speed_state['last_t'] == 0:
                speed_state['last_t'] = now
                speed_state['last_b'] = done_b
            dt = now - speed_state['last_t']
            if dt >= 0.25:
                speed_state['bps'] = (done_b - speed_state['last_b']) / dt
                speed_state['last_b'] = done_b
                speed_state['last_t'] = now
            bps = speed_state['bps']
            speed_text = f" @ {fmt_size(int(bps))}/s" if bps > 0 else ""
            eta_text = ""
            if bps > 0 and total_b > 0 and done_b < total_b:
                rem = (total_b - done_b) / bps
                if rem < 3600:
                    eta_text = f" - ETA {int(rem//60):d}:{int(rem%60):02d}"
                else:
                    eta_text = f" - ETA {int(rem//3600)}h{int((rem%3600)//60):02d}m"
            pct = int(100 * done_b / total_b) if total_b else 0
            dlg.setLabelText(
                f"File {done_f} of {total_f}: {name}\n"
                f"{fmt_size(done_b)} / {fmt_size(total_b)} ({pct}%)"
                f"{speed_text}{eta_text}")
            dlg.setValue(_scale(done_b))

        def _on_done(ok, err):
            dlg.close()
            if ok:
                dst_lister.refresh()
                self._status(f"Archived {total_files} file(s) to {out_path.name}")
            else:
                if err == "cancelled":
                    self._status(f"Archive cancelled - {out_path.name} removed")
                else:
                    QMessageBox.critical(self.w, "Archive",
                        f"Failed: {err}")
            # Allow worker to be GC'd
            self.w._archive_worker = None

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        # Keep a reference so it isn't garbage-collected mid-run
        self.w._archive_worker = worker
        worker.start()

    def _make_archive_progress_dlg(self, title, total_bytes):
        """Build the visible non-modal QProgressDialog used by all archive
        workers. Returns (dlg, scaler_callable, speed_state)."""
        from PyQt6.QtWidgets import QProgressDialog
        from PyQt6.QtCore import Qt as _Qt
        PROGRESS_TICKS = 10000
        def _scale(n):
            if total_bytes <= 0: return 0
            return min(PROGRESS_TICKS,
                       int(n * PROGRESS_TICKS / total_bytes))
        dlg = QProgressDialog(title, "Cancel", 0, PROGRESS_TICKS, self.w)
        dlg.setWindowTitle("Archive")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowModality(_Qt.WindowModality.NonModal)
        dlg.show()
        speed_state = {'last_b': 0, 'last_t': 0, 'bps': 0}
        return dlg, _scale, speed_state

    def _format_progress_label(self, speed_state, done_b, total_b,
                                done_f, total_f, name):
        import time as _t
        now = _t.monotonic()
        if speed_state['last_t'] == 0:
            speed_state['last_t'] = now
            speed_state['last_b'] = done_b
        dt = now - speed_state['last_t']
        if dt >= 0.25:
            speed_state['bps'] = (done_b - speed_state['last_b']) / dt
            speed_state['last_b'] = done_b
            speed_state['last_t'] = now
        bps = speed_state['bps']
        speed_text = f" @ {fmt_size(int(bps))}/s" if bps > 0 else ""
        eta_text = ""
        if bps > 0 and total_b > 0 and done_b < total_b:
            rem = (total_b - done_b) / bps
            if rem < 3600:
                eta_text = f" - ETA {int(rem//60):d}:{int(rem%60):02d}"
            else:
                eta_text = f" - ETA {int(rem//3600)}h{int((rem%3600)//60):02d}m"
        pct = int(100 * done_b / total_b) if total_b else 0
        return (f"File {done_f} of {total_f}: {name}\n"
                f"{fmt_size(done_b)} / {fmt_size(total_b)} ({pct}%)"
                f"{speed_text}{eta_text}")

    def _run_tar_in_thread(self, paths, out_path, kind, dst_lister):
        """Build a TAR (optionally gz/bz2/xz compressed) in a worker thread."""
        from PyQt6.QtCore import QThread, pyqtSignal

        # Gather file list
        file_jobs = []
        for p in paths:
            if p.is_file():
                try: sz = p.stat().st_size
                except Exception: sz = 0
                file_jobs.append((p, p.name, sz))
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: sz = sub.stat().st_size
                        except Exception: sz = 0
                        arc = sub.relative_to(p.parent)
                        file_jobs.append((sub, str(arc), sz))
        total_files = len(file_jobs)
        total_bytes = sum(s for _, _, s in file_jobs)
        if total_files == 0:
            QMessageBox.information(self.w, "Archive",
                "Nothing to archive.")
            return

        mode_map = {
            'tar': 'w', 'tar.gz': 'w:gz',
            'tar.bz2': 'w:bz2', 'tar.xz': 'w:xz',
        }
        tar_mode = mode_map[kind]

        class _TarWorker(QThread):
            progress = pyqtSignal(int, int, int, int, str)
            done     = pyqtSignal(bool, str)
            def __init__(self, jobs, out, mode):
                super().__init__()
                self.jobs = jobs; self.out = out; self.mode = mode
                self._cancel = False
            def cancel(self): self._cancel = True
            def run(self):
                import tarfile
                done_b = 0
                try:
                    with tarfile.open(self.out, self.mode) as tf:
                        for i, (src, arc, sz) in enumerate(self.jobs, 1):
                            if self._cancel:
                                raise InterruptedError("cancelled")
                            self.progress.emit(i, len(self.jobs),
                                                done_b, total_bytes, arc)
                            tf.add(src, arcname=arc, recursive=False)
                            done_b += sz
                            self.progress.emit(i, len(self.jobs),
                                                done_b, total_bytes, arc)
                    self.done.emit(True, "")
                except InterruptedError:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, "cancelled")
                except Exception as e:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, str(e))

        worker = _TarWorker(file_jobs, out_path, tar_mode)
        dlg, scaler, speed_state = self._make_archive_progress_dlg(
            f"Creating {out_path.name}...", total_bytes)
        dlg.canceled.connect(worker.cancel)

        def _on_progress(done_f, total_f, done_b, total_b, name):
            dlg.setLabelText(self._format_progress_label(
                speed_state, done_b, total_b, done_f, total_f, name))
            dlg.setValue(scaler(done_b))

        def _on_done(ok, err):
            dlg.close()
            if ok:
                dst_lister.refresh()
                self._status(f"Archived {total_files} files to {out_path.name}")
            elif err == "cancelled":
                self._status(f"Archive cancelled - {out_path.name} removed")
            else:
                QMessageBox.critical(self.w, "Archive", f"Failed: {err}")
            self.w._archive_worker = None

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self.w._archive_worker = worker
        worker.start()

    def _run_gz_in_thread(self, src_file, out_path, dst_lister):
        """Bare GZIP a single file in a worker thread."""
        from PyQt6.QtCore import QThread, pyqtSignal
        try:
            total_bytes = src_file.stat().st_size
        except Exception:
            total_bytes = 0

        class _GzWorker(QThread):
            progress = pyqtSignal(int, int, int, int, str)
            done     = pyqtSignal(bool, str)
            def __init__(self, src, out):
                super().__init__()
                self.src = src; self.out = out; self._cancel = False
            def cancel(self): self._cancel = True
            def run(self):
                import gzip
                CHUNK = 1 << 20
                done_b = 0
                try:
                    with open(self.src, 'rb') as fr, \
                         gzip.open(self.out, 'wb') as fw:
                        while True:
                            if self._cancel:
                                raise InterruptedError("cancelled")
                            buf = fr.read(CHUNK)
                            if not buf: break
                            fw.write(buf)
                            done_b += len(buf)
                            self.progress.emit(1, 1, done_b, total_bytes,
                                                self.src.name)
                    self.done.emit(True, "")
                except InterruptedError:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, "cancelled")
                except Exception as e:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, str(e))

        worker = _GzWorker(src_file, out_path)
        dlg, scaler, speed_state = self._make_archive_progress_dlg(
            f"Compressing {src_file.name}...", total_bytes)
        dlg.canceled.connect(worker.cancel)

        def _on_progress(done_f, total_f, done_b, total_b, name):
            dlg.setLabelText(self._format_progress_label(
                speed_state, done_b, total_b, done_f, total_f, name))
            dlg.setValue(scaler(done_b))

        def _on_done(ok, err):
            dlg.close()
            if ok:
                dst_lister.refresh()
                self._status(f"Compressed to {out_path.name}")
            elif err == "cancelled":
                self._status(f"GZ cancelled - {out_path.name} removed")
            else:
                QMessageBox.critical(self.w, "Archive", f"Failed: {err}")
            self.w._archive_worker = None

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self.w._archive_worker = worker
        worker.start()

    def _run_lha_in_thread(self, paths, out_path, dst_lister):
        """Pack into a LHA file by invoking the lha binary in a worker
        thread. Lhasa's `lha` doesn't pack — it only extracts. We need
        the original `lha` from Yoshiyuki Oki / jhsdomain or `lha32.exe`
        on Windows."""
        from PyQt6.QtCore import QThread, pyqtSignal

        # Search candidates - on Linux Lhasa's lha can't pack, so prefer
        # 'lha' from sourceforge.net/projects/lha/. On Windows look for
        # lha32.exe / lha.exe / lhaforge etc.
        lha_bin = None
        candidates = ["lha", "lha.exe", "lha32.exe", "jlha"]
        for cand in candidates:
            found = shutil.which(cand)
            if found:
                lha_bin = found
                break
        if not lha_bin:
            for cand in (r"C:\Program Files\LHA\lha.exe",
                          r"C:\Program Files (x86)\LHA\lha.exe",
                          r"C:\Program Files\LhaForge\lha.exe"):
                if Path(cand).is_file():
                    lha_bin = cand
                    break
        if not lha_bin:
            QMessageBox.warning(self.w, "Archive (LHA)",
                "Cannot create LHA archives: no 'lha' binary found.\n\n"
                "On Linux: install 'lhasa' is NOT enough — it only "
                "extracts. You need the original 'lha' utility from "
                "https://sourceforge.net/projects/lha/.\n\n"
                "On Windows: install LHA32.exe or LhaForge and add it "
                "to your PATH.\n\n"
                "Note: lhafile (Python lib) only DECOMPRESSES; creating "
                "LHA archives always requires an external binary.")
            return

        # Count input bytes for progress
        total_bytes = 0; total_files = 0
        for p in paths:
            if p.is_file():
                try: total_bytes += p.stat().st_size
                except Exception: pass
                total_files += 1
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: total_bytes += sub.stat().st_size
                        except Exception: pass
                        total_files += 1

        # Resolve dst dir for cwd - lha resolves paths relative to cwd
        # We pass absolute archive path + relative source paths so the
        # archive doesn't include parent-dir hierarchy.
        # Most flexible: switch to common-parent directory.
        import os.path as _osp
        cwd_dir = str(_osp.commonpath([str(p.resolve()) for p in paths]))
        if not Path(cwd_dir).is_dir():
            cwd_dir = str(paths[0].resolve().parent)

        # Build relative paths
        rel_paths = []
        for p in paths:
            try:
                rel_paths.append(str(p.resolve().relative_to(cwd_dir)))
            except ValueError:
                rel_paths.append(str(p.resolve()))

        class _LhaWorker(QThread):
            progress = pyqtSignal(int, int, int, int, str)
            done     = pyqtSignal(bool, str)
            def __init__(self, bin_path, out, rels, work_dir):
                super().__init__()
                self.bin = bin_path; self.out = out
                self.rels = rels; self.work_dir = work_dir
                self._cancel = False
                self._proc = None
            def cancel(self):
                self._cancel = True
                if self._proc:
                    try: self._proc.terminate()
                    except Exception: pass
            def run(self):
                # `lha a archive.lha files...`  (a = add, recursive by default
                # in most lha implementations; some need -r)
                cmd = [self.bin, "aq", str(self.out)] + self.rels
                # 'a' = add, 'q' = quiet (single line per file). Try with -r
                # fallback if quiet mode didn't work.
                try:
                    self._proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        cwd=self.work_dir,
                        bufsize=1, universal_newlines=True,
                        errors='replace')
                    file_count = 0
                    for line in self._proc.stdout:
                        if self._cancel:
                            self._proc.terminate()
                            raise InterruptedError("cancelled")
                        line = line.strip()
                        # lha output format varies but usually shows
                        # filename being added per line
                        if line and not line.startswith(("Updating",
                                                          "Creating",
                                                          "Add ",
                                                          "Tested",
                                                          "Frozen")):
                            file_count += 1
                            fake_done = int(total_bytes * file_count
                                             / max(1, total_files))
                            self.progress.emit(file_count, total_files,
                                                fake_done, total_bytes,
                                                line[:60])
                    rc = self._proc.wait()
                    if rc != 0 and not self._cancel:
                        raise RuntimeError(f"lha exit code {rc}")
                    self.done.emit(True, "")
                except InterruptedError:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, "cancelled")
                except Exception as e:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, str(e))

        worker = _LhaWorker(lha_bin, out_path, rel_paths, cwd_dir)
        dlg, scaler, speed_state = self._make_archive_progress_dlg(
            f"Creating {out_path.name} (LHA)...", total_bytes)
        dlg.canceled.connect(worker.cancel)

        def _on_progress(done_f, total_f, done_b, total_b, name):
            dlg.setLabelText(self._format_progress_label(
                speed_state, done_b, total_b, done_f, total_f, name))
            dlg.setValue(scaler(done_b))

        def _on_done(ok, err):
            dlg.close()
            if ok:
                dst_lister.refresh()
                self._status(f"Archived {total_files} files to {out_path.name}")
            elif err == "cancelled":
                self._status(f"LHA cancelled - {out_path.name} removed")
            else:
                QMessageBox.critical(self.w, "Archive (LHA)",
                    f"Failed: {err}\n\n"
                    "LHA creation requires the 'lha' binary installed.")
            self.w._archive_worker = None

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self.w._archive_worker = worker
        worker.start()

    def _run_rar_in_thread(self, paths, out_path, dst_lister):
        """Pack into a RAR file by invoking the rar.exe binary in a worker
        thread. Captures rar's text output to feed the progress dialog."""
        from PyQt6.QtCore import QThread, pyqtSignal

        rar_bin = shutil.which("rar") or shutil.which("rar.exe")
        if not rar_bin:
            # Try common WinRAR install paths on Windows
            for cand in (r"C:\Program Files\WinRAR\rar.exe",
                          r"C:\Program Files\WinRAR\WinRAR.exe",
                          r"C:\Program Files (x86)\WinRAR\rar.exe"):
                if Path(cand).is_file():
                    rar_bin = cand
                    break
        if not rar_bin:
            QMessageBox.warning(self.w, "Archive (RAR)",
                "Cannot create RAR archives: no 'rar' binary found.\n\n"
                "Install WinRAR (https://www.rarlab.com) - the 'rar' "
                "command-line tool that comes with it must be on your "
                "PATH or installed in C:\\Program Files\\WinRAR\\.\n\n"
                "Note: rarfile (Python lib) only DECOMPRESSES; creating "
                "RAR files always requires the official rar binary.")
            return

        # Count input bytes for progress hint
        total_bytes = 0; total_files = 0
        for p in paths:
            if p.is_file():
                try: total_bytes += p.stat().st_size
                except Exception: pass
                total_files += 1
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: total_bytes += sub.stat().st_size
                        except Exception: pass
                        total_files += 1

        class _RarWorker(QThread):
            progress = pyqtSignal(int, int, int, int, str)
            done     = pyqtSignal(bool, str)
            def __init__(self, rar_bin, paths, out):
                super().__init__()
                self.rar = rar_bin; self.paths = paths
                self.out = out; self._cancel = False
                self._proc = None
            def cancel(self):
                self._cancel = True
                if self._proc:
                    try: self._proc.terminate()
                    except Exception: pass
            def run(self):
                # rar a -r -ep1 archive.rar files...
                cmd = [self.rar, "a", "-r", "-ep1", "-y",
                       str(self.out)] + [str(p) for p in self.paths]
                try:
                    self._proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        bufsize=1, universal_newlines=True,
                        errors='replace')
                    file_count = 0
                    for line in self._proc.stdout:
                        if self._cancel:
                            self._proc.terminate()
                            raise InterruptedError("cancelled")
                        line = line.strip()
                        # rar prints "Adding    foo/bar.txt    OK" lines
                        # or "Updating ..." - parse to update file counter
                        if line.startswith(("Adding", "Updating",
                                              "Compressing")):
                            # Pull out the filename token
                            parts = line.split(None, 2)
                            fname = parts[1] if len(parts) > 1 else ""
                            file_count += 1
                            # We don't know exact bytes, fake progress
                            # proportional to file count
                            fake_done = int(total_bytes * file_count
                                             / max(1, total_files))
                            self.progress.emit(file_count, total_files,
                                                fake_done, total_bytes, fname)
                    rc = self._proc.wait()
                    if rc != 0 and not self._cancel:
                        raise RuntimeError(f"rar exit code {rc}")
                    self.done.emit(True, "")
                except InterruptedError:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, "cancelled")
                except Exception as e:
                    try: self.out.unlink()
                    except Exception: pass
                    self.done.emit(False, str(e))

        worker = _RarWorker(rar_bin, paths, out_path)
        dlg, scaler, speed_state = self._make_archive_progress_dlg(
            f"Creating {out_path.name} (RAR)...", total_bytes)
        dlg.canceled.connect(worker.cancel)

        def _on_progress(done_f, total_f, done_b, total_b, name):
            dlg.setLabelText(self._format_progress_label(
                speed_state, done_b, total_b, done_f, total_f, name))
            dlg.setValue(scaler(done_b))

        def _on_done(ok, err):
            dlg.close()
            if ok:
                dst_lister.refresh()
                self._status(f"Archived {total_files} files to {out_path.name}")
            elif err == "cancelled":
                self._status(f"RAR cancelled - {out_path.name} removed")
            else:
                QMessageBox.critical(self.w, "Archive (RAR)",
                    f"Failed: {err}\n\n"
                    "RAR creation requires WinRAR/rar.exe installed.")
            self.w._archive_worker = None

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self.w._archive_worker = worker
        worker.start()

    def act_extract(self, src, dst, param):
        """Extract selected archive(s) into the other panel's directory.
        Supports ZIP, TAR (+gz/bz2/xz), LHA/LZH, RAR, GZ.

        Runs the actual extraction on a background QThread so Quopus
        stays responsive even on large RARs (where unrar.exe spawns
        per-file). A QProgressDialog shows file count + bytes done +
        speed/ETA + a Cancel button."""
        paths = src.selected_or_tagged()
        if not paths:
            QMessageBox.information(self.w, "Arc Ext",
                "Select an archive first (ZIP, TAR, LHA, RAR, GZ).")
            return
        if dst.fs.kind == 'remote':
            QMessageBox.information(self.w, "Arc Ext",
                "Cannot extract into a remote FTP folder. "
                "Move the other panel to a local folder first.")
            return

        # Phase 1: validate the archive set BEFORE starting the worker.
        # Things like "rarfile not installed" or "no unrar on PATH"
        # should fail fast with a useful message, not surface as a
        # generic worker error.
        archives = []          # list of (path, kind, handle_or_None)
        early_errors = []
        for p in paths:
            ext = p.suffix.lower()
            name_lower = p.name.lower()
            if name_lower.endswith(('.tar.gz', '.tgz',
                                      '.tar.bz2', '.tbz', '.tbz2',
                                      '.tar.xz', '.txz', '.tar')):
                archives.append((p, 'tar', None))
            elif ext == '.zip':
                archives.append((p, 'zip', None))
            elif ext in ('.lha', '.lzh'):
                try:
                    import lhafile  # noqa: F401
                    archives.append((p, 'lha', None))
                except ImportError:
                    early_errors.append(
                        f"{p.name}: install 'lhafile' for LHA support "
                        "(pip install lhafile)")
            elif ext == '.rar':
                try:
                    import rarfile
                except ImportError:
                    early_errors.append(
                        f"{p.name}: install 'rarfile' for RAR support "
                        "(pip install rarfile) and have unrar on PATH")
                    continue
                try:
                    rf = rarfile.RarFile(str(p))
                    archives.append((p, 'rar', rf))
                except rarfile.RarCannotExec:
                    early_errors.append(
                        f"{p.name}: no unrar binary on PATH "
                        "(install WinRAR or unrar)")
            elif ext == '.gz':
                archives.append((p, 'gz', None))
            else:
                early_errors.append(
                    f"{p.name}: unsupported format ({ext})")

        if not archives:
            QMessageBox.warning(self.w, "Arc Ext",
                f"Nothing to extract.\n\n" + "\n".join(early_errors[:10]))
            return

        # Phase 2: build the per-file job list. We walk each archive's
        # member list up front so the progress bar has a meaningful
        # total. RAR walks are cheap (header parse only - no decode
        # happens here); ZIP / TAR / LHA the same.
        import zipfile, tarfile
        all_jobs = []     # list of dicts with everything the worker needs
        total_bytes = 0
        for p, kind, handle in archives:
            try:
                if kind == 'tar':
                    tf = tarfile.open(p)
                    for ti in tf.getmembers():
                        sz = ti.size if ti.isfile() else 0
                        all_jobs.append({
                            'archive': p, 'kind': 'tar',
                            'handle': tf, 'name': ti.name,
                            'size': sz, 'is_dir': ti.isdir(),
                            'raw': ti})
                        total_bytes += sz
                elif kind == 'zip':
                    zf = zipfile.ZipFile(p)
                    for zi in zf.infolist():
                        is_dir = zi.is_dir() if hasattr(zi, 'is_dir') else \
                                  zi.filename.endswith('/')
                        sz = zi.file_size
                        all_jobs.append({
                            'archive': p, 'kind': 'zip',
                            'handle': zf, 'name': zi.filename,
                            'size': sz, 'is_dir': is_dir,
                            'raw': zi})
                        total_bytes += sz
                elif kind == 'lha':
                    import lhafile
                    lf = lhafile.Lhafile(str(p))
                    for info in lf.infolist():
                        fname = (info.filename or "").replace('\\', '/')
                        while '//' in fname:
                            fname = fname.replace('//', '/')
                        fname = fname.strip('/')
                        if not fname: continue
                        is_dir = (info.filename.endswith('/')
                                   and info.file_size == 0)
                        sz = info.file_size if not is_dir else 0
                        all_jobs.append({
                            'archive': p, 'kind': 'lha',
                            'handle': lf, 'name': fname,
                            'size': sz, 'is_dir': is_dir,
                            'raw': info, 'orig_name': info.filename})
                        total_bytes += sz
                elif kind == 'rar':
                    rf = handle
                    # RAR is treated as a SINGLE bulk job: spawning
                    # unrar.exe per file is catastrophically slow
                    # (50-200 ms startup overhead each, so a 200-file
                    # archive takes 10-40 s just on subprocess
                    # spawns). Total Commander runs unrar once on
                    # the whole archive and parses output for
                    # progress - that's what we do here.
                    rar_total = 0
                    rar_files = []
                    rar_dirs = []
                    for ri in rf.infolist():
                        is_dir = ri.is_dir() if hasattr(ri, 'is_dir') else \
                                  ri.isdir() if hasattr(ri, 'isdir') else \
                                  ri.filename.endswith('/')
                        if is_dir:
                            rar_dirs.append(ri.filename)
                        else:
                            rar_files.append((ri.filename, ri.file_size))
                            rar_total += ri.file_size
                    all_jobs.append({
                        'archive': p, 'kind': 'rar_bulk',
                        'handle': rf, 'name': p.name,
                        'size': rar_total,
                        'is_dir': False,
                        'raw': None,
                        # Pre-collected file list for progress
                        # tracking inside the worker.
                        'rar_files': rar_files,
                        'rar_dirs': rar_dirs,
                        'rar_total': rar_total,
                        'rar_count': len(rar_files),
                    })
                    total_bytes += rar_total
                elif kind == 'gz':
                    # bare gzip = single file; size unknown without
                    # reading the gzip footer.
                    try:
                        sz = p.stat().st_size * 4   # rough estimate
                    except Exception:
                        sz = 0
                    all_jobs.append({
                        'archive': p, 'kind': 'gz',
                        'handle': None, 'name': p.stem,
                        'size': sz, 'is_dir': False,
                        'raw': None})
                    total_bytes += sz
            except Exception as e:
                early_errors.append(f"{p.name}: scan failed: {e}")

        if not all_jobs:
            QMessageBox.warning(self.w, "Arc Ext",
                f"Nothing extractable.\n\n" + "\n".join(early_errors[:10]))
            return

        from PyQt6.QtCore import QThread, pyqtSignal, Qt as _Qt
        from PyQt6.QtWidgets import QProgressDialog
        target_dir = dst.current_path

        class _ExtractWorker(QThread):
            progress = pyqtSignal(int, int, int, int, str)   # done_f, total_f, done_b, total_b, name
            done = pyqtSignal(int, int, list)                 # n_done_archives, n_files, errors

            def __init__(self, jobs, archives, target):
                super().__init__()
                self.jobs = jobs
                self.archives = archives
                self.target = target
                self._cancel = False

            def cancel(self):
                self._cancel = True

            def run(self):
                done_b = 0
                done_f = 0
                errors = []
                files_per_archive = {}   # archive_path -> count
                try:
                    for j in self.jobs:
                        if self._cancel:
                            raise InterruptedError("cancelled")
                        # Bulk RAR jobs need their own inline progress
                        # path because they emit progress WHILE the
                        # subprocess runs, not just before/after.
                        if j['kind'] == 'rar_bulk':
                            n_ok, n_err = self._do_rar_bulk(
                                j, done_b, done_f, errors)
                            files_per_archive[j['archive']] = n_ok
                            done_b += j['size']
                            done_f += j['rar_count']
                            continue
                        self.progress.emit(done_f, len(self.jobs),
                                            done_b, total_bytes, j['name'])
                        try:
                            self._do_one(j)
                            files_per_archive[j['archive']] = \
                                files_per_archive.get(j['archive'], 0) + 1
                        except Exception as ex:
                            errors.append(
                                f"{j['archive'].name}/{j['name']}: {ex}")
                        done_b += j['size']
                        done_f += 1
                        self.progress.emit(done_f, len(self.jobs),
                                            done_b, total_bytes, j['name'])
                except InterruptedError:
                    errors.append("(cancelled by user)")
                # Count archives that contributed at least one
                # successful file - matches how the old sync code
                # reported "Extracted N archive(s)".
                n_archives = len(files_per_archive)
                n_files = sum(files_per_archive.values())
                self.done.emit(n_archives, n_files, errors)

            def _do_rar_bulk(self, j, base_b, base_f, errors):
                """Extract a RAR archive via the rarfile module's
                per-member extract(). One subprocess spawn per file
                (rarfile handles all tool detection + quoting), but
                we get accurate per-file progress out of it.

                Earlier versions of this method tried to spawn unrar
                directly with bulk flags for speed - that crashed on
                some Windows setups with access violations
                (exit code 0xC0000005) because rarfile may resolve
                to 7z / WinRAR / etc. with different CLIs. Going
                through rarfile.extract() per file is slower but
                bulletproof: rarfile knows how to call whatever
                tool is actually installed.
                """
                target = self.target
                target.mkdir(parents=True, exist_ok=True)
                # Pre-create directory entries up front.
                for d in j['rar_dirs']:
                    try:
                        (target / d).mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass

                rf = j['handle']
                size_by_name = {fn: sz for fn, sz in j['rar_files']}
                done_local_b = 0
                done_local_f = 0
                # Iterate file-by-file so we can emit progress and
                # honour cancel between members. rarfile.extract()
                # spawns the tool fresh per call - that's the slow
                # bit but it's also why rarfile is robust across
                # tool variants.
                for fname, sz in j['rar_files']:
                    if self._cancel:
                        break
                    # Emit progress BEFORE the read so the dialog
                    # shows the current filename while it's being
                    # decoded (the slow part).
                    self.progress.emit(
                        base_f + done_local_f, len(self.jobs),
                        base_b + done_local_b, total_bytes,
                        fname)
                    try:
                        rf.extract(fname, path=str(target))
                    except Exception as ex:
                        errors.append(
                            f"{j['archive'].name}/{fname}: {ex}")
                        # Continue with the next file rather than
                        # bailing - one corrupt member shouldn't
                        # kill the whole extraction.
                        continue
                    done_local_b += sz
                    done_local_f += 1
                    # Emit again so the bar advances after the file
                    # is on disk.
                    self.progress.emit(
                        base_f + done_local_f, len(self.jobs),
                        base_b + done_local_b, total_bytes,
                        fname)
                if self._cancel:
                    return done_local_f, j['rar_count'] - done_local_f
                # Final tick lands the archive at 100% in the bar
                # even if our size sums don't perfectly match the
                # archive's reported total bytes.
                self.progress.emit(
                    base_f + j['rar_count'], len(self.jobs),
                    base_b + j['size'], total_bytes,
                    j['archive'].name + " (done)")
                n_ok = done_local_f
                n_err = j['rar_count'] - n_ok
                return n_ok, n_err

            def _do_one(self, j):
                """Extract a single member to disk based on its kind.
                We re-implement what the synchronous loop did but
                per-member, so progress can be reported per-member
                for every archive type."""
                kind = j['kind']
                target = self.target
                if j['is_dir']:
                    (target / j['name']).mkdir(parents=True,
                                                  exist_ok=True)
                    return
                if kind == 'tar':
                    tf = j['handle']
                    # tarfile's extract() handles ownership/links/etc.
                    # which the per-file write_bytes path wouldn't.
                    tf.extract(j['raw'], path=target)
                elif kind == 'zip':
                    zf = j['handle']
                    zf.extract(j['raw'], path=target)
                elif kind == 'lha':
                    lf = j['handle']
                    fname = j['name']
                    data = lf.read(j['orig_name'])
                    outp = target / fname
                    outp.parent.mkdir(parents=True, exist_ok=True)
                    outp.write_bytes(data)
                elif kind == 'gz':
                    import gzip
                    src = j['archive']
                    out = target / j['name']
                    with gzip.open(src, 'rb') as fr, \
                         open(out, 'wb') as fw:
                        while True:
                            if self._cancel: return
                            buf = fr.read(1 << 20)
                            if not buf: break
                            fw.write(buf)

        worker = _ExtractWorker(all_jobs, archives, target_dir)
        # Keep a strong reference on the main window so the worker
        # isn't GC'd while it's running.
        self.w._archive_extract_worker = worker

        # Progress dialog. WindowModal so it blocks input on its
        # own window only - rest of Quopus stays interactive.
        PROGRESS_TICKS = 10000
        def _scale(b):
            if total_bytes <= 0: return 0
            return min(PROGRESS_TICKS,
                        int(b * PROGRESS_TICKS / total_bytes))
        dlg = QProgressDialog(
            f"Extracting {len(archives)} archive(s)...", "Cancel",
            0, PROGRESS_TICKS, self.w)
        dlg.setWindowTitle("Arc Ext")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setWindowModality(_Qt.WindowModality.WindowModal)
        dlg.show()
        dlg.canceled.connect(worker.cancel)

        speed = {'last_b': 0, 'last_t': 0.0, 'bps': 0.0}

        def _on_progress(done_f, total_f, done_b, total_b, name):
            import time as _t
            now = _t.monotonic()
            if speed['last_t'] == 0:
                speed['last_t'] = now
                speed['last_b'] = done_b
            dt = now - speed['last_t']
            if dt >= 0.25:
                speed['bps'] = (done_b - speed['last_b']) / dt
                speed['last_b'] = done_b
                speed['last_t'] = now
            bps = speed['bps']
            speed_text = f" @ {fmt_size(int(bps))}/s" if bps > 0 else ""
            eta_text = ""
            if bps > 0 and total_b > 0 and done_b < total_b:
                rem = (total_b - done_b) / bps
                if rem < 3600:
                    eta_text = (f" - ETA {int(rem//60):d}"
                                  f":{int(rem%60):02d}")
                else:
                    eta_text = (f" - ETA {int(rem//3600)}h"
                                  f"{int((rem%3600)//60):02d}m")
            pct = int(100 * done_b / total_b) if total_b else 0
            # Truncate long member paths so the dialog stays compact.
            disp = name if len(name) <= 60 else "..." + name[-57:]
            dlg.setLabelText(
                f"File {done_f} of {total_f}: {disp}\n"
                f"{fmt_size(done_b)} / {fmt_size(total_b)} "
                f"({pct}%){speed_text}{eta_text}")
            dlg.setValue(_scale(done_b))

        def _on_done(n_archives, n_files, errors):
            dlg.close()
            self.w._archive_extract_worker = None
            dst.refresh()
            all_errors = early_errors + errors
            if all_errors:
                head = (f"Extracted: {n_archives} archive(s), "
                          f"{n_files} file(s)\n\n")
                QMessageBox.warning(self.w, "Arc Ext",
                                      head + "\n".join(all_errors[:10]))
            else:
                self._status(
                    f"Extracted {n_archives} archive(s) "
                    f"({n_files} file(s))")

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        worker.start()

    def act_find(self, src, dst, param):
        pattern, ok = QInputDialog.getText(self.w, "Hunt", "Pattern (glob, e.g. *.txt):")
        if not ok or not pattern: return
        matches = list(src.current_path.rglob(pattern))
        text = f"Found {len(matches)} matches:\n\n" + "\n".join(str(m) for m in matches[:1000])
        self._show_text("Hunt results", text)

    def act_search(self, src, dst, param):
        """Open the Total Commander-style Find Files dialog. Three
        search modes (filename glob / text in files / hex in files),
        recursive, with live status and cancellable background scan."""
        from .find_dialog import FindDialog
        dlg = FindDialog(src, parent=self.w)
        dlg.show()

    def act_config(self, src, dst, param):
        """Config menu - lets the user pick which aspect to configure."""
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtGui import QCursor
        menu = QMenu(self.w)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: #a0a0a0; color: #000000;
                    border: 1px solid #000000;
                    font-family: "Topaz","Courier New",monospace; }}
            QMenu::item {{ padding: 4px 24px; }}
            QMenu::item:selected {{ background-color: #2040a0; color: white; }}
        """)
        menu.addAction("Action buttons...",  lambda: self._cfg_buttons())
        menu.addAction("File associations...", lambda: self._cfg_file_assoc())
        menu.addAction("Settings (font, appearance)...",
                         lambda: self._cfg_appearance())
        menu.addAction("Drive button style...",
                         lambda: self._cfg_drive_button_style())
        menu.addSeparator()
        # C64 emulator configuration - Pfad und Args-Template fuer
        # VICE (oder Hoxs64/CCS64). Wird sowohl vom 'Run in emulator'-
        # Pfad (file-assoc 'c64emu') als auch vom Disasm-Viewer-F5
        # benutzt. Zentral hier statt versteckt im File-Assoc-Dialog
        # weil der User die Args genau einmal setzen will (z.B.
        # '-binarymonitor -autostart {file}'), nicht pro Extension.
        menu.addAction("C64 emulator (path/args)...",
                         lambda: self.act_c64_emu_config(None, None, None))
        menu.addSeparator()
        # Direct toggle for the Size column display - users searched
        # for this and didn't always find the header right-click.
        cur_size = self.w.config.get("size_display", "bytes")
        if cur_size == "blocks":
            menu.addAction(
                "Switch Size column to bytes (4K, 1.2M, ...)",
                lambda: self.w._apply_size_display("bytes"))
        else:
            menu.addAction(
                "Switch Size column to C64 blocks (256 B = 1 bl)",
                lambda: self.w._apply_size_display("blocks"))
        menu.addSeparator()
        # Custom modules: discovery + management. Two entries -
        # reload re-scans the custom_modules/ directories and
        # picks up any added/changed/removed files (no restart
        # needed); open-folder uses the OS file manager to show
        # the user where to drop new modules.
        menu.addAction(
            "Reload custom modules",
            lambda: self._cfg_reload_custom_modules())
        menu.addAction(
            "Open custom modules folder",
            lambda: self._cfg_open_custom_modules_folder())
        menu.exec(QCursor.pos())

    def _cfg_reload_custom_modules(self):
        """Re-scan the custom_modules/ directories and refresh
        the action catalog. If any module failed to load, show a
        dialog with the per-file error details so the user can
        fix the typo and try again."""
        from . import custom_modules
        custom_modules.load_all()
        loaded = custom_modules.all_modules()
        errors = custom_modules.load_errors()
        # Refresh the button bank so any cells bound to a custom
        # action get their label / handler updated.
        try:
            self.w._rebuild_buttons()
        except Exception:
            pass
        # Rebuild the menu bar too - it's built from the same
        # action_catalog, so any newly-loaded custom modules need
        # to show up under their Custom Modules entry.
        try:
            self.w._build_menu_bar()
        except Exception:
            pass
        if errors:
            lines = [f"Loaded {len(loaded)} module(s); "
                     f"{len(errors)} failed:\n"]
            for path, msg in errors:
                # Trim path to filename for readability; the user
                # can find the full path in their custom_modules
                # folder.
                short_msg = msg.split("\n")[0][:200]
                lines.append(f"\n• {path.name}\n    {short_msg}")
            QMessageBox.warning(
                self.w, "Custom modules",
                "\n".join(lines))
        else:
            self._status(
                f"Loaded {len(loaded)} custom module(s)")

    def _cfg_open_custom_modules_folder(self):
        """Open the user's writable custom_modules/ directory in
        the OS file manager. Creates the directory if it doesn't
        exist yet so the user lands somewhere real."""
        from . import custom_modules
        d = custom_modules.get_user_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(
                self.w, "Custom modules",
                f"Cannot create folder:\n{d}\n{e}")
            return
        # Platform-specific open-folder. Same logic the rest of
        # Quopus uses for Reveal-in-Finder / Open-in-Explorer.
        import subprocess
        if sys.platform == "win32":
            os.startfile(str(d))                  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])

    def _cfg_buttons(self):
        from .dialogs import ButtonConfigDialog
        cfg = self.w.config
        # Pass all three layers; the dialog edits them in tabs and
        # we read them back out the same way.
        dlg = ButtonConfigDialog(
            cfg["buttons"], self.w,
            buttons_shift_cfg=cfg.get("buttons_shift"),
            buttons_shift_alt_cfg=cfg.get("buttons_shift_alt"))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cfg["buttons"] = dlg.result_config()
            cfg["buttons_shift"] = dlg.result_shift_config()
            cfg["buttons_shift_alt"] = dlg.result_shift_alt_config()
            save_config(cfg); self.w._rebuild_buttons()
            self._status("Buttons saved (main + Shift + Shift+Alt layers)")

    def _cfg_file_assoc(self):
        from .file_assoc_dialog import FileAssocDialog
        from .file_assoc import ensure_default_assoc
        ensure_default_assoc(self.w.config)
        dlg = FileAssocDialog(self.w.config, self.w)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            save_config(self.w.config)
            self._status("File associations saved")

    def _cfg_drive_button_style(self):
        """Open the drive-button-style picker dialog. Six styles
        are available (Amiga drawer, floppy, HDD, pill, LED, mixed
        per drive type, or plain text). The picker shows a live
        preview of each style; clicking Apply or OK persists the
        choice to drive_button_style in quopus.cfg and re-renders
        BOTH listers' drive bars immediately - no restart needed.
        """
        from . import drive_icons
        current = self.w.config.get("drive_button_style", "amiga")

        def _apply(new_style):
            # Persist + re-render both listers' drive bars.
            self.w.config["drive_button_style"] = new_style
            save_config(self.w.config)
            for lst in (self.w.left_lister, self.w.right_lister):
                try:
                    lst._mw_config = self.w.config
                except Exception:
                    pass
                try:
                    lst.refresh_drives_bar()
                except Exception as e:
                    print(f"[cfg] refresh_drives_bar: {e}")
            self._status(
                f"Drive button style: "
                f"{drive_icons.STYLE_LABELS.get(new_style, new_style)}")

        drive_icons.open_style_picker(
            self.w, current_style=current, on_apply=_apply)

    def _cfg_appearance(self):
        """Settings dialog: app font family, scale factor (%),
        pointsize override. Live-applies via the central
        scaled_font_px() pipeline so changes show up across
        most of the UI without a restart.

        Two complementary controls:
          - **Scale (%)**: multiplies every stylesheet base size
            by this factor. 100 = original, 150 = everything 50%
            bigger, 75 = denser. Range 50..300.
          - **Pointsize override**: replaces the BASE size for
            "body text" stylesheets (the 10/11/12 px ones) so
            the user can set e.g. 14 as their personal base.
            Larger headings keep their relative differentiation.
            0 = use original base sizes.

        These two settings stack: pointsize-override sets a new
        base, scale multiplies it. The preview updates live so
        you can see what the combo looks like before clicking
        Apply or OK.

        Buttons:
          - Apply: persist + apply to the whole running app,
            keep dialog open for iteration
          - OK: same as Apply, but close
          - Cancel: revert everything to dialog-open state, close
        """
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QFontComboBox, QSpinBox, QPushButton, QCheckBox,
            QGroupBox, QSlider, QApplication)
        from PyQt6.QtGui import QFont
        from PyQt6.QtCore import Qt
        from .config import (apply_app_font, scaled_font_px,
                              refresh_all_widgets_font)

        dlg = QDialog(self.w)
        dlg.setWindowTitle("Settings - Appearance")
        dlg.resize(560, 480)

        # Snapshot original values so Cancel can revert.
        original_font = QApplication.instance().font()
        original_family = self.w.config.get("app_font_family", "")
        original_scale = self.w.config.get(
            "app_font_scale_percent", 100)
        original_override = self.w.config.get(
            "app_font_pointsize_override", 0)

        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)

        # --- Font family (covers widgets WITHOUT inline css) ----
        gb_family = QGroupBox("Default font family (widgets without inline CSS)")
        gb_family_lay = QVBoxLayout(gb_family)
        fam_row = QHBoxLayout()
        fam_row.addWidget(QLabel("Family:"))
        cmb_family = QFontComboBox()
        if original_family:
            cmb_family.setCurrentFont(QFont(original_family))
        fam_row.addWidget(cmb_family, 1)
        gb_family_lay.addLayout(fam_row)
        chk_default_family = QCheckBox(
            "Use platform default family")
        chk_default_family.setChecked(not original_family)
        chk_default_family.setToolTip(
            "When checked, only the size settings below take "
            "effect.\nThe font family stays at whatever your "
            "OS/desktop chose.\nUncheck to pick a specific "
            "family from the list above.")
        def _sync_family_enabled():
            cmb_family.setEnabled(
                not chk_default_family.isChecked())
        chk_default_family.toggled.connect(_sync_family_enabled)
        _sync_family_enabled()
        gb_family_lay.addWidget(chk_default_family)
        lay.addWidget(gb_family)

        # --- Scale slider (the main control) -------------------
        gb_scale = QGroupBox("Font scale (%)")
        gb_scale_lay = QVBoxLayout(gb_scale)
        scale_row = QHBoxLayout()
        sld_scale = QSlider(Qt.Orientation.Horizontal)
        sld_scale.setRange(50, 300)
        sld_scale.setValue(int(original_scale))
        sld_scale.setTickInterval(25)
        sld_scale.setTickPosition(QSlider.TickPosition.TicksBelow)
        sld_scale.setToolTip(
            "Multiplies every stylesheet font size by this "
            "factor.\n100% = original sizes\n"
            "125-150% = noticeably bigger\n"
            "75% = denser, for small screens")
        spin_scale = QSpinBox()
        spin_scale.setRange(50, 300)
        spin_scale.setSuffix(" %")
        spin_scale.setValue(int(original_scale))
        spin_scale.setFixedWidth(80)
        # Two-way sync between slider and spin
        sld_scale.valueChanged.connect(spin_scale.setValue)
        spin_scale.valueChanged.connect(sld_scale.setValue)
        scale_row.addWidget(sld_scale, 1)
        scale_row.addWidget(spin_scale)
        gb_scale_lay.addLayout(scale_row)
        # Quick-set buttons for common values
        quick_row = QHBoxLayout()
        for v in (75, 100, 125, 150, 175, 200):
            btn = QPushButton(f"{v}%")
            btn.setFixedWidth(56)
            btn.clicked.connect(
                lambda _checked, val=v: spin_scale.setValue(val))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        gb_scale_lay.addLayout(quick_row)
        lay.addWidget(gb_scale)

        # --- Pointsize override (advanced) ---------------------
        gb_override = QGroupBox("Body-text pointsize override (advanced)")
        gb_override_lay = QVBoxLayout(gb_override)
        ov_row = QHBoxLayout()
        ov_row.addWidget(QLabel("Override:"))
        spin_override = QSpinBox()
        spin_override.setRange(0, 30)
        spin_override.setSpecialValueText("Off (use original)")
        spin_override.setValue(int(original_override))
        spin_override.setToolTip(
            "If non-zero, replaces the BASE size for body-text "
            "stylesheets\n(those originally at 10/11/12 px). "
            "The scale% then multiplies\nthat override. Leave "
            "at 0 (Off) to use each stylesheet's own base.")
        spin_override.setSuffix(" px")
        ov_row.addWidget(spin_override)
        ov_row.addStretch(1)
        gb_override_lay.addLayout(ov_row)
        lay.addWidget(gb_override)

        # --- Live preview -------------------------------------
        gb_preview = QGroupBox("Preview")
        prev_lay = QVBoxLayout(gb_preview)
        lbl_preview = QLabel(
            "The quick brown fox jumps over the lazy dog\n"
            "0123456789  -=+_(){}[]<>!@#$%^&*\n"
            "Quopus Commander - C64 demoscene file manager")
        lbl_preview.setStyleSheet(
            "background-color: #fafafa; color: #000; "
            "border: 1px solid #888; padding: 8px;")
        prev_lay.addWidget(lbl_preview)
        lay.addWidget(gb_preview)

        def _refresh_preview():
            # Compute what the body-text size would be with
            # current settings.
            scale_v = spin_scale.value()
            ov_v = spin_override.value()
            base = ov_v if ov_v > 0 else 11
            new_px = max(6, round(base * scale_v / 100))
            if chk_default_family.isChecked():
                fam = ""
            else:
                fam = cmb_family.currentFont().family()
            css = (
                f"background-color: #fafafa; color: #000; "
                f"border: 1px solid #888; padding: 8px; "
                f"font-size: {new_px}px;")
            if fam:
                css += f' font-family: "{fam}";'
            lbl_preview.setStyleSheet(css)
        cmb_family.currentFontChanged.connect(
            lambda _f: _refresh_preview())
        spin_scale.valueChanged.connect(
            lambda _v: _refresh_preview())
        spin_override.valueChanged.connect(
            lambda _v: _refresh_preview())
        chk_default_family.toggled.connect(
            lambda _b: _refresh_preview())
        _refresh_preview()

        # --- Drive buttons -----------------------------------
        # Two controls: a checkbox to show/hide the drive bar
        # entirely, and a dropdown picking which icon style to
        # use (Amiga drawer, floppy, HDD, pill, LED, mixed, or
        # plain). Both are applied via the same _do_apply path
        # so OK persists them with the rest of the appearance
        # settings.
        from . import drive_icons as _di
        gb_drives = QGroupBox("Drive buttons (above path edit)")
        gb_drives_lay = QVBoxLayout(gb_drives)
        original_show_drives = bool(
            self.w.config.get("show_drives_bar", True))
        original_drive_style = self.w.config.get(
            "drive_button_style", "amiga")
        chk_drives = QCheckBox("Show drive buttons in each lister")
        chk_drives.setChecked(original_show_drives)
        chk_drives.setToolTip(
            "When on, each lister gets a row of buttons - one "
            "per drive / mountpoint - above the path edit. "
            "Click a button to jump to that root.")
        gb_drives_lay.addWidget(chk_drives)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Icon style:"))
        from PyQt6.QtWidgets import QComboBox
        cmb_drive_style = QComboBox()
        for key in _di.STYLES:
            cmb_drive_style.addItem(
                _di.STYLE_LABELS.get(key, key), key)
        # Select the current style
        for i in range(cmb_drive_style.count()):
            if (cmb_drive_style.itemData(i)
                    == original_drive_style):
                cmb_drive_style.setCurrentIndex(i)
                break
        cmb_drive_style.setToolTip(
            "Visual style for the drive buttons. The dedicated "
            "picker (Config -> Drive button style...) has a "
            "richer live preview if you want to see them all "
            "compared side-by-side.")
        style_row.addWidget(cmb_drive_style, 1)
        gb_drives_lay.addLayout(style_row)

        def _sync_drive_style_enabled():
            cmb_drive_style.setEnabled(chk_drives.isChecked())
        chk_drives.toggled.connect(
            lambda _b: _sync_drive_style_enabled())
        _sync_drive_style_enabled()
        lay.addWidget(gb_drives)

        lay.addStretch(1)

        # --- Bottom button row --------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_apply = QPushButton("Apply")
        btn_apply.setToolTip(
            "Apply right now to the whole app,\nkeep dialog "
            "open for iteration.")
        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setToolTip(
            "Revert any live-applied changes and close.")
        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        def _collect():
            """Push current widget state into self.w.config."""
            if chk_default_family.isChecked():
                self.w.config["app_font_family"] = ""
            else:
                self.w.config["app_font_family"] =                     cmb_family.currentFont().family()
            self.w.config["app_font_scale_percent"] =                 spin_scale.value()
            self.w.config["app_font_pointsize_override"] =                 spin_override.value()
            # Drive bar visibility + icon style (added 2026-05-29)
            self.w.config["show_drives_bar"] = bool(
                chk_drives.isChecked())
            new_style = cmb_drive_style.currentData()
            if new_style:
                self.w.config["drive_button_style"] = new_style

        def _do_apply():
            _collect()
            # Update QApplication default font (handles widgets
            # without inline CSS)
            apply_app_font(self.w.config)
            # And re-render every styled widget so the new
            # scaled_font_px() values get re-computed
            refresh_all_widgets_font()
            # Re-render both drive bars (style change) and apply
            # visibility (checkbox change). Cheap, no flash since
            # the bar sits above the file list.
            for lst in (self.w.left_lister, self.w.right_lister):
                # Keep the lister's direct config reference in
                # sync so the next render reads the new style.
                try:
                    lst._mw_config = self.w.config
                except Exception:
                    pass
                try:
                    lst.refresh_drives_bar()
                except Exception as e:
                    print(f"[cfg] refresh_drives_bar: {e}")
                try:
                    lst._apply_top_bars_visibility()
                except Exception as e:
                    print(f"[cfg] visibility: {e}")
            # Apply persists. The classical "Apply = preview,
            # OK = save" split breaks when the user closes the
            # dialog with the X (or system close) after Apply -
            # the change is live in this session but lost at
            # next start. Treating Apply as "live + persist"
            # avoids that whole class of confusion. Cancel still
            # rolls back to the dialog-open state.
            save_config(self.w.config)

        def _do_ok():
            _do_apply()
            save_config(self.w.config)
            self._status(
                f"App font: scale "
                f"{self.w.config['app_font_scale_percent']}%, "
                f"override "
                f"{self.w.config['app_font_pointsize_override']}, "
                f"family "
                f"{self.w.config['app_font_family'] or 'default'}")
            dlg.accept()

        def _do_cancel():
            # Revert everything to dialog-open state
            self.w.config["app_font_family"] = original_family
            self.w.config["app_font_scale_percent"] = original_scale
            self.w.config["app_font_pointsize_override"] =                 original_override
            # Drive bar settings: only revert if the user changed
            # them mid-dialog (via Apply), otherwise the config
            # already holds the originals.
            self.w.config["show_drives_bar"] = original_show_drives
            self.w.config["drive_button_style"] = original_drive_style
            QApplication.instance().setFont(original_font)
            refresh_all_widgets_font()
            for lst in (self.w.left_lister, self.w.right_lister):
                try:
                    lst.refresh_drives_bar()
                    lst._apply_top_bars_visibility()
                except Exception:
                    pass
            dlg.reject()

        btn_apply.clicked.connect(_do_apply)
        btn_ok.clicked.connect(_do_ok)
        btn_cancel.clicked.connect(_do_cancel)

        dlg.exec()


    def act_about(self, src, dst, param):
        from .config import CONFIG_FILE
        QMessageBox.about(self.w, "About",
            "Directory Opus 4 - PC Clone v1.0\n"
            "by lA-sTYLe/Quantum, 05/2026\n\n"
            "* Active lister has red/yellow titlebar\n"
            "* Space tags files (orange bg)\n"
            "* * inverts tags\n"
            "* Enter opens, Backspace = parent\n"
            "* Right-click lister for makedir\n"
            "* Device panel: scrollable, up to 40 devices\n"
            "* /X dump supports selected files\n"
            f"\nConfig: {CONFIG_FILE}")


    def act_license(self, src, dst, param):
        """Open the License Info dialog. Shows the current
        license state (trial / pro / lifetime), the active
        feature flags, and lets the user import a new license
        file (or remove the existing one to go back to trial).

        Same dialog whether the user is currently in trial or
        already registered - both have the same import/remove/
        refresh actions, just different displayed info."""
        from .license_ui import show_license_info_dialog
        show_license_info_dialog(parent=self.w)


    def act_quit(self, src, dst, param): self.w.close()

    # ------------------------------------------------------------------
    # PETSCII / ASCII text conversion
    # ------------------------------------------------------------------
    def act_petscii_convert(self, src, dst, param):
        """Open the ASCII<->PETSCII converter for selected/tagged files."""
        paths = src.selected_or_tagged()
        if not paths:
            self._status("No files selected for conversion")
            return
        from .petscii_dialog import PetsciiConvertDialog
        dlg = PetsciiConvertDialog([p for p in paths if p.is_file()], self.w)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src.refresh(); dst.refresh()
            self._status("Conversion done")

    # Direct non-dialog variants for power users / scripting via param:
    #   act_ascii_to_petscii / act_petscii_to_ascii with param = output extension
    def act_ascii_to_petscii(self, src, dst, param):
        self._direct_convert(src, dst, direction='a2p',
                             out_ext=param or '.pet')

    def act_petscii_to_ascii(self, src, dst, param):
        self._direct_convert(src, dst, direction='p2a',
                             out_ext=param or '.asc')

    def _direct_convert(self, src, dst, direction, out_ext):
        from .petscii_convert import ascii_to_petscii, petscii_to_ascii
        paths = [p for p in src.selected_or_tagged() if p.is_file()]
        if not paths:
            self._status("No files selected"); return
        if not out_ext.startswith('.'):
            out_ext = '.' + out_ext
        n = 0; err = []
        for p in paths:
            try:
                data = p.read_bytes()
                conv = (ascii_to_petscii(data) if direction == 'a2p'
                        else petscii_to_ascii(data))
                target = p.with_name(p.name + out_ext) if direction == 'a2p' \
                         else p.with_suffix(out_ext)
                if target.exists():
                    if QMessageBox.question(
                            self.w, "Overwrite",
                            f"{target.name} exists. Overwrite?"
                        ) != QMessageBox.StandardButton.Yes:
                        continue
                target.write_bytes(conv); n += 1
            except Exception as e:
                err.append(f"{p.name}: {e}")
        src.refresh()
        msg = f"Converted {n} file(s)"
        if err: msg += f" ({len(err)} error(s))"
        self._status(msg)
        if err:
            QMessageBox.warning(self.w, "Errors", "\n".join(err[:10]))

    def act_ftp_site(self, src, dst, param):
        """Direct-connect FTP action button. Bypasses the connect
        dialog entirely - param is a saved bookmark name and we
        connect to it directly. Useful for action buttons configured
        to one-click into a frequently-used FTP host.

        If `param` is empty or names a non-existent bookmark, fall
        through to the regular ftp connect dialog so the user can
        still set up the connection."""
        cfg = self.w.config
        bookmarks = cfg.get("ftp_bookmarks", [])
        if param:
            for bm in bookmarks:
                if bm.get('name') == param:
                    from .ftp_backend import make_backend
                    # Defensive cleanup: strip URL prefixes like
                    # 'ftp://' from the host field. Some users paste
                    # the full URL into the host box, which then
                    # fails getaddrinfo() with the prefix attached.
                    host = (bm.get('host') or '').strip()
                    for prefix in ('ftp://', 'ftps://', 'sftp://',
                                    'http://', 'https://'):
                        if host.lower().startswith(prefix):
                            host = host[len(prefix):]
                    # Strip trailing path - host should be just the
                    # hostname (or hostname:port), no path component.
                    if '/' in host:
                        host = host.split('/', 1)[0]
                    if not host:
                        QMessageBox.warning(
                            self.w, "FTP",
                            f"Bookmark '{param}' has an empty host. "
                            f"Edit it via Strg+F → Manage... and fill "
                            f"in the host field.")
                        return
                    try:
                        backend = make_backend(
                            protocol=bm.get('protocol', 'ftp'),
                            host=host,
                            port=bm.get('port', 21),
                            user=bm.get('user', 'anonymous'),
                            password=bm.get('password', ''),
                            keyfile=bm.get('keyfile'),
                        )
                        backend.connect()
                        # Optional: jump straight into a saved remote
                        # directory after connect. Failure is non-fatal -
                        # we still mount the connection at root so the
                        # user can navigate manually.
                        rpath = (bm.get('remote_path') or '').strip()
                        if rpath:
                            try:
                                backend.cwd(rpath)
                            except Exception as e:
                                self._status(
                                    f"FTP: connected, but cwd "
                                    f"'{rpath}' failed: {e}")
                        self._mount_remote_in_other_side(
                            src, dst, backend, param)
                        return
                    except Exception as e:
                        QMessageBox.warning(
                            self.w, "FTP",
                            f"Could not connect to bookmark '{param}'\n"
                            f"  host: {host}\n"
                            f"  port: {bm.get('port', 21)}\n"
                            f"  user: {bm.get('user', '?')}\n\n"
                            f"Error: {e}")
                        return
            # No matching bookmark - tell the user but don't crash
            from PyQt6.QtWidgets import QMessageBox as _QMB
            available = [b.get('name', '?') for b in bookmarks]
            avail_str = (("Available bookmarks: "
                            + ", ".join(available))
                          if available else "No bookmarks saved yet.")
            _QMB.warning(
                self.w, "FTP site",
                f"No FTP bookmark named '{param}' found.\n\n"
                f"{avail_str}\n\n"
                f"Configure this action button by right-clicking it "
                f"and entering an existing bookmark name, or use the "
                f"plain 'FTP connect' action to open the connection "
                f"dialog instead.")
            return
        # No param given - delegate to the dialog-based action
        self.act_ftp(src, dst, None)

    def act_ftp_upload(self, src, dst, param):
        """One-click FTP upload action button.

        Convention follows F5/Copy: the file selection is taken from
        the ACTIVE panel (`src` = the side that has focus / the side
        the user just tagged files on). The OTHER panel (`dst`)
        becomes the FTP destination - this matches how F5 copies from
        the active side to the inactive side.

        Behaviour:
          1. Read the selection from the ACTIVE panel (`src`) BEFORE
             anything is mounted. If nothing is selected, abort
             without connecting (no point opening a session for
             zero files).
          2. Connect to the bookmark named in `param`, then optionally
             cwd() into the bookmark's saved `remote_path`.
          3. Mount the connection onto the OTHER panel (`dst`), so
             the active panel keeps its local view + selection intact.
          4. Run the unified transfer pipeline `_transfer(src, dst)`
             so the previously-selected files from the active panel
             get uploaded into the FTP's current remote dir, with the
             standard progress dialog, conflict resolution, etc.

        Requirements:
          - `param` must name an existing FTP bookmark (same convention
            as ftp_site).
          - The ACTIVE panel must be local AND have at least one entry
            selected/tagged.
        """
        cfg = self.w.config
        bookmarks = cfg.get("ftp_bookmarks", [])

        # ----- Step 1: validate the selection on the active side -----
        if src.fs.kind != 'local':
            QMessageBox.warning(
                self.w, "FTP upload",
                "The active panel is not a local filesystem. "
                "Switch to a local panel (TAB), select the files "
                "you want to upload, then trigger the FTP upload "
                "button again.")
            return
        entries = src.selected_entries()
        if not entries:
            QMessageBox.information(
                self.w, "FTP upload",
                f"Nothing selected in the active panel "
                f"({src.side_label}). Tag or select the files you "
                f"want to upload first, then trigger the FTP upload "
                f"button.")
            return

        # ----- Step 2: resolve the bookmark -----
        if not param:
            QMessageBox.warning(
                self.w, "FTP upload",
                "This action needs a bookmark name as its param. "
                "Right-click the button and put the name of a saved "
                "FTP bookmark into the Param field.")
            return
        bm = None
        for b in bookmarks:
            if b.get('name') == param:
                bm = b; break
        if bm is None:
            available = [b.get('name', '?') for b in bookmarks]
            avail_str = (("Available bookmarks: "
                            + ", ".join(available))
                          if available else "No bookmarks saved yet.")
            QMessageBox.warning(
                self.w, "FTP upload",
                f"No FTP bookmark named '{param}' found.\n\n"
                f"{avail_str}")
            return

        # ----- Step 3: connect -----
        from .ftp_backend import make_backend
        # Defensive host cleanup (same as act_ftp_site)
        host = (bm.get('host') or '').strip()
        for prefix in ('ftp://', 'ftps://', 'sftp://',
                        'http://', 'https://'):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        if '/' in host:
            host = host.split('/', 1)[0]
        if not host:
            QMessageBox.warning(
                self.w, "FTP upload",
                f"Bookmark '{param}' has an empty host. Edit it via "
                f"Strg+F -> Manage... and fill in the host field.")
            return
        try:
            backend = make_backend(
                protocol=bm.get('protocol', 'ftp'),
                host=host,
                port=bm.get('port', 21),
                user=bm.get('user', 'anonymous'),
                password=bm.get('password', ''),
                keyfile=bm.get('keyfile'),
            )
            backend.connect()
        except Exception as e:
            QMessageBox.warning(
                self.w, "FTP upload",
                f"Could not connect to bookmark '{param}'\n"
                f"  host: {host}\n"
                f"  port: {bm.get('port', 21)}\n"
                f"  user: {bm.get('user', '?')}\n\n"
                f"Error: {e}")
            return

        # ----- Step 4: cwd into saved remote_path (non-fatal) -----
        rpath = (bm.get('remote_path') or '').strip()
        if rpath:
            try:
                backend.cwd(rpath)
            except Exception as e:
                self._status(
                    f"FTP: connected, but cwd '{rpath}' failed: {e}")

        # ----- Step 5: mount FTP into the OTHER panel + transfer -----
        # Direct mount on dst (not via _mount_remote_in_other_side,
        # which mounts on src). This way the active panel keeps its
        # local view and the file selection that's still attached
        # to its model.
        dst.set_remote_fs(backend, param)
        self._status(
            f"FTP upload: {len(entries)} file(s) "
            f"{src.side_label} -> {dst.side_label} ({param}"
            + (f":{rpath}" if rpath else "") + ")")
        self._transfer(src, dst, move=False)

    def act_qdrive(self, src, dst, param):
        """Open a Quopus Drive connect dialog and mount the
        selected drive into the OTHER lister pane.

        Optional param: a bookmark name to open directly (same
        behavior as act_qdrive_site but with the option to fall
        back to the dialog if the bookmark is missing).

        The full dialog UI isn't built yet - this action wires
        up the proven backend (TLS + HMAC-MAC auth + drive
        listing) and shows a basic picker so the feature is
        usable today. A nicer FTP-style multi-tab manager can
        replace this later without changing the action key.
        """
        from . import qdrive_backend as qd
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
            QLineEdit, QSpinBox, QPushButton, QLabel, QComboBox,
            QMessageBox, QListWidget, QListWidgetItem,
        )
        # If we have a param + a matching bookmark, just go.
        if param:
            for bm in qd.load_bookmarks():
                if bm.name == param:
                    return self._qdrive_connect_and_mount(
                        bm, dst)
            self._status(f"No Quopus Drive bookmark "
                          f"named {param!r}")
            return

        # Quick dialog: list bookmarks + "New connection" form.
        bookmarks = qd.load_bookmarks()
        dlg = QDialog(self.w)
        dlg.setWindowTitle("Quopus Drive - connect")
        dlg.resize(640, 520)
        from .palette import C, button_qss
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {C.WB_GREY}; }}")
        lay = QVBoxLayout(dlg)

        # Bookmark list
        lay.addWidget(QLabel("Saved connections:"))
        lst = QListWidget()
        for bm in bookmarks:
            label = (f"{bm.name or '(unnamed)'}  -  "
                      f"{bm.host}:{bm.port}  "
                      f"(client {bm.client_name})")
            it = QListWidgetItem(label)
            it.setData(256, bm)        # Qt.UserRole = 256
            lst.addItem(it)
        lst.setStyleSheet(
            f"QListWidget {{ background-color: white; "
            f"color: #111; }} ")
        lay.addWidget(lst, 1)

        # New-connection form
        lay.addWidget(QLabel("Or enter connection details:"))
        form_w = QFormLayout()
        ed_name = QLineEdit();      ed_name.setPlaceholderText("My remote PC")
        ed_host = QLineEdit();      ed_host.setPlaceholderText("192.168.1.42")
        ed_port = QSpinBox();       ed_port.setRange(1, 65535)
        ed_port.setValue(qd.DEFAULT_PORT)
        ed_client = QLineEdit();    ed_client.setPlaceholderText("mario-laptop")
        ed_secret = QLineEdit();    ed_secret.setPlaceholderText("64 hex chars from server setup")
        ed_secret.setEchoMode(QLineEdit.EchoMode.Password)
        ed_fp = QLineEdit();        ed_fp.setPlaceholderText("Server SHA-256 fingerprint")
        ed_mac = QLineEdit();       ed_mac.setPlaceholderText("(optional - leave empty to try all physical NICs)")
        form_w.addRow("Bookmark name", ed_name)
        form_w.addRow("Server host", ed_host)
        form_w.addRow("Server port", ed_port)
        form_w.addRow("Client name", ed_client)
        form_w.addRow("Shared secret", ed_secret)
        form_w.addRow("Cert fingerprint", ed_fp)
        form_w.addRow("Force local MAC", ed_mac)
        lay.addLayout(form_w)

        # Buttons
        bb = QHBoxLayout()
        b_connect = QPushButton("Connect")
        b_connect.setStyleSheet(button_qss("blue"))
        b_save = QPushButton("Save bookmark")
        b_save.setStyleSheet(button_qss("orange"))
        b_cancel = QPushButton("Cancel")
        bb.addWidget(b_connect); bb.addWidget(b_save)
        bb.addStretch(1); bb.addWidget(b_cancel)
        lay.addLayout(bb)

        # When a bookmark is double-clicked, prefill the form
        # so the user can review credentials before connecting.
        def fill_from_selected():
            it = lst.currentItem()
            if not it: return None
            bm = it.data(256)
            ed_name.setText(bm.name)
            ed_host.setText(bm.host)
            ed_port.setValue(bm.port)
            ed_client.setText(bm.client_name)
            ed_secret.setText(bm.secret)
            ed_fp.setText(bm.cert_fingerprint)
            ed_mac.setText(bm.forced_mac)
            return bm
        lst.itemDoubleClicked.connect(
            lambda _it: fill_from_selected())

        def collect() -> 'qd.QDriveBookmark':
            return qd.QDriveBookmark(
                name=ed_name.text().strip(),
                host=ed_host.text().strip(),
                port=int(ed_port.value()),
                client_name=ed_client.text().strip(),
                secret=ed_secret.text().strip(),
                cert_fingerprint=ed_fp.text().strip(),
                forced_mac=ed_mac.text().strip(),
            )

        def on_save():
            bm = collect()
            if not (bm.name and bm.host and bm.client_name
                    and bm.secret and bm.cert_fingerprint):
                QMessageBox.warning(
                    dlg, "Quopus Drive",
                    "Please fill in name, host, client name, "
                    "secret, and cert fingerprint before saving.")
                return
            all_bm = [b for b in qd.load_bookmarks()
                       if b.name != bm.name]
            all_bm.append(bm)
            qd.save_bookmarks(all_bm)
            self._status(f"Saved Quopus Drive bookmark {bm.name!r}")
            # Refresh list
            lst.clear()
            for b in all_bm:
                label = (f"{b.name or '(unnamed)'}  -  "
                          f"{b.host}:{b.port}  "
                          f"(client {b.client_name})")
                it = QListWidgetItem(label)
                it.setData(256, b)
                lst.addItem(it)
            # Offer to also save it as an action-button so the
            # user can one-click reconnect from then on. This
            # reuses the same cell-picker the FTP flow uses.
            reply = QMessageBox.question(
                dlg, "Action button",
                f"Bookmark {bm.name!r} saved.\n\n"
                f"Also place it on an action button so you "
                f"can reconnect with one click?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if reply == QMessageBox.StandardButton.Yes:
                self._save_ftp_as_action_button({
                    'name':         bm.name,
                    'action_label': bm.name,
                    'action_kind':  'qdrive_site',
                })

        def on_connect():
            # Connect either from the form OR from the selected
            # bookmark, whichever was edited last.
            it = lst.currentItem()
            if it and not ed_host.text().strip():
                bm = it.data(256)
            else:
                bm = collect()
            if not (bm.host and bm.client_name and bm.secret
                    and bm.cert_fingerprint):
                QMessageBox.warning(
                    dlg, "Quopus Drive",
                    "Need host, client name, secret, and "
                    "cert fingerprint to connect.")
                return
            dlg.accept()
            self._qdrive_connect_and_mount(bm, dst)

        b_connect.clicked.connect(on_connect)
        b_save.clicked.connect(on_save)
        b_cancel.clicked.connect(dlg.reject)
        dlg.exec()

    def act_qdrive_site(self, src, dst, param):
        """Direct-connect to a saved Quopus Drive bookmark. The
        param is the bookmark name. Use this as the action for
        a one-click "connect to my home PC" button.
        """
        from . import qdrive_backend as qd
        if not param:
            self._status("qdrive_site needs a bookmark name "
                          "as its Param")
            return
        for bm in qd.load_bookmarks():
            if bm.name == param:
                self._qdrive_connect_and_mount(bm, dst)
                return
        self._status(f"No Quopus Drive bookmark named {param!r}")

    def _qdrive_connect_and_mount(self, bookmark, target_lister):
        """Shared helper: connect to a Quopus Drive bookmark,
        let the user pick a drive (or use bookmark.initial_drive
        if set), and mount the resulting QDriveFs into the
        target lister pane."""
        from . import qdrive_backend as qd
        from PyQt6.QtWidgets import (
            QInputDialog, QMessageBox,
        )
        self._status(
            f"Connecting to Quopus Drive {bookmark.host}:"
            f"{bookmark.port}...")
        try:
            conn = qd.connect_with_bookmark(bookmark)
        except qd.QDriveError as e:
            QMessageBox.critical(
                self.w, "Quopus Drive",
                f"Could not connect:\n\n{e}")
            self._status("Quopus Drive connect failed")
            return
        except Exception as e:
            QMessageBox.critical(
                self.w, "Quopus Drive",
                f"Unexpected error:\n\n"
                f"{type(e).__name__}: {e}")
            self._status("Quopus Drive connect failed")
            return

        # Pick drive
        if bookmark.initial_drive:
            drive = bookmark.initial_drive
            if not any(d.get("name") == drive
                        for d in conn.drives):
                QMessageBox.warning(
                    self.w, "Quopus Drive",
                    f"Bookmark says drive {drive!r} but the "
                    f"server doesn't list it. Available: "
                    f"{', '.join(d['name'] for d in conn.drives)}")
                conn.close()
                return
        elif len(conn.drives) == 1:
            drive = conn.drives[0]["name"]
        else:
            names = [d["name"] for d in conn.drives]
            drive, ok = QInputDialog.getItem(
                self.w, "Quopus Drive",
                f"Connected to {conn.server_name or bookmark.host}.\n"
                f"Pick a drive to mount:",
                names, 0, False)
            if not ok or not drive:
                conn.close()
                return

        # Stash the connection on the lister so it survives
        # past this function (otherwise the QDriveFs holds a
        # weakref-style reference and the socket gets GC'd).
        # The lister's existing remote-mount path (FTP/Rclone)
        # uses the same convention.
        #
        # Honor bookmark.initial_path if set so that the right-
        # click "Save current dir as default" feature has a way
        # to make qdrive_site land somewhere specific. If the
        # path doesn't exist on the server (drive layout has
        # changed, etc.) we silently fall back to "/" - that's
        # the normal behavior of cd() in QDriveFs.
        start = getattr(bookmark, 'initial_path', '') or "/"
        fs = qd.QDriveFs(conn, drive, start_path=start)
        # Defensive: if start was a stale path, do a probe-cd
        # back to "/" before returning so the lister doesn't
        # display garbage. cd() validates by issuing a list.
        if start != "/":
            try:
                fs.cd(start)
            except (NotADirectoryError, qd.QDriveError):
                # Stale path - fall back to root
                try:
                    fs.cd("/")
                except Exception:
                    pass
        target_lister._qdrive_connection = conn   # keep alive
        try:
            # Reuse the same path the FTP backend uses for
            # mounting: replace the lister's fs attribute and
            # trigger a refresh.
            target_lister.fs = fs
            target_lister.current_path = fs.current_path
            try:
                target_lister.path_edit.setText(fs.display_path())
            except AttributeError:
                pass
            target_lister.refresh()
            self._status(
                f"Mounted qdrive://{bookmark.host}/{drive}")
        except Exception as e:
            conn.close()
            QMessageBox.critical(
                self.w, "Quopus Drive",
                f"Mount failed:\n{e}")

    def act_ftp(self, src, dst, param):
        """Open an FTP/FTPS/SFTP connection dialog and mount the result
        into the OTHER lister pane (so copy/move/view work between local
        and remote via F5/F6 like Total Commander).

        Optional param: a bookmark name to open directly.
        """
        from .ftp_browser import FtpConnectDialog, FtpBookmarkManagerDialog
        from .ftp_backend import make_backend
        from PyQt6.QtWidgets import QDialog
        cfg = self.w.config

        def connect_from_kwargs(kw):
            try:
                backend = make_backend(
                    protocol=kw.get('protocol', 'ftp'),
                    host=kw.get('host', ''),
                    port=kw.get('port', 21),
                    user=kw.get('user', 'anonymous'),
                    password=kw.get('password', ''),
                    keyfile=kw.get('keyfile'),
                )
                backend.connect()
                # Optional: jump straight into a saved remote directory
                # after connect. Failure is non-fatal - we still mount
                # the connection at root so the user can navigate.
                rpath = (kw.get('remote_path') or '').strip()
                if rpath:
                    try:
                        backend.cwd(rpath)
                    except Exception as e:
                        self._status(
                            f"FTP: connected, but cwd "
                            f"'{rpath}' failed: {e}")
                return backend
            except Exception as e:
                QMessageBox.warning(self.w, "FTP", f"Connection failed:\n{e}")
                return None

        # If param names a saved bookmark, connect directly
        if param:
            for bm in cfg.get("ftp_bookmarks", []):
                if bm.get('name') == param:
                    backend = connect_from_kwargs(bm)
                    if backend:
                        self._mount_remote_in_other_side(
                            src, dst, backend, param)
                    return
            # fall through to dialog

        # Show connect dialog with Manage support
        while True:
            bookmarks = cfg.get("ftp_bookmarks", [])
            dlg = FtpConnectDialog(bookmarks, self.w)
            dlg.exec()
            if dlg.open_manager_after:
                mgr = FtpBookmarkManagerDialog(cfg, self.w)
                if mgr.exec() == QDialog.DialogCode.Accepted and mgr.connect_choice:
                    bm = mgr.connect_choice
                    backend = connect_from_kwargs(bm)
                    if backend:
                        self._mount_remote_in_other_side(
                            src, dst, backend,
                            bm.get('name', bm.get('host', 'ftp')))
                    return
                continue
            if dlg.result_kwargs is None:
                return
            kw = dlg.result_kwargs
            # Save bookmark if name provided
            if kw.get('name'):
                from .ftp_browser import _save_bookmark
                _save_bookmark(cfg, kw, self.w)
            # User clicked "Save Bookmark" only - don't connect now.
            # The bookmark has been saved above; we're done.
            if kw.get('save_bookmark_only'):
                msg_parts = [f"Saved FTP bookmark: {kw.get('name', '?')}"]
                # Even when not connecting, we still honour the
                # add-as-drive-button and add-as-action-button
                # checkboxes - the user may want to set up the entry
                # for later use without opening a session right now.
                if kw.get('add_as_drive_button'):
                    self._save_ftp_as_drive_button(kw)
                    msg_parts.append("+ drive button")
                if kw.get('add_as_action_button'):
                    if self._save_ftp_as_action_button(kw):
                        msg_parts.append("+ action button")
                if kw.get('add_as_upload_button'):
                    # Build a sibling kw dict that asks
                    # _save_ftp_as_action_button to create a button
                    # using the 'ftp_upload' action instead of the
                    # default 'ftp_site'. The label comes from the
                    # separate 'upload_label' field in the dialog.
                    upload_kw = dict(kw)
                    upload_kw['action_kind'] = 'ftp_upload'
                    upload_kw['action_label'] = kw.get('upload_label', '')
                    if self._save_ftp_as_action_button(upload_kw):
                        msg_parts.append("+ upload button")
                self._status("  ·  ".join(msg_parts))
                return
            backend = connect_from_kwargs(kw)
            if backend:
                label = kw.get('name') or f"{kw['protocol']}://{kw['host']}"
                self._mount_remote_in_other_side(src, dst, backend, label)
                # Optional: also add this connection as a drive button
                # in the left panel. Triggered by the "also add as
                # drive button" checkbox in the connect dialog.
                if kw.get('add_as_drive_button'):
                    self._save_ftp_as_drive_button(kw)
                # Same for action-button.
                if kw.get('add_as_action_button'):
                    self._save_ftp_as_action_button(kw)
                # Upload-button: build a tweaked kw with action_kind
                # = 'ftp_upload' so a click on it triggers the new
                # one-shot upload flow rather than just connecting.
                if kw.get('add_as_upload_button'):
                    upload_kw = dict(kw)
                    upload_kw['action_kind'] = 'ftp_upload'
                    upload_kw['action_label'] = kw.get('upload_label', '')
                    self._save_ftp_as_action_button(upload_kw)
            return

    def _mount_remote_in_other_side(self, src, dst, backend, label):
        """Mount the remote FS into the ACTIVE lister (src = the side whose
        button/hotkey triggered the action). The other side (dst) stays
        local so copy/move between local<->remote via F5/F6 works."""
        target = src
        target.set_remote_fs(backend, label)
        self._status(f"FTP mounted on {target.side_label}: {label}")

    def _save_ftp_as_drive_button(self, kw: dict):
        """Persist an FTP connection as a drive button in the device
        column. Called when the user ticked 'also add as drive button'
        in the connect dialog. Saves the connection details (without
        the password by default) so a single click recreates the
        connection later."""
        from PyQt6.QtWidgets import QMessageBox
        cfg = self.w.config
        devs = cfg.setdefault('drives', [])
        if len(devs) >= 40:
            QMessageBox.information(
                self.w, "Drive button",
                "Maximum 40 drive buttons reached.")
            return
        label = kw.get('drive_label', '').strip() or \
                kw.get('host', 'FTP').upper()
        # Build the device dict in the same format the device panel
        # uses for FTP entries (matches _FtpBookmarkDialog.result_dict)
        entry = {
            "type":  "ftp",
            "label": label,
            "host":  kw.get('host', ''),
            "port":  int(kw.get('port', 21)),
            "user":  kw.get('user', 'anonymous'),
            "path":  "/",
            "mode":  "passive",
        }
        # Don't auto-save passwords by default - prompt at connect time.
        # If the user really wants the password persisted they can edit
        # the bookmark via right-click and tick the checkbox there.
        devs.append(entry)
        cfg['drives'] = devs
        # Refresh UI + persist
        try:
            self.w.device_column.devices = devs
            self.w.device_column._rebuild()
        except Exception:
            pass
        from .config import save_config
        save_config(cfg)
        self._status(
            f"Added FTP drive button: {label} -> {entry['host']} "
            f"({len(devs)}/40)")

    def _save_ftp_as_action_button(self, kw: dict) -> bool:
        """Persist an FTP bookmark as an action button in the 6x6
        button grid. Asks the user which empty cell to put it in via
        a small picker dialog. Returns True if a button was placed,
        False if the user cancelled or no slot was free.

        The button uses the 'ftp_site' action with the bookmark name
        as its param, so a click reconnects to that bookmark via the
        saved-bookmarks list."""
        from PyQt6.QtWidgets import (
            QMessageBox, QDialog, QGridLayout, QPushButton, QVBoxLayout,
            QLabel, QDialogButtonBox)
        cfg = self.w.config
        bm_name = kw.get('name', '').strip()
        if not bm_name:
            QMessageBox.warning(
                self.w, "Action button",
                "An action button needs a saved bookmark name to "
                "connect to. Fill the 'Save as:' field first.")
            return False
        # Pick a layer (Main / Shift / Shift+Alt). Default = Main;
        # offer the others if Main is full so the user has somewhere
        # to put it.
        main_grid = cfg.get('buttons', [[None]*6 for _ in range(6)])
        shift_grid = cfg.get('buttons_shift',
                              [[None]*6 for _ in range(6)])
        shift_alt_grid = cfg.get('buttons_shift_alt',
                                  [[None]*6 for _ in range(6)])
        main_free = sum(1 for r in main_grid for c in r if not c)
        shift_free = sum(1 for r in shift_grid for c in r if not c)
        shift_alt_free = sum(1 for r in shift_alt_grid
                             for c in r if not c)
        if main_free == 0 and shift_free == 0 and shift_alt_free == 0:
            QMessageBox.information(
                self.w, "Action button",
                "All three button grids (main / Shift / Shift+Alt) "
                "are full. Free a cell first via Config -> Action "
                "buttons...")
            return False
        # Cell-picker dialog: show a 6x6 grid of buttons - empty cells
        # are click-to-pick, occupied cells are disabled and show
        # their current label so the user can see what they'd
        # overwrite.
        target_label = (kw.get('action_label', '').strip()
                          or bm_name)
        # Action kind: ftp_site (default - connect+browse) or
        # ftp_upload (connect + cwd + upload selection from other
        # panel) or qdrive_site (Quopus Drive direct-connect).
        # The connect dialog passes 'action_kind' along based
        # on the user's choice in the action-button selector.
        action_kind = kw.get('action_kind', 'ftp_site')
        if action_kind not in ('ftp_site', 'ftp_upload',
                                'qdrive_site'):
            action_kind = 'ftp_site'
        chosen = self._pick_grid_cell(
            main_grid, shift_grid,
            target_label, bm_name, action_kind,
            shift_alt_grid=shift_alt_grid)
        if chosen is None:
            return False
        layer_name, r, c = chosen
        if layer_name == 'main':
            target_grid = main_grid
        elif layer_name == 'shift':
            target_grid = shift_grid
        else:
            target_grid = shift_alt_grid
        # Compose the new entry. Color = blue (matches navigation)
        # unless the cell already had one we want to keep.
        existing = target_grid[r][c]
        color = (existing.get('color')
                  if existing else 'orange')
        target_grid[r][c] = {
            'label':  target_label,
            'action': action_kind,
            'color':  color,
            'param':  bm_name,
        }
        # Persist + rebuild grid
        if layer_name == 'main':
            cfg['buttons'] = target_grid
        elif layer_name == 'shift':
            cfg['buttons_shift'] = target_grid
        else:
            cfg['buttons_shift_alt'] = target_grid
        from .config import save_config
        save_config(cfg)
        try:
            self.w._rebuild_buttons()
        except Exception:
            pass
        self._status(
            f"Added FTP action button: {target_label} -> {bm_name} "
            f"(at ({r+1},{c+1}) {layer_name} layer)")
        return True

    def _pick_grid_cell(self, main_grid, shift_grid, new_label, bm_name,
                          action_kind='ftp_site',
                          shift_alt_grid=None):
        """Modal cell-picker dialog. Returns (layer, r, c) where
        layer is 'main', 'shift', or 'shift_alt', or None if
        cancelled. shift_alt_grid is optional for backwards
        compatibility - callers that haven't been updated to pass
        it get an empty 6x6 grid for the third tab."""
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
            QPushButton, QDialogButtonBox, QTabWidget, QWidget,
            QMessageBox)
        if shift_alt_grid is None:
            shift_alt_grid = [[None]*6 for _ in range(6)]
        dlg = QDialog(self.w)
        dlg.setWindowTitle(f"Pick a cell for: {new_label}")
        dlg.setModal(True)
        dlg.resize(640, 420)
        from .palette import C, button_qss
        dlg.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")
        outer = QVBoxLayout(dlg)
        hdr = QLabel(
            f"  Click an EMPTY cell to place the new button.\n"
            f"  Action: {action_kind}  ·  Param: {bm_name}\n"
            f"  Cells with text are already in use - click them only "
            f"if you want to OVERWRITE that button.  ")
        hdr.setStyleSheet(
            f"QLabel {{ background-color: {C.WB_GREY}; "
            f"color: #333; padding: 6px; }}")
        outer.addWidget(hdr)
        # Tabs: one per layer.
        tabs = QTabWidget()
        result = {'layer': None, 'r': -1, 'c': -1}
        def _make_grid_widget(layer_name, grid):
            w = QWidget()
            gl = QGridLayout(w); gl.setSpacing(2)
            for r in range(6):
                for c in range(6):
                    cur = grid[r][c]
                    if cur:
                        btn = QPushButton(
                            f"({r+1},{c+1})\n{cur.get('label','')[:8]}")
                        btn.setStyleSheet(button_qss(
                            cur.get('color', 'mid')))
                        btn.setToolTip(
                            f"Currently: {cur.get('label','')} "
                            f"[{cur.get('action','')}]\n"
                            f"Click to OVERWRITE with the new button.")
                    else:
                        btn = QPushButton(f"({r+1},{c+1})\n[empty]")
                        btn.setStyleSheet(button_qss('mid'))
                        btn.setToolTip(
                            f"Empty cell - click to place "
                            f"'{new_label}' here.")
                    btn.setMinimumHeight(50)
                    btn.clicked.connect(
                        lambda chk, ln=layer_name, rr=r, cc=c, b=cur:
                            self._on_cell_picked(dlg, result, ln, rr, cc, b))
                    gl.addWidget(btn, r, c)
            return w
        tabs.addTab(_make_grid_widget('main', main_grid), "Main layer")
        tabs.addTab(_make_grid_widget('shift', shift_grid),
                     "Shift-layer")
        tabs.addTab(_make_grid_widget('shift_alt', shift_alt_grid),
                     "Shift+Alt-layer")
        outer.addWidget(tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(dlg.reject)
        outer.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return (result['layer'], result['r'], result['c'])

    def _on_cell_picked(self, dlg, result, layer, r, c, current):
        """Confirm overwrite if the cell is occupied, then accept."""
        from PyQt6.QtWidgets import QMessageBox
        if current:
            reply = QMessageBox.question(
                dlg, "Overwrite?",
                f"Cell ({r+1},{c+1}) is currently:\n"
                f"  '{current.get('label','')}' "
                f"[{current.get('action','')}]\n\n"
                f"Replace it with the new FTP action button?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        result['layer'] = layer
        result['r'] = r
        result['c'] = c
        dlg.accept()

    # ------------------------------------------------------------------
    # External script / shell command button actions
    # ------------------------------------------------------------------
    @staticmethod
    def _quote_path_for_shell(p: str) -> str:
        """Quote a path so a subsequent shlex.split / shell parse
        gets it back as one token, even if it contains spaces or
        other shell-special characters.

        On POSIX we use shlex.quote() (handles spaces, quotes,
        $, *, etc. - the full shell quoting mess).
        On Windows, double-quote the path and escape any embedded
        double-quotes by doubling them. Native-Windows file paths
        can't actually contain " (the OS rejects it), but we cover
        it for forward-compat / network paths.
        """
        if not p:
            return '""' if os.name == 'nt' else "''"
        if os.name == 'nt':
            return '"' + p.replace('"', '""') + '"'
        import shlex as _shlex
        return _shlex.quote(p)

    def _maybe_prompt_for_input(self, param: str) -> str | None:
        """If `param` contains the %i token, pop a dialog asking the
        user to type a filename / string. Returns the user-supplied
        value (already shell-quoted), or None if the user cancelled.

        If the user leaves the field empty we auto-generate a name
        based on the current date+time+seconds so a button like
        'ef3usb r %i.d64' always produces SOMETHING readable
        without forcing the user to come up with a name.

        Returns "" (not None) if %i is not in the param at all -
        callers use that as 'no prompt needed, skip me'.
        """
        if "%i" not in param:
            return ""
        # Default suggestion: YYYYMMDD-HHMMSS so multiple captures
        # don't overwrite each other and the file order matches
        # the capture order alphabetically.
        from datetime import datetime
        default_name = datetime.now().strftime("%Y%m%d-%H%M%S")
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(
            self.w, "Enter filename",
            "Filename for the command (leave empty to auto-name):",
            text=default_name)
        if not ok:
            return None    # cancelled
        text = text.strip()
        if not text:
            text = default_name
        return self._quote_path_for_shell(text)

    def _substitute_tokens(self, s, src, dst, input_value=None):
        """
        Replace tokens in a command / arg string:
          %f  - first selected/tagged file (full path, QUOTED for shell)
          %F  - space-separated list of ALL selected/tagged files (each quoted)
          %n  - first selected file (basename only, QUOTED for shell)
          %p  - current source directory (QUOTED for shell)
          %d  - current destination (other side) directory (QUOTED for shell)
          %i  - user-supplied input string (QUOTED for shell). The caller
                must pre-collect the value via _maybe_prompt_for_input
                and pass it in via input_value, otherwise %i expands to
                empty.
          %%  - literal %

        All path tokens are shell-quoted on substitution so paths
        with spaces (e.g. "/home/soenke/combat school/...") survive
        the subsequent shlex.split. Earlier versions of this code
        substituted raw paths and broke any time a folder name had
        a space in it.
        """
        sel = src.selected_or_tagged() if src else []
        first_full = self._quote_path_for_shell(
            str(sel[0])) if sel else ""
        first_base = self._quote_path_for_shell(
            sel[0].name) if sel else ""
        all_quoted = " ".join(
            self._quote_path_for_shell(str(p)) for p in sel)
        src_dir = self._quote_path_for_shell(
            str(src.current_path)) if src else ""
        dst_dir = self._quote_path_for_shell(
            str(dst.current_path)) if dst else ""
        # %i value already pre-quoted by _maybe_prompt_for_input.
        ivalue = input_value or ""

        out = []
        i = 0
        while i < len(s):
            if s[i] == '%' and i + 1 < len(s):
                tok = s[i + 1]
                if   tok == 'f': out.append(first_full)
                elif tok == 'F': out.append(all_quoted)
                elif tok == 'n': out.append(first_base)
                elif tok == 'p': out.append(src_dir)
                elif tok == 'd': out.append(dst_dir)
                elif tok == 'i': out.append(ivalue)
                elif tok == '%': out.append('%')
                else:
                    out.append(s[i:i+2])  # unknown token, pass through
                i += 2
            else:
                out.append(s[i]); i += 1
        return ''.join(out)

    def act_external_script(self, src, dst, param):
        """
        Run an external program / script file.
        The button 'param' holds:  <program_path>  [optional args...]
        Tokens (%f, %F, %n, %p, %d) are substituted.

        Per-button options (set in the Button Configuration dialog):
            Show     - capture stdout/stderr in a Quopus output window
                        instead of running detached. Useful for tools
                        like 'unp64 %f' whose output is the whole point.
            Refresh  - re-read both panels after the command finishes.

        Example param values:
            C:\\tools\\myscript.bat %f
            python C:\\scripts\\process.py %p %F
            /usr/local/bin/upload.sh %F
        """
        if not param:
            QMessageBox.information(self.w, "External Script",
                "This button has no script configured.\n\n"
                "Right-click the button (or open Config → Action buttons) "
                "and set 'param' to the script path with optional arguments.\n\n"
                "Tokens:\n"
                "  %f = first selected file\n"
                "  %F = all selected files (quoted)\n"
                "  %n = basename of first selected\n"
                "  %p = current directory\n"
                "  %d = other-side directory\n"
                "  %i = ask user for filename / string before running")
            return

        opts = self._current_opts or {}
        show_output = bool(opts.get("show_output"))
        refresh_after = bool(opts.get("refresh_after"))
        in_terminal = bool(opts.get("in_terminal"))

        ivalue = self._maybe_prompt_for_input(param)
        if ivalue is None:
            self._status("Cancelled")
            return
        substituted = self._substitute_tokens(
            param, src, dst, input_value=ivalue)
        # Split into program + args using shlex so quoted paths survive
        try:
            parts = shlex.split(substituted, posix=(os.name != 'nt'))
        except ValueError as e:
            QMessageBox.warning(self.w, "External Script",
                                f"Cannot parse command:\n{e}")
            return
        # Windows quoting fix: see the matching block in act_run -
        # shlex.split(posix=False) keeps surrounding "..." quotes
        # attached to the token, which Popen then passes literally
        # to the child app, breaking apps like notepad that see a
        # filename starting with `"`. Strip leading+trailing quote
        # pairs so the path arrives clean.
        if os.name == 'nt':
            parts = [
                (p[1:-1] if len(p) >= 2
                             and p[0] == '"' and p[-1] == '"'
                          else p)
                for p in parts]
        if not parts:
            self._status("External Script: empty command")
            return

        try:
            # Determine a sensible working directory:
            # - If the first token looks like a script (python foo.py),
            #   use the script's directory
            # - Else use the program's own directory
            # - Fallback: Quopus current path
            from pathlib import Path as _P
            run_cwd = None
            for tok in parts:
                tp = _P(tok)
                if tp.suffix.lower() in (".py", ".pyw", ".sh", ".bat", ".cmd",
                                         ".ps1", ".pl", ".rb", ".js") \
                   and tp.is_absolute() and tp.parent.is_dir():
                    run_cwd = str(tp.parent); break
            if run_cwd is None:
                pp = _P(parts[0])
                if pp.is_absolute() and pp.parent.is_dir():
                    run_cwd = str(pp.parent)
            if run_cwd is None and src is not None:
                run_cwd = str(src.current_path)

            on_done = (self._refresh_both_panels
                        if refresh_after else None)
            if in_terminal:
                # Real terminal window - needed for interactive
                # programs (telnet, ssh, vim, REPLs).
                self._spawn_in_terminal(parts, cwd=run_cwd)
                self._status(f"Launched in terminal: {parts[0]}")
                # We have no reliable signal for "user closed the
                # terminal", so fire the refresh immediately if asked.
                if refresh_after:
                    self._refresh_both_panels()
            elif show_output:
                self._run_with_output_dialog(
                    parts, cwd=run_cwd,
                    title=f"External Script: {parts[0]}",
                    on_finished=on_done)
                self._status(f"Running: {parts[0]}")
            else:
                self._spawn_detached(parts, cwd=run_cwd)
                self._status(f"Launched: {parts[0]} (cwd={run_cwd})")
                # No output dialog means we don't know when it finishes.
                # Refresh fires immediately - the user can re-trigger
                # F2 if they need a later refresh.
                if refresh_after:
                    self._refresh_both_panels()
        except FileNotFoundError:
            QMessageBox.warning(self.w, "External Script",
                                f"Program not found:\n{parts[0]}")
        except Exception as e:
            QMessageBox.warning(self.w, "External Script", str(e))

    def act_execute_command(self, src, dst, param):
        """
        Run a shell command line via the system shell (cmd on Windows,
        /bin/sh on Unix). Tokens are substituted.

        Per-button options (set in the Button Configuration dialog):
            Show     - capture stdout/stderr in a Quopus output window.
            Refresh  - re-read both panels after the command finishes.

        Useful when you need shell features (pipes, redirection, env vars).
        Example param:
            dir %p > %d\\listing.txt
            cat %F | gzip > %d/files.gz
            explorer %p
        """
        if not param:
            QMessageBox.information(self.w, "Execute Command",
                "This button has no command configured.\n\n"
                "Right-click the button (or open Config → Action buttons) "
                "and set 'param' to a shell command.\n\n"
                "Tokens:\n"
                "  %f = first selected file\n"
                "  %F = all selected files (quoted)\n"
                "  %n = basename of first selected\n"
                "  %p = current directory\n"
                "  %d = other-side directory\n"
                "  %i = ask user for filename / string before running\n\n"
                "This runs through the system shell, so pipes, redirection "
                "and env vars work.")
            return

        opts = self._current_opts or {}
        show_output = bool(opts.get("show_output"))
        refresh_after = bool(opts.get("refresh_after"))
        in_terminal = bool(opts.get("in_terminal"))

        ivalue = self._maybe_prompt_for_input(param)
        if ivalue is None:
            self._status("Cancelled")
            return
        substituted = self._substitute_tokens(
            param, src, dst, input_value=ivalue)
        try:
            cwd = str(src.current_path) if src else None
            on_done = (self._refresh_both_panels
                        if refresh_after else None)
            if in_terminal:
                # The whole substituted line is a shell command;
                # _spawn_in_terminal hands it to bash inside the
                # terminal, so pipes/redirection work as expected.
                self._spawn_in_terminal(substituted, cwd=cwd)
                short = (substituted if len(substituted) < 60
                          else substituted[:57] + "...")
                self._status(f"Launched in terminal: {short}")
                if refresh_after:
                    self._refresh_both_panels()
            elif show_output:
                self._run_with_output_dialog(
                    substituted, cwd=cwd, shell=True,
                    title=f"Execute: {substituted[:60]}",
                    on_finished=on_done)
                short = (substituted if len(substituted) < 60
                          else substituted[:57] + "...")
                self._status(f"Running: {short}")
            else:
                self._spawn_detached(substituted, shell=True, cwd=cwd)
                short = (substituted if len(substituted) < 60
                          else substituted[:57] + "...")
                self._status(f"Exec: {short}")
                if refresh_after:
                    self._refresh_both_panels()
        except Exception as e:
            QMessageBox.warning(self.w, "Execute Command", str(e))

    # ------------------------------------------------------------------

    def act_assign(self, src, dst, param):
        QMessageBox.information(self.w, "Assign",
            "Right-click any device button to edit label and path.\n"
            "Click + at bottom of device panel to add.")

    def act_comment(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: return
        p = paths[0]
        side = p.with_suffix(p.suffix + ".comment")
        current = side.read_text(encoding="utf-8") if side.exists() else ""
        text, ok = QInputDialog.getMultiLineText(self.w, "Comment",
            f"Comment for {p.name}:", current)
        if ok:
            if text.strip():
                side.write_text(text, encoding="utf-8"); self._status("Saved")
            elif side.exists():
                side.unlink(); self._status("Removed")

    def act_datestamp(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: return
        now = datetime.now().timestamp()
        for p in paths:
            try: os.utime(p, (now, now))
            except Exception as e: QMessageBox.warning(self.w, "Datestamp", f"{p.name}: {e}")
        src.refresh(); self._status(f"Datestamped {len(paths)}")

    def act_protect(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: return
        flags, ok = QInputDialog.getText(self.w, "Protect",
            "Amiga flags HSPARWED: R=Read W=Write E=Execute D=Delete:", text="RWED")
        if not ok: return
        mode = 0; fu = flags.upper()
        if "R" in fu: mode |= stmod.S_IRUSR | stmod.S_IRGRP | stmod.S_IROTH
        if "W" in fu: mode |= stmod.S_IWUSR
        if "E" in fu: mode |= stmod.S_IXUSR | stmod.S_IXGRP | stmod.S_IXOTH
        for p in paths:
            try: os.chmod(p, mode)
            except Exception as e: QMessageBox.warning(self.w, "Protect", f"{p.name}: {e}")
        src.refresh(); self._status(f"Protected {len(paths)}")

    def act_checkfit(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: self._status("Nothing selected"); return
        total = 0
        for p in paths:
            if p.is_file(): total += p.stat().st_size
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: total += sub.stat().st_size
                        except Exception: pass
        try:
            free = shutil.disk_usage(dst.current_path).free
            QMessageBox.information(self.w, "Check Fit",
                f"Selected: {fmt_size(total)}\n"
                f"Free on dest: {fmt_size(free)}\n\n"
                f"{'FITS' if total <= free else 'DOES NOT FIT'}")
        except Exception as e:
            QMessageBox.warning(self.w, "Check Fit", str(e))

    def act_getsizes(self, src, dst, param):
        paths = src.selected_or_tagged()
        if not paths: self._status("Nothing selected"); return
        lines = []; grand = 0
        for p in paths:
            sz = 0
            if p.is_file(): sz = p.stat().st_size
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        try: sz += sub.stat().st_size
                        except Exception: pass
            grand += sz
            lines.append(f"{fmt_size(sz):>10}  {p.name}")
        lines.append("-" * 50)
        lines.append(f"{fmt_size(grand):>10}  TOTAL")
        self._show_text("GetSizes", "\n".join(lines))

    def act_edit(self, src, dst, param):
        """F4 / Edit - dispatches through the file-association system.
        User can configure internal TextReader or any external editor."""
        src._edit_selected()

    def act_buffers(self, src, dst, param):
        dlg = BuffersDialog(self.w.left_lister, self.w.right_lister,
                            self.w.buffers, self.w)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.selected_path and dlg.target == 'left':
                self.w.left_lister.goto(dlg.selected_path)
            elif dlg.selected_path and dlg.target == 'right':
                self.w.right_lister.goto(dlg.selected_path)

    def act_dir_reverse(self, src, dst, param):
        # Pass currently selected/tagged files for single-file /X dump
        sel = src.selected_or_tagged()
        dlg = DirReverseDialog(src.current_path, selected_files=sel, parent=self.w)
        dlg.exec()

    def act_custom_cmd(self, src, dst, param):
        if not param:
            param, ok = QInputDialog.getText(self.w, "Custom", "Command (use {file}):")
            if not ok: return
        paths = src.selected_or_tagged()
        fname = str(paths[0]) if paths else ""
        cmd = param.replace("{file}", f'"{fname}"')
        try:
            self._spawn_detached(cmd, shell=True,
                                  cwd=str(src.current_path))
            self._status(f"Ran: {param}")
        except Exception as e:
            QMessageBox.warning(self.w, "Custom", str(e))


# ========================================================
# Module-level helpers callable from outside ActionDispatcher
# ========================================================

def open_u64_config_dialog(parent, cfg):
    """Open the Ultimate-64 device config dialog and persist any
    changes the user accepts. Module-level so it can be invoked
    from places that don't have an ActionDispatcher instance -
    notably u64_devices.pick_device() when it discovers no
    devices are configured yet and wants to offer "Configure
    now" right in the warning dialog.

    Mirrors ActionDispatcher.act_u64_config but takes the
    parent widget and config dict explicitly. No status-bar
    update here because this helper doesn't own the main
    window's status bar; callers that have it should do their
    own showMessage() afterward.
    """
    from .u64_streamer import (
        U64ConfigDialog,
        PORT_VIDEO, PORT_AUDIO, PORT_TELNET, PORT_HTTP,
    )
    from .u64_devices import (
        get_active_device, sync_legacy_keys,
    )
    # Seed the dialog with whatever the currently-active device
    # looks like. The dialog re-reads the full device list from
    # cfg internally and shows all slots, so these constructor
    # args really only matter for the brand-new case where no
    # devices exist yet.
    active = get_active_device(cfg) or {}
    host = active.get("host", "") or ""
    video_port = int(active.get("video_port", PORT_VIDEO))
    audio_port = int(active.get("audio_port", PORT_AUDIO))
    telnet_port = int(active.get("telnet_port", PORT_TELNET))
    http_port = int(active.get("http_port", PORT_HTTP))
    password = active.get("password", "") or ""
    video_only = bool(active.get("video_only", False))
    always_on_top = bool(active.get("always_on_top", False))
    screenshot_dir = cfg.get("u64_screenshot_dir", "")
    dlg = U64ConfigDialog(
        host=host,
        video_port=video_port,
        audio_port=audio_port,
        telnet_port=telnet_port,
        http_port=http_port,
        password=password,
        video_only=video_only,
        always_on_top=always_on_top,
        screenshot_dir=screenshot_dir,
        parent=parent)
    from PyQt6.QtWidgets import QDialog
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    v = dlg.values()
    # Persist the new device list + active index. The dialog's
    # values() already mirrored the active device into the
    # legacy keys; we also call sync_legacy_keys() to be sure
    # in case the active index was changed.
    if 'u64_devices' in v:
        cfg['u64_devices'] = v['u64_devices']
    if 'u64_active_device' in v:
        cfg['u64_active_device'] = v['u64_active_device']
    cfg['u64_host']           = v['u64_host']
    cfg['u64_video_port']     = v['u64_video_port']
    cfg['u64_audio_port']     = v['u64_audio_port']
    cfg['u64_telnet_port']    = v['u64_telnet_port']
    cfg['u64_http_port']      = v['u64_http_port']
    cfg['u64_password']       = v['u64_password']
    cfg['u64_video_only']     = v['u64_video_only']
    cfg['u64_always_on_top']  = v['u64_always_on_top']
    cfg['u64_screenshot_dir'] = v['u64_screenshot_dir']
    try:
        sync_legacy_keys(cfg)
    except Exception:
        pass
    from .config import save_config
    save_config(cfg)
