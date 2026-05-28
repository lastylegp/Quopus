# Custom Modules — API reference

> Plugin system for Quopus Commander. Drop a Python file into `custom_modules/`, get a new bindable button action.

This document is the complete reference for what your `run(api)` function receives, what you can do with it, and what Quopus-internal pieces are safe to reach for when the public API isn't enough.

For a quick start, copy `custom_modules/example_hello.py` and edit it. For a more involved example with a custom QDialog UI see `custom_modules/text_reader_sample.py`. This file is the technical reference behind both.

---

## Contents

- [What a custom module looks like](#what-a-custom-module-looks-like)
- [Module-level metadata](#module-level-metadata)
- [The `api` object](#the-api-object)
  - [Panel context (read-only)](#panel-context-read-only)
  - [Output / user-facing](#output--user-facing)
  - [Input dialogs](#input-dialogs)
- [How modules are discovered](#how-modules-are-discovered)
- [How modules are dispatched](#how-modules-are-dispatched)
- [Reload during development](#reload-during-development)
- [Going beyond the public API](#going-beyond-the-public-api)
- [Quopus internals you might call](#quopus-internals-you-might-call)
- [Safety, errors, threading](#safety-errors-threading)
- [Distribution & sharing](#distribution--sharing)

---

## What a custom module looks like

A custom module is **one `.py` file** with at least two things: a name and a `run` function.

```python
ACTION_NAME = "my_action"          # required: unique key, stable
def run(api):                      # required: the actual work
    api.notify("Hello", "It works!")
```

That's it. Save the file in your custom-modules folder (Config → Open custom modules folder), then **Config → Reload custom modules**. Your action appears under "Custom Modules" in the action picker (right-click any button → Assign to button, or F10).

A bit more realistic:

```python
ACTION_NAME = "count_prgs"
ACTION_LABEL = "Count .prg files"
ACTION_DESCRIPTION = "Count *.prg in the active panel's directory."
ACTION_PARAM_LABEL = "Extension (default: prg)"

def run(api):
    ext = api.param.strip() or "prg"
    if api.src_path is None:
        api.notify("Count", "No active panel", kind="warn")
        return
    n = sum(1 for _ in api.src_path.glob(f"*.{ext}"))
    api.notify("Count", f"{n} *.{ext} file(s) in {api.src_path}")
```

---

## Module-level metadata

The loader looks at these globals:

| Name | Required | Type | Purpose |
|------|----------|------|---------|
| `ACTION_NAME` | **yes** | `str` | Unique identifier. Used as the action key the button stores. Convention: `snake_case`, no spaces. If two modules share an `ACTION_NAME`, the user-config copy wins and a warning is logged. |
| `ACTION_LABEL` | no | `str` | Pretty name shown in the action picker. Defaults to `ACTION_NAME` capitalised. |
| `ACTION_DESCRIPTION` | no | `str` | Tooltip / status-bar hint. Defaults to an empty string. |
| `ACTION_PARAM_LABEL` | no | `str` | If your action consumes the button's "Param" field, set this to a short placeholder hint so the button editor knows what to suggest. Set to `""` (or omit) to indicate the action takes no param. |
| `run` | **yes** | `Callable[[api], None]` | The entry point. Quopus calls this every time the user triggers the action. Return value is ignored. |

That's the entire metadata surface. Anything else you put at module level is yours: helper functions, constants, lazy imports, cached resources between dispatches (the module is imported once per Quopus session — see [Reload during development](#reload-during-development) below).

---

## The `api` object

`api` is a `CustomModuleAPI` instance, fresh for every dispatch. It exposes the active panel state plus a small set of UI helpers, all designed to be stable across Quopus versions. We only **add** to this surface, never break it.

### Panel context (read-only)

| Attribute / property | Type | Meaning |
|---|---|---|
| `api.src_path` | `Optional[pathlib.Path]` | Directory of the **active** panel (the one with the visible focus border). `None` if Quopus can't determine an active panel — defensive check it before using. |
| `api.dst_path` | `Optional[pathlib.Path]` | Directory of the **other** panel. Useful for "copy this here", "diff active vs other", etc. |
| `api.selected` | `list[pathlib.Path]` | All files/dirs selected in the active panel. If the user has nothing explicitly tagged, this usually contains just the focused row. Always a list (never `None`), possibly empty. |
| `api.param` | `str` | The Param field on the button that triggered the action (or `""` for actions triggered another way). Use this for "configure this button instance" — e.g. one button per archive format, with the format in Param. |
| `api.config` | `dict` | Quopus's runtime config dict. **Treat as read-only**: edits aren't auto-persisted. If you need to write back, write your own JSON/INI in your module's data folder. |
| `api.parent_widget` | `QWidget` | The Quopus main window. Pass this as the `parent` argument when you create your own `QDialog` so it inherits modality, window placement, icon, and Qt-style. |

### Output / user-facing

| Method | Returns | Notes |
|---|---|---|
| `api.log(msg)` | `None` | Writes `msg` to the status bar at the bottom of the main window. Cheap; use it for progress (`"Scanning 1/47..."`) and breadcrumbs. Falls back to `print()` if the status bar isn't reachable. |
| `api.refresh()` | `None` | Forces a directory re-read in **both** panels. Call this after creating, moving, renaming or deleting files from your module so the listings update. |
| `api.notify(title, body, kind="info")` | `None` | Modal `QMessageBox`. `kind` is `"info"`, `"warn"`, or `"error"` (anything else falls back to info). Body can be multi-line. |

### Input dialogs

| Method | Returns | Notes |
|---|---|---|
| `api.input(title, prompt, default="")` | `Optional[str]` | One-line text input. Returns `None` if the user cancels. |
| `api.ask_yes_no(title, body)` | `bool` | Yes/No confirmation. `True` means Yes. |
| `api.pick_file(title="Pick a file", *, save=False, filters="All files (*)")` | `Optional[pathlib.Path]` | Native file picker. `save=True` switches to a Save-As dialog. `filters` follows Qt's syntax: `"Text files (*.txt);;All files (*)"`. |
| `api.pick_dir(title="Pick a folder")` | `Optional[pathlib.Path]` | Native directory picker. |

All four return `None` on cancel — handle that explicitly:

```python
out = api.pick_file("Save report", save=True,
                    filters="Markdown (*.md)")
if out is None:
    return   # user cancelled, nothing to do
out.write_text("...")
```

---

## How modules are discovered

At startup (and on **Reload custom modules**), Quopus scans **two** directories in order:

1. `<exe-dir>/custom_modules/` — portable / shipped with the application bundle.
   - For PyInstaller builds: alongside `quopus.exe`.
   - For source checkouts: in the repository root (next to `quopus.py`).
2. `<user-config-dir>/custom_modules/` — user-private modules. **This is where you put new modules.** Survives Quopus updates.
   - Windows: `%APPDATA%\quopus\custom_modules\`
   - macOS: `~/Library/Application Support/quopus/custom_modules/`
   - Linux: `~/.config/quopus/custom_modules/` (or `$XDG_CONFIG_HOME/quopus/custom_modules/`)

Every `*.py` file in those directories that isn't an `__init__.py` or starting with `_` is loaded. If both folders contain a module with the same `ACTION_NAME`, **the user-config one wins** and a `WARNING` is logged for the duplicate. So you can override a bundled module by dropping a same-named file into your user folder.

You can get the exact paths programmatically:

```python
from quopus_lib import custom_modules
print(custom_modules.get_module_dirs())   # all scanned dirs
print(custom_modules.get_user_dir())      # the writeable one
```

`Config → Open custom modules folder` opens `get_user_dir()` in your OS file manager.

---

## How modules are dispatched

When the user clicks a button that's bound to a custom action, Quopus's main dispatcher (`actions.Actions.dispatch`) does:

1. Look up the action key in the built-in registry. If found, call it. ←  most actions land here
2. If not, call `custom_modules.dispatch(action_name, host, param)`.
3. The custom-modules dispatcher finds the loaded module, builds a fresh `CustomModuleAPI(host, param)`, calls `module.run(api)`, and catches/logs any exception so a buggy plugin can't crash Quopus.

You **don't** wire any of that yourself. Just provide `ACTION_NAME` and `run(api)` — the loader and dispatcher do the rest.

---

## Reload during development

Quopus loads custom modules **once per session** (so module-level state persists between dispatches — useful for caching). To pick up edits, choose **Config → Reload custom modules**. This rescans the directories, replaces existing modules in `sys.modules` under a unique key, and rebuilds the action picker.

Reload doesn't re-import top-level Python packages, just the custom-module files themselves. If your module does `import some_heavy_package` and that package is already imported (because it's used by Quopus too), the cached version is reused — which is what you want. If you're iterating on a helper file that lives outside `custom_modules/`, you'll need a Quopus restart for that file to reload.

Tip: keep a tiny helper module in the **same** `custom_modules/` folder and import it relatively. Helper files there get reloaded with the rest:

```
custom_modules/
    my_action.py         # contains ACTION_NAME + run(api)
    my_action_helpers.py # contains shared logic
```

```python
# my_action.py
from .my_action_helpers import some_function
```

This pattern only works because the loader registers each module under a package-style name. If you want plain `import my_action_helpers`, add the directory to `sys.path` yourself once at module top.

---

## Going beyond the public API

`CustomModuleAPI` is a deliberately small surface. If you need something it doesn't expose, you have two escape hatches — but with consequences:

### 1. Reach into Quopus internals via `api._host` / `api.parent_widget`

`api._host` is the live `Actions` instance. From there you can reach the main window (`api._host.w`), the listers (`api._host.w.left_lister`, `api._host.w.right_lister`), the config (`api._host.w.config`), and any registered subsystem (config dialog, FTP browser, U64 device pool, …).

**This is unsupported.** Internals can be renamed or restructured between versions. The trade-off: you get the full Quopus power, you take on the maintenance.

```python
def run(api):
    mw = api._host.w
    # Reach the inactive lister explicitly
    other = mw.right_lister if api._host._active()[0] is mw.left_lister \
        else mw.left_lister
    api.log(f"Other panel: {other.current_path}")
```

### 2. Pure Python / PyQt6

Your module is just a Python file. Anything that's importable in the same interpreter as Quopus is available — `pathlib`, `subprocess`, `requests`, `paramiko`, `PyQt6`, scientific libraries, your own packages. Build whatever UI you want with `QDialog`, `QGraphicsView`, model/view, etc. The `text_reader_sample.py` shows a hand-built dialog that does its own font handling, find bar, and Save-As.

---

## Quopus internals you might call

If you choose to go beyond the public API, here's a roadmap to the most useful pieces. Treat as informal — none of this is contractually stable.

### Listers — `quopus_lib.lister.Lister`

```python
left  = api._host.w.left_lister
right = api._host.w.right_lister

left.current_path           # pathlib.Path of the directory shown
left.selected_paths()       # list[Path] of selected entries
left.refresh()              # re-read from disk
left.navigate(path)         # change directory (validates path)
left.set_filter(ext_or_glob)# apply the lister's extension filter
```

### Actions — `quopus_lib.actions.Actions`

```python
host = api._host
host._active()              # -> (active_lister, other_lister)
host._status("msg")         # write to status bar (api.log wraps this)
host.dispatch("hexread")    # run a built-in action by key
```

The built-in action keys live in `quopus_lib/action_catalog.py` — that's the authoritative list of what `dispatch()` accepts. Useful ones for chaining: `"hexread"`, `"view"`, `"edit"`, `"refresh"`, `"copy"`, `"move"`, `"delete"`, `"makedir"`, `"search"`.

### Config — `quopus_lib.config`

```python
from quopus_lib.config import CONFIG_DIR, load_config, save_config

cfg = api._host.w.config       # live dict; mutating it doesn't persist
load_config()                  # re-read quopus.cfg from disk
save_config(cfg)               # write current config back to disk
```

`CONFIG_DIR` is the folder where `quopus.cfg`, `quopus.lic`, and `custom_modules/` (user copy) all live. Use it to store **your** plugin's persistent state — e.g. `Path(CONFIG_DIR) / "my_plugin.json"` — instead of inventing your own location.

### Viewers — `quopus_lib.readers`

```python
from quopus_lib.readers import TextReader, HexReader

TextReader(path, parent=api.parent_widget).exec()
HexReader(path, parent=api.parent_widget).exec()
```

Use these if you want to delegate "show this file" to the built-in viewer rather than building your own — they handle PETSCII, Topaz, ANSI, paginated hex with edit mode and search out of the box.

### TAP toolkit — `quopus_lib.tap_toolkit`

```python
from quopus_lib.tap_toolkit import open_tap_toolkit
open_tap_toolkit(path, parent=api.parent_widget,
                 config=api.config)
```

### U64 devices — `quopus_lib.u64_devices`

```python
from quopus_lib.u64_devices import get_pool
pool = get_pool(api.config)        # configured U64s + Discovery results
for dev in pool.devices():
    print(dev.name, dev.host, dev.online)
```

### Streaming / long-running work

If your action takes more than a couple of seconds, the UI will freeze. The polite approach: spin up a `QThread` and signal completion back to your dialog. The lazy approach: `QApplication.processEvents()` in your loop. The hard rule: don't sleep on the main thread without giving Qt the event loop back.

---

## Safety, errors, threading

- **No sandbox.** Custom modules run in the same Python interpreter as Quopus. They can do anything Quopus can: read/write any file, open network connections, spawn subprocesses, mess with the UI. Treat `custom_modules/` like your `~/.bashrc` — only drop in code you trust.

- **Exceptions are caught.** If `run(api)` raises, the dispatcher logs the traceback and shows a message; Quopus stays alive. So an exception during a plugin run is debuggable, not catastrophic.

- **Module-load errors are visible.** If your `.py` file has a syntax error or fails to import, it's listed in `custom_modules.load_errors()`. The action picker shows broken modules with their error so you can find them.

- **Main-thread rule.** Any Qt UI call must happen on the main thread. `api.notify`, `api.input`, etc. already obey that — they're synchronous from the main thread's perspective. If you fork a `QThread`, marshal UI updates back via signals.

- **`api.refresh()` after FS changes.** Listers cache entries; without an explicit refresh the user won't see your new files until they navigate away and back.

---

## Distribution & sharing

A custom module is a single `.py` file. To share one, just send the file. The recipient drops it in their `custom_modules/` user folder and hits **Reload custom modules**.

If your module needs Python packages that aren't bundled with Quopus, list them at the top of the file in a comment so the user knows what to `pip install`:

```python
"""
Requires: requests, pillow
"""
ACTION_NAME = "..."
```

Importing them inside `run()` (lazy import) means the rest of Quopus still loads if the user forgot the install — they only see the error when they trigger the action.

There's no central registry / app-store for custom modules right now. If a community list emerges, this document will point at it.

---

*Last updated alongside Quopus Commander v1.0. The `CustomModuleAPI` surface is stable; the "Quopus internals you might call" section is best-effort.*
