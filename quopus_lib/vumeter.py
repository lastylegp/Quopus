"""Stereo VU meter widget - classic green/yellow/red segmented bar.

Vertical orientation, two bars (L and R), with discrete segments
that light up to the current RMS level. A peak-hold marker rides
above the bars and slowly decays.

Audio data is fed via feed_block(np.ndarray of shape (N, 2) int16).
The widget computes RMS internally and runs its own decay timer
so it keeps animating even when blocks come irregularly.

Color zones:
  - 0%   .. 65%  -> green   (normal level)
  - 65%  .. 85%  -> yellow  (loud, OK)
  - 85%  .. 100% -> red     (clipping risk)

This matches what you'd see on a 1980s hi-fi receiver or a hardware
equalizer's level meters - simple, immediately readable, no fancy
spectrum/needle stuff.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont
from PyQt6.QtWidgets import QWidget

from .palette import C


# ---------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------
# Number of horizontal segments stacked vertically per bar.
SEGMENTS = 24

# Color zones (segment count from the bottom)
GREEN_TOP  = int(SEGMENTS * 0.65)    # segments 0..GREEN_TOP-1 are green
YELLOW_TOP = int(SEGMENTS * 0.85)    # segments GREEN_TOP..YELLOW_TOP-1 are yellow

# Per-tick decay: how fast the bar falls when the input goes silent.
# 0.85 means each tick drops the value to 85% of its previous value.
# Smaller = faster fall. With a 50ms tick that gives us ~1 second
# from full to ~5% which feels about right.
DECAY = 0.85

# Peak hold drops slower than the bar (looks more "decisive")
PEAK_DECAY = 0.96

# Tick interval - controls how smoothly the bars update visually,
# independent of how often audio chunks arrive.
TICK_MS = 50


# ---------------------------------------------------------------------
# VUMeter widget
# ---------------------------------------------------------------------
class VUMeter(QWidget):
    """Two-channel vertical VU meter. Set min size via sizeHint().

    Public slots:
        feed_block(np.ndarray) - hand a stereo int16 chunk for level
                                 computation (the most recent block
                                 wins; we don't queue).

    The widget runs its own QTimer so the bars decay smoothly even
    when audio blocks arrive irregularly (e.g. during pause)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Current displayed level, 0.0 .. 1.0, per channel.
        # Index 0 = left, 1 = right.
        self._level = [0.0, 0.0]
        # Target level from the most recent audio block - we lerp
        # toward this on each tick for smoother animation.
        self._target = [0.0, 0.0]
        # Peak-hold (the little marker that "sticks" near the top of
        # the recent peak)
        self._peak = [0.0, 0.0]
        # Modest minimum size for sensible default layouts; the
        # parent should give us more space if available.
        self.setMinimumSize(60, 120)
        # Set a black-ish background so the unlit segments are dark.
        self.setAutoFillBackground(False)
        # Animation timer
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def sizeHint(self):
        # Default size if the parent hasn't constrained us
        return QSize(70, 180)

    # -----------------------------------------------------------------
    # Public API: feed audio
    # -----------------------------------------------------------------
    def feed_block(self, chunk: np.ndarray):
        """Compute RMS for each channel and store as the new target.
        Accepts int16 stereo chunks of shape (N, 2). Mono input
        (1-D or shape (N, 1)) is duplicated to both bars."""
        if chunk is None:
            return
        if chunk.ndim == 1:
            # Mono - same level on both bars
            mono = chunk.astype(np.float32)
            r = self._rms_to_level(mono)
            self._target[0] = r
            self._target[1] = r
            return
        if chunk.shape[0] == 0:
            return
        if chunk.shape[1] == 1:
            mono = chunk[:, 0].astype(np.float32)
            r = self._rms_to_level(mono)
            self._target[0] = r
            self._target[1] = r
            return
        # Stereo
        left = chunk[:, 0].astype(np.float32)
        right = chunk[:, 1].astype(np.float32)
        self._target[0] = self._rms_to_level(left)
        self._target[1] = self._rms_to_level(right)

    @staticmethod
    def _rms_to_level(samples: np.ndarray) -> float:
        """Convert a buffer of int16 samples (already cast to float)
        to a 0..1 display level. We use a logarithmic mapping (~ -36dB
        floor) so quiet sounds still light up some segments instead
        of staying invisible."""
        if samples.size == 0:
            return 0.0
        # RMS in the int16 range
        rms = float(np.sqrt(np.mean(samples * samples)))
        if rms < 1.0:
            return 0.0
        # int16 max is 32767. Convert to dBFS (decibels relative to
        # full scale): 0 dB at 32767, -infinity at 0.
        # Then map a -36..0 dB range linearly to 0..1 for display.
        # Most music sits in -20..-10 dB RMS so this gives a meter
        # that uses most of its range during normal playback.
        db = 20.0 * np.log10(rms / 32767.0 + 1e-9)
        FLOOR_DB = -36.0
        if db <= FLOOR_DB:
            return 0.0
        # Map -36..0 dB to 0..1
        return min(1.0, (db - FLOOR_DB) / -FLOOR_DB)

    def reset(self):
        """Drop bars to zero immediately. Called on stop/track switch."""
        self._level = [0.0, 0.0]
        self._target = [0.0, 0.0]
        self._peak = [0.0, 0.0]
        self.update()

    # -----------------------------------------------------------------
    # Animation
    # -----------------------------------------------------------------
    def _tick(self):
        # Lerp current level toward target. Falling is slower than
        # rising so peaks register clearly.
        for i in range(2):
            t = self._target[i]
            cur = self._level[i]
            if t >= cur:
                # Snap up immediately on a louder sample
                self._level[i] = t
            else:
                # Decay toward the new (lower) target
                self._level[i] = cur * DECAY
            # Drift target down too so a single loud block doesn't
            # leave the bar pinned high forever
            self._target[i] *= DECAY
            # Peak-hold: rises with current level, falls slower
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
        # Background - solid black so unlit segments are visible
        p.fillRect(0, 0, w, h, QColor(0, 0, 0))

        # Layout: small label area at the top for "L"/"R" tags,
        # then the two bars side by side with a small gap.
        TAG_H = 14
        gap = 4
        # Two bars + gap = w - 2*margin
        bar_w = max(8, (w - 3 * gap) // 2)
        bar_x = [gap, gap + bar_w + gap]
        bar_y = TAG_H + gap
        bar_h = h - bar_y - gap

        # Draw L / R tags
        p.setPen(QPen(QColor(C.WHITE)))
        f = QFont("Topaz", 8); f.setBold(True)
        try: f.setStyleHint(QFont.StyleHint.Monospace)
        except Exception: pass
        p.setFont(f)
        for i, lbl in enumerate(("L", "R")):
            p.drawText(
                bar_x[i], 0, bar_w, TAG_H,
                Qt.AlignmentFlag.AlignCenter, lbl)

        # Each bar is divided into SEGMENTS horizontal slices.
        # Slice height includes a 1px dark gap at the bottom so the
        # individual segments are visible.
        seg_h = max(2, bar_h // SEGMENTS)
        # Recompute actual bar height to fit even slices exactly
        actual_bar_h = seg_h * SEGMENTS

        for ch in range(2):
            x = bar_x[ch]
            level = self._level[ch]
            peak = self._peak[ch]
            n_lit = int(round(level * SEGMENTS))
            n_lit = max(0, min(SEGMENTS, n_lit))

            for s in range(SEGMENTS):
                # y of this segment - segment 0 is at the bottom
                y = bar_y + actual_bar_h - (s + 1) * seg_h
                # Color depends on which zone this segment is in
                if s < GREEN_TOP:
                    base = QColor(0, 220, 0)        # green
                    dim = QColor(0, 60, 0)
                elif s < YELLOW_TOP:
                    base = QColor(240, 220, 0)      # yellow
                    dim = QColor(60, 50, 0)
                else:
                    base = QColor(240, 40, 30)      # red
                    dim = QColor(60, 10, 5)
                # Lit if at or below the current level
                col = base if s < n_lit else dim
                p.fillRect(x, y, bar_w, seg_h - 1, col)

            # Peak-hold marker - thin bright line at the peak segment
            if peak > 0.01:
                peak_seg = int(round(peak * SEGMENTS))
                peak_seg = max(1, min(SEGMENTS, peak_seg))
                if peak_seg < GREEN_TOP:
                    pc = QColor(120, 255, 120)
                elif peak_seg < YELLOW_TOP:
                    pc = QColor(255, 255, 120)
                else:
                    pc = QColor(255, 120, 100)
                py = bar_y + actual_bar_h - peak_seg * seg_h
                p.fillRect(x, py, bar_w, 2, pc)

        # 1px frame around the whole widget so it sits clearly on
        # the player's grey background
        p.setPen(QPen(QColor(60, 60, 60)))
        p.drawRect(0, 0, w - 1, h - 1)
