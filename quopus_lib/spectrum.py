"""Spectrum analyzer widget - classic 10-band hi-fi equalizer style.

Vertical bars for logarithmically-spaced frequency bands. Each bar
shows the energy in that band, computed from an FFT of the most
recent audio block. Same green/yellow/red segmented look as a 1980s
graphic equalizer's level meters.

Default bands are the ISO 1/3-octave centers used on most 10-band
equalizers: 31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000 Hz.

Audio data is fed via feed_block(np.ndarray, sample_rate). Stereo is
mixed to mono before FFT. The widget runs its own decay timer so
bars keep falling smoothly when audio blocks arrive irregularly.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtWidgets import QWidget

from .palette import C


# ---------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------
# Number of horizontal segments stacked vertically per bar.
SEGMENTS = 20

# Color zones (segment index from the bottom, exclusive upper bound)
GREEN_TOP  = int(SEGMENTS * 0.65)
YELLOW_TOP = int(SEGMENTS * 0.85)

# Per-tick decay - bars fall to this fraction each tick
DECAY = 0.85

# Peak-hold drops slower
PEAK_DECAY = 0.96

# Animation tick rate
TICK_MS = 50

# ISO 1/3-octave-ish centers for a classic 10-band graphic EQ display.
# Center frequencies are stored alongside short labels for the axis.
DEFAULT_BANDS = [
    (31,    "31"),
    (62,    "62"),
    (125,   "125"),
    (250,   "250"),
    (500,   "500"),
    (1000,  "1k"),
    (2000,  "2k"),
    (4000,  "4k"),
    (8000,  "8k"),
    (16000, "16k"),
]


def _band_edges(centers, sample_rate):
    """Compute the FFT-bin range covered by each band.

    Bands are spaced an octave apart, so the edges sit at the
    geometric mean between consecutive centers (== sqrt(2) * lower
    for octave bands). The lowest band's lower edge is sqrt(2) below
    its center; the highest band's upper edge is sqrt(2) above.
    Edges are clamped to [0, sample_rate/2].
    """
    nyq = sample_rate / 2.0
    edges = []
    for i, c in enumerate(centers):
        if i == 0:
            lo = c / np.sqrt(2)
        else:
            lo = np.sqrt(centers[i-1] * c)
        if i == len(centers) - 1:
            hi = c * np.sqrt(2)
        else:
            hi = np.sqrt(c * centers[i+1])
        lo = max(0.0, lo)
        hi = min(nyq, hi)
        edges.append((lo, hi))
    return edges


# ---------------------------------------------------------------------
# Spectrum analyzer widget
# ---------------------------------------------------------------------
class SpectrumAnalyzer(QWidget):
    """N-band logarithmically-spaced spectrum bars.

    Public API:
        feed_block(chunk, sample_rate) - hand over a stereo or mono
            int16 chunk; the analyzer mixes to mono, FFTs, and bins.
        reset() - drop bars to zero immediately.
    """

    def __init__(self, parent=None, bands=None,
                 show_labels=True):
        super().__init__(parent)
        self._bands = bands or DEFAULT_BANDS
        self._n = len(self._bands)
        self._show_labels = show_labels
        # Current displayed level per band, 0..1
        self._level = [0.0] * self._n
        # Most-recent input target - we lerp toward this each tick
        self._target = [0.0] * self._n
        # Peak-hold marker per band
        self._peak = [0.0] * self._n
        # Cached band edges + sample rate; recomputed when sample
        # rate changes
        self._sr = 0
        self._edges = None
        # Cached Hann window (recomputed when block size changes)
        self._win_size = 0
        self._win = None

        # Reasonable default - parent should give us more room
        self.setMinimumSize(180, 120)
        self.setAutoFillBackground(False)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def sizeHint(self):
        return QSize(280, 180)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def feed_block(self, chunk: np.ndarray, sample_rate: int = 44100):
        """Compute per-band energy from chunk and store as new target."""
        if chunk is None:
            return
        if chunk.ndim == 2 and chunk.shape[1] >= 2:
            mono = ((chunk[:, 0].astype(np.float32)
                      + chunk[:, 1].astype(np.float32)) * 0.5)
        elif chunk.ndim == 2:
            mono = chunk[:, 0].astype(np.float32)
        else:
            mono = chunk.astype(np.float32)
        n = mono.size
        if n < 64:
            return
        # Recompute window if size changed
        if self._win_size != n:
            # Hann window reduces spectral leakage at the bin edges
            self._win = np.hanning(n).astype(np.float32)
            self._win_size = n
        # Recompute edges if sample rate changed
        if self._sr != sample_rate:
            centers = [c for c, _ in self._bands]
            self._edges = _band_edges(centers, sample_rate)
            self._sr = sample_rate
        # Scale samples down from int16 range so we work with -1..1
        # floats - keeps the FFT magnitudes in a comfortable range.
        x = mono * self._win / 32768.0
        # Real FFT - we only care about magnitudes 0..nyq
        spec = np.abs(np.fft.rfft(x))
        # Normalize by N so amplitude is independent of block length
        spec = spec / (n * 0.5)
        # Bin frequency for each FFT bin
        bin_hz = sample_rate / n
        # Per-band: sum the squared magnitudes (= energy) of bins
        # whose centers fall within the band, then take sqrt to get
        # back to amplitude units; convert to dB.
        for i, (lo, hi) in enumerate(self._edges):
            lo_bin = int(np.floor(lo / bin_hz))
            hi_bin = int(np.ceil(hi / bin_hz))
            lo_bin = max(0, lo_bin)
            hi_bin = min(spec.size, hi_bin + 1)
            if hi_bin <= lo_bin:
                self._target[i] = 0.0
                continue
            slice_ = spec[lo_bin:hi_bin]
            # Energy = sum of squares; amplitude = sqrt of that.
            energy = float(np.sum(slice_ * slice_))
            amp = np.sqrt(energy)
            if amp <= 1e-9:
                self._target[i] = 0.0
                continue
            # Convert to dBFS-ish (relative to a reference of 1.0
            # which corresponds to a full-scale int16 sine wave of
            # this band only). Map -60 dB .. 0 dB to 0..1 for display.
            db = 20.0 * np.log10(amp + 1e-9)
            FLOOR_DB = -60.0
            CEIL_DB = 0.0
            if db <= FLOOR_DB:
                self._target[i] = 0.0
            elif db >= CEIL_DB:
                self._target[i] = 1.0
            else:
                self._target[i] = (db - FLOOR_DB) / (CEIL_DB - FLOOR_DB)

    def reset(self):
        self._level = [0.0] * self._n
        self._target = [0.0] * self._n
        self._peak = [0.0] * self._n
        self.update()

    # -----------------------------------------------------------------
    # Animation
    # -----------------------------------------------------------------
    def _tick(self):
        for i in range(self._n):
            t = self._target[i]
            cur = self._level[i]
            if t >= cur:
                self._level[i] = t
            else:
                self._level[i] = cur * DECAY
            self._target[i] *= DECAY
            if self._level[i] > self._peak[i]:
                self._peak[i] = self._level[i]
            else:
                self._peak[i] *= PEAK_DECAY
        self.update()

    # -----------------------------------------------------------------
    # Painting
    # -----------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        p.fillRect(0, 0, w, h, QColor(0, 0, 0))

        LABEL_H = 14 if self._show_labels else 0
        gap = 2
        margin_x = 4
        # Available area
        bars_y = gap
        bars_h = h - bars_y - gap - LABEL_H
        bars_h = max(20, bars_h)
        # Total horizontal space split evenly across bars
        total_w = w - 2 * margin_x
        # +1 gap between bars * (n-1), plus margin
        bar_w = max(6, (total_w - (self._n - 1) * gap) // self._n)

        # Segment height based on the bars area
        seg_h = max(2, bars_h // SEGMENTS)
        actual_h = seg_h * SEGMENTS

        # Draw labels first (under the bars)
        if self._show_labels:
            f = QFont("Topaz", 7)
            try: f.setStyleHint(QFont.StyleHint.Monospace)
            except Exception: pass
            p.setFont(f)
            p.setPen(QPen(QColor(C.WHITE)))

        for b in range(self._n):
            x = margin_x + b * (bar_w + gap)
            level = self._level[b]
            peak = self._peak[b]
            n_lit = int(round(level * SEGMENTS))
            n_lit = max(0, min(SEGMENTS, n_lit))

            for s in range(SEGMENTS):
                y = bars_y + actual_h - (s + 1) * seg_h
                if s < GREEN_TOP:
                    base = QColor(0, 220, 0)
                    dim = QColor(0, 60, 0)
                elif s < YELLOW_TOP:
                    base = QColor(240, 220, 0)
                    dim = QColor(60, 50, 0)
                else:
                    base = QColor(240, 40, 30)
                    dim = QColor(60, 10, 5)
                col = base if s < n_lit else dim
                p.fillRect(x, y, bar_w, seg_h - 1, col)

            # Peak-hold marker
            if peak > 0.01:
                peak_seg = int(round(peak * SEGMENTS))
                peak_seg = max(1, min(SEGMENTS, peak_seg))
                if peak_seg < GREEN_TOP:
                    pc = QColor(120, 255, 120)
                elif peak_seg < YELLOW_TOP:
                    pc = QColor(255, 255, 120)
                else:
                    pc = QColor(255, 120, 100)
                py = bars_y + actual_h - peak_seg * seg_h
                p.fillRect(x, py, bar_w, 2, pc)

            # Frequency label centered under each bar
            if self._show_labels:
                lbl = self._bands[b][1]
                p.drawText(
                    x, bars_y + actual_h + 2, bar_w, LABEL_H,
                    Qt.AlignmentFlag.AlignCenter, lbl)

        p.setPen(QPen(QColor(60, 60, 60)))
        p.drawRect(0, 0, w - 1, h - 1)
