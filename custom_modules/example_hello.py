"""
Example custom module for Quopus.

Demonstrates the basic structure of a user-defined action.
Drop a copy of this file into your custom_modules/ folder
(Config -> Open custom modules folder) and edit it to add
your own actions.

After saving, pick Config -> Reload custom modules and your
new action will appear under "Custom Modules" in the action
picker (right-click on any button, or F10 -> Action buttons).
"""

# ---- required metadata --------------------------------------
ACTION_NAME = "example_hello"

# ---- optional metadata --------------------------------------
ACTION_LABEL = "Example: Say Hello"
ACTION_DESCRIPTION = (
    "Demo custom module - shows the active panel's current "
    "directory and selection count in a popup. Use this file "
    "as a template for your own modules.")
ACTION_PARAM_LABEL = "Optional name to greet"


def run(api):
    """The action body. `api` is a CustomModuleAPI instance -
    see quopus_lib/custom_modules.py for the full list of
    methods. The most common ones:

      api.src_path        Path of active panel's dir
      api.dst_path        Path of other panel's dir
      api.selected        list[Path] of selected items
      api.param           str: the button's Param field
      api.log(msg)        status bar
      api.notify(t, b)    QMessageBox
      api.input(t, p)     text input dialog (Optional[str])
      api.ask_yes_no()    yes/no confirmation
      api.refresh()       re-list both panels
    """
    name = api.param.strip() or "World"
    body = [
        f"Hello, {name}!",
        "",
        f"Active panel:   {api.src_path}",
        f"Other panel:    {api.dst_path}",
        f"Selected items: {len(api.selected)}",
    ]
    if api.selected:
        body.append("")
        body.append("First few selections:")
        for p in api.selected[:5]:
            body.append(f"  - {p.name}")
        if len(api.selected) > 5:
            body.append(f"  ... and {len(api.selected) - 5} more")
    api.notify("Hello from a custom module", "\n".join(body))
