"""File associations: map extensions to internal/external viewer+editor.

Each extension has up to two handlers:
  - viewer (F3 / read):     internal (auto-route) or external program
  - editor (F4 / edit):     internal (internal TextReader with edit flag,
                             currently read-only) or external program

External entries:
  {
    "mode": "external",
    "program": "C:/Program Files/Notepad++/notepad++.exe",
    "args": ["%f"],    # %f = file path; may include other flags before/after
  }

Internal entries:
  {
    "mode": "internal",
    "type": "auto"|"text"|"image"|"archive"|"hex"
  }

Stored in main config under "file_assoc" key:
  {
    ".txt": { "viewer": {...}, "editor": {...} },
    ".asm": { ... },
    ...
    "*":   { ... }           # fallback/default
  }
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


# Default associations: common extensions route to sensible internal handlers.
# User can override any of these via the FileAssocDialog.
DEFAULT_ASSOC = {
    # Fallback for unknown
    "*": {
        "viewer": {"mode": "internal", "type": "auto"},
        "editor": {"mode": "internal", "type": "text"},
    },
    # Text / source
    ".txt": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".md":  {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".asm": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".c":   {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".py":  {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    # Images
    ".png":  {"viewer": {"mode": "internal", "type": "image"},
              "editor": {"mode": "internal", "type": "image"}},
    ".jpg":  {"viewer": {"mode": "internal", "type": "image"},
              "editor": {"mode": "internal", "type": "image"}},
    ".gif":  {"viewer": {"mode": "internal", "type": "image"},
              "editor": {"mode": "internal", "type": "image"}},
    ".bmp":  {"viewer": {"mode": "internal", "type": "image"},
              "editor": {"mode": "internal", "type": "image"}},
    # Archives
    ".zip":  {"viewer": {"mode": "internal", "type": "archive"},
              "editor": {"mode": "internal", "type": "archive"}},
    ".lha":  {"viewer": {"mode": "internal", "type": "archive"},
              "editor": {"mode": "internal", "type": "archive"}},
    ".lzh":  {"viewer": {"mode": "internal", "type": "archive"},
              "editor": {"mode": "internal", "type": "archive"}},
    ".tar":  {"viewer": {"mode": "internal", "type": "archive"},
              "editor": {"mode": "internal", "type": "archive"}},
    ".gz":   {"viewer": {"mode": "internal", "type": "archive"},
              "editor": {"mode": "internal", "type": "archive"}},
    # Retro
    ".seq": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".ans": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".nfo": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    ".diz": {"viewer": {"mode": "internal", "type": "text"},
             "editor": {"mode": "internal", "type": "text"}},
    # AmigaGuide hypertext
    ".guide": {"viewer": {"mode": "internal", "type": "amigaguide"},
               "editor": {"mode": "internal", "type": "text"}},
    ".hlp":   {"viewer": {"mode": "internal", "type": "amigaguide"},
               "editor": {"mode": "internal", "type": "text"}},
    # C64 binaries - 6502 disassembler
    ".prg": {"viewer": {"mode": "internal", "type": "c64disasm"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".bin": {"viewer": {"mode": "internal", "type": "c64disasm"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".crt": {"viewer": {"mode": "internal", "type": "crt_toolkit"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".tap": {"viewer": {"mode": "internal", "type": "c64disasm"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".sid": {"viewer": {"mode": "internal", "type": "sidplay"},
             "editor": {"mode": "internal", "type": "hex"}},
    # C64 graphics - Koala, Hi-Res, Charset. Format-detection
    # passiert anhand der Dateigroesse in show_retro_gfx_viewer.
    ".kla": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".koa": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".chr": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".fnt": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".64c": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".art": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # C64 native bitmap formats (handled by built-in decoders)
    ".aas": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ocp": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".fli": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".afl": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ifl": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".iph": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ipt": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".drp": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".drz": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".drl": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".dlp": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ami": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".dd":  {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".jj":  {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".gun": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".fun": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".fp2": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".cdu": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".bfli":{"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".hed": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".vid": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # Amiga IFF / ILBM (RECOIL handles these)
    ".iff": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ilbm":{"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".lbm": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".ham": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".sham":{"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".acbm":{"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # Atari ST / Falcon
    ".pi1": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".pi2": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".pi3": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".neo": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".degas":{"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".pc1": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".pc2": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".pc3": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # ZX Spectrum
    ".scr": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # MSX
    ".sc2": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".sc5": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".sc7": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    ".sc8": {"viewer": {"mode": "internal", "type": "retrogfx"},
             "editor": {"mode": "internal", "type": "hex"}},
    # Tracker modules - ProTracker / FastTracker II / ScreamTracker /
    # Impulse Tracker / OpenMPT and the dozens of related formats.
    # All handled by the libopenmpt-backed module player.
    ".mod":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".xm":    {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".s3m":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".it":    {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".mptm":  {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".med":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".mtm":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".stm":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".669":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
    ".okt":   {"viewer": {"mode": "internal", "type": "modplay"},
                "editor": {"mode": "internal", "type": "hex"}},
}


def get_assoc(config, ext, action):
    """
    Look up the handler for (extension, action) where action = 'viewer' or 'editor'.
    Falls back to the "*" wildcard if the extension has no entry.

    Special case for action='editor': if the resolved handler is the
    internal text reader (which is read-only and not actually an editor),
    consult the user's global text_editor preference. This way the user
    only has to set one path (config['text_editor']) and ALL extensions
    that fall back to "internal text" pick it up. Extensions that
    explicitly route to e.g. internal hex or image viewer for editing
    are not touched - those are deliberate choices.
    """
    ext = (ext or "").lower()
    assoc = config.get("file_assoc", {})
    entry = assoc.get(ext)
    if not entry:
        entry = assoc.get("*", DEFAULT_ASSOC["*"])
    handler = entry.get(action, DEFAULT_ASSOC["*"][action])

    if action == 'editor' \
            and handler.get("mode") == "internal" \
            and handler.get("type") == "text":
        global_editor = _resolve_global_editor(config)
        if global_editor:
            return global_editor
    return handler


def _resolve_global_editor(config):
    """
    Return an external-handler dict for the user's preferred text
    editor, or None if nothing usable is available.

    Resolution order:
      1. Explicit override in config['text_editor'] (string path or
         dict with program/args).
      2. $VISUAL or $EDITOR environment variable.
      3. Platform-specific auto-detection of a sensible default that
         actually exists on the system (notepad/notepad++ on Windows,
         gedit/kate/kwrite/xdg-open/nano on Linux,
         TextEdit/open on macOS).

    Result is cached on the config dict so we don't re-probe on every
    F4 keypress. Setting config['text_editor'] to '' (empty string)
    explicitly disables the fallback and keeps the internal reader.
    """
    # Explicit user override wins. Accept either a plain string path
    # or a full handler dict.
    user = config.get("text_editor", None)
    if isinstance(user, dict) and user.get("program"):
        return {"mode": "external",
                "program": user["program"],
                "args": user.get("args", ["%f"])}
    if isinstance(user, str):
        if user == "":
            # Empty string = explicit "use internal reader, don't
            # auto-detect". Distinguishes from None/missing.
            return None
        return {"mode": "external", "program": user, "args": ["%f"]}

    # Try $VISUAL / $EDITOR. Common values are 'nano', 'vim', 'code'.
    # We resolve via shutil.which so PATH-only entries work.
    import shutil
    for var in ("VISUAL", "EDITOR"):
        v = os.environ.get(var, "").strip()
        if v:
            # The variable may include args (e.g. "code --wait").
            parts = shlex.split(v, posix=(os.name != 'nt'))
            if parts and shutil.which(parts[0]):
                return {"mode": "external",
                        "program": parts[0],
                        "args": parts[1:] + ["%f"]}

    # Platform auto-detection - pick the first one that actually exists.
    # The list is intentionally mainstream-first so first-time users on
    # a default install get something sensible without configuration.
    if os.name == 'nt':
        candidates = [
            (r"C:\Program Files\Notepad++\notepad++.exe", ["%f"]),
            (r"C:\Program Files (x86)\Notepad++\notepad++.exe", ["%f"]),
            ("notepad.exe", ["%f"]),  # always present on Windows
        ]
    elif sys.platform == 'darwin':
        candidates = [
            ("/Applications/TextEdit.app/Contents/MacOS/TextEdit", ["%f"]),
            ("open", ["-e", "%f"]),     # opens in TextEdit
        ]
    else:
        # Linux/BSD - prefer GUI editors when in a desktop session,
        # otherwise xdg-open lets the desktop environment decide. We
        # check $DISPLAY/$WAYLAND_DISPLAY to avoid spawning a GUI app
        # from a pure-tty session.
        has_display = bool(os.environ.get("DISPLAY")
                           or os.environ.get("WAYLAND_DISPLAY"))
        gui_editors = [
            ("gedit",     ["%f"]),
            ("kate",      ["%f"]),
            ("kwrite",    ["%f"]),
            ("mousepad",  ["%f"]),
            ("xed",       ["%f"]),
            ("pluma",     ["%f"]),
            ("leafpad",   ["%f"]),
            ("featherpad",["%f"]),
            ("code",      ["--wait", "%f"]),  # VS Code
            ("subl",      ["%f"]),            # Sublime Text
            ("gvim",      ["%f"]),
        ]
        tty_editors = [
            ("nano",  ["%f"]),
            ("vim",   ["%f"]),
            ("vi",    ["%f"]),
        ]
        candidates = (gui_editors + [("xdg-open", ["%f"])] + tty_editors
                       if has_display else tty_editors)

    for prog, args in candidates:
        if os.path.isabs(prog):
            if os.path.isfile(prog) and os.access(prog, os.X_OK):
                return {"mode": "external", "program": prog, "args": args}
        else:
            import shutil as _sh
            resolved = _sh.which(prog)
            if resolved:
                return {"mode": "external", "program": resolved, "args": args}
    return None


def run_external(handler, filepath):
    """
    Launch an external program with the file.
    `handler` is {"mode":"external","program":..., "args":[...]}.
    The "%f" token in args is replaced with the full file path; if no "%f"
    is present the path is appended as the last argument.
    """
    program = handler.get("program", "")
    if not program:
        raise ValueError("External handler has no program set")

    args_template = handler.get("args") or []
    if isinstance(args_template, str):
        # Allow a single string like '-n "%f"' - shlex-split it
        args_template = shlex.split(args_template, posix=(os.name != 'nt'))

    path_str = str(filepath)
    substituted = []
    seen_token = False
    for a in args_template:
        if "%f" in a:
            substituted.append(a.replace("%f", path_str))
            seen_token = True
        else:
            substituted.append(a)
    if not seen_token:
        substituted.append(path_str)

    cmd = [program] + substituted
    # Start in the directory containing the target program/script so
    # relative paths inside the script (fonts, data files, etc.) resolve
    # correctly. For a .py script invoked with 'python script.py' we use
    # the script's directory; otherwise the program's own directory.
    from pathlib import Path as _P
    cwd = None
    try:
        # If any arg looks like a script with a known extension, use its dir
        for a in substituted:
            ap = _P(a)
            if ap.suffix.lower() in (".py", ".pyw", ".sh", ".bat", ".cmd",
                                     ".ps1", ".pl", ".rb", ".js") \
               and ap.is_absolute() and ap.parent.is_dir():
                cwd = str(ap.parent); break
        if cwd is None:
            pp = _P(program)
            if pp.is_absolute() and pp.parent.is_dir():
                cwd = str(pp.parent)
            else:
                # PATH-resolved - use the target file's directory as fallback
                # so relative resources work when opening the file itself
                tp = _P(path_str)
                if tp.parent.is_dir():
                    cwd = str(tp.parent)
    except Exception:
        cwd = None
    try:
        # Detach so Quopus doesn't block and the launched program survives
        # Quopus closing.
        kwargs = {
            "cwd": cwd,
            "stdin":  subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == 'nt':
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = (DETACHED_PROCESS |
                                        CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True
        subprocess.Popen(cmd, **kwargs)
        return True
    except FileNotFoundError:
        raise FileNotFoundError(f"Program not found: {program}")


def ensure_default_assoc(config):
    """
    Merge DEFAULT_ASSOC into config if keys are missing; user-set entries
    are never overwritten.

    Special case: the C64 binary extensions (.prg/.bin/.crt/.tap) are
    auto-upgraded to the c64disasm viewer if they currently have a
    "soft" internal handler (auto/text/hex). The .sid extension is
    additionally upgraded from c64disasm to sidplay since we now have
    a real SID player. The .crt extension is also upgraded from
    c64disasm to crt_toolkit (proper cartridge browser with bank
    inspection, EAPI/EasyFS/Yeti detection, embedded-blob scan).
    External (user-configured) handlers like "open in VICE" are NEVER
    touched.
    """
    if "file_assoc" not in config:
        config["file_assoc"] = {}

    C64_EXTS = {".prg", ".bin", ".crt", ".tap", ".sid"}

    for ext, handlers in DEFAULT_ASSOC.items():
        cur = config["file_assoc"].get(ext)
        if cur is None:
            # Not present at all - install default
            config["file_assoc"][ext] = {
                "viewer": dict(handlers["viewer"]),
                "editor": dict(handlers["editor"]),
            }
            continue
        # Auto-upgrade for known C64 extensions
        if ext in C64_EXTS:
            v = cur.get("viewer", {})
            cur_type = v.get("type")
            if v.get("mode") == "internal":
                # General soft-types upgrade (no explicit choice made)
                if cur_type in (None, "auto", "text", "hex"):
                    cur["viewer"] = dict(handlers["viewer"])
                # Special .sid upgrade: c64disasm was the old default,
                # now we ship a real SID player
                elif ext == ".sid" and cur_type == "c64disasm":
                    cur["viewer"] = dict(handlers["viewer"])
                # Special .crt upgrade: c64disasm was the old default,
                # now we ship a real CRT cartridge toolkit (bank
                # browser, EAPI/EasyFS/Yeti detection, blob scanner).
                elif ext == ".crt" and cur_type == "c64disasm":
                    cur["viewer"] = dict(handlers["viewer"])
    return config
