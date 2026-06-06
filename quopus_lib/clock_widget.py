# date_time: 2026-06-06 16:40
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
    QAbstractItemView, QComboBox,
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

    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        # Compact mode = the tiny per-row clocks rendered next
        # to each city in the world-clock table. They need to
        # fit a table row (~28 px) so we override the normal
        # 360x360 minimum and drop margins to zero. The full-
        # face dialog version keeps the original sizing so it
        # has room for numerals + ticks.
        if compact:
            self.setMinimumSize(28, 28)
            self.setMaximumSize(56, 56)
        else:
            self.setMinimumSize(360, 360)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        # Active timezone. None = local time. Otherwise a
        # zoneinfo.ZoneInfo. Set via set_timezone() so the world-
        # clock list can swap which city the analog face shows.
        self._tz = None
        # Visual style. Default is the classic Junghans-ish
        # wall clock; the world-clock dialog lets the user
        # pick from CLOCK_STYLES via set_style().
        self._style = "classic"
        self._compact = compact
        self._timer = QTimer(self)
        # Compact clocks tick at 1Hz - they don't need to be
        # smoother than the city's digital readout. The full
        # dialog face uses its own faster timer for buttery
        # second-hand motion.
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.update)

    def set_timezone(self, tz):
        """Switch the displayed timezone. Pass None for local
        system time, or a zoneinfo.ZoneInfo instance for a
        specific city. Triggers an immediate repaint."""
        self._tz = tz
        self.update()

    def set_style(self, style_name):
        """Pick a visual style. Names come from CLOCK_STYLES;
        an unknown name silently falls back to 'classic' so
        the widget never goes blank."""
        self._style = style_name if style_name in CLOCK_STYLES \
            else "classic"
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

        # Compute current time once and pass to the style
        # painter - all styles share the same time data, only
        # the visual interpretation differs.
        if self._tz is not None:
            now = datetime.now(self._tz)
        else:
            now = datetime.now()
        hour = now.hour % 12
        minute = now.minute
        second = now.second
        ms = now.microsecond / 1_000_000
        hour_angle = math.radians(
            (hour + minute / 60.0) * 30 - 90)
        minute_angle = math.radians(
            (minute + second / 60.0) * 6 - 90)
        second_angle = math.radians(
            (second + ms) * 6 - 90)
        ctx = {
            "cx": cx, "cy": cy, "r": r,
            "now": now,
            "hour_angle": hour_angle,
            "minute_angle": minute_angle,
            "second_angle": second_angle,
            "font": self.font(),
        }

        # Dispatch to the chosen style. CLOCK_STYLES is a
        # name -> paint function map defined at module level
        # below this class so the styles are easy to find,
        # add to and read independently of the widget plumbing.
        style_fn = CLOCK_STYLES.get(self._style)
        if style_fn is None:
            style_fn = CLOCK_STYLES["classic"]
        style_fn(p, ctx)

        p.end()


# Helper: draw a single hand. Pulled out so every style can
# reuse the same geometry math while picking its own colors
# and stroke widths.
def _draw_hand(p, cx, cy, angle, length, width, color, tail=0.0,
                cap="round"):
    from PyQt6.QtCore import Qt as _Qt
    cap_style = (_Qt.PenCapStyle.RoundCap if cap == "round"
                 else _Qt.PenCapStyle.FlatCap)
    p.setPen(QPen(QColor(color), width,
                    _Qt.PenStyle.SolidLine, cap_style))
    tx = cx - math.cos(angle) * length * tail
    ty = cy - math.sin(angle) * length * tail
    x2 = cx + math.cos(angle) * length
    y2 = cy + math.sin(angle) * length
    p.drawLine(QPointF(tx, ty), QPointF(x2, y2))


# ---------------------------------------------------------------
# Style: Classic Junghans-style wall clock
# ---------------------------------------------------------------

def _paint_classic(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#fafafa")))
    p.setPen(QPen(QColor("#222"), max(2.0, r * 0.012)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    font = QFont(ctx["font"])
    font.setPointSizeF(max(10.0, r * 0.085))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#1a1a1a"), max(2.0, r * 0.022)))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.88,
                     cy + math.sin(a) * r * 0.88),
            QPointF(cx + math.cos(a) * r * 0.96,
                     cy + math.sin(a) * r * 0.96))
        s = str(12 if h == 0 else h)
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.76 - br.width() / 2,
            cy + math.sin(a) * r * 0.76 + br.height() / 3), s)
    p.setPen(QPen(QColor("#555"), max(1.0, r * 0.008)))
    for m in range(60):
        if m % 5 == 0:
            continue
        a = math.radians(m * 6 - 90)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.92,
                     cy + math.sin(a) * r * 0.92),
            QPointF(cx + math.cos(a) * r * 0.96,
                     cy + math.sin(a) * r * 0.96))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.52,
                max(4.0, r * 0.05), "#1a1a1a", tail=0.15)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.72,
                max(3.0, r * 0.035), "#1a1a1a", tail=0.15)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.78,
                max(1.5, r * 0.012), "#cc0000", tail=0.2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#cc0000")))
    p.drawEllipse(QPointF(cx, cy), r * 0.04, r * 0.04)
    p.setBrush(QBrush(QColor("#1a1a1a")))
    p.drawEllipse(QPointF(cx, cy), r * 0.018, r * 0.018)


# ---------------------------------------------------------------
# Style: Minimalist - no numerals, just ticks
# ---------------------------------------------------------------

def _paint_minimalist(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#ffffff")))
    p.setPen(QPen(QColor("#1a1a1a"), max(1.5, r * 0.008)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(QPen(QColor("#1a1a1a"), max(2.0, r * 0.020)))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        # Thicker ticks at 12, 3, 6, 9
        long_tick = (h % 3 == 0)
        inner = 0.82 if long_tick else 0.88
        p.drawLine(
            QPointF(cx + math.cos(a) * r * inner,
                     cy + math.sin(a) * r * inner),
            QPointF(cx + math.cos(a) * r * 0.96,
                     cy + math.sin(a) * r * 0.96))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.52,
                max(3.5, r * 0.04), "#1a1a1a")
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.74,
                max(2.5, r * 0.028), "#1a1a1a")
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.78,
                max(1.0, r * 0.008), "#888888")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#1a1a1a")))
    p.drawEllipse(QPointF(cx, cy), r * 0.025, r * 0.025)


# ---------------------------------------------------------------
# Style: Roman Numerals - I, II, III, IV ... XII
# ---------------------------------------------------------------

_ROMAN = ["XII", "I", "II", "III", "IV", "V",
          "VI", "VII", "VIII", "IX", "X", "XI"]


def _paint_roman(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # Ivory face, dark gold accent
    p.setBrush(QBrush(QColor("#f8efd9")))
    p.setPen(QPen(QColor("#3c2a14"), max(2.0, r * 0.016)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Inner gold ring
    p.setPen(QPen(QColor("#b89253"), max(1.0, r * 0.006)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r * 0.88, r * 0.88)
    font = QFont("Times New Roman", 1)
    font.setPointSizeF(max(11.0, r * 0.10))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#3c2a14")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = _ROMAN[h]
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.78 - br.width() / 2,
            cy + math.sin(a) * r * 0.78 + br.height() / 3), s)
    # Minute dots
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#7a5526")))
    for m in range(60):
        a = math.radians(m * 6 - 90)
        sz = r * (0.013 if m % 5 == 0 else 0.006)
        p.drawEllipse(QPointF(
            cx + math.cos(a) * r * 0.93,
            cy + math.sin(a) * r * 0.93), sz, sz)
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.50,
                max(4.0, r * 0.04), "#3c2a14", tail=0.18)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                max(3.0, r * 0.028), "#3c2a14", tail=0.18)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.74,
                max(1.5, r * 0.010), "#a8703a", tail=0.20)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#a8703a")))
    p.drawEllipse(QPointF(cx, cy), r * 0.030, r * 0.030)


# ---------------------------------------------------------------
# Style: Submariner - black dial, lume indices
# ---------------------------------------------------------------

def _paint_submariner(p, ctx):
    from PyQt6.QtGui import QRadialGradient
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # Black dial with subtle dark blue gradient
    grad = QRadialGradient(QPointF(cx, cy), r)
    grad.setColorAt(0, QColor("#0a1420"))
    grad.setColorAt(1, QColor("#000000"))
    p.setBrush(QBrush(grad))
    # Steel bezel ring
    p.setPen(QPen(QColor("#8a8a8a"), max(3.0, r * 0.04)))
    p.drawEllipse(QPointF(cx, cy), r * 0.96, r * 0.96)
    p.setBrush(QBrush(QColor("#0a0f1a")))
    p.setPen(QPen(QColor("#222"), max(1.0, r * 0.004)))
    p.drawEllipse(QPointF(cx, cy), r * 0.88, r * 0.88)
    # Lume-green hour indices: big rectangles
    p.setPen(QPen(QColor("#1a1a1a"), 1))
    p.setBrush(QBrush(QColor("#9ec48a")))   # vintage lume
    for h in range(12):
        a = math.radians(h * 30 - 90)
        if h == 0:
            # 12 o'clock: triangle marker
            tip_x = cx + math.cos(a) * r * 0.82
            tip_y = cy + math.sin(a) * r * 0.82
            from PyQt6.QtGui import QPolygonF
            poly = QPolygonF([
                QPointF(tip_x, tip_y),
                QPointF(cx + math.cos(a - 0.1) * r * 0.70,
                         cy + math.sin(a - 0.1) * r * 0.70),
                QPointF(cx + math.cos(a + 0.1) * r * 0.70,
                         cy + math.sin(a + 0.1) * r * 0.70)])
            p.drawPolygon(poly)
        else:
            # Rectangular marker
            ax = cx + math.cos(a) * r * 0.76
            ay = cy + math.sin(a) * r * 0.76
            sz = r * 0.06
            p.drawEllipse(QPointF(ax, ay), sz, sz)
    # Minute pip ring
    p.setPen(QPen(QColor("#9ec48a"), max(0.8, r * 0.004)))
    for m in range(60):
        if m % 5 == 0:
            continue
        a = math.radians(m * 6 - 90)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.83,
                     cy + math.sin(a) * r * 0.83),
            QPointF(cx + math.cos(a) * r * 0.86,
                     cy + math.sin(a) * r * 0.86))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.50,
                max(5.0, r * 0.07), "#d8d8d8", tail=0.10)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                max(4.0, r * 0.05), "#d8d8d8", tail=0.10)
    # Red-tipped second hand (Mercedes-style center disc next)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.78,
                max(1.5, r * 0.014), "#d8d8d8", tail=0.30)
    # Lollipop on second hand tip
    sa = ctx["second_angle"]
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#d8d8d8")))
    p.drawEllipse(QPointF(
        cx + math.cos(sa) * r * 0.62,
        cy + math.sin(sa) * r * 0.62),
        r * 0.04, r * 0.04)
    # Center cap
    p.setBrush(QBrush(QColor("#1a1a1a")))
    p.setPen(QPen(QColor("#888"), max(1.0, r * 0.005)))
    p.drawEllipse(QPointF(cx, cy), r * 0.06, r * 0.06)


# ---------------------------------------------------------------
# Style: Pilot - white indices on black, big-numerals
# ---------------------------------------------------------------

def _paint_pilot(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#0c0c0c")))
    p.setPen(QPen(QColor("#1a1a1a"), max(3.0, r * 0.04)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Big white arabic numerals
    font = QFont(ctx["font"])
    font.setPointSizeF(max(14.0, r * 0.13))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#f0e8d8")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = str(12 if h == 0 else h)
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.72 - br.width() / 2,
            cy + math.sin(a) * r * 0.72 + br.height() / 3), s)
    # Triangle at 12
    from PyQt6.QtGui import QPolygonF
    p.setBrush(QBrush(QColor("#ff7a00")))   # orange marker
    p.setPen(Qt.PenStyle.NoPen)
    poly = QPolygonF([
        QPointF(cx, cy - r * 0.92),
        QPointF(cx - r * 0.04, cy - r * 0.86),
        QPointF(cx + r * 0.04, cy - r * 0.86)])
    p.drawPolygon(poly)
    # Minute ticks
    p.setPen(QPen(QColor("#bbbbbb"), max(1.0, r * 0.005)))
    for m in range(60):
        a = math.radians(m * 6 - 90)
        inner = 0.86 if m % 5 == 0 else 0.91
        p.drawLine(
            QPointF(cx + math.cos(a) * r * inner,
                     cy + math.sin(a) * r * inner),
            QPointF(cx + math.cos(a) * r * 0.95,
                     cy + math.sin(a) * r * 0.95))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.48,
                max(5.0, r * 0.06), "#f0e8d8", tail=0.15)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.68,
                max(3.5, r * 0.04), "#f0e8d8", tail=0.15)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.76,
                max(1.5, r * 0.012), "#ff7a00", tail=0.25)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#ff7a00")))
    p.drawEllipse(QPointF(cx, cy), r * 0.035, r * 0.035)


# ---------------------------------------------------------------
# Style: Neon glow - cyan & magenta on black
# ---------------------------------------------------------------

def _paint_neon(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#04050a")))
    p.setPen(QPen(QColor("#1a1330"), max(2.0, r * 0.012)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Cyan glow ring (drawn with a few overlaid pens to fake bloom)
    for w, color, alpha in [
            (r * 0.06, "#00ffff", 35),
            (r * 0.03, "#00ffff", 80),
            (r * 0.012, "#a0ffff", 255)]:
        c = QColor(color); c.setAlpha(alpha)
        p.setPen(QPen(c, w))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r * 0.94, r * 0.94)
    # Tick marks
    for h in range(12):
        a = math.radians(h * 30 - 90)
        for w, color, alpha in [
                (r * 0.04, "#ff00aa", 60),
                (r * 0.015, "#ff8ad0", 255)]:
            c = QColor(color); c.setAlpha(alpha)
            p.setPen(QPen(c, w, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(cx + math.cos(a) * r * 0.78,
                         cy + math.sin(a) * r * 0.78),
                QPointF(cx + math.cos(a) * r * 0.88,
                         cy + math.sin(a) * r * 0.88))
    # Hands with bloom
    for w, alpha in [(r * 0.05, 40), (r * 0.02, 100),
                       (r * 0.008, 255)]:
        c = QColor("#00ffff"); c.setAlpha(alpha)
        _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.50,
                    w, c.name(QColor.NameFormat.HexArgb), tail=0.10)
    for w, alpha in [(r * 0.04, 40), (r * 0.018, 100),
                       (r * 0.006, 255)]:
        c = QColor("#aaffff"); c.setAlpha(alpha)
        _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                    w, c.name(QColor.NameFormat.HexArgb), tail=0.10)
    for w, alpha in [(r * 0.04, 60), (r * 0.012, 255)]:
        c = QColor("#ff00aa"); c.setAlpha(alpha)
        _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.76,
                    w, c.name(QColor.NameFormat.HexArgb), tail=0.15)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#ff00aa")))
    p.drawEllipse(QPointF(cx, cy), r * 0.035, r * 0.035)


# ---------------------------------------------------------------
# Style: C64 - chunky pixel ticks, C64 palette
# ---------------------------------------------------------------

def _paint_c64(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # C64 light blue background, dark blue frame
    p.setBrush(QBrush(QColor("#7869c4")))
    p.setPen(QPen(QColor("#352879"), max(4.0, r * 0.06)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Pixelly chunky ticks
    pixel = max(2.0, r * 0.045)
    p.setPen(Qt.PenStyle.NoPen)
    for h in range(12):
        a = math.radians(h * 30 - 90)
        px = cx + math.cos(a) * r * 0.78
        py = cy + math.sin(a) * r * 0.78
        # Black outline + white fill = C64 sprite look
        p.setBrush(QBrush(QColor("#000000")))
        p.drawRect(int(px - pixel), int(py - pixel),
                    int(pixel * 2), int(pixel * 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawRect(int(px - pixel * 0.7),
                    int(py - pixel * 0.7),
                    int(pixel * 1.4), int(pixel * 1.4))
    # Hands as chunky rectangles, white with black outline
    def chunky_hand(angle, length, width, color):
        # Build a rotated rectangle for the hand
        from PyQt6.QtGui import QPolygonF, QTransform
        # Compute end-points
        x_end = cx + math.cos(angle) * length
        y_end = cy + math.sin(angle) * length
        # Perpendicular offset
        perp = angle + math.pi / 2
        ox = math.cos(perp) * width
        oy = math.sin(perp) * width
        poly = QPolygonF([
            QPointF(cx + ox, cy + oy),
            QPointF(cx - ox, cy - oy),
            QPointF(x_end - ox, y_end - oy),
            QPointF(x_end + ox, y_end + oy)])
        p.setPen(QPen(QColor("#000000"), 2))
        p.setBrush(QBrush(QColor(color)))
        p.drawPolygon(poly)
    chunky_hand(ctx["hour_angle"], r * 0.52, r * 0.05, "#ffd700")
    chunky_hand(ctx["minute_angle"], r * 0.70, r * 0.04, "#ffffff")
    chunky_hand(ctx["second_angle"], r * 0.78, r * 0.020, "#c0392b")
    p.setPen(QPen(QColor("#000000"), 2))
    p.setBrush(QBrush(QColor("#ffd700")))
    p.drawRect(int(cx - r * 0.05), int(cy - r * 0.05),
                int(r * 0.10), int(r * 0.10))


# ---------------------------------------------------------------
# Style: Bauhaus - geometric + primary colors
# ---------------------------------------------------------------

def _paint_bauhaus(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#f4ecd8")))
    p.setPen(QPen(QColor("#1a1a1a"), max(2.0, r * 0.012)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # 4 colored quadrant arcs
    colors = ["#d33a2c", "#ffd400", "#0066cc", "#1a1a1a"]
    for i in range(12):
        a = math.radians(i * 30 - 90)
        # Quadrant: 12-3 red, 3-6 yellow, 6-9 blue, 9-12 black
        if i < 3:    col = colors[0]
        elif i < 6:  col = colors[1]
        elif i < 9:  col = colors[2]
        else:        col = colors[3]
        if i % 3 == 0:
            # Triangle at quarters
            from PyQt6.QtGui import QPolygonF
            tip_x = cx + math.cos(a) * r * 0.86
            tip_y = cy + math.sin(a) * r * 0.86
            poly = QPolygonF([
                QPointF(tip_x, tip_y),
                QPointF(cx + math.cos(a - 0.08) * r * 0.72,
                         cy + math.sin(a - 0.08) * r * 0.72),
                QPointF(cx + math.cos(a + 0.08) * r * 0.72,
                         cy + math.sin(a + 0.08) * r * 0.72)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(col)))
            p.drawPolygon(poly)
        else:
            # Circle dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(col)))
            p.drawEllipse(QPointF(
                cx + math.cos(a) * r * 0.80,
                cy + math.sin(a) * r * 0.80),
                r * 0.035, r * 0.035)
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.48,
                max(5.0, r * 0.06), "#1a1a1a", tail=0.0)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                max(3.5, r * 0.035), "#1a1a1a", tail=0.0)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.74,
                max(2.0, r * 0.016), "#d33a2c", tail=0.0)
    p.setPen(QPen(QColor("#1a1a1a"), 2))
    p.setBrush(QBrush(QColor("#ffd400")))
    p.drawEllipse(QPointF(cx, cy), r * 0.05, r * 0.05)


# ---------------------------------------------------------------
# Style: Pocket Watch - sepia, ornate, vintage
# ---------------------------------------------------------------

def _paint_pocket(p, ctx):
    from PyQt6.QtGui import QRadialGradient
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    grad = QRadialGradient(QPointF(cx, cy), r)
    grad.setColorAt(0, QColor("#f5e4c1"))
    grad.setColorAt(1, QColor("#c89d62"))
    p.setBrush(QBrush(grad))
    # Tarnished brass rim - 3 concentric rings
    p.setPen(QPen(QColor("#7c4f1d"), max(3.0, r * 0.03)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(QPen(QColor("#a06d2c"), max(1.5, r * 0.012)))
    p.drawEllipse(QPointF(cx, cy), r * 0.92, r * 0.92)
    p.setPen(QPen(QColor("#5a3712"), max(1.0, r * 0.006)))
    p.drawEllipse(QPointF(cx, cy), r * 0.84, r * 0.84)
    # Roman numerals in vintage script
    font = QFont("Times New Roman", 1)
    font.setPointSizeF(max(11.0, r * 0.10))
    font.setItalic(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#3a2210")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = _ROMAN[h]
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.74 - br.width() / 2,
            cy + math.sin(a) * r * 0.74 + br.height() / 3), s)
    # Ornate hands - thicker base tapering to point
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.46,
                max(5.0, r * 0.05), "#3a2210", tail=0.10)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.66,
                max(3.0, r * 0.030), "#3a2210", tail=0.10)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.70,
                max(1.5, r * 0.010), "#7c4f1d", tail=0.20)
    p.setPen(QPen(QColor("#3a2210"), max(1.0, r * 0.006)))
    p.setBrush(QBrush(QColor("#c89d62")))
    p.drawEllipse(QPointF(cx, cy), r * 0.035, r * 0.035)


# ---------------------------------------------------------------
# Style: Skeleton - transparent face with gears
# ---------------------------------------------------------------

def _paint_skeleton(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # Outer steel ring
    p.setBrush(QBrush(QColor("#1a1a1a")))
    p.setPen(QPen(QColor("#888"), max(3.0, r * 0.030)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # "Gears" - several offset circles inside
    p.setPen(QPen(QColor("#5a5a5a"), max(1.0, r * 0.006)))
    p.setBrush(QBrush(QColor("#222")))
    gears = [
        (cx + r * 0.30, cy - r * 0.20, r * 0.20),
        (cx - r * 0.28, cy + r * 0.18, r * 0.22),
        (cx - r * 0.18, cy - r * 0.32, r * 0.14),
        (cx + r * 0.22, cy + r * 0.32, r * 0.12),
    ]
    for gx, gy, gr in gears:
        # Gear teeth (12 around)
        for i in range(12):
            a = math.radians(i * 30)
            p.drawLine(
                QPointF(gx + math.cos(a) * gr,
                         gy + math.sin(a) * gr),
                QPointF(gx + math.cos(a) * gr * 1.18,
                         gy + math.sin(a) * gr * 1.18))
        p.drawEllipse(QPointF(gx, gy), gr, gr)
        # Center hole
        p.setBrush(QBrush(QColor("#0a0a0a")))
        p.drawEllipse(QPointF(gx, gy), gr * 0.20, gr * 0.20)
        p.setBrush(QBrush(QColor("#222")))
    # Faint hour markers - thin lines
    p.setPen(QPen(QColor("#aaaaaa"), max(1.5, r * 0.008)))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.88,
                     cy + math.sin(a) * r * 0.88),
            QPointF(cx + math.cos(a) * r * 0.94,
                     cy + math.sin(a) * r * 0.94))
    # Hollow skeleton hands
    def hollow_hand(angle, length, width, color):
        from PyQt6.QtGui import QPolygonF
        x_end = cx + math.cos(angle) * length
        y_end = cy + math.sin(angle) * length
        perp = angle + math.pi / 2
        ox = math.cos(perp) * width
        oy = math.sin(perp) * width
        poly = QPolygonF([
            QPointF(cx + ox, cy + oy),
            QPointF(cx - ox, cy - oy),
            QPointF(x_end - ox * 0.3, y_end - oy * 0.3),
            QPointF(x_end + ox * 0.3, y_end + oy * 0.3)])
        p.setPen(QPen(QColor(color), max(1.5, r * 0.008)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(poly)
    hollow_hand(ctx["hour_angle"], r * 0.50, r * 0.035, "#dddddd")
    hollow_hand(ctx["minute_angle"], r * 0.68, r * 0.025, "#dddddd")
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.76,
                max(1.0, r * 0.008), "#ff4040", tail=0.20)
    p.setPen(QPen(QColor("#888"), 2))
    p.setBrush(QBrush(QColor("#ff4040")))
    p.drawEllipse(QPointF(cx, cy), r * 0.030, r * 0.030)


# ---------------------------------------------------------------
# Style: Art Deco - gold sunburst
# ---------------------------------------------------------------

def _paint_artdeco(p, ctx):
    from PyQt6.QtGui import QRadialGradient
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    p.setBrush(QBrush(QColor("#0a0808")))
    p.setPen(QPen(QColor("#d4af37"), max(3.0, r * 0.025)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Sunburst lines from center
    p.setPen(QPen(QColor("#7a5e1a"), max(1.0, r * 0.005)))
    for i in range(24):
        a = math.radians(i * 15)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.10,
                     cy + math.sin(a) * r * 0.10),
            QPointF(cx + math.cos(a) * r * 0.78,
                     cy + math.sin(a) * r * 0.78))
    # Gold ring
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#d4af37"), max(2.0, r * 0.014)))
    p.drawEllipse(QPointF(cx, cy), r * 0.82, r * 0.82)
    # Numerals - thin gold
    font = QFont(ctx["font"])
    font.setPointSizeF(max(11.0, r * 0.095))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#d4af37")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = str(12 if h == 0 else h)
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.72 - br.width() / 2,
            cy + math.sin(a) * r * 0.72 + br.height() / 3), s)
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.48,
                max(4.0, r * 0.04), "#d4af37", tail=0.15)
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.66,
                max(3.0, r * 0.030), "#d4af37", tail=0.15)
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.74,
                max(1.5, r * 0.010), "#ffffff", tail=0.20)
    p.setPen(QPen(QColor("#d4af37"), 2))
    p.setBrush(QBrush(QColor("#0a0808")))
    p.drawEllipse(QPointF(cx, cy), r * 0.045, r * 0.045)


# ---------------------------------------------------------------
# Style: War Room - military / radar aesthetic
# ---------------------------------------------------------------

def _paint_warroom(p, ctx):
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # Dark olive face
    p.setBrush(QBrush(QColor("#1a2018")))
    p.setPen(QPen(QColor("#4a5a30"), max(3.0, r * 0.025)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Crosshairs
    p.setPen(QPen(QColor("#6a8050"), max(1.0, r * 0.006)))
    p.drawLine(QPointF(cx - r * 0.95, cy),
                QPointF(cx + r * 0.95, cy))
    p.drawLine(QPointF(cx, cy - r * 0.95),
                QPointF(cx, cy + r * 0.95))
    # Inner radar rings
    for ring_r in (0.30, 0.55, 0.80):
        p.drawEllipse(QPointF(cx, cy), r * ring_r, r * ring_r)
    # 24-hour numerals (military time on outer ring)
    font = QFont("Courier New")
    font.setPointSizeF(max(8.0, r * 0.065))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#a8c878")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = f"{(12 if h == 0 else h):02d}"
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.90 - br.width() / 2,
            cy + math.sin(a) * r * 0.90 + br.height() / 3), s)
    # Minute ticks
    p.setPen(QPen(QColor("#6a8050"), max(1.0, r * 0.006)))
    for m in range(60):
        a = math.radians(m * 6 - 90)
        if m % 5 == 0:
            inner = 0.78
        else:
            inner = 0.82
        p.drawLine(
            QPointF(cx + math.cos(a) * r * inner,
                     cy + math.sin(a) * r * inner),
            QPointF(cx + math.cos(a) * r * 0.85,
                     cy + math.sin(a) * r * 0.85))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.50,
                max(4.0, r * 0.04), "#c8e078", tail=0.10,
                cap="flat")
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                max(3.0, r * 0.028), "#c8e078", tail=0.10,
                cap="flat")
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.78,
                max(1.5, r * 0.010), "#ff5050", tail=0.20)
    p.setPen(QPen(QColor("#c8e078"), 2))
    p.setBrush(QBrush(QColor("#0a0a08")))
    p.drawEllipse(QPointF(cx, cy), r * 0.040, r * 0.040)


# ---------------------------------------------------------------
# Style: Amiga - Workbench gradient blue + orange
# ---------------------------------------------------------------

def _paint_amiga(p, ctx):
    from PyQt6.QtGui import QLinearGradient
    cx, cy, r = ctx["cx"], ctx["cy"], ctx["r"]
    # Workbench 1.3 blue and orange
    grad = QLinearGradient(cx, cy - r, cx, cy + r)
    grad.setColorAt(0, QColor("#0055aa"))
    grad.setColorAt(1, QColor("#0033aa"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor("#ffffff"), max(2.0, r * 0.020)))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # White + orange double border (Workbench window style)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#ff8800"), max(2.0, r * 0.015)))
    p.drawEllipse(QPointF(cx, cy), r * 0.92, r * 0.92)
    # Numerals in topaz orange
    font = QFont("Courier New")
    font.setPointSizeF(max(11.0, r * 0.10))
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)
    p.setPen(QPen(QColor("#ff8800")))
    for h in range(12):
        a = math.radians(h * 30 - 90)
        s = str(12 if h == 0 else h)
        br = fm.boundingRect(s)
        p.drawText(QPointF(
            cx + math.cos(a) * r * 0.78 - br.width() / 2,
            cy + math.sin(a) * r * 0.78 + br.height() / 3), s)
    # Pixel ticks
    p.setPen(QPen(QColor("#ffffff"), max(1.5, r * 0.012),
                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
    for m in range(60):
        if m % 5 == 0:
            continue
        a = math.radians(m * 6 - 90)
        p.drawLine(
            QPointF(cx + math.cos(a) * r * 0.86,
                     cy + math.sin(a) * r * 0.86),
            QPointF(cx + math.cos(a) * r * 0.90,
                     cy + math.sin(a) * r * 0.90))
    _draw_hand(p, cx, cy, ctx["hour_angle"], r * 0.50,
                max(4.5, r * 0.05), "#ffffff", tail=0.10,
                cap="flat")
    _draw_hand(p, cx, cy, ctx["minute_angle"], r * 0.70,
                max(3.5, r * 0.035), "#ffffff", tail=0.10,
                cap="flat")
    _draw_hand(p, cx, cy, ctx["second_angle"], r * 0.74,
                max(2.0, r * 0.014), "#ff8800", tail=0.20,
                cap="flat")
    p.setPen(QPen(QColor("#ffffff"), 2))
    p.setBrush(QBrush(QColor("#ff8800")))
    p.drawRect(int(cx - r * 0.04), int(cy - r * 0.04),
                int(r * 0.08), int(r * 0.08))


# ---------------------------------------------------------------
# Registry. Order matters - this is what the dialog dropdown shows.
# ---------------------------------------------------------------

CLOCK_STYLES = {
    "classic":      _paint_classic,
    "minimalist":   _paint_minimalist,
    "roman":        _paint_roman,
    "pocket":       _paint_pocket,
    "submariner":   _paint_submariner,
    "pilot":        _paint_pilot,
    "skeleton":     _paint_skeleton,
    "artdeco":      _paint_artdeco,
    "bauhaus":      _paint_bauhaus,
    "warroom":      _paint_warroom,
    "neon":         _paint_neon,
    "c64":          _paint_c64,
    "amiga":        _paint_amiga,
}

# Pretty labels for the dropdown
CLOCK_STYLE_LABELS = {
    "classic":      "Classic (Junghans)",
    "minimalist":   "Minimalist",
    "roman":        "Roman Numerals",
    "pocket":       "Pocket Watch (Vintage)",
    "submariner":   "Submariner (Diver)",
    "pilot":        "Pilot",
    "skeleton":     "Skeleton (Gears)",
    "artdeco":      "Art Deco (Gold)",
    "bauhaus":      "Bauhaus",
    "warroom":      "War Room (Radar)",
    "neon":         "Neon Glow",
    "c64":          "C64 / Retro",
    "amiga":        "Amiga Workbench",
}


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

        # Pull the persisted style out of the parent's config
        # (if there is one). Falls back to "classic" so the
        # standalone-test path still works. parent is normally
        # the QuopusMain window which holds a `config` dict.
        self._saved_style = "classic"
        if parent is not None:
            cfg = getattr(parent, "config", None)
            if isinstance(cfg, dict):
                cand = cfg.get("clock_style", "classic")
                if cand in CLOCK_STYLES:
                    self._saved_style = cand

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Style picker. Lets the user pick from CLOCK_STYLES
        # (Classic, Minimalist, Roman, Submariner, Pilot,
        # Neon, C64, Bauhaus, Pocket, Skeleton, Art Deco, War
        # Room, Amiga). The face widget repaints itself on each
        # change, no other state is touched.
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style:"))
        self.cb_style = QComboBox()
        for key, label in CLOCK_STYLE_LABELS.items():
            self.cb_style.addItem(label, key)
        # Restore the persisted selection (or 'classic' if none).
        # We set this before wiring up the signal so the
        # save-back doesn't fire spuriously during construction.
        keys_in_order = list(CLOCK_STYLE_LABELS.keys())
        try:
            idx = keys_in_order.index(self._saved_style)
        except ValueError:
            idx = 0
        self.cb_style.setCurrentIndex(idx)
        self.cb_style.currentIndexChanged.connect(
            self._on_style_changed)
        style_row.addWidget(self.cb_style, 1)
        lay.addLayout(style_row)

        # Analog face takes the bulk of the space
        self.face = ClockFaceWidget(self)
        # Apply the persisted style straight away so the first
        # paint already uses the user's pick instead of flashing
        # 'classic' for a tick.
        self.face.set_style(self._saved_style)
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
            len(WORLD_CITIES_FALLBACK), 4)
        self.tbl_world.setHorizontalHeaderLabels(
            ["", "City", "Time", "Date"])
        self.tbl_world.verticalHeader().setVisible(False)
        self.tbl_world.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_world.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_world.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        # Clock column fixed width = row height square so each
        # mini-face has room to render without distortion.
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)
        self.tbl_world.setColumnWidth(0, 40)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_world.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        # Row height generous enough to give the clocks
        # breathing room - 40 px puts them visibly above
        # micro-detail-loss territory.
        self.tbl_world.verticalHeader().setDefaultSectionSize(40)
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
        # Pre-fill city names + spawn a tiny analog clock per
        # row. Each city gets a randomly-picked style so the
        # table looks like a wall full of unique clocks rather
        # than 12 copies of the same dial. Random is seeded
        # from the city name so the assignments are stable
        # across launches - clicking around won't shuffle
        # them. The mini widget shares ClockFaceWidget's paint
        # logic via `compact=True`, just at row-height scale.
        import random as _random
        # Style pool excludes 'pocket' and 'skeleton' for the
        # mini view - their ornate details turn to mud below
        # ~60 px and just read as 'brown blob' / 'dark blob'.
        # Everything else holds up at thumbnail size.
        mini_pool = [s for s in CLOCK_STYLES
                       if s not in ("pocket", "skeleton")]
        self._mini_clocks = []
        for i, (city, _tz_id, *_) in enumerate(
                WORLD_CITIES_FALLBACK):
            rnd = _random.Random(city)
            style = rnd.choice(mini_pool)
            mini = ClockFaceWidget(compact=True)
            mini.set_style(style)
            try:
                _, _, tz = _city_now(i)
                mini.set_timezone(tz)
            except Exception:
                pass
            self.tbl_world.setCellWidget(i, 0, mini)
            self._mini_clocks.append(mini)
            self.tbl_world.setItem(i, 1,
                QTableWidgetItem(city))
            self.tbl_world.setItem(i, 2,
                QTableWidgetItem("--:--:--"))
            self.tbl_world.setItem(i, 3,
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
                self.tbl_world.setItem(i, 2, t_item)
                self.tbl_world.setItem(i, 3, d_item)
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

    def _on_style_changed(self, _idx):
        """Forward the dropdown selection to the face widget
        AND persist it to the parent's config so the choice
        survives across launches. Silent on save failures -
        the in-session change still takes effect even if disk
        I/O glitches."""
        key = self.cb_style.currentData()
        if not key:
            return
        self.face.set_style(key)
        # Persist to the main window's config dict.
        parent = self.parent()
        if parent is not None:
            cfg = getattr(parent, "config", None)
            if isinstance(cfg, dict):
                cfg["clock_style"] = key
                # Save right now rather than waiting for the
                # main window's closeEvent. Mario sometimes
                # task-kills the app; without immediate save
                # the picked style would be lost.
                try:
                    from .config import save_config
                    save_config(cfg)
                except Exception:
                    pass

    def keyPressEvent(self, ev):
        # ESC closes - QDialog usually does this via
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
