# Custom Modules

This is where your custom Quopus actions live. Drop a `.py` file
here, hit **Config → Reload custom modules**, and it appears as
a bindable action under "Custom Modules" in the action picker.

## Quick start

1. Copy `example_hello.py` to a new name.
2. Edit `ACTION_NAME` (must be unique) and the `run(api)` body.
3. **Config → Reload custom modules**.
4. Right-click a button → Assign to button → pick your action.

## The two example modules

- `example_hello.py` — minimal template, the absolute basics.
- `text_reader_sample.py` — bigger example with its own `QDialog`,
  find bar, encoding fallback, multiple imports done lazily.

## Full reference

For the complete API reference — every method on the `api`
object, how discovery and reload work, how to reach into
Quopus internals when the public surface isn't enough — see:

**[`CUSTOM_MODULES.md`](../CUSTOM_MODULES.md)** in the Quopus
repository root.

## At a glance

```python
ACTION_NAME = "my_action"          # required, unique key
ACTION_LABEL = "Pretty Name"       # optional, for the picker
ACTION_DESCRIPTION = "Tooltip"     # optional
ACTION_PARAM_LABEL = "Param hint"  # optional

def run(api):
    # api.src_path / api.dst_path  -> Path of active / other panel
    # api.selected                  -> list[Path] of selected items
    # api.param                     -> str: button's Param field
    # api.config                    -> dict: Quopus config
    # api.parent_widget             -> QWidget for QDialog parenting
    # api.log("msg")                -> status bar
    # api.refresh()                 -> re-list both panels
    # api.notify(t, b, kind=...)    -> QMessageBox
    # api.input(t, p, default="")   -> text input, None on cancel
    # api.ask_yes_no(t, b)          -> bool
    # api.pick_file(t, save=False, filters="...") -> Path or None
    # api.pick_dir(t)               -> Path or None
    api.notify("Hello", f"Active dir: {api.src_path}")
```

## Where this folder lives

Two locations are scanned (user copy wins on name collision):

1. `<exe-dir>/custom_modules/` — portable / shipped.
2. `<user-config-dir>/custom_modules/` — the writeable one,
   survives Quopus updates. **This is where you put new modules.**
   - Windows: `%APPDATA%\quopus\custom_modules\`
   - macOS: `~/Library/Application Support/quopus/custom_modules/`
   - Linux: `~/.config/quopus/custom_modules/`

Open it from inside Quopus: **Config → Open custom modules folder**.

## Safety

Custom modules run in the same Python interpreter as Quopus, with
full filesystem and network access. There is no sandbox. Treat
this folder like your `~/.bashrc` — only drop in code you trust.
