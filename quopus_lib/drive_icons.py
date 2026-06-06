# date_time: 2026-06-06 19:16
"""
Drive button icon rendering for the lister's drives bar.

Six visual styles, all selectable via the Settings dialog and
persisted in quopus.cfg under `drive_button_style`. Each style
produces a small QPixmap (drawn at integer device pixels with
QPainter / antialiasing) suitable for use as a QToolButton icon.

Styles:
  "floppy"      - classic 3.5" diskette with label area in the
                  middle for the drive letter (retro)
  "hdd"         - small horizontal hard-disk-drive with slots
                  and a green LED, letter in the middle band
  "amiga"       - Amiga Workbench drawer (DH0:/DF0: style),
                  matches the Quopus retro theme
  "pill"        - modern colored pill with a mini drive glyph
                  and the letter; color hints at media type
  "led"         - round dark badge with a glowing green letter
                  (LED on disk aesthetic)
  "mixed"       - per-drive: HOME→house, fixed→HDD, network→globe,
                  removable→USB, optical→CD. Most informative.
  "plain"       - no icon, plain text button (the original look,
                  kept so users can revert without removing the
                  selector)

A consumer (the lister) calls:

    render_drive_icon(style, label, drive_type, size=32) -> QPixmap

`drive_type` is one of "home", "fixed", "removable", "remote",
"cdrom", "ramdisk", "unknown" - it's only consulted by the
"mixed" and "pill" styles; everything else ignores it.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QRect, QPointF, QRectF
from PyQt6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QBrush, QFont, QPainterPath,
)


# All known styles, in the order the picker presents them.
STYLES = [
    # Original 7
    "amiga", "floppy", "hdd", "pill", "led", "mixed", "plain",
    # Retro storage
    "c1541", "floppy525", "cassette", "cartridge", "zipdisk",
    "reel",
    # Modern storage
    "tile", "sdcard", "ssd", "cd",
    # Stylised
    "folder", "terminal", "nixie",
]

# Human-readable labels for the picker (shown next to the preview)
STYLE_LABELS = {
    "amiga":     "Amiga Workbench drawer",
    "floppy":    "Floppy diskette (3.5\")",
    "hdd":       "Hard disk drive",
    "pill":      "Color pill with mini glyph",
    "led":       "Round LED badge",
    "mixed":     "Mixed (per drive type)",
    "plain":     "Plain text (no icon)",
    "c1541":     "Commodore 1541 disk drive",
    "floppy525": "Floppy diskette (5.25\")",
    "cassette":  "Cassette tape (Datasette)",
    "cartridge": "Cartridge (C64 game cart)",
    "zipdisk":   "Iomega Zip disk",
    "reel":      "Open-reel tape",
    "tile":      "Flat tile, big letter",
    "sdcard":    "SD / memory card",
    "ssd":       "NVMe / SSD board",
    "cd":        "Optical disc (CD/DVD)",
    "folder":    "Folder tab, colored spine",
    "terminal":  "Terminal chip [C]",
    "nixie":     "Nixie tube glow",
}

STYLE_DESCRIPTIONS = {
    "amiga":     ("Tabbed drawer in the Amiga Workbench style "
                  "(DH0:/DF0:). Fits the Quopus retro look best."),
    "floppy":    ("Classic 3.5\" floppy with metal shutter and a "
                  "label area holding the drive letter."),
    "hdd":       ("Compact hard-disk-drive icon with two platter "
                  "slots and a green activity LED."),
    "pill":      ("Modern flat pill in a colour that hints at the "
                  "drive type (blue=net, green=fixed, brown=USB, "
                  "red=optical), with a small drive glyph and the "
                  "letter."),
    "led":       ("Round dark token with the letter glowing green "
                  "like an LED. Smallest footprint per button."),
    "mixed":     ("Different icon per drive type: HOME gets a "
                  "house, network drives a globe, USB sticks the "
                  "USB shape, optical drives a CD. Most "
                  "informative."),
    "plain":     ("No icon, just the label text - the original "
                  "Quopus look. Pick this to disable drive icons."),
    "c1541":     ("The iconic Commodore 1541 floppy station: cream "
                  "case, brown drive door, red power + green "
                  "activity LEDs. Maximum demoscene cred."),
    "floppy525": ("The big black 5.25\" floppy disk with "
                  "write-protect notch and centre hub; the C64's "
                  "native disk format."),
    "cassette":  ("Audio cassette with two reels and a label band "
                  "for the letter. A nod to the Datasette and your "
                  "TAP toolkit."),
    "cartridge": ("A C64-style cartridge with a ridged top and a "
                  "label panel. For people who like their storage "
                  "plug-in."),
    "zipdisk":   ("The chunky 90s Iomega Zip disk - bevelled "
                  "corner, sliding shutter, colour label."),
    "reel":      ("Open-reel computer tape - a big spinning reel "
                  "on a hub. Mainframe nostalgia."),
    "tile":      ("Solid colour rounded tile with an oversized "
                  "letter (Windows 11 / Fluent vibe). The most "
                  "legible style; a tiny corner badge marks "
                  "removable drives."),
    "sdcard":    ("Notched-corner memory card with contact "
                  "stripes. Good for USB / SD / removable drives."),
    "ssd":       ("Slim PCB-green NVMe board with chip squares and "
                  "gold contacts. The contemporary 'drive'."),
    "cd":        ("A shiny optical disc with concentric rings and "
                  "centre hole. For CD/DVD-leaning setups."),
    "folder":    ("Manila folder with a coloured left spine that "
                  "encodes the drive type, and the letter on the "
                  "tab."),
    "terminal":  ("Flat dark chip with the letter in [brackets] "
                  "glowing phosphor-green. BBS / hacker terminal "
                  "aesthetic."),
    "nixie":     ("A glowing nixie tube - amber digit-style letter "
                  "inside a dark glass cylinder. Retro-futuristic."),
}


# ------------------------------------------------------------------
# Drive type detection
# ------------------------------------------------------------------

def detect_drive_type(label: str, path: str) -> str:
    """Best-effort classification of a drive into one of:
    home / fixed / removable / remote / cdrom / ramdisk / unknown.

    We special-case HOME/TEMP labels first since they're injected
    by Quopus's own _system_default_drives() and don't represent
    a real drive type. After that, on Windows we ask the OS via
    GetDriveTypeW(); on POSIX we make educated guesses from the
    path (/tmp, /mnt/* etc.).
    """
    lbl = (label or "").upper().strip()
    if lbl in ("HOME", "~"):
        return "home"
    if lbl in ("TEMP", "TMP"):
        return "fixed"
    if lbl in ("ROOT", "/"):
        return "fixed"
    if lbl in ("MNT", "MEDIA"):
        return "fixed"

    import os
    if os.name == "nt":
        try:
            import ctypes
            # Windows API returns:
            #   0  DRIVE_UNKNOWN
            #   1  DRIVE_NO_ROOT_DIR
            #   2  DRIVE_REMOVABLE  (USB, floppy)
            #   3  DRIVE_FIXED      (HDD, SSD)
            #   4  DRIVE_REMOTE     (network)
            #   5  DRIVE_CDROM      (CD/DVD)
            #   6  DRIVE_RAMDISK
            t = ctypes.windll.kernel32.GetDriveTypeW(
                ctypes.c_wchar_p(path))
            return {
                2: "removable", 3: "fixed", 4: "remote",
                5: "cdrom",      6: "ramdisk",
            }.get(t, "unknown")
        except Exception:
            return "unknown"

    # POSIX heuristics - we don't have a clean equivalent to
    # GetDriveTypeW so we just guess from the mount path.
    p = (path or "").rstrip("/")
    if p.startswith("/media") or p.startswith("/mnt/"):
        return "removable"
    if p.startswith("/run/media") or p.startswith("/Volumes/"):
        return "removable"
    if p in ("", "/"):
        return "fixed"
    return "unknown"


# ------------------------------------------------------------------
# Letter rendering helper
# ------------------------------------------------------------------

def _short_letter(label: str) -> str:
    """Pick the most useful 1-2 character glyph from a label, OR
    an empty string when the label is meant to be shown next to
    the icon rather than inside it.

    Single-letter drive labels ("C:", "D:") get the letter -
    those icons render with the letter centered. Multi-character
    labels like "HOME", "outputs", "public" return "" - in the
    lister we show them next to the icon, so painting text inside
    the small icon as well would be redundant and crowded. The
    one exception is single-character non-alpha labels like "/" -
    we keep them since they fit and identify the root mount.
    """
    lbl = (label or "?").strip()
    if not lbl:
        return ""
    # Drive letter labels: "C:" -> "C"
    if len(lbl) <= 2 and lbl[0].isalpha():
        return lbl[0].upper()
    # Single-char punctuation like "/" - keep, it fits
    if len(lbl) == 1:
        return lbl
    # Multi-char label -> blank, the lister will show it next to
    # the icon as a text label
    return ""


# ------------------------------------------------------------------
# Style renderers
# ------------------------------------------------------------------

def _new_pixmap(w: int, h: int) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    return pm


def _paint_text_centered(p: QPainter, rect: QRectF, text: str,
                          color: QColor, px: int = 11,
                          bold: bool = True) -> None:
    """Draw `text` centered inside `rect` using a monospace font
    sized at `px`. We use Topaz / Courier New so the letter
    matches Quopus's overall typography."""
    f = QFont("Topaz", px)
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    f.setBold(bold)
    if not f.exactMatch():
        f = QFont("Courier New", px)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        f.setBold(bold)
    p.setFont(f)
    p.setPen(color)
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def render_floppy(label: str, size: int = 32) -> QPixmap:
    """Style 1 - 3.5\" floppy diskette.

    The label area needs to be tall enough for an 11px letter to
    fit comfortably; at small icon sizes the shutter has to give
    up some height. We use 25% for the shutter (down from 30%)
    and only 2px of vertical padding around the label rect so
    that even at size=20 the letter is fully visible.
    """
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Outer shell
    p.setPen(QPen(QColor("#000"), 0.8))
    p.setBrush(QColor("#222"))
    p.drawRoundedRect(QRectF(1, 1, s - 2, s - 2), 2, 2)
    # Metal shutter at the top - 25% of height so the label area
    # below stays roughly 60% tall enough for the letter.
    shutter_h = max(4, int(s * 0.25))
    p.setBrush(QColor("#9c9c9c"))
    p.drawRect(QRectF(2, 2, s - 4, shutter_h))
    # Shutter dark slot
    p.setBrush(QColor("#222"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(QRectF(s * 0.63, 3, s * 0.20,
                       max(2, shutter_h - 2)))
    # Label area - tight 2px gap above and below
    label_y = shutter_h + 3
    label_h = s - label_y - 2
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#e8e6dc"))
    p.drawRect(QRectF(2, label_y, s - 4, label_h))
    # Use a font that scales with the icon - at size=20 px=10
    # is enough, at size=32 px=12 looks better.
    font_px = max(9, min(13, int(s * 0.5)))
    _paint_text_centered(
        p, QRectF(2, label_y, s - 4, label_h),
        _short_letter(label), QColor("#000"), px=font_px)
    p.end()
    return pm


def render_hdd(label: str, size: int = 32) -> QPixmap:
    """Style 2 - small hard disk drive.

    The middle band that carries the letter must be tall enough
    for legible text; at small icon sizes we steal height from
    the top/bottom platter slots so the letter band stays at
    least ~30% of the icon height.
    """
    pm = _new_pixmap(int(size * 1.18), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.18)
    h = size
    # Body
    p.setPen(QPen(QColor("#000"), 0.8))
    p.setBrush(QColor("#7a7a7a"))
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 2, 2)
    # Top platter slot - slimmer than before so the letter band
    # below can be wider.
    band_h = max(2, int(h * 0.14))
    p.setBrush(QColor("#9a9a9a"))
    p.drawRect(QRectF(2, 3, w - 4, band_h))
    # Middle dark band (where the letter lives) - ~36% of total
    # so a 10-12px letter has space to render.
    mid_y = 3 + band_h + 1
    mid_h = max(8, int(h * 0.36))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#5a5a5a"))
    p.drawRect(QRectF(2, mid_y, w - 4, mid_h))
    font_px = max(9, min(13, int(h * 0.5)))
    _paint_text_centered(
        p, QRectF(2, mid_y, w - 4, mid_h),
        _short_letter(label), QColor("#fff"), px=font_px)
    # Bottom platter slot
    bot_y = mid_y + mid_h + 1
    bot_h = h - bot_y - 2
    if bot_h > 0:
        p.setPen(QPen(QColor("#000"), 0.5))
        p.setBrush(QColor("#9a9a9a"))
        p.drawRect(QRectF(2, bot_y, w - 4, bot_h))
        # Activity LED
        p.setBrush(QColor("#22c81e"))
        p.setPen(QPen(QColor("#000"), 0.3))
        led_r = max(1.2, h * 0.06)
        p.drawEllipse(QPointF(w - 5, bot_y + bot_h / 2),
                      led_r, led_r)
    p.end()
    return pm


def render_amiga(label: str, size: int = 32) -> QPixmap:
    """Style 3 - Amiga Workbench drawer."""
    pm = _new_pixmap(int(size * 1.30), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.30)
    h = size
    # The drawer outline - a tab on the upper-left, body fills
    # the rest. Drawn as a single path so the silhouette is
    # clean.
    path = QPainterPath()
    tab_w = w * 0.35
    tab_h = h * 0.18
    path.moveTo(0, tab_h)
    path.lineTo(tab_w - tab_h, tab_h)
    path.lineTo(tab_w, 0)
    path.lineTo(w - 1, 0)
    path.lineTo(w - 1, h - 1)
    path.lineTo(0, h - 1)
    path.closeSubpath()
    p.setPen(QPen(QColor("#000"), 0.6))
    p.setBrush(QColor("#bababa"))
    p.drawPath(path)
    # Inset white label area
    inset_x = 3
    inset_y = tab_h + 3
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#ffffff"))
    p.drawRect(QRectF(inset_x, inset_y,
                      w - 2 * inset_x, h - inset_y - 3))
    # Single blue accent line in the upper half of the inset
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#005ba2"))
    p.drawRect(QRectF(inset_x + 2, inset_y + 3,
                      w - 2 * inset_x - 4, 2))
    # Letter (with trailing ":" if it's a single-letter drive)
    # Letter (with trailing ":" if it's a single-letter drive).
    # An empty s means the caller is using icon-besides-text mode
    # for a multi-character label - skip the colon there too, the
    # outer button label already has the punctuation it needs.
    s = _short_letter(label)
    if s and len(s) == 1 and s.isalpha():
        s = s + ":"
    font_px = max(9, min(13, int(h * 0.45)))
    _paint_text_centered(
        p,
        QRectF(inset_x, inset_y, w - 2 * inset_x,
               h - inset_y - 3),
        s, QColor("#000"), px=font_px)
    p.end()
    return pm


# Per drive-type colors for the "pill" style
_PILL_COLORS = {
    "home":      ("#185fa5", "#0c447c", "#85b7eb"),
    "fixed":     ("#3b6d11", "#27500a", "#97c459"),
    "removable": ("#854f0b", "#633806", "#fac775"),
    "remote":    ("#185fa5", "#0c447c", "#85b7eb"),
    "cdrom":     ("#a32d2d", "#791f1f", "#f09595"),
    "ramdisk":   ("#534ab7", "#3c3489", "#afa9ec"),
    "unknown":   ("#5f5e5a", "#444441", "#b4b2a9"),
}


def render_pill(label: str, drive_type: str = "unknown",
                size: int = 32) -> QPixmap:
    """Style 4 - colored pill with mini drive glyph + letter."""
    pm = _new_pixmap(int(size * 1.56), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.56)
    h = size
    fill, border, accent = _PILL_COLORS.get(
        drive_type, _PILL_COLORS["unknown"])
    p.setPen(QPen(QColor(border), 0.6))
    p.setBrush(QColor(fill))
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 6, 6)
    # Mini drive glyph on the left side
    gx = 4
    gy = h * 0.30
    gw = h * 0.40
    gh = h * 0.40
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(accent))
    p.drawRoundedRect(QRectF(gx, gy, gw, gh), 1, 1)
    p.setBrush(QColor(border))
    p.drawRect(QRectF(gx + 2, gy + 2, gw - 4, 2))
    p.setBrush(QColor("#22c81e"))
    p.drawEllipse(QPointF(gx + gw - 3, gy + gh - 3), 1.0, 1.0)
    # Letter on the right
    txt_rect = QRectF(gx + gw, 0, w - (gx + gw) - 2, h)
    _paint_text_centered(
        p, txt_rect, _short_letter(label), QColor("#fff"), px=12)
    p.end()
    return pm


def render_led(label: str, size: int = 32) -> QPixmap:
    """Style 5 - round LED-style badge."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    cx, cy = s / 2, s / 2
    # Outer dark ring
    p.setPen(QPen(QColor("#000"), 0.6))
    p.setBrush(QColor("#3a3a3a"))
    p.drawEllipse(QPointF(cx, cy), s / 2 - 1, s / 2 - 1)
    # Inner darker disc
    p.setPen(QPen(QColor("#000"), 0.3))
    p.setBrush(QColor("#1a1a1a"))
    p.drawEllipse(QPointF(cx, cy), s * 0.40, s * 0.40)
    # Green "LED" letter
    _paint_text_centered(
        p, QRectF(0, 0, s, s),
        _short_letter(label), QColor("#22c81e"), px=12)
    p.end()
    return pm


def render_house(size: int = 32) -> QPixmap:
    """Mixed-style helper: a small house (for HOME)."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    path = QPainterPath()
    # Roof + body
    path.moveTo(2, s * 0.55)
    path.lineTo(s / 2, 3)
    path.lineTo(s - 2, s * 0.55)
    path.lineTo(s - 2, s - 3)
    path.lineTo(2, s - 3)
    path.closeSubpath()
    p.setPen(QPen(QColor("#7a4b08"), 0.6))
    p.setBrush(QColor("#e8b95c"))
    p.drawPath(path)
    # Door
    p.setBrush(QColor("#5a3306"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(QRectF(s * 0.42, s * 0.65, s * 0.18, s * 0.32))
    # Window
    p.setBrush(QColor("#fff5e0"))
    p.drawRect(QRectF(s * 0.20, s * 0.60, s * 0.16, s * 0.18))
    p.end()
    return pm


def render_globe(size: int = 32) -> QPixmap:
    """Mixed-style helper: a globe (for network drives)."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    cx, cy = s / 2, s / 2
    r = s / 2 - 2
    p.setPen(QPen(QColor("#0c447c"), 0.6))
    p.setBrush(QColor("#185fa5"))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Latitude / longitude lines
    p.setPen(QPen(QColor("#ffffff"), 0.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r * 0.40, r)        # vertical
    p.drawEllipse(QPointF(cx, cy), r, r * 0.40)        # horizontal
    p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
    p.end()
    return pm


def render_terminal_window(size: int = 32) -> QPixmap:
    """Mixed-style helper: a small terminal/console window with a
    title bar and a green command prompt, used for the 'open a
    command shell here' button in the drives bar. (Distinct from
    render_terminal(label,...) which is a selectable drive-icon
    style - this one is a standalone console-window glyph.)"""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Window body (dark console)
    body = QRectF(1.5, 3, s - 3, s - 6)
    p.setPen(QPen(QColor("#000000"), 0.8))
    p.setBrush(QColor("#1c1c1c"))
    p.drawRoundedRect(body, 2, 2)
    # Title bar
    tbar = QRectF(1.5, 3, s - 3, s * 0.22)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3a6ea5"))
    p.drawRoundedRect(tbar, 2, 2)
    # square off the bottom of the title bar
    p.drawRect(QRectF(1.5, 3 + tbar.height() * 0.5,
                      s - 3, tbar.height() * 0.5))
    # Three little window dots
    p.setBrush(QColor("#e0e0e0"))
    dot_y = 3 + tbar.height() * 0.5
    dr = max(0.8, s * 0.035)
    for i in range(3):
        p.drawEllipse(QPointF(s * 0.16 + i * s * 0.11, dot_y),
                      dr, dr)
    # Green prompt  >_
    f = QFont("Courier New", max(6, int(s * 0.30)))
    f.setBold(True)
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    p.setFont(f)
    p.setPen(QColor("#33ff66"))
    p.drawText(QRectF(s * 0.16, s * 0.34, s * 0.8, s * 0.6),
               int(Qt.AlignmentFlag.AlignLeft
                   | Qt.AlignmentFlag.AlignVCenter),
               ">_")
    p.end()
    return pm



    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Metal connector at the top
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#5a5a5a"))
    p.drawRoundedRect(QRectF(s * 0.30, 2, s * 0.40, s * 0.45),
                      2, 2)
    p.setBrush(QColor("#9a9a9a"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(QRectF(s * 0.36, s * 0.15, s * 0.28, s * 0.06))
    p.drawRect(QRectF(s * 0.36, s * 0.30, s * 0.28, s * 0.06))
    # Body
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#bababa"))
    p.drawRoundedRect(QRectF(s * 0.18, s * 0.46,
                              s * 0.64, s * 0.50),
                      1, 1)
    p.end()
    return pm


def render_usb(size: int = 32) -> QPixmap:
    """USB stick - horizontal, silver metal connector on the
    left, plastic body on the right with a small activity LED
    and a hint of a keyring loop. Used by render_mixed for
    removable drives and also reachable as its own style for
    callers that want the stick shape without a letter overlay.
    The lower half is intentionally kept clear so render_mixed
    can paint the drive letter on top.
    """
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Body (right ~60% of the icon) - blue plastic
    body_left = s * 0.32
    body_w = s * 0.58
    body_top = s * 0.30
    body_h = s * 0.40
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#1f4d8a"))
    p.drawRoundedRect(
        QRectF(body_left, body_top, body_w, body_h),
        2, 2)
    # Subtle highlight stripe on the top edge so the body
    # doesn't look flat
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 50))
    p.drawRoundedRect(
        QRectF(body_left + 1, body_top + 1,
               body_w - 2, body_h * 0.18),
        1.5, 1.5)
    # Activity LED on the body - small green dot near the
    # right edge, where you'd actually find it on a real stick
    p.setBrush(QColor("#2ecc40"))
    led_r = s * 0.04
    p.drawEllipse(
        QPointF(body_left + body_w - s * 0.10,
                body_top + body_h / 2),
        led_r, led_r)
    # Metal connector (left side) - lighter silver
    conn_left = s * 0.04
    conn_w = s * 0.30
    conn_top = s * 0.36
    conn_h = s * 0.28
    p.setPen(QPen(QColor("#444"), 0.5))
    p.setBrush(QColor("#bdc3c7"))
    p.drawRect(QRectF(conn_left, conn_top, conn_w, conn_h))
    # Connector ridges (the gold-coloured contact strip at
    # the tip)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#c89b3c"))
    p.drawRect(QRectF(
        conn_left + 1, conn_top + conn_h * 0.30,
        conn_w * 0.55, conn_h * 0.40))
    # Keyring loop hint at the far right
    p.setPen(QPen(QColor("#000"), 0.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    loop_x = body_left + body_w - s * 0.05
    loop_y = body_top + body_h / 2
    p.drawEllipse(
        QPointF(loop_x + s * 0.03, loop_y),
        s * 0.025, s * 0.025)
    p.end()
    return pm


def render_cd(size: int = 32) -> QPixmap:
    """Mixed-style helper: an optical disc."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    cx, cy = s / 2, s / 2
    r = s / 2 - 2
    p.setPen(QPen(QColor("#666"), 0.5))
    p.setBrush(QColor("#cccccc"))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Concentric rings
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#aaa"), 0.4))
    p.drawEllipse(QPointF(cx, cy), r * 0.78, r * 0.78)
    p.drawEllipse(QPointF(cx, cy), r * 0.58, r * 0.58)
    # Centre hole
    p.setPen(QPen(QColor("#666"), 0.4))
    p.setBrush(QColor("#fff"))
    p.drawEllipse(QPointF(cx, cy), r * 0.22, r * 0.22)
    p.setBrush(QColor("#666"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(cx, cy), r * 0.08, r * 0.08)
    p.end()
    return pm


def render_mixed(label: str, drive_type: str,
                  size: int = 32) -> QPixmap:
    """Style 6 - dispatch to a per-type icon, with the letter
    overlaid on the lower-right corner for fixed/USB so the user
    can still see which mount it points to."""
    dt = drive_type or "unknown"
    if dt == "home":
        pm = render_house(size)
        # No letter overlay - the house shape IS the label
        return pm
    if dt == "remote":
        pm = render_globe(size)
        # Letter centered (the globe has a calmer centre)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_text_centered(
            p, QRectF(0, 0, size, size),
            _short_letter(label), QColor("#fff"), px=11)
        p.end()
        return pm
    if dt == "removable":
        pm = render_usb(size)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_text_centered(
            p, QRectF(0, size * 0.50, size, size * 0.45),
            _short_letter(label), QColor("#000"), px=11)
        p.end()
        return pm
    if dt == "cdrom":
        pm = render_cd(size)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_text_centered(
            p, QRectF(0, size * 0.70, size, size * 0.25),
            _short_letter(label), QColor("#000"), px=9)
        p.end()
        return pm
    # Default for fixed/ramdisk/unknown: use the HDD icon
    return render_hdd(label, size)


# ------------------------------------------------------------------
# Additional styles (to reach 20 total)
# ------------------------------------------------------------------

def render_c1541(label: str, size: int = 32) -> QPixmap:
    """Commodore 1541 disk drive - cream case, brown door, red
    power + green activity LEDs."""
    pm = _new_pixmap(int(size * 1.30), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.30)
    h = size
    # Case
    p.setPen(QPen(QColor("#5a5446"), 0.6))
    p.setBrush(QColor("#d8d0bc"))
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 2, 2)
    # Drive door
    p.setPen(QPen(QColor("#3a2810"), 0.4))
    p.setBrush(QColor("#7a5230"))
    door_y = h * 0.18
    door_h = h * 0.30
    p.drawRoundedRect(QRectF(w * 0.10, door_y, w * 0.80, door_h),
                      1, 1)
    p.setBrush(QColor("#4a3018"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(QRectF(w * 0.16, door_y + door_h * 0.25,
                      w * 0.45, door_h * 0.4))
    # LEDs
    led_y = h * 0.72
    p.setBrush(QColor("#e83020"))
    p.drawEllipse(QPointF(w * 0.22, led_y), 1.6, 1.6)
    p.setBrush(QColor("#22c81e"))
    p.drawEllipse(QPointF(w * 0.36, led_y), 1.6, 1.6)
    # Letter on the right of the LEDs
    font_px = max(9, min(13, int(h * 0.45)))
    _paint_text_centered(
        p, QRectF(w * 0.45, h * 0.62, w * 0.5, h * 0.32),
        _short_letter(label), QColor("#000"), px=font_px)
    p.end()
    return pm


def render_floppy525(label: str, size: int = 32) -> QPixmap:
    """5.25\" floppy - black flexible sleeve, write-protect notch,
    label sticker carries the letter, centre hub hole."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Sleeve
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#1a1a1a"))
    p.drawRoundedRect(QRectF(1, 1, s - 2, s - 2), 1, 1)
    # Hub slot at top
    p.setBrush(QColor("#444"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(s * 0.33, 2, s * 0.34, s * 0.16),
                      3, 3)
    # Label sticker
    p.setPen(QPen(QColor("#888"), 0.4))
    p.setBrush(QColor("#e8e6dc"))
    lab_y = s * 0.32
    lab_h = s * 0.36
    p.drawRect(QRectF(3, lab_y, s - 6, lab_h))
    font_px = max(9, min(12, int(s * 0.42)))
    _paint_text_centered(
        p, QRectF(3, lab_y, s - 6, lab_h),
        _short_letter(label), QColor("#000"), px=font_px)
    # Centre hub hole
    p.setPen(QPen(QColor("#555"), 0.5))
    p.setBrush(QColor("#000"))
    p.drawEllipse(QPointF(s / 2, s * 0.85), s * 0.09, s * 0.09)
    # Write-protect notch (left edge)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#000"))
    p.drawRect(QRectF(0, s * 0.28, 2, s * 0.12))
    p.end()
    return pm


def render_cassette(label: str, size: int = 32) -> QPixmap:
    """Audio cassette - two reels, label band holds the letter."""
    pm = _new_pixmap(int(size * 1.30), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.30)
    h = size
    p.setPen(QPen(QColor("#000"), 0.5))
    p.setBrush(QColor("#2a2a2a"))
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 3, 3)
    # Label band
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#e8e6dc"))
    p.drawRoundedRect(QRectF(3, 3, w - 6, h * 0.36), 1, 1)
    font_px = max(8, min(11, int(h * 0.32)))
    _paint_text_centered(
        p, QRectF(3, 3, w - 6, h * 0.36),
        _short_letter(label), QColor("#000"), px=font_px)
    # Two reels
    reel_y = h * 0.68
    for cx in (w * 0.30, w * 0.70):
        p.setPen(QPen(QColor("#888"), 0.5))
        p.setBrush(QColor("#555"))
        p.drawEllipse(QPointF(cx, reel_y), h * 0.16, h * 0.16)
        p.setBrush(QColor("#222"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, reel_y), h * 0.05, h * 0.05)
    p.end()
    return pm


def render_cartridge(label: str, size: int = 32) -> QPixmap:
    """A game cartridge - ridged top, label panel with letter."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    p.setPen(QPen(QColor("#1a1a1a"), 0.5))
    p.setBrush(QColor("#3a3a3a"))
    p.drawRoundedRect(QRectF(s * 0.12, 1, s * 0.76, s - 2), 2, 2)
    # Ridges on the top
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#555"))
    for i in range(3):
        p.drawRect(QRectF(s * 0.18 + i * s * 0.22, 3,
                          s * 0.12, s * 0.10))
    # Label panel
    p.setPen(QPen(QColor("#888"), 0.4))
    p.setBrush(QColor("#e8c66a"))
    lab_y = s * 0.30
    lab_h = s * 0.52
    p.drawRect(QRectF(s * 0.18, lab_y, s * 0.64, lab_h))
    font_px = max(9, min(13, int(s * 0.42)))
    _paint_text_centered(
        p, QRectF(s * 0.18, lab_y, s * 0.64, lab_h),
        _short_letter(label), QColor("#5a4410"), px=font_px)
    p.end()
    return pm


def render_zipdisk(label: str, size: int = 32) -> QPixmap:
    """Iomega Zip disk - bevelled corner, shutter, colour label."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    # Body with a bevelled top-right corner
    path = QPainterPath()
    bev = s * 0.22
    path.moveTo(2, 2)
    path.lineTo(s - bev, 2)
    path.lineTo(s - 2, 2 + bev)
    path.lineTo(s - 2, s - 2)
    path.lineTo(2, s - 2)
    path.closeSubpath()
    p.setPen(QPen(QColor("#1c2a48"), 0.6))
    p.setBrush(QColor("#34567a"))
    p.drawPath(path)
    # Shutter at the top
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#9aa6b8"))
    p.drawRect(QRectF(s * 0.20, 3, s * 0.48, s * 0.18))
    # Colour label band
    p.setBrush(QColor("#d04828"))
    p.drawRect(QRectF(3, s * 0.34, s - 6, s * 0.34))
    font_px = max(9, min(12, int(s * 0.40)))
    _paint_text_centered(
        p, QRectF(3, s * 0.34, s - 6, s * 0.34),
        _short_letter(label), QColor("#fff"), px=font_px)
    p.end()
    return pm


def render_reel(label: str, size: int = 32) -> QPixmap:
    """Open-reel computer tape - a big spinning reel."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    cx, cy = s / 2, s / 2
    r = s / 2 - 2
    # Tape pack (outer dark ring)
    p.setPen(QPen(QColor("#222"), 0.5))
    p.setBrush(QColor("#3a2e22"))
    p.drawEllipse(QPointF(cx, cy), r, r)
    # Flange (lighter ring with spoke holes)
    p.setBrush(QColor("#9a9488"))
    p.drawEllipse(QPointF(cx, cy), r * 0.62, r * 0.62)
    # Three spoke holes
    import math
    p.setBrush(QColor("#3a2e22"))
    p.setPen(Qt.PenStyle.NoPen)
    for k in range(3):
        ang = math.radians(90 + k * 120)
        hx = cx + math.cos(ang) * r * 0.40
        hy = cy - math.sin(ang) * r * 0.40
        p.drawEllipse(QPointF(hx, hy), r * 0.12, r * 0.12)
    # Hub + letter
    p.setBrush(QColor("#d8d0bc"))
    p.drawEllipse(QPointF(cx, cy), r * 0.30, r * 0.30)
    font_px = max(8, min(11, int(s * 0.34)))
    _paint_text_centered(
        p, QRectF(cx - r * 0.30, cy - r * 0.30,
                  r * 0.60, r * 0.60),
        _short_letter(label), QColor("#000"), px=font_px)
    p.end()
    return pm


def render_tile(label: str, drive_type: str = "unknown",
                size: int = 32) -> QPixmap:
    """Flat colour tile with a big bold letter (Fluent style).
    A tiny corner badge marks removable drives."""
    pm = _new_pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size
    colors = {
        "home": "#534ab7", "fixed": "#378add", "root": "#378add",
        "removable": "#ba7517", "remote": "#1d9e75",
        "cdrom": "#a32d2d", "ramdisk": "#7f77dd",
        "system": "#5f5e5a", "unknown": "#5f5e5a",
    }
    fill = colors.get(drive_type, "#378add")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(fill))
    p.drawRoundedRect(QRectF(1, 1, s - 2, s - 2), s * 0.22,
                      s * 0.22)
    font_px = max(11, min(18, int(s * 0.62)))
    _paint_text_centered(
        p, QRectF(0, 0, s, s),
        _short_letter(label), QColor("#fff"), px=font_px,
        bold=True)
    p.end()
    return pm


def render_sdcard(label: str, size: int = 32) -> QPixmap:
    """SD / memory card - notched corner, contact stripes."""
    pm = _new_pixmap(int(size * 0.85), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 0.85)
    h = size
    path = QPainterPath()
    notch = w * 0.34
    path.moveTo(1, notch * 0.5 + 1)
    path.lineTo(notch, 1)
    path.lineTo(w - 1, 1)
    path.lineTo(w - 1, h - 1)
    path.lineTo(1, h - 1)
    path.closeSubpath()
    p.setPen(QPen(QColor("#1c3268"), 0.6))
    p.setBrush(QColor("#3056a8"))
    p.drawPath(path)
    # Contact stripes
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#d0d0d0"))
    for i in range(4):
        p.drawRect(QRectF(w * 0.14 + i * w * 0.16, h * 0.18,
                          w * 0.08, h * 0.20))
    font_px = max(9, min(12, int(h * 0.40)))
    _paint_text_centered(
        p, QRectF(0, h * 0.45, w, h * 0.5),
        _short_letter(label), QColor("#fff"), px=font_px)
    p.end()
    return pm


def render_ssd(label: str, size: int = 32) -> QPixmap:
    """NVMe / SSD board - PCB-green with chips and gold contacts."""
    pm = _new_pixmap(int(size * 1.5), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.5)
    h = size
    p.setPen(QPen(QColor("#0d3a20"), 0.6))
    p.setBrush(QColor("#1a6b3a"))
    p.drawRoundedRect(QRectF(1, h * 0.12, w - 2, h * 0.76), 2, 2)
    # Chip squares
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#0d2818"))
    p.drawRoundedRect(QRectF(w * 0.06, h * 0.24, w * 0.20,
                             h * 0.52), 1, 1)
    p.drawRoundedRect(QRectF(w * 0.30, h * 0.24, w * 0.20,
                             h * 0.52), 1, 1)
    # Gold contacts on the right
    p.setBrush(QColor("#c8a830"))
    p.drawRect(QRectF(w * 0.84, h * 0.30, w * 0.14, h * 0.12))
    p.drawRect(QRectF(w * 0.84, h * 0.50, w * 0.14, h * 0.12))
    font_px = max(9, min(12, int(h * 0.40)))
    _paint_text_centered(
        p, QRectF(w * 0.52, h * 0.12, w * 0.30, h * 0.76),
        _short_letter(label), QColor("#fff"), px=font_px)
    p.end()
    return pm


def render_cd_style(label: str, size: int = 32) -> QPixmap:
    """Standalone optical-disc style with the letter on the disc."""
    pm = render_cd(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Put the letter on the upper part of the disc (away from the
    # centre hole)
    font_px = max(8, min(11, int(size * 0.34)))
    _paint_text_centered(
        p, QRectF(0, size * 0.10, size, size * 0.34),
        _short_letter(label), QColor("#444"), px=font_px)
    p.end()
    return pm


def render_folder(label: str, drive_type: str = "unknown",
                  size: int = 32) -> QPixmap:
    """Manila folder with a coloured left spine encoding the
    drive type and the letter on the tab."""
    pm = _new_pixmap(int(size * 1.25), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.25)
    h = size
    spine_colors = {
        "home": "#534ab7", "fixed": "#378add", "root": "#378add",
        "removable": "#ba7517", "remote": "#1d9e75",
        "cdrom": "#a32d2d", "system": "#5f5e5a",
        "unknown": "#5f5e5a",
    }
    spine = spine_colors.get(drive_type, "#378add")
    # Folder body with a tab
    path = QPainterPath()
    tab_w = w * 0.32
    tab_h = h * 0.16
    path.moveTo(0, tab_h)
    path.lineTo(tab_w, tab_h)
    path.lineTo(tab_w + tab_h, 2)
    path.lineTo(w - 1, 2)
    path.lineTo(w - 1, h - 1)
    path.lineTo(0, h - 1)
    path.closeSubpath()
    p.setPen(QPen(QColor("#9a7a20"), 0.6))
    p.setBrush(QColor("#e8c66a"))
    p.drawPath(path)
    # Coloured spine on the left
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(spine))
    p.drawRect(QRectF(0, tab_h, w * 0.13, h - tab_h - 1))
    font_px = max(9, min(13, int(h * 0.45)))
    _paint_text_centered(
        p, QRectF(w * 0.13, tab_h, w * 0.87 - 2, h - tab_h - 1),
        _short_letter(label), QColor("#5a4410"), px=font_px)
    p.end()
    return pm


def render_terminal(label: str, size: int = 32) -> QPixmap:
    """Flat dark chip with the letter in [brackets], phosphor
    green. BBS / hacker terminal aesthetic."""
    pm = _new_pixmap(int(size * 1.25), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 1.25)
    h = size
    p.setPen(QPen(QColor("#1a3a22"), 0.8))
    p.setBrush(QColor("#0d1f12"))
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 2, 2)
    s = _short_letter(label)
    txt = f"[{s}]" if s else "[ ]"
    font_px = max(9, min(13, int(h * 0.42)))
    _paint_text_centered(
        p, QRectF(0, 0, w, h), txt, QColor("#33ff66"),
        px=font_px)
    p.end()
    return pm


def render_nixie(label: str, size: int = 32) -> QPixmap:
    """A glowing nixie tube - amber digit-style letter inside a
    dark glass cylinder."""
    pm = _new_pixmap(int(size * 0.8), size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = int(size * 0.8)
    h = size
    # Glass tube body
    p.setPen(QPen(QColor("#222"), 0.6))
    p.setBrush(QColor("#15110a"))
    p.drawRoundedRect(QRectF(2, 2, w - 4, h - 4),
                      w * 0.30, w * 0.30)
    # Top cap
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3a3530"))
    p.drawRoundedRect(QRectF(w * 0.20, 1, w * 0.60, h * 0.10),
                      2, 2)
    # Base
    p.setBrush(QColor("#2a2620"))
    p.drawRect(QRectF(w * 0.18, h * 0.88, w * 0.64, h * 0.10))
    # Glowing amber letter
    font_px = max(10, min(14, int(h * 0.5)))
    _paint_text_centered(
        p, QRectF(0, 0, w, h),
        _short_letter(label), QColor("#ff9d2a"), px=font_px,
        bold=True)
    p.end()
    return pm


# ------------------------------------------------------------------
# Public dispatch
# ------------------------------------------------------------------

def render_drive_icon(style: str, label: str,
                       drive_type: str = "unknown",
                       size: int = 32) -> Optional[QPixmap]:
    """Render a drive-button icon in `style`, with `label` for
    the visible glyph and `drive_type` for styles that vary by
    type. Returns None when style == "plain" (no icon wanted -
    the button shows the label as text only)."""
    style = (style or "amiga").lower()
    if style == "plain":
        return None
    # Styles that take (label, drive_type, size)
    if style == "pill":
        return render_pill(label, drive_type, size)
    if style == "mixed":
        return render_mixed(label, drive_type, size)
    if style == "tile":
        return render_tile(label, drive_type, size)
    if style == "folder":
        return render_folder(label, drive_type, size)
    # Styles that take (label, size)
    simple = {
        "floppy":    render_floppy,
        "hdd":       render_hdd,
        "amiga":     render_amiga,
        "led":       render_led,
        "c1541":     render_c1541,
        "floppy525": render_floppy525,
        "cassette":  render_cassette,
        "cartridge": render_cartridge,
        "zipdisk":   render_zipdisk,
        "reel":      render_reel,
        "sdcard":    render_sdcard,
        "ssd":       render_ssd,
        "cd":        render_cd_style,
        "terminal":  render_terminal,
        "nixie":     render_nixie,
    }
    fn = simple.get(style)
    if fn is not None:
        return fn(label, size)
    # Unknown -> default
    return render_amiga(label, size)


# ------------------------------------------------------------------
# Picker dialog
# ------------------------------------------------------------------

def open_style_picker(parent, current_style: str = "amiga",
                       on_apply=None):
    """Show a modal dialog letting the user pick one of the six
    drive-button styles. Each style is shown with a row of five
    sample drive buttons (HOME / C: / D: / E: / F:) plus a short
    description.

    Parameters
    ----------
    parent : QWidget
        The dialog parent (usually the main window).
    current_style : str
        Which style entry is selected on open.
    on_apply : Optional[callable[[str], None]]
        Called with the chosen style key when the user clicks
        Apply or OK. Use this to persist + re-render the listers.
    """
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QListWidget, QListWidgetItem, QWidget, QSizePolicy,
    )
    from PyQt6.QtGui import QIcon
    from PyQt6.QtCore import QSize, Qt

    dlg = QDialog(parent)
    dlg.setWindowTitle("Drive button style")
    dlg.resize(640, 460)

    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(10, 10, 10, 10)
    outer.setSpacing(8)

    intro = QLabel(
        "Pick how the drive-shortcut buttons appear at the top of "
        "each lister. Each style is rendered live - click an entry "
        "to preview it, then Apply or OK to save.")
    intro.setWordWrap(True)
    outer.addWidget(intro)

    body = QHBoxLayout()
    body.setSpacing(10)

    # Left: list of style names
    listw = QListWidget()
    listw.setFixedWidth(220)
    for key in STYLES:
        it = QListWidgetItem(STYLE_LABELS.get(key, key))
        it.setData(Qt.ItemDataRole.UserRole, key)
        listw.addItem(it)
    # Select the current style on open
    for i in range(listw.count()):
        if (listw.item(i).data(Qt.ItemDataRole.UserRole)
                == current_style):
            listw.setCurrentRow(i)
            break
    if listw.currentRow() < 0:
        listw.setCurrentRow(0)
    body.addWidget(listw)

    # Right: preview panel
    preview_box = QWidget()
    pv = QVBoxLayout(preview_box)
    pv.setContentsMargins(0, 0, 0, 0)
    pv.setSpacing(6)

    pv_title = QLabel("Preview")
    f = pv_title.font(); f.setBold(True); pv_title.setFont(f)
    pv.addWidget(pv_title)

    pv_desc = QLabel("")
    pv_desc.setWordWrap(True)
    pv_desc.setMinimumHeight(48)
    pv.addWidget(pv_desc)

    # Sample drive set - one per likely drive type so the
    # "mixed" / "pill" styles actually show their variation.
    SAMPLES = [
        ("HOME", "/home/me",   "home"),
        ("C:",   "C:/",        "fixed"),
        ("D:",   "D:/",        "fixed"),
        ("E:",   "E:/",        "removable"),
        ("F:",   "F:/",        "cdrom"),
        ("Z:",   "Z:/",        "remote"),
    ]

    preview_row = QWidget()
    pr = QHBoxLayout(preview_row)
    pr.setContentsMargins(8, 8, 8, 8)
    pr.setSpacing(2)
    preview_row.setStyleSheet(
        "QWidget { background-color: #C0C0C0; "
        "border: 1px solid #808080; }")
    pv.addWidget(preview_row)

    pv_note = QLabel(
        "Preview uses sample drives of different types (fixed, "
        "removable, optical, network) so you can see how each "
        "type is coloured. Your real bar colours each button by "
        "its actual type - if all your drives are the same type "
        "they'll share one colour. Colour-by-type applies to the "
        "tile, pill, mixed and folder styles.")
    pv_note.setWordWrap(True)
    pv_note.setStyleSheet("color: #555;")
    pv.addWidget(pv_note)
    pv.addStretch(1)

    body.addWidget(preview_box, 1)
    outer.addLayout(body)

    # The preview buttons - we rebuild them whenever the
    # selection changes so the new style is shown immediately.
    preview_buttons = []

    def _rebuild_preview(style_key):
        # Clear previous buttons
        for b in preview_buttons:
            pr.removeWidget(b)
            b.deleteLater()
        preview_buttons.clear()
        for (lbl, path, dt) in SAMPLES:
            btn = QPushButton()
            pm = render_drive_icon(style_key, lbl, dt, size=32)
            if pm is not None:
                btn.setIcon(QIcon(pm))
                btn.setIconSize(QSize(pm.width(), pm.height()))
                btn.setText("")
            else:
                btn.setText(lbl)
            btn.setToolTip(f"{lbl}  {path}")
            btn.setStyleSheet(
                "QPushButton { background-color: #C0C0C0; "
                "color: #000; "
                "border: 1px outset #808080; "
                "padding: 2px 4px; min-width: 22px; "
                "font-family: 'Topaz','Courier New',monospace; "
                "font-size: 11px; }")
            pr.addWidget(btn)
            preview_buttons.append(btn)
        pr.addStretch(1)
        # Description
        pv_desc.setText(
            STYLE_DESCRIPTIONS.get(style_key, ""))

    def _on_select():
        it = listw.currentItem()
        if it is None:
            return
        key = it.data(Qt.ItemDataRole.UserRole)
        _rebuild_preview(key)

    listw.currentRowChanged.connect(lambda _i: _on_select())
    _on_select()                # initial render

    # Buttons row
    btns = QHBoxLayout()
    btns.addStretch(1)
    btn_apply = QPushButton("Apply")
    btn_ok = QPushButton("OK")
    btn_cancel = QPushButton("Cancel")
    btns.addWidget(btn_apply)
    btns.addWidget(btn_ok)
    btns.addWidget(btn_cancel)
    outer.addLayout(btns)

    def _picked():
        it = listw.currentItem()
        if it is None:
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def _do_apply():
        key = _picked()
        if key is None or on_apply is None:
            return
        try:
            on_apply(key)
        except Exception as e:
            print(f"[drive_icons] apply failed: {e}")

    def _do_ok():
        _do_apply()
        dlg.accept()

    btn_apply.clicked.connect(_do_apply)
    btn_ok.clicked.connect(_do_ok)
    btn_cancel.clicked.connect(dlg.reject)

    dlg.exec()
