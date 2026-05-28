# date_time: 2026-05-28 08:20
"""
Central catalog of all Quopus actions, organized into groups.

This is the single source of truth for the list of actions that can
be bound to action-buttons, both in the per-button edit dialog (reached
via right-click on a button) and in the bulk button-config dialog
(reached via F10 → Action buttons...). Previously each of those dialogs
maintained its own copy of the action list, with different ordering
and different completeness - the right-click dialog had grouped
submenus while the F10 dialog had a flat ComboBox-style dropdown.

Ordering inside each group is by frequency-of-use where clear,
otherwise alphabetical. Trial-tier gating is enforced by the
dispatcher itself in actions.py, not here.

Schema:
  ACTION_GROUPS: list[(group_label, list[(key, label)])]

Helpers:
  flat_action_keys()       - returns [key, ...] preserving group order
  action_label_map()       - returns {key: label}
  build_action_picker_btn(parent, current_key, on_change_cb, *,
                           include_empty=False) -> QPushButton

  The picker is a QPushButton whose menu is the grouped action list.
  on_change_cb is called with the new key whenever the user picks one.
  include_empty adds an "(empty)" entry that maps to "" so callers
  using this for slot-assignment grids can show unbound rows.
"""
from __future__ import annotations
from .config import scaled_font_px


# --------------------------------------------------------------------
# The catalog itself
# --------------------------------------------------------------------
ACTION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Viewers", [
        ("read",         "Read (internal auto)"),
        ("hexread",      "Hex read"),
        ("show",         "Show"),
        ("edit",         "Edit (configured editor)"),
        ("play",         "Play"),
        ("info",         "Info / properties"),
        ("compare",      "Compare files (text/hex diff)"),
    ]),
    ("File operations", [
        ("copy",         "Copy"),
        ("move",         "Move"),
        ("delete",       "Delete"),
        ("rename",       "Rename"),
        ("multi_rename", "Multi-rename tool"),
        ("makedir",      "Make directory"),
        ("select_all",   "Select all"),
        ("select_none",  "Select none"),
        ("getsizes",     "Get sizes"),
        ("checkfit",     "Check fit"),
        ("comment",      "Edit comment"),
        ("datestamp",    "Datestamp"),
        ("protect",      "Protect/attributes"),
        ("archive",      "Archive"),
        ("extract",      "Extract / Arc Ext"),
    ]),
    ("Navigation", [
        ("parent",       "Parent"),
        ("root",         "Root"),
        ("goto_dir",     "Go to directory (folder shortcut)"),
        ("back",         "Back"),
        ("forward",      "Forward"),
        ("swap",         "Swap sides"),
        ("reread",       "Reread"),
        ("search",       "Search"),
        ("find",         "Hunt"),
        ("dir_reverse",  "Dir reverse (AmiExpress)"),
        ("toggle_non_dos83", "Toggle 'Hide 8+3 filenames' filter"),
        ("buffers",      "Buffers"),
    ]),
    ("Audio: SID", [
        ("sidplayer",          "SID Player (single file)"),
        ("sidplayer_playlist", "SID Playlist (browse selected)"),
        ("multi_sid",          "Multi-SID (parallel mix)"),
        ("shuffle_sids",       "Shuffle play SIDs"),
    ]),
    ("Audio: MOD", [
        ("modplayer",          "MOD Player (single file)"),
        ("modplayer_playlist", "MOD Playlist (browse selected)"),
        ("shuffle_mods",       "Shuffle play modules"),
    ]),
    ("CBM / C64 tools", [
        ("d64editor",     "D64/D71/D81 disk editor"),
        ("basic_editor",  "BASIC v2 editor"),
        ("disasm",        "C64 6502 disassembler"),
        ("crt_toolkit",   "CRT cartridge toolkit"),
        ("asm64",         "Assembly64 browser"),
        ("run_emu",       "Run in C64 emulator (VICE/x64sc)"),
        ("run_u64",       "Run on Ultimate-64 (real hardware)"),
        ("u64view",       "Ultimate 64 stream viewer"),
        ("u64_config",    "Ultimate 64 device config"),
        ("vice_memory",   "VICE memory grab (binary monitor)"),
        ("c64_emu_config", "C64 emulator config (path/args)"),
    ]),
    ("Amiga tools", [
        ("adf_viewer",        "ADF disk image viewer/editor"),
        ("adf_new",           "ADF: create new blank disk"),
        ("amigaguide_viewer", "AmigaGuide hypertext viewer"),
    ]),
    ("Graphics viewers", [
        ("image_viewer",     "Image viewer (PNG/JPG/GIF/...)"),
        ("archive_viewer",   "Archive viewer (ZIP/LHA/LZX/...)"),
        ("retrogfx",         "Retro GFX viewer (open selected file)"),
        ("retrogfx_browser", "Retro GFX browser (550+ formats)"),
        ("retrogfx_file",    "Retro GFX open (selection or picker)"),
    ]),
    ("Networking", [
        ("ftp",          "FTP connect (open dialog)"),
        ("ftp_site",     "FTP site (direct connect to bookmark)"),
        ("ftp_upload",   "FTP upload (upload selection from other panel)"),
        ("telnet",       "Telnet / SSH / Raw TCP client"),
        ("qdrive",       "Quopus Drive connect (open dialog)"),
        ("qdrive_site",  "Quopus Drive site (direct connect to bookmark)"),
        ("database",     "Quopus Database (catalog and search archives)"),
    ]),
    ("Cloud storage", [
        ("rclone",       "Rclone browser (70+ cloud providers via rclone)"),
        ("rclone_setup", "Rclone setup (configure cloud accounts)"),
    ]),
    ("Text conversion", [
        ("petscii_convert",  "ASCII<->PETSCII (dialog)"),
        ("ascii_to_petscii", "ASCII->PETSCII (direct)"),
        ("petscii_to_ascii", "PETSCII->ASCII (direct)"),
    ]),
    ("Execute / custom", [
        ("run",              "Run / Execute"),
        ("shell",            "Shell"),
        ("print",            "Print"),
        ("external_script",  "External script"),
        ("execute_command",  "Execute shell command"),
        ("custom_cmd",       "Custom command"),
        ("assign",           "Assign (drive)"),
        ("hotkey",           "Hotkey (simulate built-in shortcut)"),
    ]),
    ("System", [
        ("config",  "Config"),
        ("license", "License info / register Pro"),
        ("about",   "About"),
        ("quit",    "Quit"),
    ]),
]


def _custom_modules_group() -> tuple[str, list[tuple[str, str]]]:
    """Build the Custom Modules group from the live registry.
    Returns an empty list of items when no modules are loaded,
    so callers can drop the group entirely if they want."""
    try:
        from . import custom_modules
        items = [(m.action_name, m.label)
                 for m in custom_modules.all_modules()]
    except Exception:
        items = []
    return ("Custom Modules", items)


def get_action_groups(include_custom: bool = True
                       ) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return the catalog as a list of (group_name, items) pairs.

    With include_custom=True (the default) we append a
    "Custom Modules" group built dynamically from the
    custom_modules registry. Both editors (right-click and F10)
    call us with include_custom=True so user-installed actions
    appear right next to the built-ins.

    The Custom Modules group is OMITTED entirely when there are
    no loaded modules - the user shouldn't see an empty submenu
    in the picker. Once they drop a file into custom_modules/
    and pick "Reload custom modules", the group appears.
    """
    groups = list(ACTION_GROUPS)
    if include_custom:
        cm_group = _custom_modules_group()
        if cm_group[1]:                     # only if non-empty
            groups.append(cm_group)
    return groups


def flat_action_keys() -> list[str]:
    """Return all action keys in catalog order (groups concatenated).
    Used by the F10 → Action buttons dialog when populating the
    legacy ComboBox path; ensures the dropdown order matches the
    right-click submenu order so users don't see two different
    sortings. Includes custom-module actions if any are loaded."""
    out = []
    for _grp, items in get_action_groups(include_custom=True):
        for key, _label in items:
            out.append(key)
    return out


def action_label_map() -> dict[str, str]:
    """Return {key: human_label} for every catalog entry,
    including custom-module entries."""
    out = {}
    for _grp, items in get_action_groups(include_custom=True):
        for key, label in items:
            out[key] = label
    return out


def build_action_picker_button(parent, current_key: str,
                                on_change,
                                *, include_empty: bool = False,
                                width_hint: int | None = None):
    """Build a QPushButton that, when clicked, drops a hierarchical
    menu of action groups. Picking an entry calls on_change(new_key).

    The button label is "Label  [key]" so the user sees what's
    currently selected without opening the menu. Styling matches
    the Quopus Workbench look (grey menu, blue selection bar) used
    by the lister context menu and the right-click button editor.

    parent:        QWidget owner (for menu parenting + style scope)
    current_key:   action key shown initially; "" means empty
    on_change:     callable(str) invoked with the new key
    include_empty: prepend an "(empty)" choice that yields key ""
    width_hint:    optional minimum button width in pixels; useful
                   when laying out a grid of pickers so they line up

    Returns the QPushButton. Caller is responsible for adding it
    to a layout. The button keeps refs to its menu via setMenu, and
    the menu actions hold references to the on_change callback via
    closure capture, so no extra bookkeeping is needed.
    """
    from PyQt6.QtWidgets import QPushButton, QMenu

    labels = action_label_map()

    btn = QPushButton(parent)
    btn.setStyleSheet(
        "QPushButton { background-color: #ffffff; "
        "color: #000000; border: 1px solid #000000; "
        "padding: 4px 8px; "
        "font-family: 'Topaz','Courier New',monospace; "
        f"font-size: {scaled_font_px(12)}px; text-align: left; }}"
        "QPushButton::menu-indicator { "
        "subcontrol-origin: padding; subcontrol-position: "
        "right center; }")
    if width_hint:
        btn.setMinimumWidth(width_hint)

    menu = QMenu(parent)
    menu.setStyleSheet(
        "QMenu { background-color: #cccccc; color: #000000; "
        "border: 1px solid #000000; "
        "font-family: 'Topaz','Courier New',monospace; "
        f"font-size: {scaled_font_px(12)}px; }} "
        "QMenu::item:selected { background-color: #5566ff; "
        "color: #ffffff; }")

    # State holder closure so the button can refresh its own label
    state = {"key": current_key if current_key in labels
                                or current_key == "" else "read"}

    def _refresh_label():
        k = state["key"]
        if not k:
            btn.setText("(empty)  ▼")
        else:
            lbl = labels.get(k, k)
            btn.setText(f"{lbl}  [{k}]")

    def _pick(new_key):
        state["key"] = new_key
        _refresh_label()
        try:
            on_change(new_key)
        except Exception as e:
            # Don't let UI callbacks bring down the dialog; just
            # log so the user gets feedback that something broke.
            print(f"  [action_picker] on_change callback failed: {e}")

    def _make_handler(k):
        # Capture k in a closure so each menu entry remembers its
        # own key, not the loop variable's last value.
        def _h():
            _pick(k)
        return _h

    if include_empty:
        act = menu.addAction("(empty - clear slot)")
        act.triggered.connect(_make_handler(""))
        menu.addSeparator()

    for grp_name, items in get_action_groups(include_custom=True):
        sub = menu.addMenu(grp_name)
        sub.setStyleSheet(menu.styleSheet())
        for key, label in items:
            a = sub.addAction(f"{label}  [{key}]")
            a.triggered.connect(_make_handler(key))

    btn.setMenu(menu)

    # Expose getter + refresh for callers that want to update the
    # picker programmatically (e.g. when restoring per-row state
    # in a grid editor).
    def _get_key():
        return state["key"]
    def _set_key(k, fire=False):
        state["key"] = k
        _refresh_label()
        if fire:
            try:
                on_change(k)
            except Exception:
                pass
    btn.get_action_key = _get_key       # type: ignore[attr-defined]
    btn.set_action_key = _set_key       # type: ignore[attr-defined]

    # ComboBox-compatible shims so this picker can drop into call
    # sites that were written for QComboBox. The bulk button-config
    # dialog uses .currentText() to read the chosen action and
    # .setCurrentText() to set it, and a clear-row callback that
    # passes A.setCurrentText("") to wipe a slot.
    btn.currentText = _get_key          # type: ignore[attr-defined]
    btn.setCurrentText = lambda t: _set_key(   # type: ignore[attr-defined]
        t if t in labels or t == "" else state["key"])

    _refresh_label()
    return btn
