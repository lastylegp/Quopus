# date_time: 2026-06-07 14:58
"""Confirmation dialog for the Smart-Fill copy / move feature.

Shows the user exactly which directories Quopus picked to fit
into the destination drive's free space, plus the ones it had
to skip, before they commit to the operation.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QGroupBox,
)


def _fmt(n):
    """Same byte formatter as ActionsMixin._fmt_bytes - lifted
    here so the dialog stays self-contained and doesn't have
    to reach back into the parent."""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            if u == "B":
                return f"{int(n)} {u}"
            return f"{n:.2f} {u}"
        n /= 1024.0
    return f"{n:.2f} EB"


class SmartFillConfirmDialog(QDialog):
    """Modal preview of which directories will be transferred
    and which were too big to fit. User confirms with OK,
    backs out with Cancel."""

    def __init__(self, parent, op_name, picked, skipped,
                 free_bytes, safety_bytes):
        super().__init__(parent)
        self.setWindowTitle(f"Smart Fill - {op_name}")
        self.setModal(True)
        self.resize(700, 560)

        total_picked = sum(s for s, _ in picked)
        total_skipped = sum(s for s, _ in skipped)
        leftover = free_bytes - safety_bytes - total_picked

        outer = QVBoxLayout(self)

        # Summary banner: target drive's available bytes vs.
        # what we'll actually use vs. what's left. Tells the
        # user at a glance whether the pack-job left a lot of
        # room on the table or filled the disk to the brim.
        summary = QLabel(
            f"<b>Free space on target:</b> {_fmt(free_bytes)}"
            f" &nbsp;&nbsp; "
            f"<b>Safety margin:</b> {_fmt(safety_bytes)}<br>"
            f"<b>Will transfer:</b> {len(picked)} "
            f"directories &nbsp;= {_fmt(total_picked)}<br>"
            f"<b>Leftover headroom after transfer:</b> "
            f"{_fmt(max(0, leftover))}")
        summary.setStyleSheet(
            "QLabel { padding: 6px; "
            "background: #f0f0f0; border: 1px solid #c0c0c0; }")
        outer.addWidget(summary)

        # Picked group: the directories that DID fit. Shown
        # biggest-first so the user can quickly see the heavy
        # hitters at the top. Each row gets a size column for
        # context.
        g_pick = QGroupBox(
            f"Will be {op_name.lower()}d ({len(picked)})")
        gpl = QVBoxLayout(g_pick)
        self.tree_pick = QTreeWidget()
        self.tree_pick.setHeaderLabels(["Directory", "Size"])
        self.tree_pick.setRootIsDecorated(False)
        self.tree_pick.setAlternatingRowColors(True)
        for sz, d in picked:
            it = QTreeWidgetItem([d.name, _fmt(sz)])
            it.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight)
            self.tree_pick.addTopLevelItem(it)
        self.tree_pick.setColumnWidth(0, 430)
        gpl.addWidget(self.tree_pick)
        outer.addWidget(g_pick, 2)

        # Skipped group: the directories that did NOT fit, so
        # the user knows what's being left behind. If everything
        # fit there's nothing to show here and we hide the
        # group entirely.
        if skipped:
            g_skip = QGroupBox(
                f"Skipped - did not fit "
                f"({len(skipped)}, total {_fmt(total_skipped)})")
            gsl = QVBoxLayout(g_skip)
            self.tree_skip = QTreeWidget()
            self.tree_skip.setHeaderLabels(
                ["Directory", "Size"])
            self.tree_skip.setRootIsDecorated(False)
            self.tree_skip.setAlternatingRowColors(True)
            for sz, d in skipped:
                it = QTreeWidgetItem([d.name, _fmt(sz)])
                it.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignRight)
                self.tree_skip.addTopLevelItem(it)
            self.tree_skip.setColumnWidth(0, 430)
            gsl.addWidget(self.tree_skip)
            outer.addWidget(g_skip, 1)

        # Button row at the bottom: OK / Cancel.
        bot = QHBoxLayout()
        bot.addStretch(1)
        b_ok = QPushButton(f"{op_name} these directories")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.accept)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        bot.addWidget(b_ok)
        bot.addWidget(b_cancel)
        outer.addLayout(bot)
