# date_time: 2026-05-30 18:24
"""
Custom-module loader for Quopus Commander.

Lets users drop their own Python files into a `custom_modules/`
directory and have them appear as bindable action-buttons in
Quopus, just like the built-in actions.

== File format ==

A custom module is a single .py file with at least:

    ACTION_NAME = "my_action"        # required, unique key
    def run(api):                    # required, the actual work
        ...

Optional metadata:

    ACTION_LABEL = "My Action"           # shown in the picker
    ACTION_DESCRIPTION = "..."           # tooltip / status hint
    ACTION_PARAM_LABEL = "Folder name"   # if the action takes a
                                          # text param the button
                                          # passes through, this
                                          # is shown as the
                                          # placeholder in the
                                          # button editor

The `api` object the module receives has the following shape
(stable across Quopus versions - if we ever need to add fields
we'll only ADD, never remove):

    api.src_path        Path of the active panel's current dir
    api.dst_path        Path of the other panel's current dir
    api.selected        list[Path] of files selected in active panel
    api.param           str: per-button param string (label etc.)
    api.config          dict: full Quopus config (read-only-ish)
    api.parent_widget   QWidget: main window, for QDialog parenting
    api.log(msg)        print to Quopus's status bar
    api.refresh()       force a re-list of both panels
    api.notify(title,
               body,
               kind='info'|'warn'|'error')
                        pop a small message box
    api.input(title,
              prompt,
              default='') -> Optional[str]
                        prompt the user; None on cancel
    api.ask_yes_no(title, body) -> bool
                        confirmation dialog
    api.pick_file(title, *, save=False,
                   filters=...) -> Optional[Path]
                        OS file dialog
    api.pick_dir(title) -> Optional[Path]
                        OS folder dialog

The api object is reconstructed for every dispatch, so plugins
don't have to worry about caching it. They CAN keep state in
their own module-level globals between calls (the module is
loaded once per Quopus session).

== Discovery ==

Two directories are scanned, in this order:

1. `<exe-dir>/custom_modules/` - portable / shipped with the
   application bundle. Read-only on most installs.
2. `<user-config-dir>/custom_modules/` - user-private modules.
   This is the default place for new modules; it survives
   Quopus updates.

If both directories contain a file with the same ACTION_NAME,
the user-config one wins. A WARNING is logged for the duplicate.

== Safety note ==

Custom modules run in the same Python interpreter as Quopus.
They have full filesystem and network access. Quopus does NOT
sandbox them. Users should treat custom_modules/ the way they'd
treat ~/.bashrc - drop in code you trust, written by yourself
or by someone you'd run a shell script from.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------
# Registry: mapping action_name -> _LoadedModule
# ---------------------------------------------------------------------
class _LoadedModule:
    """In-memory record of a discovered custom module."""

    __slots__ = ("action_name", "label", "description",
                 "param_label", "run_func", "source_path",
                 "module_obj")

    def __init__(self, action_name: str, label: str,
                  description: str, param_label: str,
                  run_func: Callable, source_path: Path,
                  module_obj: Any):
        self.action_name = action_name
        self.label = label
        self.description = description
        self.param_label = param_label
        self.run_func = run_func
        self.source_path = source_path
        self.module_obj = module_obj

    def __repr__(self):
        return (f"<_LoadedModule {self.action_name!r} "
                f"from {self.source_path}>")


# Global registry. Populated by load_all() during startup AND
# whenever the user picks "Reload custom modules" from the menu.
_REGISTRY: dict[str, _LoadedModule] = {}

# List of (path, error_msg) for modules that failed to load.
# Surfaced in the UI so the user can find and fix typos.
_LOAD_ERRORS: list[tuple[Path, str]] = []


# ---------------------------------------------------------------------
# Discovery paths
# ---------------------------------------------------------------------
def _default_paths() -> list[Path]:
    """Return the list of directories that get scanned for
    custom modules, in priority order (later items win on name
    collision)."""
    paths: list[Path] = []

    # 1. Portable / bundled directory next to the EXE (or next to
    #    quopus.py when running from source). PyInstaller users
    #    can ship a default set of modules this way.
    try:
        if getattr(sys, 'frozen', False):
            # Running as PyInstaller bundle - sys.executable is
            # the exe path
            exe_dir = Path(sys.executable).parent
        else:
            # Running from source - find quopus.py
            exe_dir = Path(__file__).resolve().parent.parent
        paths.append(exe_dir / "custom_modules")
    except Exception:
        pass

    # 2. User config dir - the default place for user-private
    #    modules. We pull this from quopus_lib.config so it
    #    matches wherever quopus.cfg and quopus.lic live.
    try:
        from .config import CONFIG_DIR
        paths.append(Path(CONFIG_DIR) / "custom_modules")
    except Exception:
        # Fallback to a sane platform-specific default if config
        # isn't importable yet (very early-startup edge case).
        import os
        if sys.platform == "win32":
            base = Path(os.environ.get(
                "APPDATA", str(Path.home() / "AppData/Roaming")))
            paths.append(base / "quopus" / "custom_modules")
        elif sys.platform == "darwin":
            paths.append(Path.home() / "Library" / "Application Support"
                         / "quopus" / "custom_modules")
        else:
            base = Path(os.environ.get(
                "XDG_CONFIG_HOME", str(Path.home() / ".config")))
            paths.append(base / "quopus" / "custom_modules")

    return paths


def get_module_dirs() -> list[Path]:
    """Public accessor for the list of scanned directories.
    Used by the UI to surface "Open custom_modules folder"."""
    return _default_paths()


def get_user_dir() -> Path:
    """Return THE writeable directory for user-installed modules
    (the second entry in _default_paths). Used by the "Open
    folder" menu item so the user lands in the right place when
    they want to add a new module."""
    paths = _default_paths()
    return paths[-1] if paths else (Path.home() / "custom_modules")


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------
def _load_single(path: Path) -> Optional[_LoadedModule]:
    """Load one .py file and return a _LoadedModule, or None on
    failure (with the error recorded in _LOAD_ERRORS).

    Each module is loaded under a unique name in sys.modules so
    that a Reload picks up the freshly edited code and doesn't
    return the cached import.
    """
    # Build a sys.modules name that won't collide with anything
    # real. Path-based so collisions between different dirs are
    # impossible even if filenames repeat.
    mod_name = f"quopus_custom_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            _LOAD_ERRORS.append(
                (path, "Python could not build a module spec"))
            return None
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec so the module can reference itself
        # if it really wants to. We pop it back out if the import
        # itself fails so we don't leak.
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(mod_name, None)
            _LOAD_ERRORS.append((path, f"{type(e).__name__}: {e}\n"
                                  f"{traceback.format_exc()}"))
            return None
    except Exception as e:
        _LOAD_ERRORS.append(
            (path, f"Unexpected loader failure: {e}"))
        return None

    # Required fields
    action_name = getattr(module, "ACTION_NAME", None)
    if not action_name or not isinstance(action_name, str):
        _LOAD_ERRORS.append(
            (path, "Missing or non-string ACTION_NAME"))
        return None
    if not action_name.replace("_", "").isalnum():
        _LOAD_ERRORS.append(
            (path, f"ACTION_NAME {action_name!r} must be "
                   f"alphanumeric + underscores only"))
        return None
    run_func = getattr(module, "run", None)
    if not callable(run_func):
        _LOAD_ERRORS.append(
            (path, f"{path.name}: must define a run(api) function"))
        return None

    # Optional metadata - default to sensible values if missing.
    label = getattr(module, "ACTION_LABEL", None)
    if not label or not isinstance(label, str):
        label = action_name.replace("_", " ").title()
    description = getattr(module, "ACTION_DESCRIPTION", "")
    if not isinstance(description, str):
        description = ""
    param_label = getattr(module, "ACTION_PARAM_LABEL", "")
    if not isinstance(param_label, str):
        param_label = ""

    return _LoadedModule(
        action_name=action_name,
        label=label,
        description=description,
        param_label=param_label,
        run_func=run_func,
        source_path=path,
        module_obj=module,
    )


def load_all() -> None:
    """Scan all configured module directories and (re)populate
    the global registry. Safe to call multiple times - existing
    registry entries are replaced wholesale, sys.modules entries
    for prior loads are evicted.
    """
    # Clear previous state. We can't just clear _REGISTRY because
    # the old module objects might still have references held by
    # bound action callbacks - but pruning the registry means new
    # dispatches go to the new code.
    _REGISTRY.clear()
    _LOAD_ERRORS.clear()
    # Drop any previously-loaded custom modules from sys.modules
    # so a reload genuinely re-executes the file. Names we created
    # start with our well-known prefix.
    for k in list(sys.modules.keys()):
        if k.startswith("quopus_custom_"):
            del sys.modules[k]

    for d in _default_paths():
        if not d.exists():
            # Auto-create the user dir on first run so opening
            # "custom_modules folder" from the menu shows an
            # empty dir rather than "doesn't exist".
            try:
                d.mkdir(parents=True, exist_ok=True)
                _write_readme_if_missing(d)
            except Exception:
                # Read-only filesystem (e.g. the bundled exe-dir
                # path under /Applications on macOS) - skip
                # silently, that's expected.
                pass
        if not d.is_dir():
            continue
        for py in sorted(d.glob("*.py")):
            if py.name.startswith("_"):
                # Convention: underscored files are helpers /
                # libraries imported by other modules, not
                # standalone actions.
                continue
            loaded = _load_single(py)
            if loaded is None:
                continue
            # Name collision: later-discovered (user) dir wins,
            # earlier (bundled) loses. Log so the user knows.
            if loaded.action_name in _REGISTRY:
                old = _REGISTRY[loaded.action_name]
                print(f"  [custom_modules] {loaded.action_name!r} "
                      f"from {loaded.source_path} overrides "
                      f"earlier definition in {old.source_path}")
            _REGISTRY[loaded.action_name] = loaded


def _write_readme_if_missing(d: Path) -> None:
    """Drop a quickstart README into the user's custom_modules
    folder on first creation so they know what goes there."""
    readme = d / "README.md"
    if readme.exists():
        return
    try:
        readme.write_text(
            "# Quopus Custom Modules\n\n"
            "Drop your own Python files into this folder to add\n"
            "new action-buttons to Quopus. Each module needs at\n"
            "least:\n\n"
            "```python\n"
            "ACTION_NAME = \"my_action\"\n\n"
            "def run(api):\n"
            "    api.notify(\"Hello\", \"It works!\")\n"
            "```\n\n"
            "Optional metadata:\n\n"
            "```python\n"
            "ACTION_LABEL = \"My Action\"\n"
            "ACTION_DESCRIPTION = \"What it does\"\n"
            "ACTION_PARAM_LABEL = \"Folder name\"\n"
            "```\n\n"
            "Files whose names start with an underscore are\n"
            "treated as helper modules and not loaded as actions\n"
            "(use those for shared code imported by your real\n"
            "modules).\n\n"
            "After editing or adding a file, pick\n"
            "**Config -> Reload custom modules** in Quopus or\n"
            "restart the application to pick up the changes.\n\n"
            "## Security\n\n"
            "Custom modules run in the same Python process as\n"
            "Quopus. They have full filesystem and network\n"
            "access. Only put code here that you trust.\n",
            encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------
# API object passed to run()
# ---------------------------------------------------------------------
class CustomModuleAPI:
    """Stable plugin-facing interface. See the module docstring
    for the supported attributes / methods.

    Implementation note: this class is intentionally lightweight
    and lazy - it grabs widgets / paths from the host actions
    object only when the plugin actually asks for them, so a
    plugin that ignores half the API doesn't pay for setup of
    those parts.
    """

    def __init__(self, host, param: str = ""):
        # `host` is the actions.Actions instance - we keep it as
        # an attribute (not a public one) so we can route calls
        # back to the main Quopus singletons without forcing
        # plugins to know about them.
        self._host = host
        self.param = param or ""

    # ---- panel context ---------------------------------------
    @property
    def src_path(self) -> Optional[Path]:
        """Active panel's directory."""
        try:
            src, _dst = self._host._active()
            # Lister exposes current_path as an attribute
            # (pathlib.Path), not a method - match the convention
            # used everywhere else in actions.py.
            return Path(src.current_path)
        except Exception:
            return None

    @property
    def dst_path(self) -> Optional[Path]:
        """Other panel's directory."""
        try:
            _src, dst = self._host._active()
            return Path(dst.current_path)
        except Exception:
            return None

    @property
    def selected(self) -> list[Path]:
        """Selected files/dirs in the active panel."""
        try:
            src, _dst = self._host._active()
            return [Path(p) for p in src.selected_paths()]
        except Exception:
            return []

    @property
    def config(self) -> dict:
        """Quopus's runtime config dict. Modifications are NOT
        auto-persisted - call api.save_config() if you really
        want to write them back."""
        return getattr(self._host.w, "config", {})

    @property
    def parent_widget(self):
        """QWidget to use as parent for any QDialogs you open,
        so they inherit Quopus's window placement and modality."""
        return self._host.w

    # ---- output ----------------------------------------------
    def log(self, msg: str) -> None:
        """Show a message in Quopus's status bar."""
        try:
            self._host._status(str(msg))
        except Exception:
            print(f"[custom_module] {msg}")

    def refresh(self) -> None:
        """Force a directory re-read in both panels. Call after
        creating / deleting / moving files so Quopus updates
        its listing."""
        try:
            self._host.w.left_lister.refresh()
            self._host.w.right_lister.refresh()
        except Exception:
            pass

    def notify(self, title: str, body: str,
                kind: str = "info") -> None:
        """Pop a QMessageBox. kind = 'info' / 'warn' / 'error'."""
        from PyQt6.QtWidgets import QMessageBox
        icon = {
            "info":  QMessageBox.Icon.Information,
            "warn":  QMessageBox.Icon.Warning,
            "error": QMessageBox.Icon.Critical,
        }.get(kind, QMessageBox.Icon.Information)
        box = QMessageBox(self.parent_widget)
        box.setIcon(icon)
        box.setWindowTitle(str(title))
        box.setText(str(body))
        box.exec()

    # ---- input -----------------------------------------------
    def input(self, title: str, prompt: str,
                default: str = "") -> Optional[str]:
        """Text-input dialog. Returns the typed string, or None
        if the user cancelled."""
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(
            self.parent_widget, str(title), str(prompt),
            text=str(default))
        return text if ok else None

    def ask_yes_no(self, title: str, body: str) -> bool:
        """Confirmation dialog. Returns True for Yes."""
        from PyQt6.QtWidgets import QMessageBox
        r = QMessageBox.question(
            self.parent_widget, str(title), str(body),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        return r == QMessageBox.StandardButton.Yes

    def pick_file(self, title: str = "Pick a file", *,
                    save: bool = False,
                    filters: str = "All files (*)"
                    ) -> Optional[Path]:
        """Native file picker. save=True opens a Save-As dialog
        instead of Open."""
        from PyQt6.QtWidgets import QFileDialog
        if save:
            path, _ = QFileDialog.getSaveFileName(
                self.parent_widget, title, "", filters)
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.parent_widget, title, "", filters)
        return Path(path) if path else None

    def pick_dir(self, title: str = "Pick a folder"
                  ) -> Optional[Path]:
        """Native directory picker."""
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(
            self.parent_widget, title)
        return Path(path) if path else None

    # ---- persistence -----------------------------------------
    def save_config(self) -> None:
        """Persist the runtime config dict to disk. Use after
        mutating api.config so your changes survive a restart
        (e.g. a plugin storing its own bookmarks / settings under
        a private key)."""
        try:
            from .config import save_config as _save
            _save(getattr(self._host.w, "config", {}))
        except Exception as e:
            print(f"[custom_module] save_config failed: {e}")


# ---------------------------------------------------------------------
# Dispatch (called by actions.Actions.dispatch)
# ---------------------------------------------------------------------
def dispatch(action_name: str, host, param: str = "") -> bool:
    """If action_name corresponds to a loaded custom module, run
    it and return True. Otherwise return False so the host can
    fall through to its 'Unknown action' handling.

    Exceptions raised by the plugin are caught and surfaced via
    a QMessageBox - they don't crash Quopus."""
    rec = _REGISTRY.get(action_name)
    if rec is None:
        return False
    api = CustomModuleAPI(host, param=param)
    try:
        rec.run_func(api)
    except Exception as e:
        # Print the full traceback to stderr for debugging, show
        # the user a short version in a dialog.
        traceback.print_exc()
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(
            host.w, f"Custom module: {rec.label}",
            f"{rec.action_name} raised an error:\n\n"
            f"{type(e).__name__}: {e}\n\n"
            f"(Full traceback printed to console.)")
    return True


# ---------------------------------------------------------------------
# Accessors for the rest of Quopus
# ---------------------------------------------------------------------
def all_modules() -> list[_LoadedModule]:
    """Return all loaded modules in label-sorted order. The
    action-catalog uses this to populate the "Custom Modules"
    submenu in the right-click button editor and the F10 Action
    buttons dialog."""
    return sorted(_REGISTRY.values(),
                  key=lambda m: m.label.lower())


def load_errors() -> list[tuple[Path, str]]:
    """Return any module-load failures from the last load_all()
    pass. Surfaced in Config -> Reload custom modules's status
    dialog."""
    return list(_LOAD_ERRORS)


def get(action_name: str) -> Optional[_LoadedModule]:
    """Look up a single module by ACTION_NAME, or None if no
    such module is loaded. Used by the button-config tooltip to
    show the description of a custom-action button."""
    return _REGISTRY.get(action_name)
