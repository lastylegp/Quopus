"""Trial-mode UI hooks: nag screen at startup, window-title
watermark, registration dialog.

All three are no-ops when license.is_registered() returns True.
The intent is that the entire registration UI never appears for
paying customers, while trial users are reminded every session.
"""
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QFileDialog, QApplication,
)

from . import license
from .config import scaled_font_px


# ============================================================
# Nag screen at startup
# ============================================================


class NagDialog(QDialog):
    """The "register me" reminder shown on each trial startup.

    Designed to be mildly annoying (3-second delay before the OK
    button enables) but not abusive. Power users will work around
    it but each trial session gets the chance to convert."""

    NAG_DELAY_SECONDS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quopus Commander - Trial Version")
        self.setModal(True)
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        # Headline
        title = QLabel("Quopus Commander - Trial Version")
        f = QFont("Arial", 16, QFont.Weight.Bold)
        title.setFont(f)
        title.setStyleSheet("color: #1a3a8a;")
        lay.addWidget(title)

        # Body text - explains the trial limits without making
        # the user feel scolded
        body = QLabel(
            "<p>You are running the unregistered trial version.</p>"
            "<p>Trial limitations:</p>"
            "<ul>"
            "<li>Phonebook limited to 3 saved sessions</li>"
            "<li>Telnet sessions auto-disconnect after 5 minutes</li>"
            "<li>SID Player tunes time out after 30 seconds</li>"
            "<li>Multi-SID, encrypted modules disabled</li>"
            "<li>Window titles show 'TRIAL'</li>"
            "</ul>"
            "<p>Buy a license to unlock all features and remove "
            "this dialog:</p>"
            "<p><b>https://your-website.example/quopus</b></p>")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        lay.addWidget(body)

        # Buttons
        row = QHBoxLayout()
        self.btn_register = QPushButton("Enter License File...")
        self.btn_register.clicked.connect(self._on_register)
        row.addWidget(self.btn_register)
        row.addStretch(1)
        self.btn_ok = QPushButton(
            f"Continue Trial ({self.NAG_DELAY_SECONDS}s)")
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_ok.setDefault(True)
        row.addWidget(self.btn_ok)
        lay.addLayout(row)

        # Tick the countdown so the OK button enables after a few
        # seconds. Even if the user spams Enter, they wait.
        self._remaining = self.NAG_DELAY_SECONDS
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self.btn_ok.setText("Continue Trial")
            self.btn_ok.setEnabled(True)
            self._timer.stop()
        else:
            self.btn_ok.setText(
                f"Continue Trial ({self._remaining}s)")

    def _on_register(self):
        """Open a file dialog to import a .lic file. If valid,
        copy it into config/ and close the nag screen."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Quopus License File",
            str(Path.home()),
            "Quopus Licenses (*.lic);;All Files (*)")
        if not path:
            return
        # Verify before copying so a corrupt file doesn't pollute
        # the config dir.
        info = license.parse_license_file(Path(path))
        if not info.valid:
            QMessageBox.warning(
                self, "Invalid License",
                f"That license file isn't valid:\n\n{info.error}")
            return
        # Copy to config dir
        from .config import CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        target = CONFIG_DIR / "quopus.lic"
        target.write_bytes(Path(path).read_bytes())
        license.reset_cache()
        # Now load + show confirmation
        new_info = license.load_license()
        if new_info.valid:
            QMessageBox.information(
                self, "Welcome",
                f"Registration successful!\n\n"
                f"Registered to: {new_info.name}\n"
                f"Email: {new_info.email}\n"
                f"Features: {', '.join(new_info.features) or '(none)'}"
                f"\n\nRestart Quopus to activate all premium features.")
            self.accept()


def show_nag_if_needed(parent=None):
    """Show the nag dialog if this is a trial user. Blocks until
    the user clicks Continue. No-op for registered users."""
    if license.is_registered():
        return
    if license.has_feature(license.FEATURE_NO_NAG):
        # Some license tiers might suppress the nag - this lets
        # us issue special "no nag" trial licenses for press
        # reviewers etc without giving them full pro access.
        return
    dlg = NagDialog(parent)
    dlg.exec()


# ============================================================
# Window title watermark
# ============================================================


def watermark_title(base_title: str) -> str:
    """Decorate a window title based on license state.

    Three cases:
      - No license:    "<base>  [TRIAL]"
      - Trial license: "<base>  [<Name> TRIAL]"
                       (demo-signed, has name but is_registered=False)
      - Pro license:   "<base>  [<Name> <email>]"
                       (real key signature, is_registered=True)
    """
    lic = license.load_license()

    # No license OR demo-trial-signed license -> trial branding
    if not license.is_registered():
        # NO_WATERMARK suppresses the [TRIAL] tag - for press
        # review copies that should look like Pro
        if license.has_feature(license.FEATURE_NO_WATERMARK):
            return base_title
        # If we have name from a trial license, include it
        if lic.valid and lic.name:
            return f"{base_title}  [{lic.name} TRIAL]"
        return f"{base_title}  [TRIAL]"

    # Registered (Pro/lifetime): personalize the title with name
    if lic.name and lic.email:
        return f"{base_title}  [{lic.name} <{lic.email}>]"
    if lic.name:
        return f"{base_title}  [{lic.name}]"
    if lic.email:
        return f"{base_title}  [{lic.email}]"
    return base_title


def apply_watermark(widget):
    """Apply the license-aware decoration to a widget's title.

    We detect an existing decoration by the double-space separator
    "  [" we put there ourselves - this lets us strip + re-apply
    safely on title changes, without false-positives from titles
    that happen to contain a "[" somewhere (e.g. a filename with
    bracket characters in a tab title)."""
    cur = widget.windowTitle()
    # Strip any prior watermark we added
    if "  [TRIAL]" in cur:
        cur = cur.replace("  [TRIAL]", "")
    elif "  [" in cur:
        # Drop everything from "  [" onwards - that's our marker
        cur = cur.split("  [", 1)[0]
    widget.setWindowTitle(watermark_title(cur))


# ============================================================
# License Info dialog (visible from action picker)
# ============================================================


class LicenseInfoDialog(QDialog):
    """Show the current license status and let the user import a
    new .lic file or remove the existing one.

    Layout:
        Top section:   header + status box (license details)
        Middle:        list of feature flags this license grants
        Bottom:        action buttons (Import file / Import ZIP /
                       Remove / Refresh / Close)

    All operations are non-destructive except 'Remove' which has
    a confirm dialog. After any import / remove we call
    license.reload_license() and refresh the display, then notify
    the main window so the title-bar watermark and About dialog
    update too.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quopus License")
        self.resize(640, 520)
        self.setModal(True)
        self._parent_window = parent
        self._build_ui()
        self._refresh_display()

    def _build_ui(self):
        from PyQt6.QtWidgets import (
            QGroupBox, QPlainTextEdit, QListWidget,
        )
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(12, 12, 12, 12)

        # Header - tier badge prominently
        self.lbl_tier_badge = QLabel("")
        self.lbl_tier_badge.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self.lbl_tier_badge.setStyleSheet(
            f"font-size: {scaled_font_px(22)}px; font-weight: bold; padding: 8px;")
        outer.addWidget(self.lbl_tier_badge)

        # License details group
        gb_info = QGroupBox("License details")
        info_lay = QVBoxLayout(gb_info)
        self.txt_info = QPlainTextEdit()
        self.txt_info.setReadOnly(True)
        self.txt_info.setMaximumHeight(160)
        self.txt_info.setStyleSheet(
            "font-family: 'Courier New', monospace; "
            f"font-size: {scaled_font_px(11)}px; background: #f8f8f8;")
        info_lay.addWidget(self.txt_info)
        outer.addWidget(gb_info)

        # Features list
        gb_feat = QGroupBox("Active features")
        feat_lay = QVBoxLayout(gb_feat)
        self.lst_features = QListWidget()
        self.lst_features.setStyleSheet(
            "QListWidget { background: #ffffff; color: #000; }")
        feat_lay.addWidget(self.lst_features)
        outer.addWidget(gb_feat, 1)

        # Status message (errors, success after import etc.)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(
            f"color: #444; padding: 4px; font-size: {scaled_font_px(11)}px;")
        outer.addWidget(self.lbl_status)

        # Action buttons row
        btn_row = QHBoxLayout()
        btn_import = QPushButton("Import .lic file...")
        btn_import.setToolTip(
            "Select a quopus.lic file to install. The file is\n"
            "copied to <quopus>/config/quopus.lic and the\n"
            "license is reloaded immediately - no restart.")
        btn_import.clicked.connect(self._on_import_lic)
        btn_row.addWidget(btn_import)

        btn_import_pkg = QPushButton("Import .zip package...")
        btn_import_pkg.setToolTip(
            "Import a customer ZIP that contains both\n"
            "quopus.lic AND quopus_keys.cfg. This is what you\n"
            "get when the issuer used 'package-license' - one\n"
            "file with everything you need.")
        btn_import_pkg.clicked.connect(self._on_import_zip)
        btn_row.addWidget(btn_import_pkg)

        btn_row.addStretch(1)

        btn_remove = QPushButton("Remove license")
        btn_remove.setToolTip(
            "Delete the installed license file and revert to\n"
            "trial mode. Doesn't touch quopus_keys.cfg, so a\n"
            "future Pro license import still works.")
        btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(btn_remove)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.setToolTip(
            "Re-read the license file from disk. Useful if\n"
            "you manually edited config/quopus.lic.")
        btn_refresh.clicked.connect(self._on_refresh)
        btn_row.addWidget(btn_refresh)

        btn_diag = QPushButton("Diagnostics...")
        btn_diag.setToolTip(
            "Show details about where Quopus looked for your\n"
            "license and key config file, which public key is\n"
            "active (Demo vs Production), and a hint about\n"
            "common causes of 'License invalid' errors.")
        btn_diag.clicked.connect(self._on_show_diagnostics)
        btn_row.addWidget(btn_diag)

        outer.addLayout(btn_row)

        # Close
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_close.setDefault(True)
        close_row.addWidget(btn_close)
        outer.addLayout(close_row)

    def _refresh_display(self):
        """Pull the current license state and update every UI
        element. Called at construction and after every import /
        remove operation."""
        lic = license.load_license()
        path = license.find_license_file()

        # Tier badge - colored differently per tier so the
        # status is readable at a glance
        if not lic.valid:
            # Trial / no license / verification failed
            if path is None:
                badge = "TRIAL MODE"
                color = "#888"
            else:
                badge = "LICENSE INVALID"
                color = "#a00"
        elif lic.tier == "trial":
            badge = "TRIAL (signed)"
            color = "#888"
        elif lic.tier == "pro":
            badge = "PRO"
            color = "#080"
        elif lic.tier == "lifetime":
            badge = "LIFETIME PRO"
            color = "#a06"
        else:
            badge = lic.tier.upper()
            color = "#444"
        self.lbl_tier_badge.setText(badge)
        self.lbl_tier_badge.setStyleSheet(
            f"font-size: {scaled_font_px(22)}px; font-weight: bold; "
            f"padding: 8px; color: {color}; "
            f"border: 2px solid {color}; "
            f"border-radius: 6px;")

        # License details text block
        lines = []
        if path is None:
            lines.append("No license file installed.")
            lines.append("")
            lines.append(
                "Quopus is running in Trial mode. All features")
            lines.append(
                "are usable but with the limits described in the")
            lines.append(
                "feature list below.")
        else:
            lines.append(f"License file: {path}")
            lines.append("")
            if lic.error:
                lines.append(f"[ERROR] {lic.error}")
                lines.append("")
            if lic.name:
                lines.append(f"Licensed to: {lic.name}")
            if lic.email:
                lines.append(f"E-mail:      {lic.email}")
            if lic.license_id:
                lines.append(f"License ID:  {lic.license_id}")
            if lic.tier:
                lines.append(f"Tier:        {lic.tier}")
            if lic.issued_at:
                import datetime
                dt = datetime.datetime.fromtimestamp(
                    lic.issued_at)
                lines.append(
                    f"Issued:      "
                    f"{dt.strftime('%Y-%m-%d')}")
            if lic.expires_at:
                import datetime
                dt = datetime.datetime.fromtimestamp(
                    lic.expires_at)
                lines.append(
                    f"Expires:     "
                    f"{dt.strftime('%Y-%m-%d')}")
            elif lic.valid and lic.tier in ("pro", "lifetime"):
                lines.append(f"Expires:     never")
        self.txt_info.setPlainText("\n".join(lines))

        # Feature list - show every known Pro flag with a marker
        # for whether THIS license grants it. Without this, missing
        # flags are invisible: if the license has PRO_TELNET but
        # not PRO_DB_UNLIMITED, the old list just showed
        # PRO_TELNET with a check and the user assumed everything
        # else was a bug. Now they see the complete picture.
        self.lst_features.clear()
        if lic.valid:
            # Catalog: each known Pro flag with a human label and
            # one-line explanation of what it unlocks. Source of
            # truth for the list itself is license.py's
            # FEATURE_* constants - we just pair them with text
            # here.
            known_features = [
                ("PRO_DB_UNLIMITED",
                 "Database indexer",
                 "Unlimited catalog size (trial cap: 1000 disks)"),
                ("PRO_TELNET",
                 "Telnet client",
                 "Unlimited session length (trial: 5 minutes)"),
                ("PRO_SID",
                 "SID player",
                 "Full track playback (trial: 30 second preview)"),
                ("PRO_MULTI",
                 "Multi-SID player",
                 "Compare multiple SIDs side by side"),
                ("PRO_PHONEBOOK_UNLIMITED",
                 "Telnet phonebook",
                 "Unlimited saved sessions (trial: 3)"),
                ("PRO_ASM64_SAVE",
                 "Assembly64 saved results",
                 "Save and reload named search snapshots"),
                ("PRO_NO_NAG",
                 "Nag screen",
                 "Suppress trial reminder dialog"),
                ("PRO_NO_WATERMARK",
                 "Title-bar branding",
                 "Remove [TRIAL] watermark from window title"),
            ]
            # Use itemDataRole to colour the rows - present in
            # green, missing in muted red. We rebuild with
            # explicit foreground colours per item rather than a
            # stylesheet because QListWidget items don't pick up
            # parent stylesheet rules reliably.
            from PyQt6.QtGui import QColor, QBrush
            from PyQt6.QtWidgets import QListWidgetItem
            for flag, label, blurb in known_features:
                granted = flag in lic.features
                mark = "\u2713" if granted else "\u2717"
                txt = f"  {mark}  {label:<28} - {blurb}"
                item = QListWidgetItem(txt)
                if granted:
                    item.setForeground(QBrush(QColor("#0a7f1a")))
                else:
                    item.setForeground(QBrush(QColor("#a02020")))
                # Tooltip shows the actual flag name in case the
                # user needs to email the issuer with details
                item.setToolTip(
                    f"Flag: {flag}\n"
                    f"Status: {'GRANTED' if granted else 'MISSING'}")
                self.lst_features.addItem(item)
            # If the license also has any UNKNOWN flag (someone
            # might define custom flags in a forked build), append
            # them too so the user can see what's there.
            extras = sorted(set(lic.features)
                            - {f for f, _, _ in known_features})
            if extras:
                self.lst_features.addItem("")
                hdr = QListWidgetItem("Custom / unknown flags:")
                hdr.setForeground(
                    QBrush(QColor("#555")))
                self.lst_features.addItem(hdr)
                for x in extras:
                    self.lst_features.addItem(f"  \u2713  {x}")
        else:
            # Trial / no license / invalid - show what's gated
            self.lst_features.addItem(
                "Trial limits in effect:")
            self.lst_features.addItem("")
            self.lst_features.addItem(
                "  - SID Player: 30 second preview only")
            self.lst_features.addItem(
                "  - Telnet client: 5 minute session limit")
            self.lst_features.addItem(
                "  - Phonebook: 3 entries max")
            self.lst_features.addItem(
                "  - Database: 1000-disk catalog cap")
            self.lst_features.addItem(
                "  - Assembly64 bulk save: disabled")
            self.lst_features.addItem(
                "  - Multi-SID player: disabled")
            self.lst_features.addItem(
                "  - Nag screen + title watermark shown")

        # Clear any prior status message - the action handlers
        # set this when something happened.
        self.lbl_status.setText("")

    def _on_import_lic(self):
        """File-picker for a quopus.lic, copy into config/, reload."""
        f, _ = QFileDialog.getOpenFileName(
            self, "Select quopus.lic to import",
            str(Path.home()),
            "Quopus license (*.lic);;All files (*)")
        if not f:
            return
        self._install_lic_from_path(Path(f))

    def _on_import_zip(self):
        """File-picker for a customer ZIP, extract quopus.lic +
        quopus_keys.cfg into config/, reload."""
        f, _ = QFileDialog.getOpenFileName(
            self, "Select customer ZIP package",
            str(Path.home()),
            "License package (*.zip);;All files (*)")
        if not f:
            return
        self._install_zip_from_path(Path(f))

    def _install_lic_from_path(self, src: Path):
        """Copy src into CONFIG_DIR/quopus.lic and reload."""
        target = license.CONFIG_DIR / "quopus.lic"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(src, target)
        except OSError as e:
            self._show_error(
                f"Couldn't copy license to {target}:\n\n{e}")
            return
        self._reload_and_check(target)

    def _install_zip_from_path(self, src: Path):
        """Extract quopus.lic + quopus_keys.cfg from the ZIP and
        install both into CONFIG_DIR.

        We don't blindly extract everything - a malicious ZIP
        could have a path-traversal entry. We pull exactly the
        two known filenames by basename and write them where we
        know is safe."""
        try:
            import zipfile
            target_dir = license.CONFIG_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            found_lic = False
            found_cfg = False
            with zipfile.ZipFile(src) as zf:
                for name in zf.namelist():
                    base = Path(name).name.lower()
                    if base == "quopus.lic":
                        with zf.open(name) as fh:
                            (target_dir / "quopus.lic"
                             ).write_bytes(fh.read())
                        found_lic = True
                    elif base == "quopus_keys.cfg":
                        with zf.open(name) as fh:
                            (target_dir / "quopus_keys.cfg"
                             ).write_bytes(fh.read())
                        found_cfg = True
            if not found_lic:
                self._show_error(
                    "The ZIP does not contain a quopus.lic file.")
                return
            if not found_cfg:
                # Not fatal - they might already have the right
                # keys.cfg from a previous import. But warn
                # because this is the common failure mode for
                # "license is invalid" on fresh installs.
                self.lbl_status.setText(
                    "Imported quopus.lic but no "
                    "quopus_keys.cfg in the ZIP - if this is a "
                    "Pro license on a fresh install, you may "
                    "need to import the keys.cfg separately.")
        except zipfile.BadZipFile:
            self._show_error("The selected file is not a valid ZIP.")
            return
        except OSError as e:
            self._show_error(f"Couldn't extract ZIP:\n\n{e}")
            return
        self._reload_and_check(target_dir / "quopus.lic")

    def _reload_and_check(self, lic_path: Path):
        """Force the license module to re-read from disk, then
        refresh the display. If the new license isn't valid,
        leave the file in place but show the error so the user
        can copy it for support."""
        new = license.reload_license()
        self._refresh_display()
        if new.valid and new.tier in ("pro", "lifetime"):
            self.lbl_status.setText(
                f"License imported successfully. "
                f"Tier: {new.tier}")
            # Notify parent so title bar / About update
            self._notify_parent_relicensed()
        elif new.valid and new.tier == "trial":
            self.lbl_status.setText(
                "License accepted but tier is 'trial' - this "
                "is the demo key path. To activate Pro features "
                "you also need quopus_keys.cfg with the "
                "issuer's production public key.")
        else:
            self.lbl_status.setText(
                f"License imported but verification failed: "
                f"{new.error}")

    def _on_remove(self):
        """Delete CONFIG_DIR/quopus.lic and reload (-> trial)."""
        path = license.find_license_file()
        if path is None:
            self.lbl_status.setText("No license to remove.")
            return
        if QMessageBox.question(
                self, "Remove license",
                f"Delete this license file?\n\n"
                f"  {path}\n\n"
                f"Quopus will revert to trial mode. The change "
                f"is permanent.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink()
        except OSError as e:
            self._show_error(f"Couldn't delete {path}:\n\n{e}")
            return
        license.reload_license()
        self._refresh_display()
        self.lbl_status.setText(
            "License removed. Quopus is now in trial mode.")
        self._notify_parent_relicensed()

    def _on_refresh(self):
        """Force a re-read of the license without changing files.
        Useful if the user just edited config/quopus.lic in a
        text editor and wants to see whether it still parses."""
        license.reload_license()
        self._refresh_display()
        self.lbl_status.setText("License reloaded from disk.")

    def _on_show_diagnostics(self):
        """Pop a modal with all the license-loading details that
        help diagnose 'license invalid' / 'features missing'
        problems. Shows search paths, which files were found,
        and what the heuristic problem analyzer thinks is wrong.

        We also include the contents of license_debug.log if
        it exists - that's the running record of every license
        load attempt.
        """
        from PyQt6.QtWidgets import (
            QDialog as _QDialog, QPlainTextEdit as _QPlainText,
            QApplication as _QApp,
        )
        report = license.diagnostic_report()
        lines = []
        lines.append("=" * 60)
        lines.append("LICENSE LOADING DIAGNOSTICS")
        lines.append("=" * 60)
        lines.append("")
        lines.append("Search paths for quopus.lic:")
        for p in report["lic_search_paths"]:
            mark = "[FOUND]" if p == report["lic_found"] else "       "
            lines.append(f"  {mark}  {p}")
        if report["lic_found"] is None:
            lines.append("")
            lines.append("  -> No license file found in any "
                         "of the above.")
        lines.append("")
        lines.append("Search paths for quopus_keys.cfg "
                     "(production public key):")
        for p in report["keys_search_paths"]:
            mark = "[FOUND]" if p == report["keys_found"] else "       "
            lines.append(f"  {mark}  {p}")
        if report["keys_found"] is None:
            lines.append("")
            lines.append("  -> No keys config found - falling "
                         "back to the built-in DEMO public key.")
        lines.append("")
        lines.append("Effective public key:")
        # Show first/last segment only - keys are 64 hex chars,
        # printing the whole thing is just noise.
        pk = report["pubkey_effective_hex"]
        lines.append(f"  {pk[:16]}...{pk[-8:]}")
        lines.append(f"  Source: "
                     f"{'DEMO (built-in)' if report['pubkey_is_demo'] else 'Production (from quopus_keys.cfg)'}")
        lines.append("")
        if report["problem"]:
            lines.append("PROBLEM DETECTED:")
            # Wrap the problem text at ~58 chars for readability
            problem = report["problem"]
            words = problem.split()
            cur = "  "
            for w in words:
                if len(cur) + len(w) + 1 > 60:
                    lines.append(cur)
                    cur = "  " + w
                else:
                    cur = (cur + " " + w) if cur.strip() else "  " + w
            if cur.strip():
                lines.append(cur)
        else:
            lines.append("No problems detected. If features are "
                         "still missing,")
            lines.append("check the license details panel above "
                         "to confirm which")
            lines.append("feature flags this license actually "
                         "grants.")
        lines.append("")
        # Add the contents of license_debug.log if any. As of v1.0
        # this file lives in the platform user-config dir (XDG/
        # APPDATA/Library), not next to the EXE - see
        # quopus_lib.license._debug_log_path for the rules.
        from .license import _debug_log_path
        log_path = _debug_log_path()
        if log_path.is_file():
            try:
                log_content = log_path.read_text(
                    encoding="utf-8", errors="replace")
                # Only the last ~80 lines so the dialog isn't
                # overwhelming - the most recent entries are the
                # ones relevant to "the current state".
                log_lines = log_content.splitlines()
                tail = log_lines[-80:]
                lines.append("=" * 60)
                lines.append(f"license_debug.log "
                             f"(last {len(tail)} lines)")
                lines.append("=" * 60)
                lines.extend(tail)
            except OSError as e:
                lines.append(f"(Could not read log: {e})")
        else:
            lines.append("(license_debug.log not present yet - "
                         "it gets written")
            lines.append(f" to {log_path.parent} when the "
                         f"license system runs.)")

        dlg = _QDialog(self)
        dlg.setWindowTitle("License Diagnostics")
        dlg.resize(700, 520)
        lay = QVBoxLayout(dlg)
        txt = _QPlainText()
        txt.setReadOnly(True)
        txt.setPlainText("\n".join(lines))
        txt.setStyleSheet(
            "font-family: 'Courier New', monospace; "
            f"font-size: {scaled_font_px(11)}px; background: #f8f8f8;")
        lay.addWidget(txt)
        # Bottom: Copy + Close
        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy to clipboard")
        btn_copy.setToolTip(
            "Copy the diagnostics text - paste into a support "
            "email so the issuer can see exactly what's wrong.")
        def _copy():
            cb = _QApp.clipboard()
            cb.setText("\n".join(lines))
            btn_copy.setText("Copied")
        btn_copy.clicked.connect(_copy)
        btn_row.addWidget(btn_copy)
        btn_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        btn_close.setDefault(True)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
        dlg.exec()

    def _notify_parent_relicensed(self):
        """Tell the main window that the license state changed so
        it can refresh the title-bar watermark and About content.
        We use a best-effort attribute lookup since this dialog
        is sometimes opened from places other than MainWindow."""
        mw = self._parent_window
        if mw is None:
            return
        for attr in ("on_license_changed",
                     "refresh_license_state",
                     "apply_watermark"):
            fn = getattr(mw, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                return
        # Fallback: try the module-level watermark applier
        try:
            apply_watermark(mw)
        except Exception:
            pass

    def _show_error(self, msg: str):
        QMessageBox.warning(self, "License", msg)


def show_license_info_dialog(parent=None):
    """Public entry point - opens the License Info dialog modally
    and returns when the user closes it. Returns nothing useful;
    the side-effect is that the license state may have changed."""
    dlg = LicenseInfoDialog(parent)
    dlg.exec()
    return None
