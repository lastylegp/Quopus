# date_time: 2026-05-28 22:01
"""
Commodore 64 .TAP cassette image toolkit - GUI dialog.

Mirrors the CRT toolkit's layout and behaviour but for tape
images. Provides:

  * a list of decoded blocks/files (CBM header/data, turbo)
  * per-block hex / ASCII view
  * PRG extraction (save a reconstructed file as .prg)
  * a pulse-length histogram (which widths dominate the tape)
  * a waveform-ish pulse visualization (pulse length over time)
  * Run-on-emulator (passes the whole .tap to VICE/x64sc, which
    autostarts tape images natively)
  * Run-on-U64 (extracts the selected CBM file to a .prg and
    DMA-loads it; the U64 REST API has no direct run_tap, so we
    send the reconstructed program instead)
  * multi-file playlist navigation (Prev/Next) when several .tap
    files are opened from the lister at once

The heavy lifting (container parse, pulse decode, file
reconstruction) lives in tap_decoder.py; this module is purely
the Qt presentation layer.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QTextEdit,
    QTabWidget, QFileDialog, QMessageBox, QWidget, QSizePolicy,
    QLineEdit, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QFont, QTextCursor,
    QTextCharFormat,
)

from . import tap_decoder as td
from .config import scaled_font_px
from .palette import get_topaz_font, get_mono_font


# ---------------------------------------------------------------------
# Pulse visualization widgets
# ---------------------------------------------------------------------

class _HistogramWidget(QWidget):
    """Bar chart of pulse-length frequency. X axis = pulse length
    in clock cycles (bucketed), Y axis = how many pulses had that
    length. The dominant bars reveal the loader's pulse widths."""

    def __init__(self, histogram: dict, parent=None):
        super().__init__(parent)
        self._hist = histogram or {}
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_histogram(self, histogram: dict):
        self._hist = histogram or {}
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 20, 30))
        if not self._hist:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No pulse data")
            p.end()
            return
        w = self.width()
        h = self.height()
        margin = 24
        plot_w = max(1, w - 2 * margin)
        plot_h = max(1, h - 2 * margin)
        # Cap the cycle range we display to keep bit-pulses visible
        # (long gaps would squash everything otherwise).
        items = [(k, v) for k, v in self._hist.items() if k <= 1200]
        if not items:
            items = list(self._hist.items())
        max_count = max(v for _, v in items) if items else 1
        max_cycle = max(k for k, _ in items) if items else 1
        bar_w = max(1, plot_w / max(1, len(items)))
        # Axes
        p.setPen(QColor(90, 90, 110))
        p.drawLine(margin, h - margin, w - margin, h - margin)
        p.drawLine(margin, margin, margin, h - margin)
        # Bars
        for i, (cyc, count) in enumerate(items):
            x = margin + i * bar_w
            bar_h = (count / max_count) * plot_h
            y = h - margin - bar_h
            # color by CBM band so the standard widths stand out
            col = QColor(80, 140, 220)
            if abs(cyc - td.CBM_SHORT) <= td.CBM_TOL:
                col = QColor(90, 220, 120)      # short = green
            elif abs(cyc - td.CBM_MEDIUM) <= td.CBM_TOL:
                col = QColor(230, 200, 80)      # medium = yellow
            elif abs(cyc - td.CBM_LONG) <= td.CBM_TOL:
                col = QColor(230, 110, 90)      # long = red
            p.fillRect(int(x), int(y),
                       max(1, int(bar_w - 1)), int(bar_h), col)
        # Labels
        p.setPen(QColor(170, 170, 190))
        f = QFont("monospace")
        f.setPointSize(7)
        p.setFont(f)
        p.drawText(margin, h - margin + 14, "0")
        p.drawText(w - margin - 40, h - margin + 14,
                   f"{max_cycle}cyc")
        p.drawText(2, margin + 8, f"{max_count}")
        p.end()


class _WaveformWidget(QWidget):
    """Pulse-length-over-time strip: each pulse drawn as a vertical
    bar whose height is its length. Reads like an oscilloscope
    envelope of the tape - pilots show as flat runs, data as
    alternating heights, gaps as spikes."""

    def __init__(self, pulses: list, parent=None):
        super().__init__(parent)
        self._pulses = pulses or []
        self._offset = 0          # first pulse index shown
        self._span = 2000         # how many pulses across the width
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_pulses(self, pulses: list):
        self._pulses = pulses or []
        self._offset = 0
        self.update()

    def set_view(self, offset: int, span: int):
        self._offset = max(0, offset)
        self._span = max(50, span)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(16, 16, 24))
        if not self._pulses:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No pulse data")
            p.end()
            return
        w = self.width()
        h = self.height()
        view = self._pulses[self._offset:self._offset + self._span]
        if not view:
            p.end()
            return
        # Clamp very long pulses so the scale stays useful
        cap = 1000
        maxp = min(cap, max(view)) or 1
        n = len(view)
        p.setPen(QPen(QColor(90, 200, 150), 1))
        if n <= w:
            # Fewer pulses than pixels: one line per pulse.
            step = w / n
            for i, pulse in enumerate(view):
                x = i * step
                val = min(pulse, cap)
                bar_h = (val / maxp) * (h - 10)
                p.drawLine(int(x), h - 5, int(x),
                           int(h - 5 - bar_h))
        else:
            # More pulses than pixels: draw one line per PIXEL
            # column showing the peak pulse in that column. This
            # caps the work at ~widget-width line draws no matter
            # how many pulses are in view (a 700k-pulse tape would
            # otherwise issue 700k drawLine calls and freeze Qt).
            per_col = n / w
            for col in range(int(w)):
                lo = int(col * per_col)
                hi = int((col + 1) * per_col)
                if hi <= lo:
                    hi = lo + 1
                seg = view[lo:hi]
                if not seg:
                    continue
                val = min(cap, max(seg))
                bar_h = (val / maxp) * (h - 10)
                p.drawLine(col, h - 5, col, int(h - 5 - bar_h))
        # Footer: range info
        p.setPen(QColor(170, 170, 190))
        f = QFont("monospace")
        f.setPointSize(7)
        p.setFont(f)
        p.drawText(4, 12,
                   f"pulses {self._offset}..{self._offset + n} "
                   f"of {len(self._pulses)}  (cap {cap}cyc)")
        p.end()


# ---------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------

class _TapToolkitDialog(QDialog):
    def __init__(self, decoded: "td.TapDecodeResult", path: Path,
                 parent=None, config=None,
                 playlist=None, playlist_index=0,
                 save_cb=None, raw=None, tp=None):
        super().__init__(parent)
        self.decoded = decoded
        self.path = Path(path)
        self.config = config or {}
        self._save_cb = save_cb
        self._raw = raw
        self._tp = tp
        self._parent_for_reopen = parent
        self._playlist = list(playlist) if playlist else None
        self._playlist_index = int(playlist_index)
        self._last_save_dir = str(self.path.parent)

        title_suffix = ""
        if self._playlist and len(self._playlist) > 1:
            title_suffix = (f"  ({self._playlist_index + 1} of "
                            f"{len(self._playlist)})")
        self.setWindowTitle(
            f"TAP Toolkit: {self.path.name}" + title_suffix)
        self.resize(1080, 720)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(3)

        # ---- Playlist nav row ----
        if self._playlist and len(self._playlist) > 1:
            nav = QHBoxLayout()
            nav.setSpacing(4)
            self._btn_prev = QPushButton("◀ Prev")
            self._btn_next = QPushButton("Next ▶")
            self._btn_prev.clicked.connect(self._go_prev)
            self._btn_next.clicked.connect(self._go_next)
            self._btn_prev.setShortcut("Alt+Left")
            self._btn_next.setShortcut("Alt+Right")
            self._lbl_nav = QLabel(
                f"TAP {self._playlist_index + 1} of "
                f"{len(self._playlist)}")
            nav.addWidget(self._btn_prev)
            nav.addWidget(self._btn_next)
            nav.addWidget(self._lbl_nav)
            nav.addStretch(1)
            v.addLayout(nav)
            self._btn_prev.setEnabled(self._playlist_index > 0)
            self._btn_next.setEnabled(
                self._playlist_index < len(self._playlist) - 1)

        # ---- Summary bar ----
        self._summary = QLabel(decoded.summary)
        self._summary.setStyleSheet(
            f"font-size: {scaled_font_px(11, config)}px; "
            f"padding: 2px; color: #d0d0e0;")
        self._summary.setWordWrap(True)
        v.addWidget(self._summary)

        # ---- Toolbar ----
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._btn_extract = QPushButton("Extract as PRG…")
        self._btn_extract.clicked.connect(self._extract_selected)
        self._btn_extract_all = QPushButton("Extract all files…")
        self._btn_extract_all.clicked.connect(self._extract_all)
        self._btn_run_emu = QPushButton("Run TAP in emulator")
        self._btn_run_emu.clicked.connect(self._run_in_emulator)
        self._btn_run_u64 = QPushButton("Run file on U64")
        self._btn_run_u64.clicked.connect(self._run_on_u64)
        self._btn_save_report = QPushButton("Save report…")
        self._btn_save_report.clicked.connect(self._save_report)
        self._btn_clean = QPushButton("Clean / optimize…")
        self._btn_clean.clicked.connect(self._clean_tap)
        self._btn_wav = QPushButton("Export WAV…")
        self._btn_wav.clicked.connect(self._export_wav)
        bar.addWidget(self._btn_extract)
        bar.addWidget(self._btn_extract_all)
        bar.addWidget(self._btn_run_emu)
        bar.addWidget(self._btn_run_u64)
        bar.addWidget(self._btn_save_report)
        bar.addWidget(self._btn_clean)
        bar.addWidget(self._btn_wav)
        bar.addStretch(1)
        v.addLayout(bar)

        # ---- Main splitter: file list left, tabs right ----
        split = QSplitter(Qt.Orientation.Horizontal)
        v.addWidget(split, 1)

        # Left: block/file list
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Blocks / files:"))
        self._list = QListWidget()
        self._list.setFont(get_topaz_font(11))
        self._list.currentRowChanged.connect(self._on_select)
        lv.addWidget(self._list)
        split.addWidget(left)

        # Right: tabbed views
        self._tabs = QTabWidget()
        split.addWidget(self._tabs)
        split.setSizes([320, 760])

        # Hex tab
        hex_outer = QWidget()
        hl = QVBoxLayout(hex_outer)
        hl.setContentsMargins(2, 2, 2, 2)

        # --- search bar ---
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            "Search… (hex bytes like 'DE AD BE EF' "
            "or text like 'BOOT')")
        self._search_edit.returnPressed.connect(
            lambda: self._hex_search(forward=True))
        self._search_edit.textChanged.connect(
            self._on_search_text_changed)
        self._search_hex_mode = QCheckBox("Hex")
        self._search_hex_mode.setToolTip(
            "On: interpret the query as hex byte values "
            "(e.g. 'DEAD BEEF').\n"
            "Off: interpret as ASCII/PETSCII text.")
        # auto-detect default: if it looks like hex, tick it
        self._search_hex_mode.setChecked(True)
        self._search_hex_mode.stateChanged.connect(
            lambda _=0: setattr(self, "_search_last_pos", -1))
        self._btn_find_next = QPushButton("Find next")
        self._btn_find_next.clicked.connect(
            lambda: self._hex_search(forward=True))
        self._btn_find_prev = QPushButton("Find prev")
        self._btn_find_prev.clicked.connect(
            lambda: self._hex_search(forward=False))
        self._search_status = QLabel("")
        self._search_status.setMinimumWidth(90)
        search_row.addWidget(self._search_edit, 1)
        search_row.addWidget(self._search_hex_mode)
        search_row.addWidget(self._btn_find_prev)
        search_row.addWidget(self._btn_find_next)
        search_row.addWidget(self._search_status)
        hl.addLayout(search_row)

        self._hex = QTextEdit()
        self._hex.setReadOnly(True)
        self._hex.setFont(get_mono_font(10))
        self._hex.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        hl.addWidget(self._hex)
        self._tabs.addTab(hex_outer, "Hex / ASCII")

        # search state
        self._hex_bytes = b""        # raw bytes of current block
        self._search_last_pos = -1   # last matched byte offset

        # Histogram tab
        hist_outer = QWidget()
        hil = QVBoxLayout(hist_outer)
        hil.setContentsMargins(2, 2, 2, 2)
        hil.addWidget(QLabel(
            "Pulse-length histogram (green=CBM short, "
            "yellow=medium, red=long):"))
        self._hist_widget = _HistogramWidget(decoded.histogram)
        hil.addWidget(self._hist_widget, 1)
        self._tabs.addTab(hist_outer, "Histogram")

        # Waveform tab
        wave_outer = QWidget()
        wl = QVBoxLayout(wave_outer)
        wl.setContentsMargins(2, 2, 2, 2)
        wl.addWidget(QLabel(
            "Pulse envelope (length over time):"))
        self._wave_widget = _WaveformWidget(decoded.pulses.pulses)
        wl.addWidget(self._wave_widget, 1)
        # Simple navigation for the waveform
        wnav = QHBoxLayout()
        btn_wstart = QPushButton("|◀ Start")
        btn_wprev = QPushButton("◀")
        btn_wnext = QPushButton("▶")
        btn_wzoom_in = QPushButton("Zoom +")
        btn_wzoom_out = QPushButton("Zoom −")
        self._wave_offset = 0
        self._wave_span = 2000

        def _wupdate():
            self._wave_widget.set_view(self._wave_offset,
                                        self._wave_span)

        def _wstart():
            self._wave_offset = 0
            _wupdate()

        def _wprev():
            self._wave_offset = max(
                0, self._wave_offset - self._wave_span // 2)
            _wupdate()

        def _wnext():
            self._wave_offset += self._wave_span // 2
            _wupdate()

        def _wzin():
            self._wave_span = max(100, self._wave_span // 2)
            _wupdate()

        def _wzout():
            # Cap the visible span: rendering hundreds of thousands
            # of line segments per paintEvent freezes Qt. 20k is
            # already more than the pixel width can resolve, so a
            # larger span buys nothing but lag.
            self._wave_span = min(20000, self._wave_span * 2)
            _wupdate()

        btn_wstart.clicked.connect(_wstart)
        btn_wprev.clicked.connect(_wprev)
        btn_wnext.clicked.connect(_wnext)
        btn_wzoom_in.clicked.connect(_wzin)
        btn_wzoom_out.clicked.connect(_wzout)
        for b in (btn_wstart, btn_wprev, btn_wnext,
                  btn_wzoom_in, btn_wzoom_out):
            wnav.addWidget(b)
        wnav.addStretch(1)
        wl.addLayout(wnav)
        self._tabs.addTab(wave_outer, "Waveform")

        # Info tab
        info_outer = QWidget()
        iol = QVBoxLayout(info_outer)
        iol.setContentsMargins(2, 2, 2, 2)
        self._info = QTextEdit()
        self._info.setReadOnly(True)
        self._info.setFont(get_mono_font(10))
        iol.addWidget(self._info)
        self._tabs.addTab(info_outer, "Tape info")

        # TAPClean report tab
        report_outer = QWidget()
        rol = QVBoxLayout(report_outer)
        rol.setContentsMargins(2, 2, 2, 2)
        self._report = QTextEdit()
        self._report.setReadOnly(True)
        self._report.setFont(get_mono_font(10))
        self._report.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        rol.addWidget(self._report)
        self._tabs.addTab(report_outer, "TAPClean report")

        # Analysis. We PREFER the real TAPClean binary (bundled
        # GPL source, compiled on first use) - it has all 93
        # loader scanners and gives 100%-accurate file detection
        # and PRG extraction. If TAPClean can't be built/run (no
        # compiler, etc.) we fall back to the built-in Python
        # analyzer.
        self._analysis = None        # Python analyzer report
        self._tapclean = None        # TAPClean report (preferred)
        from . import tap_decoder as _td

        used_tapclean = False
        tapclean_status = ""        # human-readable status line
        try:
            from . import tap_tapclean
            if tap_tapclean.is_available():
                tcrep = tap_tapclean.analyze(
                    str(self.path), extract_prgs=True)
                if tcrep is None:
                    tapclean_status = (
                        "TAPClean failed to run on this tape "
                        "(timeout, crash or bad path) - using "
                        "Python fallback analyzer.")
                elif not tcrep.files:
                    tapclean_status = (
                        "TAPClean ran but reported no files - "
                        "using Python fallback analyzer.")
                else:
                    self._tapclean = tcrep
                    self._report.setPlainText(tcrep.raw_report)
                    # Build the GUI block list from TAPClean's
                    # files. Skip PAUSE entries; for header blocks
                    # the useful load addr is the DATA file's addr.
                    conv = []
                    for f in tcrep.files:
                        if f.file_type.upper().startswith("PAUSE"):
                            continue
                        la = f.load_addr
                        ea = f.end_addr
                        # headers know the DATA file's real addr
                        if f.data_load_addr >= 0:
                            la = f.data_load_addr
                            ea = f.data_end_addr
                        data = b""
                        if f.prg_path is not None:
                            try:
                                data = f.prg_path.read_bytes()
                            except OSError:
                                data = b""
                        conv.append(_td.TapFileEntry(
                            index=f.seq,
                            kind=("turbo" if "TURBO" in
                                  f.file_type.upper() else
                                  "cbm-data"),
                            loader=f.file_type,
                            name=f.name,
                            file_type=0,
                            load_addr=la, end_addr=ea,
                            data=data,
                            pulse_start=0, pulse_end=0,
                            checksum_ok=f.checkbyte_pass,
                            notes=(f"{f.read_errors} read error(s)"
                                   if f.read_errors else
                                   (f.file_id or ""))))
                    if conv:
                        self.decoded.files = conv
                        used_tapclean = True
                        tapclean_status = (
                            f"TAPClean: {len(conv)} files "
                            f"(loader: {tcrep.loader_id or '?'})")
            else:
                # No binary found and auto-build either disabled or
                # failed - tell the user where to drop the binary.
                tapclean_status = (
                    "TAPClean binary not available - using "
                    "Python fallback analyzer. " +
                    (tap_tapclean.build_error() or
                     "Drop a prebuilt tapclean[.exe] into "
                     "external/tapclean/src/ or external/."))
        except Exception as exc:
            used_tapclean = False
            tapclean_status = (
                f"TAPClean integration error: {exc} - using "
                f"Python fallback analyzer.")
        # remember for the info bar
        self._tapclean_status = tapclean_status

        if not used_tapclean:
            # Fallback: built-in Python analyzer.
            try:
                from . import tap_analyzer
                self._analysis = tap_analyzer.analyze_tap(
                    str(self.path), _raw=self._raw, _tp=self._tp)
                self._report.setPlainText(
                    self._analysis.format_report())
            except Exception as e:
                self._report.setPlainText(
                    f"Analysis failed:\n{e}")

            if self._analysis is not None and self._analysis.files:
                conv = []
                for fr in self._analysis.files:
                    kind = ("cbm-data"
                            if fr.file_type.startswith("CBM")
                            else "turbo")
                    conv.append(_td.TapFileEntry(
                        index=fr.seq,
                        kind=kind,
                        loader=fr.loader,
                        name=fr.name,
                        file_type=0,
                        load_addr=fr.load_addr,
                        end_addr=fr.end_addr,
                        data=fr.data,
                        pulse_start=0, pulse_end=0,
                        checksum_ok=fr.checkbyte_pass,
                        notes=(f"{fr.read_errors} read error(s)"
                               if fr.read_errors else "")))
                self.decoded.files = conv

        # Update the summary bar to reflect what actually happened
        # so the user can see whether TAPClean ran and, if not,
        # why it didn't. This is the difference between "loader
        # said CBM ROM with 2 blocks" (wrong, just the Python
        # fallback) and "TAPClean: 179 files (loader: Cyberload)"
        # (right).
        base = self.decoded.summary
        # If we got real files via TAPClean, refresh the block
        # count in the base summary since decoded.summary was
        # built before the analyzer ran.
        nfiles = len(self.decoded.files)
        if used_tapclean:
            base = (f"TAP v{self._tp.version} | "
                    f"{self._tp.pulse_count} pulses | "
                    f"{self._tp.duration_seconds:.1f}s | "
                    f"{nfiles} file(s)")
        if self._tapclean_status:
            base = base + "  |  " + self._tapclean_status
        self._summary.setText(base)

        # Populate
        self._populate_list()
        self._populate_info()
        if self._list.count():
            self._list.setCurrentRow(0)
        self._update_button_states()

    # ----- population -----

    def _populate_list(self):
        self._list.clear()
        for fe in self.decoded.files:
            la = (f"${fe.load_addr:04X}"
                  if fe.load_addr >= 0 else "?")
            label = f"[{fe.index}] {fe.kind}"
            if fe.name:
                label += f"  '{fe.name}'"
            label += f"  {fe.size}B"
            if fe.load_addr >= 0:
                label += f"  @{la}"
            item = QListWidgetItem(label)
            chk = fe.checksum_ok
            if chk is True:
                item.setForeground(QColor(120, 220, 120))
            elif chk is False:
                item.setForeground(QColor(230, 120, 120))
            self._list.addItem(item)
        if not self.decoded.files:
            self._list.addItem(QListWidgetItem(
                "(no decodable blocks found)"))

    def _populate_info(self):
        tp = self.decoded.pulses
        lines = [
            f"File:       {self.path.name}",
            f"TAP version: {tp.version}",
            f"Platform:    {tp.platform} "
            f"(0=C64,1=VIC20,2=C16)",
            f"Video:       {'NTSC' if tp.video == 1 else 'PAL'} "
            f"(clock {tp.clock} Hz)",
            f"Pulses:      {tp.pulse_count}",
            f"Duration:    {tp.duration_seconds:.2f} s",
            f"Data size:   {tp.data_size} bytes (declared)",
            "",
            f"Detected loaders: "
            f"{', '.join(self.decoded.detected_loaders) or 'none'}",
            "",
            "Blocks:",
        ]
        for fe in self.decoded.files:
            la = f"${fe.load_addr:04X}" if fe.load_addr >= 0 else "?"
            ea = f"${fe.end_addr:04X}" if fe.end_addr >= 0 else "?"
            lines.append(
                f"  [{fe.index}] {fe.kind} loader={fe.loader}")
            lines.append(
                f"        name={fe.name!r} type={fe.file_type} "
                f"load={la} end={ea} size={fe.size}")
            if fe.notes:
                lines.append(f"        note: {fe.notes}")
        self._info.setPlainText("\n".join(lines))

    def _selected_entry(self):
        row = self._list.currentRow()
        if 0 <= row < len(self.decoded.files):
            return self.decoded.files[row]
        return None

    def _on_select(self, row):
        fe = self._selected_entry()
        if fe is None:
            self._hex.clear()
            self._hex_bytes = b""
            self._search_last_pos = -1
            self._update_button_states()
            return
        self._hex_bytes = fe.data or b""
        self._search_last_pos = -1
        self._search_status.setText("")
        self._hex.setPlainText(self._hexdump(fe.data))
        self._update_button_states()

    # ----- hex search -----

    def _on_search_text_changed(self, _text):
        """A new query means the next 'Find' should start from the
        top, not from the previous match."""
        self._search_last_pos = -1
        self._search_status.setText("")

    def _parse_search_query(self):
        """Turn the search box content into a bytes() pattern.
        Returns (pattern_bytes, case_insensitive, error_message).
        In hex mode the query is parsed as hex byte values
        (whitespace optional: 'DE AD', 'DEAD', '0xDE 0xAD' all
        work) and matched exactly. In text mode it's encoded as
        Latin-1 and matched case-insensitively.
        """
        q = self._search_edit.text().strip()
        if not q:
            return b"", False, "empty"
        if self._search_hex_mode.isChecked():
            # strip 0x prefixes and whitespace, keep hex digits
            cleaned = q.replace("0x", "").replace("0X", "")
            cleaned = "".join(cleaned.split())
            if not cleaned:
                return b"", False, "no hex digits"
            if len(cleaned) % 2 != 0:
                return b"", False, "odd hex length"
            try:
                return bytes.fromhex(cleaned), False, ""
            except ValueError:
                return b"", False, "bad hex"
        else:
            try:
                return q.encode("latin-1"), True, ""
            except UnicodeEncodeError:
                return q.encode("utf-8", "replace"), True, ""

    def _hex_search(self, forward=True):
        """Find the search pattern in the current block's bytes
        and highlight + scroll to it. Wraps around at the ends.
        Text searches are case-insensitive; hex searches are
        exact."""
        if not self._hex_bytes:
            self._search_status.setText("no data")
            return
        pattern, ci, err = self._parse_search_query()
        if not pattern:
            self._search_status.setText(err or "—")
            return

        data = self._hex_bytes
        hay = data.lower() if ci else data
        pat = pattern.lower() if ci else pattern
        n = len(data)
        if forward:
            start = self._search_last_pos + 1
            idx = hay.find(pat, start)
            if idx < 0:                  # wrap to top
                idx = hay.find(pat, 0)
        else:
            # search backwards before the last match
            end = (self._search_last_pos
                   if self._search_last_pos >= 0 else n)
            idx = hay.rfind(pat, 0, max(0, end))
            if idx < 0:                  # wrap to bottom
                idx = hay.rfind(pat)

        if idx < 0:
            self._search_status.setText("not found")
            self._search_last_pos = -1
            return

        self._search_last_pos = idx
        self._highlight_hex_range(idx, len(pattern))
        # 1-based count of matches would be nice but keep it simple
        self._search_status.setText(f"@ ${idx:04X}")

    def _highlight_hex_range(self, byte_off, length):
        """Select the bytes [byte_off, byte_off+length) in both the
        hex column and the ASCII column of the dump, and scroll the
        first match into view.

        The dump line layout (see _hexdump) is:
            "OOOO  HH HH HH ... HH  AAAAAAAAAAAAAAAA"
        offset col = 4 chars + 2 spaces = 6
        each hex byte = 3 chars (2 digits + 1 space), 16 per row
        ascii starts after 6 + 47 + 2 = 55
        """
        doc = self._hex.document()

        # clear previous highlight
        plain_fmt = QTextCharFormat()
        cur_all = QTextCursor(doc)
        cur_all.select(QTextCursor.SelectionType.Document)
        cur_all.setCharFormat(plain_fmt)

        hl_fmt = QTextCharFormat()
        hl_fmt.setBackground(QColor(255, 220, 90))
        hl_fmt.setForeground(QColor(20, 20, 20))

        first_block_pos = None
        # Build a cursor by counting characters per line. Each dump
        # line is fixed-width; we compute the absolute character
        # position of each byte's hex pair.
        for k in range(length):
            off = byte_off + k
            row = off // 16
            col = off % 16
            # characters before this row:
            #   each row prints 4(off)+2(sp)+47(hex)+2(sp)+
            #   up-to-16(ascii) + 1 newline
            # but ascii length == bytes in that row; for full rows
            # it's 16. Rows before `row` are all full (16 bytes)
            # except possibly the data isn't a multiple of 16, but
            # earlier rows ARE full, so 16 each.
            line_len = 6 + 47 + 2 + 16 + 1   # 72
            row_start = row * line_len
            hex_char = row_start + 6 + col * 3   # start of "HH"
            cur = QTextCursor(doc)
            cur.setPosition(hex_char)
            cur.setPosition(hex_char + 2,
                            QTextCursor.MoveMode.KeepAnchor)
            cur.mergeCharFormat(hl_fmt)
            # ascii char
            asc_char = row_start + 6 + 47 + 2 + col
            cur2 = QTextCursor(doc)
            cur2.setPosition(asc_char)
            cur2.setPosition(asc_char + 1,
                             QTextCursor.MoveMode.KeepAnchor)
            cur2.mergeCharFormat(hl_fmt)
            if first_block_pos is None:
                first_block_pos = hex_char

        # scroll the first matched byte into view
        if first_block_pos is not None:
            sc = QTextCursor(doc)
            sc.setPosition(first_block_pos)
            self._hex.setTextCursor(sc)
            self._hex.ensureCursorVisible()

    @staticmethod
    def _hexdump(data: bytes) -> str:
        out = []
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            hexpart = " ".join(f"{b:02X}" for b in chunk)
            hexpart = hexpart.ljust(47)
            asc = "".join(
                chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"{off:04X}  {hexpart}  {asc}")
        return "\n".join(out) if out else "(empty)"

    def _update_button_states(self):
        fe = self._selected_entry()
        has = fe is not None and fe.size > 0
        self._btn_extract.setEnabled(has)
        # Extract-all is available whenever the tape decoded into
        # at least one file.
        any_files = bool(self.decoded.files) or bool(
            self._analysis and self._analysis.files) or bool(
            self._tapclean and self._tapclean.files)
        self._btn_extract_all.setEnabled(any_files)
        # Only CBM blocks with a known load address make sense to
        # run on the U64 as a PRG.
        runnable = (fe is not None and fe.load_addr >= 0
                    and fe.size > 0)
        self._btn_run_u64.setEnabled(runnable)

    # ----- actions -----

    def _extract_selected(self):
        fe = self._selected_entry()
        if fe is None or fe.size == 0:
            return
        default_name = (fe.name.strip() or
                        f"{self.path.stem}_{fe.index}")
        # sanitize
        safe = "".join(c if c.isalnum() or c in "._- " else "_"
                       for c in default_name).strip() or "extract"
        suggested = os.path.join(self._last_save_dir,
                                  safe + ".prg")
        fn, _ = QFileDialog.getSaveFileName(
            self, "Extract block as PRG", suggested,
            "C64 program (*.prg);;All files (*)")
        if not fn:
            return
        try:
            with open(fn, "wb") as f:
                f.write(fe.as_prg())
            self._last_save_dir = os.path.dirname(fn)
            QMessageBox.information(
                self, "Extract",
                f"Wrote {fe.size + 2} bytes to\n{fn}")
        except OSError as e:
            QMessageBox.warning(self, "Extract",
                                f"Could not write file:\n{e}")

    def _extract_all(self):
        """Extract every reconstructed file to a chosen folder,
        using TAPClean's filename convention:

            <seq> (<start>-<end>) [name].prg     (BAD if errors)

        Each file is start-addr-LE + data, exactly as TAPClean's
        database.c writes them. We pull the file list from the
        TAPClean-style analysis (rich per-file detail with start/
        end addresses) when available, else fall back to the
        decoder's blocks.
        """
        # Prefer the analyzer's file reports (they have LA/EA and
        # error counts); fall back to decoded blocks.
        entries = []
        # Fast path: if the real TAPClean ran, it already wrote
        # perfect PRGs (correct names/addresses/checksums) - just
        # copy those.
        if self._tapclean is not None:
            from . import tap_tapclean
            prgs = tap_tapclean.list_prgs(self._tapclean)
            if prgs:
                folder = QFileDialog.getExistingDirectory(
                    self, "Choose folder for extracted PRGs",
                    self._last_save_dir)
                if not folder:
                    return
                target = os.path.join(
                    folder, self.path.stem + "_prg")
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as e:
                    QMessageBox.warning(
                        self, "Extract all",
                        f"Could not create folder:\n{e}")
                    return
                import shutil as _sh
                written = 0
                failed = 0
                for _f, p in prgs:
                    try:
                        _sh.copy2(p, os.path.join(
                            target, p.name))
                        written += 1
                    except OSError:
                        failed += 1
                self._last_save_dir = folder
                msg = (f"Extracted {written} file(s) to\n{target}"
                       f"\n\n(via TAPClean - "
                       f"names/addresses are exact)")
                if failed:
                    msg += (f"\n\n{failed} file(s) could not be "
                            f"written.")
                QMessageBox.information(self, "Extract all", msg)
                return

        if self._analysis is not None and self._analysis.files:
            for fr in self._analysis.files:
                if fr.size <= 0:
                    continue
                entries.append(dict(
                    seq=fr.seq, start=fr.load_addr,
                    end=fr.end_addr, name=fr.name,
                    errors=fr.read_errors,
                    data=fr.data, load=fr.load_addr))
        else:
            for fe in self.decoded.files:
                if fe.size <= 0:
                    continue
                entries.append(dict(
                    seq=fe.index, start=fe.load_addr,
                    end=fe.end_addr, name=fe.name,
                    errors=0, data=fe.data, load=fe.load_addr))

        if not entries:
            QMessageBox.information(
                self, "Extract all",
                "No decodable files to extract.")
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder for extracted PRGs",
            self._last_save_dir)
        if not folder:
            return

        # TAPClean drops everything in a 'prg' subfolder; mirror
        # that so a tape's files stay grouped.
        target = os.path.join(folder, self.path.stem + "_prg")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Extract all",
                                f"Could not create folder:\n{e}")
            return

        written = 0
        failed = 0
        for ent in entries:
            seq = ent["seq"] + 1            # TAPClean is 1-based
            start = ent["start"] if ent["start"] >= 0 else 0
            end = ent["end"] if ent["end"] >= 0 else (
                start + len(ent["data"]))
            fname = f"{seq:03d} ({start:04X}-{end:04X})"
            nm = (ent["name"] or "").strip()
            if nm:
                safe = "".join(c if c.isalnum() or c in "._- "
                               else "_" for c in nm).strip()
                if safe:
                    fname += f" [{safe}]"
            if ent["errors"]:
                fname += " BAD"
            fname += ".prg"
            path = os.path.join(target, fname)
            # PRG body: 2-byte LE load address + data
            load = ent["load"] if ent["load"] >= 0 else start
            body = bytes([load & 0xFF, (load >> 8) & 0xFF]) \
                + ent["data"]
            try:
                with open(path, "wb") as f:
                    f.write(body)
                written += 1
            except OSError:
                failed += 1

        self._last_save_dir = folder
        msg = (f"Extracted {written} file(s) to\n{target}\n\n"
               f"Naming: <seq> (<start>-<end>) [name].prg")
        if failed:
            msg += f"\n\n{failed} file(s) could not be written."
        QMessageBox.information(self, "Extract all", msg)

    def _run_in_emulator(self):
        """Hand the whole .tap to the configured C64 emulator -
        VICE/x64sc autostart a tape image directly. We pass the
        original file path, not a reconstruction, so the emulator
        does the real tape decoding."""
        try:
            from .c64_disasm import run_in_c64_emulator
            from .config import save_config
        except Exception as e:
            QMessageBox.warning(self, "Run in emulator",
                                f"Emulator support unavailable:\n{e}")
            return
        run_in_c64_emulator(
            str(self.path), self, self.config,
            lambda: save_config(self.config) if self.config else None)

    def _run_on_u64(self):
        """The U64 REST API has no direct run_tap, so we send the
        currently-selected reconstructed CBM file as a PRG via
        DMA load. Only enabled for blocks with a known load
        address."""
        fe = self._selected_entry()
        if fe is None or fe.load_addr < 0:
            QMessageBox.information(
                self, "Run on U64",
                "Select a CBM file block with a known load "
                "address.\n\nThe Ultimate-64 REST API can't play "
                "a raw .tap directly, so the toolkit sends the "
                "reconstructed program instead. Turbo blocks "
                "without a decoded load address can't be sent "
                "this way - extract and inspect them first.")
            return
        try:
            from .u64_devices import pick_device
            from .u64_streamer import u64_run_prg
        except Exception as e:
            QMessageBox.warning(self, "Run on U64",
                                f"U64 support unavailable:\n{e}")
            return
        device = pick_device(
            self, self.config, title="Run on U64",
            prompt=f"Which Ultimate-64 should run "
                   f"'{fe.name or self.path.stem}'?")
        if device is None:
            return
        host = (device.get('host', '') or '').strip()
        if not host:
            QMessageBox.warning(self, "Run on U64",
                                "Selected device has no host set.")
            return
        password = device.get('password', '') or ''
        http_port = int(device.get('http_port', 80))
        ok, msg = u64_run_prg(host, fe.as_prg(),
                              password=password, port=http_port)
        if ok:
            QMessageBox.information(
                self, "Run on U64",
                f"Sent '{fe.name or self.path.stem}' to "
                f"U64 at {host}.")
        else:
            QMessageBox.warning(
                self, "Run on U64",
                f"U64 at {host} rejected the request:\n{msg}")

    # ----- analyzer actions -----

    def _save_report(self):
        """Write the TAPClean report to a text file (tcreport.txt
        style)."""
        # Prefer the real TAPClean report text if we have it.
        report_text = None
        if self._tapclean is not None:
            report_text = self._tapclean.raw_report
        elif self._analysis is not None:
            report_text = self._analysis.format_report()
        if not report_text:
            QMessageBox.information(
                self, "Save report",
                "No analysis available to save.")
            return
        suggested = os.path.join(
            self._last_save_dir,
            self.path.stem + "_tcreport.txt")
        fn, _ = QFileDialog.getSaveFileName(
            self, "Save TAPClean report", suggested,
            "Text file (*.txt);;All files (*)")
        if not fn:
            return
        try:
            with open(fn, "w", encoding="utf-8") as f:
                f.write(report_text)
            self._last_save_dir = os.path.dirname(fn)
            QMessageBox.information(self, "Save report",
                                    f"Report written to\n{fn}")
        except OSError as e:
            QMessageBox.warning(self, "Save report",
                                f"Could not write file:\n{e}")

    def _clean_tap(self):
        """Optimize the TAP (snap bit pulses to cluster centers)
        and write a new .tap."""
        try:
            from . import tap_analyzer
        except Exception as e:
            QMessageBox.warning(self, "Clean TAP",
                                f"Analyzer unavailable:\n{e}")
            return
        suggested = os.path.join(
            self._last_save_dir,
            self.path.stem + "_clean.tap")
        fn, _ = QFileDialog.getSaveFileName(
            self, "Save cleaned TAP", suggested,
            "C64 tape image (*.tap);;All files (*)")
        if not fn:
            return
        try:
            stats = tap_analyzer.clean_tap(str(self.path), fn)
            self._last_save_dir = os.path.dirname(fn)
            QMessageBox.information(
                self, "Clean TAP",
                f"Optimized TAP written to\n{fn}\n\n"
                f"Snapped {stats['snapped']} of {stats['pulses']} "
                f"pulses to cluster centers "
                f"{stats['clusters']}.\n"
                f"Output size: {stats['out_size']} bytes.")
        except Exception as e:
            QMessageBox.warning(self, "Clean TAP",
                                f"Could not clean TAP:\n{e}")

    def _export_wav(self):
        """Render the pulse stream to a WAV for recording back to
        a real datasette."""
        try:
            from . import tap_analyzer
        except Exception as e:
            QMessageBox.warning(self, "Export WAV",
                                f"Analyzer unavailable:\n{e}")
            return
        suggested = os.path.join(
            self._last_save_dir, self.path.stem + ".wav")
        fn, _ = QFileDialog.getSaveFileName(
            self, "Export WAV", suggested,
            "WAV audio (*.wav);;All files (*)")
        if not fn:
            return
        try:
            stats = tap_analyzer.export_wav(str(self.path), fn)
            self._last_save_dir = os.path.dirname(fn)
            QMessageBox.information(
                self, "Export WAV",
                f"WAV written to\n{fn}\n\n"
                f"{stats['samples']} samples, "
                f"{stats['duration']:.1f}s @ "
                f"{stats['sample_rate']} Hz.")
        except Exception as e:
            QMessageBox.warning(self, "Export WAV",
                                f"Could not export WAV:\n{e}")

    # ----- playlist nav -----

    def _go_prev(self):
        if self._playlist and self._playlist_index > 0:
            self._open_playlist_index(self._playlist_index - 1)

    def _go_next(self):
        if (self._playlist
                and self._playlist_index
                < len(self._playlist) - 1):
            self._open_playlist_index(self._playlist_index + 1)

    def _open_playlist_index(self, idx):
        path = self._playlist[idx]
        playlist = self._playlist
        parent = self._parent_for_reopen
        config = self.config
        save_cb = self._save_cb
        self.close()
        # Defer opening so this dialog's deleteLater settles first.
        QTimer.singleShot(0, lambda: open_tap_toolkit(
            path, parent=parent, config=config,
            playlist=playlist, playlist_index=idx,
            save_cb=save_cb))


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def open_tap_toolkit(path, parent=None, config=None,
                     playlist=None, playlist_index=0,
                     save_cb=None):
    """Open the TAP toolkit dialog for the given .tap file.

    `path` is a path/str to the .tap. `playlist` (optional) is a
    list of paths for Prev/Next navigation, with `playlist_index`
    the position of `path` within it. Returns the dialog instance
    (already shown) or None on parse error.
    """
    p = Path(path)
    try:
        # Parse the container ONCE and share it between the
        # decoder and the analyzer so a multi-MB tape isn't read
        # and pulse-expanded twice (that doubling, plus the old
        # O(n^2) CBM scan, was what made big tapes appear to hang
        # on open).
        raw = p.read_bytes()
        tp = td.parse_tap_container(raw, source_path=p)
        decoded = td.decode_tap(tp)
    except td.TapParseError as e:
        QMessageBox.warning(
            parent, "TAP Toolkit",
            f"Not a valid TAP file:\n{p.name}\n\n{e}")
        return None
    except Exception as e:
        QMessageBox.warning(
            parent, "TAP Toolkit",
            f"Could not decode TAP:\n{p.name}\n\n{e}")
        return None
    dlg = _TapToolkitDialog(
        decoded, p, parent=parent, config=config,
        playlist=playlist, playlist_index=playlist_index,
        save_cb=save_cb, raw=raw, tp=tp)
    dlg.show()
    return dlg
