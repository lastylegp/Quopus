# date_time: 2026-05-28 08:36
"""Multi-Ultimate-64 device management for Quopus.

Some users have several Ultimate-64 boards on the same network -
one in the main rig, one in the workshop, one on the bookshelf,
etc. Rather than re-typing IP addresses every time you want to
push a file, Quopus stores up to N device profiles. The user
picks which one to talk to via a small dropdown / picker dialog.

Config schema (in main_window's config dict):

    u64_devices = [
        {
            "name":           "Living room U64",
            "host":           "192.168.1.50",
            "video_port":     11000,
            "audio_port":     11001,
            "telnet_port":    23,
            "http_port":      80,
            "password":       "",
            "video_only":     False,
            "always_on_top":  False,
        },
        ...
    ]
    u64_active_device = 0     # index into u64_devices

Plus the legacy keys u64_host / u64_video_port / ... are kept
in sync with the ACTIVE device so the rest of the codebase
(streamer, send-to-ultimate, asm64 run-on-U64) keeps working
without per-call modifications. New code should use the device-
aware get_active_device() and get_device_by_index() helpers.

`MAX_DEVICES = 3` cap is enforced in the config dialog UI so
users don't accidentally type a fourth one - the underlying
list could hold more, but our pickers and the config editor
only show three slots. If a future build wants more, raise the
constant and bump the config UI to add more tabs.
"""

from __future__ import annotations

from typing import Optional


MAX_DEVICES = 3


# Default values for a brand new device entry. Mirrors the
# constants from u64_streamer for port assignments.
_DEFAULT_DEVICE = {
    "name":          "",
    "host":          "",
    "video_port":    11000,
    "audio_port":    11001,
    "telnet_port":   23,
    "http_port":     80,
    "password":      "",
    "video_only":    False,
    "always_on_top": False,
}


def get_devices(config: dict) -> list[dict]:
    """Return the device list from config. Migrates the old
    single-device legacy keys (u64_host etc.) into a one-entry
    device list on first call so the rest of the code can be
    device-list-only.

    Migration is one-way - once we've built a device list, the
    legacy keys are still WRITTEN by sync_legacy_keys() to keep
    old callers happy, but never read again.
    """
    devices = config.get("u64_devices")
    if devices is None or not isinstance(devices, list):
        devices = []

    # If the legacy single-device keys are populated but the new
    # list is empty, build a one-entry list from the legacy
    # values. Preserves the user's existing config across the
    # upgrade.
    if not devices and config.get("u64_host"):
        legacy = dict(_DEFAULT_DEVICE)
        legacy["name"] = "U64"
        legacy["host"] = str(config.get("u64_host", ""))
        legacy["video_port"] = int(
            config.get("u64_video_port",
                       _DEFAULT_DEVICE["video_port"]))
        legacy["audio_port"] = int(
            config.get("u64_audio_port",
                       _DEFAULT_DEVICE["audio_port"]))
        legacy["telnet_port"] = int(
            config.get("u64_telnet_port",
                       _DEFAULT_DEVICE["telnet_port"]))
        legacy["http_port"] = int(
            config.get("u64_http_port",
                       _DEFAULT_DEVICE["http_port"]))
        legacy["password"] = str(config.get("u64_password", ""))
        legacy["video_only"] = bool(
            config.get("u64_video_only", False))
        legacy["always_on_top"] = bool(
            config.get("u64_always_on_top", False))
        devices = [legacy]
        config["u64_devices"] = devices

    # Normalise each entry: fill in any missing fields with the
    # defaults. Protects against partial dicts that survived a
    # crash mid-save, hand-edited configs, etc.
    normalised = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        full = dict(_DEFAULT_DEVICE)
        full.update(d)
        normalised.append(full)
    return normalised


def set_devices(config: dict, devices: list[dict]) -> None:
    """Store the (possibly edited) device list back into config.
    Truncates to MAX_DEVICES; padded entries that are empty
    placeholders (no host) are dropped.

    Also calls sync_legacy_keys() to update the old single-device
    config keys to match the new active device, so legacy
    callers see consistent values.
    """
    cleaned = []
    for d in devices[:MAX_DEVICES]:
        if not isinstance(d, dict):
            continue
        # Drop fully-empty placeholder rows. A device with no
        # host is unusable and just clutters the picker.
        if not (d.get("host") or "").strip():
            continue
        full = dict(_DEFAULT_DEVICE)
        full.update(d)
        cleaned.append(full)
    config["u64_devices"] = cleaned

    # Clamp active index to a valid range
    active = int(config.get("u64_active_device", 0) or 0)
    if cleaned:
        if active < 0 or active >= len(cleaned):
            active = 0
    else:
        active = 0
    config["u64_active_device"] = active

    sync_legacy_keys(config)


def get_active_index(config: dict) -> int:
    """Index of the currently-active device, clamped to a valid
    range. Returns 0 if no devices exist (callers should check
    get_devices() first to know whether to act)."""
    devices = get_devices(config)
    if not devices:
        return 0
    idx = int(config.get("u64_active_device", 0) or 0)
    if idx < 0 or idx >= len(devices):
        idx = 0
        config["u64_active_device"] = 0
    return idx


def set_active_index(config: dict, idx: int) -> None:
    """Persist a new active-device choice. Idempotent if the
    index is the same. Also re-syncs the legacy keys so the
    streamer / send-to-ultimate / asm64-run pick up the change
    on their next read."""
    devices = get_devices(config)
    if not devices:
        config["u64_active_device"] = 0
        return
    if idx < 0:
        idx = 0
    elif idx >= len(devices):
        idx = len(devices) - 1
    config["u64_active_device"] = idx
    sync_legacy_keys(config)


def get_active_device(config: dict) -> Optional[dict]:
    """Returns the device dict for the currently-active U64, or
    None if no devices are configured. Callers checking
    'do we have a U64 at all?' should look at the truthiness
    of the returned value or call get_devices() and check len()."""
    devices = get_devices(config)
    if not devices:
        return None
    return devices[get_active_index(config)]


def get_device_by_index(config: dict,
                        idx: int) -> Optional[dict]:
    """Pick a specific device by index without changing the
    active one. Used by 'send to which U64?' pickers where the
    user picks per-action without changing the global default."""
    devices = get_devices(config)
    if not devices or idx < 0 or idx >= len(devices):
        return None
    return devices[idx]


def sync_legacy_keys(config: dict) -> None:
    """Mirror the active device's fields into the legacy single-
    device keys (u64_host, u64_video_port, etc.) so the existing
    code in actions.py / u64_streamer.py / asm64_browser.py
    keeps working without per-call modifications.

    Called automatically by set_devices() and set_active_index().
    Safe to call manually if a caller wants to ensure consistency
    after modifying the active device in place.
    """
    devices = get_devices(config)
    if not devices:
        return
    active = devices[get_active_index(config)]
    config["u64_host"] = active.get("host", "") or ""
    config["u64_video_port"] = int(
        active.get("video_port",
                   _DEFAULT_DEVICE["video_port"]))
    config["u64_audio_port"] = int(
        active.get("audio_port",
                   _DEFAULT_DEVICE["audio_port"]))
    config["u64_telnet_port"] = int(
        active.get("telnet_port",
                   _DEFAULT_DEVICE["telnet_port"]))
    config["u64_http_port"] = int(
        active.get("http_port",
                   _DEFAULT_DEVICE["http_port"]))
    config["u64_password"] = active.get("password", "") or ""
    config["u64_video_only"] = bool(
        active.get("video_only", False))
    config["u64_always_on_top"] = bool(
        active.get("always_on_top", False))


def device_display_name(device: dict, idx: int = -1) -> str:
    """Human-readable label for a device. Prefers the user-set
    name; falls back to the host with an index hint if no name
    is set. Used in dropdowns / pickers / status labels.

    Examples:
        {"name": "Living room", "host": "192.168.1.50"}
            -> "Living room  (192.168.1.50)"
        {"name": "", "host": "192.168.1.50"}, idx=1
            -> "U64 #2 - 192.168.1.50"
    """
    name = (device.get("name") or "").strip()
    host = (device.get("host") or "").strip()
    if name:
        if host:
            return f"{name}  ({host})"
        return name
    if host:
        if idx >= 0:
            return f"U64 #{idx + 1} - {host}"
        return host
    return f"U64 #{idx + 1}" if idx >= 0 else "U64"


def pick_device(parent, config: dict,
                title: str = "Choose Ultimate-64",
                prompt: str = "Which U64 should "
                              "receive this?") -> Optional[dict]:
    """Modal picker dialog. Returns the chosen device dict or
    None if the user cancelled.

    Behaviour by device count:
      0 devices: show a warning, return None - caller falls back
                 to its own "no U64 configured" handling
      1 device:  skip the dialog entirely, return that device
      2+:        QInputDialog.getItem-style chooser

    Doesn't modify config - the user's pick is per-action, not
    a default change. Use the Config dialog to permanently
    change the active device.
    """
    from PyQt6.QtWidgets import (
        QInputDialog, QMessageBox, QPushButton)
    devices = get_devices(config)
    if not devices:
        # Friendlier than just a warning - offer to open the
        # config dialog right now. The action key the launcher
        # uses for "Ultimate 64 device config" is "u64_config",
        # registered in actions.py:act_u64_config and reachable
        # through the actions dropdown in the main toolbar.
        # Most users hit this dialog before they know that key
        # exists, so we provide a direct shortcut.
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText("No Ultimate-64 device is configured yet.")
        box.setInformativeText(
            "Add at least one device with its IP address "
            "before running things on a U64.\n\n"
            "Click 'Configure now' to open the device config "
            "dialog, or 'Cancel' to skip.")
        configure_btn = box.addButton(
            "Configure now",
            QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is configure_btn:
            # Trigger the u64_config action directly. Imported
            # lazily so this helper module doesn't take a hard
            # dependency on actions.py at import time.
            try:
                from .actions import open_u64_config_dialog
            except ImportError:
                open_u64_config_dialog = None
            if open_u64_config_dialog is not None:
                open_u64_config_dialog(parent, config)
                # Re-check after the user closed the config
                # dialog. If they added a device, fall through
                # to the normal picker logic.
                devices = get_devices(config)
                if devices:
                    if len(devices) == 1:
                        return devices[0]
                    # Multiple devices - normal picker
                else:
                    return None
            else:
                # Config helper not exposed - just tell the
                # user where to find it.
                QMessageBox.information(
                    parent, title,
                    "Open the actions dropdown in the main "
                    "toolbar and pick:\n"
                    "  CBM / C64 tools  ->  "
                    "Ultimate 64 device config")
                return None
        else:
            return None
    if len(devices) == 1:
        return devices[0]
    # 2+ devices: custom picker dialog with a third "Config..."
    # button alongside OK/Cancel so the user can jump straight
    # into the device config without having to cancel, hunt for
    # the config action, edit, and re-trigger. The config button
    # reopens the picker afterwards so a freshly-added/edited
    # device is immediately selectable.
    return _pick_device_dialog(parent, config, title, prompt)


def _pick_device_dialog(parent, config, title, prompt):
    """Custom multi-device picker with OK / Cancel / Config...
    buttons. Returns the chosen device dict or None.

    Split out from pick_device so the post-config re-entry can
    just call this again (the no-device and single-device fast
    paths in pick_device don't need to repeat).
    """
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
        QPushButton)
    devices = get_devices(config)
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0]

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(360)
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel(prompt))

    cmb = QComboBox()
    labels = [device_display_name(d, i)
              for i, d in enumerate(devices)]
    cmb.addItems(labels)
    default_idx = get_active_index(config)
    if 0 <= default_idx < len(labels):
        cmb.setCurrentIndex(default_idx)
    v.addWidget(cmb)

    # Button row: Cancel | Config... | OK
    # Config sits between Cancel and OK exactly as requested.
    btn_row = QHBoxLayout()
    btn_cancel = QPushButton("Cancel")
    btn_config = QPushButton("Config...")
    btn_config.setToolTip(
        "Open the Ultimate-64 device configuration\n"
        "to add, edit or remove devices. The picker\n"
        "refreshes afterwards.")
    btn_ok = QPushButton("OK")
    btn_ok.setDefault(True)
    btn_row.addStretch(1)
    btn_row.addWidget(btn_cancel)
    btn_row.addWidget(btn_config)
    btn_row.addWidget(btn_ok)
    v.addLayout(btn_row)

    # Result holder - we use a mutable cell so the nested
    # handlers can write to it.
    result = {"value": None, "reopen": False, "chosen_idx": -1}

    def _do_ok():
        idx = cmb.currentIndex()
        if 0 <= idx < len(devices):
            result["value"] = devices[idx]
            result["chosen_idx"] = idx
        dlg.accept()

    def _do_cancel():
        result["value"] = None
        dlg.reject()

    def _do_config():
        # Open the config dialog. After it closes, we want to
        # reopen the picker with the (possibly changed) device
        # list - so we set a reopen flag and close this dialog
        # with a neutral result. pick_device's caller path
        # handles the reopen below.
        result["reopen"] = True
        dlg.accept()

    btn_ok.clicked.connect(_do_ok)
    btn_cancel.clicked.connect(_do_cancel)
    btn_config.clicked.connect(_do_config)

    dlg.exec()

    if result["reopen"]:
        # User clicked Config... - open the device config dialog,
        # then re-run the picker so the new device list is shown.
        try:
            from .actions import open_u64_config_dialog
        except ImportError:
            open_u64_config_dialog = None
        if open_u64_config_dialog is not None:
            open_u64_config_dialog(parent, config)
        # Re-evaluate after config: device count may have changed.
        new_devices = get_devices(config)
        if not new_devices:
            return None
        if len(new_devices) == 1:
            return new_devices[0]
        # Still multiple - show the picker again. Recursion depth
        # is bounded by user patience; each Config... round trips
        # through a modal dialog so there's no runaway loop.
        return _pick_device_dialog(parent, config, title, prompt)

    # User picked a device with OK - remember it as the new
    # default so the next time the picker opens (Run on U64,
    # Stream, Mount disk, ...) this device is pre-selected.
    # We persist to config AND save it to disk so the choice
    # survives a Quopus restart, not just the session. Cancel
    # and Config... don't reach here with a chosen_idx so the
    # default only moves on a deliberate OK selection.
    if result["chosen_idx"] >= 0:
        set_active_index(config, result["chosen_idx"])
        try:
            from .config import save_config
            save_config(config)
        except Exception:
            # Non-fatal: the in-memory default is updated even
            # if the disk write fails, so the picker still
            # remembers within the session.
            pass

    return result["value"]
