# date_time: 2026-06-06 00:35
"""Big clock dialog for Quopus.

When the user clicks the status-bar time label, this dialog
pops up showing a big analog clock face above a large digital
read-out, both ticking every second. ESC or click-anywhere
closes it.

The analog face is drawn with QPainter primitives - no SVG, no
external assets - so it scales cleanly with the dialog size and
inherits the application font. Hour ticks are thick, minute
ticks thin; hands are styled like a classic Junghans wall
clock with a red sweep second hand.

Lives in its own module so main_window.py doesn't grow another
~150 lines of painting code, and so future viewers (e.g. the
About dialog) can reuse the same ClockFaceWidget.
"""
import math
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False
    ZoneInfo = None  # type: ignore

# Pre-flight check: zoneinfo works only if the OS provides an
# IANA tzdb (Linux/macOS do, Windows does NOT by default). On
# Windows the user needs to `pip install tzdata` which lets
# zoneinfo find the database via the tzdata package.
# We try to construct one ZoneInfo here so we know up-front
# whether to use the real library or our hardcoded fallback.
_ZONEINFO_OK = False
if _HAS_ZONEINFO:
    try:
        ZoneInfo("Europe/Berlin")
        _ZONEINFO_OK = True
    except Exception:
        # Try harder: maybe the user has the tzdata package
        # but it's not yet been imported. Importing it makes
        # zoneinfo find the data.
        try:
            import tzdata  # noqa: F401
            ZoneInfo("Europe/Berlin")
            _ZONEINFO_OK = True
        except Exception:
            _ZONEINFO_OK = False


def _last_sunday(year, month):
    """Date of the last Sunday of (year, month). Used for DST
    start/end calculations - most European/US transitions
    happen on the last (or first) Sunday of a month."""
    from datetime import date
    # Walk back from the last day of the month.
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != 6:  # Sunday is 6 in Python
        d -= timedelta(days=1)
    return d


def _nth_sunday(year, month, n):
    """Date of the n-th Sunday (1-based) of (year, month)."""
    from datetime import date
    d = date(year, month, 1)
    # Move to the first Sunday
    while d.weekday() != 6:
        d += timedelta(days=1)
    d += timedelta(weeks=n - 1)
    return d


def _eu_dst_active(utc_now):
    """European Union DST window: last Sun March 01:00 UTC to
    last Sun October 01:00 UTC. Covers Berlin, London, Cairo,
    Paris, Madrid etc."""
    y = utc_now.year
    start = datetime(y, 3, _last_sunday(y, 3).day, 1, 0,
                      tzinfo=timezone.utc)
    end = datetime(y, 10, _last_sunday(y, 10).day, 1, 0,
                    tzinfo=timezone.utc)
    return start <= utc_now < end


def _us_dst_active(utc_now):
    """US DST window: 2nd Sun March 02:00 local to 1st Sun Nov
    02:00 local. We approximate by checking month boundaries
    (good enough - we'd be off by a few hours twice a year on
    those exact Sundays, no big deal for a wall clock)."""
    y = utc_now.year
    start_day = _nth_sunday(y, 3, 2)
    end_day = _nth_sunday(y, 11, 1)
    start = datetime(y, 3, start_day.day, 7, 0,
                      tzinfo=timezone.utc)  # 02:00 EST = 07:00 UTC
    end = datetime(y, 11, end_day.day, 6, 0,
                    tzinfo=timezone.utc)    # 02:00 EDT = 06:00 UTC
    return start <= utc_now < end


def _aus_dst_active(utc_now):
    """Australia (NSW/VIC) DST: 1st Sun Oct 02:00 local to 1st
    Sun April 03:00 local. Reverse hemisphere - DST is during
    our winter."""
    y = utc_now.year
    # Spring forward: 1st Sun Oct
    spring_y = y if utc_now.month >= 4 else y - 1
    fall_y = y if utc_now.month >= 4 else y
    spring = datetime(spring_y, 10,
                       _nth_sunday(spring_y, 10, 1).day,
                       16, 0, tzinfo=timezone.utc)  # 02:00 AEST
    fall = datetime(fall_y, 4,
                     _nth_sunday(fall_y, 4, 1).day,
                     16, 0, tzinfo=timezone.utc)
    return utc_now >= spring or utc_now < fall


def _nz_dst_active(utc_now):
    """New Zealand DST: last Sun September 02:00 local to
    1st Sun April 03:00 local."""
    y = utc_now.year
    spring_y = y if utc_now.month >= 4 else y - 1
    fall_y = y if utc_now.month >= 4 else y
    spring = datetime(spring_y, 9,
                       _last_sunday(spring_y, 9).day,
                       14, 0, tzinfo=timezone.utc)
    fall = datetime(fall_y, 4,
                     _nth_sunday(fall_y, 4, 1).day,
                     14, 0, tzinfo=timezone.utc)
    return utc_now >= spring or utc_now < fall


# City fallback table. (display_name, tz_id, standard_offset_minutes,
# dst_offset_minutes_when_active, dst_rule_function_or_None).
# Used when zoneinfo isn't available (Windows w/o tzdata package).
WORLD_CITIES_FALLBACK = [
    ("Los Angeles", "America/Los_Angeles", -8 * 60, -7 * 60, _us_dst_active),
    ("New York",    "America/New_York",    -5 * 60, -4 * 60, _us_dst_active),
    ("São Paulo",   "America/Sao_Paulo",   -3 * 60, -3 * 60, None),
    ("London",      "Europe/London",        0,       1 * 60, _eu_dst_active),
    ("Berlin",      "Europe/Berlin",        1 * 60,  2 * 60, _eu_dst_active),
    ("Cairo",       "Africa/Cairo",         2 * 60,  3 * 60, _eu_dst_active),
    ("Moscow",      "Europe/Moscow",        3 * 60,  3 * 60, None),
    ("Dubai",       "Asia/Dubai",           4 * 60,  4 * 60, None),
    ("Mumbai",      "Asia/Kolkata",         5 * 60 + 30, 5 * 60 + 30, None),
    ("Shanghai",    "Asia/Shanghai",        8 * 60,  8 * 60, None),
    ("Tokyo",       "Asia/Tokyo",           9 * 60,  9 * 60, None),
    ("Sydney",      "Australia/Sydney",    10 * 60, 11 * 60, _aus_dst_active),
    ("Auckland",    "Pacific/Auckland",    12 * 60, 13 * 60, _nz_dst_active),
]


def _city_now(idx):
    """Return (datetime, label, tzinfo_or_None) for the given
    city index in WORLD_CITIES_FALLBACK. Prefers zoneinfo when
    available, otherwise uses our hardcoded offset+DST rules.
    """
    name, tz_id, std_off, dst_off, dst_rule = WORLD_CITIES_FALLBACK[idx]
    if _ZONEINFO_OK:
        try:
            tz = ZoneInfo(tz_id)
            return datetime.now(tz), name, tz
        except Exception:
            pass
    # Fallback path
    utc_now = datetime.now(timezone.utc)
    if dst_rule is not None and dst_rule(utc_now):
        offset_min = dst_off
    else:
        offset_min = std_off
    # Build a fixed-offset tzinfo so the returned datetime can be
    # passed to ClockFaceWidget.set_timezone and behave normally.
    tz = timezone(timedelta(minutes=offset_min))
    return utc_now.astimezone(tz), name, tz


# Convenience alias preserved for any external code that imports it.
WORLD_CITIES = [(name, tz_id) for name, tz_id, *_ in WORLD_CITIES_FALLBACK]

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QWidget,
    QSizePolicy, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView,
)


# (WORLD_CITIES, WORLD_CITIES_FALLBACK and the city helpers
# live near the top of this module, above the duplicate
# QtWidgets import block. The duplicate import is intentional -
# the fallback path uses table-only widgets and we keep imports
# close to their use sites for readability.)


class ClockFaceWidget(QWidget):
    """Self-drawing analog clock face. Updates itself once per
    second via an internal QTimer that's started/stopped by
    showEvent / hideEvent - so we don't burn CPU when the
    dialog is closed or hidden behind another window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        # Active timezone. None = local time. Otherwise a
        # zoneinfo.ZoneInfo. Set via set_timezone() so the world-
        # clock list can swap which city the analog face shows.
        self._tz = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)  # 1 second
        self._timer.timeout.connect(self.update)

    def set_timezone(self, tz):
        """Switch the displayed timezone. Pass None for local
        system time, or a zoneinfo.ZoneInfo instance for a
        specific city. Triggers an immediate repaint."""
        self._tz = tz
        self.update()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._timer.start()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._timer.stop()

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Fit a circle into the smaller dimension with a
        # 4px margin so the rim is never clipped at small sizes.
        side = min(self.width(), self.height()) - 8
        cx = self.width() / 2
        cy = self.height() / 2
        r = side / 2

        # --- Clock face background ---
        face_grad = QColor("#fafafa")
        p.setBrush(QBrush(face_grad))
        p.setPen(QPen(QColor("#222"), max(2.0, r * 0.012)))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # --- Hour ticks + numbers ---
        font = QFont(self.font())
        font.setPointSizeF(max(10.0, r * 0.085))
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        p.setPen(QPen(QColor("#1a1a1a"),
                       max(2.0, r * 0.022)))
        for h in range(12):
            angle = math.radians(h * 30 - 90)
            # Hour tick: from r*0.88 to r*0.96
            x1 = cx + math.cos(angle) * r * 0.88
            y1 = cy + math.sin(angle) * r * 0.88
            x2 = cx + math.cos(angle) * r * 0.96
            y2 = cy + math.sin(angle) * r * 0.96
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            # Numeral at r*0.76
            num_str = str(12 if h == 0 else h)
            num_x = cx + math.cos(angle) * r * 0.76
            num_y = cy + math.sin(angle) * r * 0.76
            br = fm.boundingRect(num_str)
            p.drawText(QPointF(
                num_x - br.width() / 2,
                num_y + br.height() / 3),
                num_str)

        # --- Minute ticks (thin) ---
        p.setPen(QPen(QColor("#555"), max(1.0, r * 0.008)))
        for m in range(60):
            if m % 5 == 0:
                continue  # already drawn as hour tick
            angle = math.radians(m * 6 - 90)
            x1 = cx + math.cos(angle) * r * 0.92
            y1 = cy + math.sin(angle) * r * 0.92
            x2 = cx + math.cos(angle) * r * 0.96
            y2 = cy + math.sin(angle) * r * 0.96
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # --- Hands ---
        if self._tz is not None:
            now = datetime.now(self._tz)
        else:
            now = datetime.now()
        hour = now.hour % 12
        minute = now.minute
        second = now.second
        ms = now.microsecond / 1_000_000

        # Smooth hands: include fractional contribution from
        # the next-finer unit so they don't jump in discrete
        # steps.
        hour_angle = math.radians(
            (hour + minute / 60.0) * 30 - 90)
        minute_angle = math.radians(
            (minute + second / 60.0) * 6 - 90)
        second_angle = math.radians(
            (second + ms) * 6 - 90)

        def draw_hand(angle, length, width, color, tail=0.0):
            p.setPen(QPen(QColor(color), width,
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
            tx = cx - math.cos(angle) * length * tail
            ty = cy - math.sin(angle) * length * tail
            x2 = cx + math.cos(angle) * length
            y2 = cy + math.sin(angle) * length
            p.drawLine(QPointF(tx, ty), QPointF(x2, y2))

        # Hour hand: short, thick, black
        draw_hand(hour_angle, r * 0.52,
                   max(4.0, r * 0.05), "#1a1a1a",
                   tail=0.15)
        # Minute hand: longer, medium, black
        draw_hand(minute_angle, r * 0.72,
                   max(3.0, r * 0.035), "#1a1a1a",
                   tail=0.15)
        # Second hand: longest, thin, red (the classic
        # Junghans accent)
        draw_hand(second_angle, r * 0.78,
                   max(1.5, r * 0.012), "#cc0000",
                   tail=0.2)

        # Center cap
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#cc0000")))
        p.drawEllipse(QPointF(cx, cy),
                       r * 0.04, r * 0.04)
        p.setBrush(QBrush(QColor("#1a1a1a")))
        p.drawEllipse(QPointF(cx, cy),
                       r * 0.018, r * 0.018)

        p.end()


class BigClockDialog(QDialog):
    """Modeless big-clock window. Shows analog face + digital
    HH:MM:SS read-out + the date.

    Click anywhere on the face or press ESC to close. The
    dialog auto-sizes to about 480x600 on first open and
    remembers nothing across launches - it's meant as a quick
    glance, not a panel.
    """

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clock")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinMaxButtonsHint)
        # Bigger default so the world-clock table has room to
        # show ~13 cities without forcing a scroll. User can
        # still shrink the window.
        self.resize(560, 880)

        # Currently-displayed timezone. None means local. When
        # the user clicks a row in the world-clock table this
        # gets swapped to the corresponding ZoneInfo.
        self._active_tz = None
        self._active_label = "Local"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Analog face takes the bulk of the space
        self.face = ClockFaceWidget(self)
        lay.addWidget(self.face, 1)

        # Digital read-out: huge HH:MM:SS using a system mono
        # font so the colons stay aligned and digits don't
        # jiggle as they update.
        self.lbl_digital = QLabel("--:--:--")
        f_digital = QFont(self.font())
        f_digital.setPointSize(48)
        f_digital.setBold(True)
        f_digital.setFamily("Courier New")
        self.lbl_digital.setFont(f_digital)
        self.lbl_digital.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self.lbl_digital.setStyleSheet(
            "QLabel { color: #1a1a1a; "
            "background: #f8f8f8; "
            "border: 1px solid #ccc; "
            "border-radius: 4px; padding: 4px 12px; }")
        lay.addWidget(self.lbl_digital)

        # Date line + active-city label combined
        self.lbl_date = QLabel("")
        f_date = QFont(self.font())
        f_date.setPointSize(13)
        self.lbl_date.setFont(f_date)
        self.lbl_date.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self.lbl_date.setStyleSheet(
            "QLabel { color: #444; padding: 2px; }")
        lay.addWidget(self.lbl_date)

        # World-clock section. Short hint then the table. The
        # table is always shown - we have a hardcoded fallback
        # for the case where zoneinfo can't load the OS tzdb
        # (Windows without the tzdata pip package), so the
        # cells stay populated regardless.
        hint = QLabel("World clock - click a row to set "
                        "the analog face")
        hint.setStyleSheet(
            "QLabel { color: #666; font-style: italic; "
            "padding: 4px 0 2px 0; font-size: 12px; }")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hint)

        self.tbl_world = QTableWidget(
            len(WORLD_CITIES_FALLBACK), 3)
        self.tbl_world.setHorizontalHeaderLabels(
            ["City", "Time", "Date"])
        self.tbl_world.verticalHeader().setVisible(False)
        self.tbl_world.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_world.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_world.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_world.setStyleSheet(
            "QTableWidget {"
            "  background: #ffffff;"
            "  alternate-background-color: #f6f6f6;"
            "  color: #111;"
            "  selection-background-color: #2867c2;"
            "  selection-color: #ffffff;"
            "  font-family: 'Courier New', monospace;"
            "  font-size: 13px;"
            "}"
            "QTableWidget::item:selected:!active {"
            "  background: #6ea1e0; color: #ffffff;"
            "}"
            "QHeaderView::section {"
            "  background: #e8e8e8; color: #000;"
            "  font-weight: bold; padding: 3px 6px;"
            "  border: 1px solid #c0c0c0;"
            "}")
        self.tbl_world.setAlternatingRowColors(True)
        self.tbl_world.cellClicked.connect(
            self._on_world_row_clicked)
        # Pre-fill city names; time/date columns get overwritten
        # on the first tick.
        for i, (city, _tz, *_) in enumerate(
                WORLD_CITIES_FALLBACK):
            self.tbl_world.setItem(i, 0,
                QTableWidgetItem(city))
            self.tbl_world.setItem(i, 1,
                QTableWidgetItem("--:--:--"))
            self.tbl_world.setItem(i, 2,
                QTableWidgetItem(""))
        lay.addWidget(self.tbl_world, 1)

        # Update timer for the digital + date labels + the world
        # clock table. The analog face has its own internal
        # timer (so it can repaint even when the dialog is
        # otherwise idle). 4Hz keeps the digital readout smooth.
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._tick()
        self._timer.start()

    def _tick(self):
        # Update digital + date for the ACTIVE timezone (which
        # may be local or a clicked world-clock entry).
        if self._active_tz is not None:
            now = datetime.now(self._active_tz)
        else:
            now = datetime.now()
        self.lbl_digital.setText(now.strftime("%H:%M:%S"))
        self.lbl_date.setText(
            f"{now.strftime('%A, %d %B %Y')}  -  "
            f"{self._active_label}")
        # Update world-clock table - every row, every tick. The
        # table is short (13 rows) so updating all is trivial,
        # and it means rows stay in sync even when the user
        # never clicks one.
        if self.tbl_world is not None:
            self._refresh_world_table()

    def _refresh_world_table(self):
        # Block signals while updating to avoid the cellClicked
        # handler firing as a side effect of programmatic
        # changes (Qt can synthesize selectionChanged signals
        # when items get replaced underneath the cursor).
        was_blocked = self.tbl_world.blockSignals(True)
        try:
            for i in range(len(WORLD_CITIES_FALLBACK)):
                try:
                    now, _city, _tz = _city_now(i)
                    time_str = now.strftime("%H:%M:%S")
                    # Compact date: weekday + day. Full month
                    # would bloat the column; weekday + day is
                    # enough to spot date-line crossings (e.g.
                    # Sydney already tomorrow when Berlin is
                    # still today).
                    date_str = now.strftime("%a %d")
                except Exception:
                    time_str = "?"
                    date_str = ""
                # Replace the items wholesale rather than calling
                # setText() on the existing ones. setText on a
                # QTableWidgetItem doesn't always trigger a
                # viewport repaint on Windows + Linux when the
                # cell is in selected:!active state, which made
                # Mario's table look frozen even though the
                # underlying timer was firing.
                t_item = QTableWidgetItem(time_str)
                d_item = QTableWidgetItem(date_str)
                t_item.setFlags(
                    t_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                d_item.setFlags(
                    d_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.tbl_world.setItem(i, 1, t_item)
                self.tbl_world.setItem(i, 2, d_item)
        finally:
            self.tbl_world.blockSignals(was_blocked)
        self.tbl_world.viewport().update()

    def _on_world_row_clicked(self, row, _col):
        """Click on a city row: swap the analog face + digital
        readout to that city's timezone. Uses _city_now which
        handles both real zoneinfo and the hardcoded offset
        fallback transparently."""
        if row < 0 or row >= len(WORLD_CITIES_FALLBACK):
            return
        try:
            _now, city, tz = _city_now(row)
        except Exception:
            return
        self._active_tz = tz
        self._active_label = city
        self.face.set_timezone(tz)
        self._tick()
        # Also update window title so the user remembers what
        # they're looking at if they Alt-Tab back to it.
        self.setWindowTitle(f"Clock - {city}")

    def mousePressEvent(self, ev):
        # Click on the analog face / digital / date area closes
        # the dialog. Clicks on the world-clock table are NOT
        # close-triggers - those route to the table normally so
        # the user can pick a city without immediately losing
        # the window. The table is its own child widget so we
        # only see clicks here when they hit the dialog
        # background or one of the upper labels.
        self.close()

    def keyPressEvent(self, ev):
        # ESC also closes - QDialog usually does this via
        # accept/reject but a Window flag dialog needs the
        # explicit handler.
        if ev.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        try:
            self._timer.stop()
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(ev)


def show_big_clock(parent=None):
    """Convenience: create + show a single BigClockDialog
    attached to parent. If the parent already has one open,
    raise it instead of creating a duplicate."""
    existing = getattr(parent, "_big_clock_dlg", None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return existing
    dlg = BigClockDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def _on_closed():
        if parent is not None:
            try:
                parent._big_clock_dlg = None
            except Exception:
                pass
    dlg.closed.connect(_on_closed)
    if parent is not None:
        try:
            parent._big_clock_dlg = dlg
        except Exception:
            pass
    dlg.show()
    return dlg
