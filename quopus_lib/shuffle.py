"""Shuffle-mode helper for the MOD and SID players.

Recursively scans a directory for files matching a predicate, then
plays them in random order. Supports prev/next navigation. Used by
both the ProTracker module player and the GoatTracker SID player.

Design notes:
- The scan can be slow on big trees (HVSC = ~50k files) so it runs
  in a background thread; the player's prev/next buttons are
  enabled as soon as the first batch arrives.
- Order is shuffled once at scan-completion. Prev/next navigates
  the same shuffled order so the user can backtrack.
- We don't reshuffle on wraparound - the user can hit "Reshuffle"
  if they want a new ordering.
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Callable, Optional, List

from PyQt6.QtCore import QObject, QThread, pyqtSignal


# ---------------------------------------------------------------------
# Background scanner
# ---------------------------------------------------------------------
class ShuffleScanner(QThread):
    """Walks a directory tree, collecting files matched by a predicate.
    Emits progress as it goes so the UI can show a count, and a final
    `finished_with_files` with the full shuffled list."""

    progress = pyqtSignal(int)             # files found so far
    finished_with_files = pyqtSignal(list)  # final shuffled file list

    def __init__(self, root: Path,
                  predicate: Callable[[Path], bool],
                  parent: Optional[QObject] = None):
        super().__init__(parent)
        self._root = root
        self._predicate = predicate
        self._stop = False
        # Hard cap on files scanned to avoid runaway with huge trees.
        # 50000 is enough for HVSC + most personal collections.
        self._max_files = 50000

    def stop(self):
        self._stop = True

    def run(self):
        files: List[Path] = []
        try:
            # Use os.walk style via Path for speed
            stack = [self._root]
            while stack and not self._stop:
                cur = stack.pop()
                try:
                    entries = list(cur.iterdir())
                except (PermissionError, OSError):
                    continue
                # Sort so the order is deterministic before shuffling
                entries.sort(key=lambda p: p.name.lower())
                for e in entries:
                    if self._stop: break
                    if len(files) >= self._max_files: break
                    try:
                        if e.is_dir() and not e.is_symlink():
                            stack.append(e)
                        elif e.is_file():
                            if self._predicate(e):
                                files.append(e)
                                if len(files) % 50 == 0:
                                    self.progress.emit(len(files))
                    except (PermissionError, OSError):
                        continue
                if len(files) >= self._max_files: break
        except Exception:
            pass
        if not self._stop:
            random.shuffle(files)
            self.finished_with_files.emit(files)


# ---------------------------------------------------------------------
# Playlist navigator
# ---------------------------------------------------------------------
class ShufflePlaylist:
    """Holds the shuffled file list and current index. Provides
    prev/next/peek with optional wraparound."""

    def __init__(self, files: List[Path] | None = None,
                  start: Path | None = None):
        self._files: List[Path] = files or []
        self._index = 0
        if start is not None and self._files:
            try:
                self._index = self._files.index(start)
            except ValueError:
                # If the start file isn't in the shuffled list (e.g.
                # because it didn't match the predicate), put it
                # first so the user starts where they expected.
                self._files.insert(0, start)
                self._index = 0

    def __len__(self) -> int:
        return len(self._files)

    @property
    def index(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._files)

    def current(self) -> Optional[Path]:
        if not self._files: return None
        return self._files[self._index]

    def next(self) -> Optional[Path]:
        if not self._files: return None
        self._index = (self._index + 1) % len(self._files)
        return self.current()

    def prev(self) -> Optional[Path]:
        if not self._files: return None
        self._index = (self._index - 1) % len(self._files)
        return self.current()

    def reshuffle(self) -> None:
        """Re-randomize the order, keeping the current file as
        the new index 0."""
        cur = self.current()
        random.shuffle(self._files)
        if cur is not None:
            try:
                self._index = self._files.index(cur)
            except ValueError:
                self._index = 0
        else:
            self._index = 0

    def replace_files(self, files: List[Path]) -> None:
        """Swap in a new file list (e.g. when scan completes after
        we already started playing one file)."""
        cur = self.current()
        self._files = files
        if cur is not None:
            try:
                self._index = self._files.index(cur)
                return
            except ValueError:
                pass
        self._index = 0
