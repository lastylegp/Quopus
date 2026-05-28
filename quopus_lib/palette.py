# date_time: 2026-05-28 00:26
"""Colors, stylesheets, font loading from ./fonts/."""
from pathlib import Path
from PyQt6.QtGui import QFont, QFontDatabase
from .config import scaled_font_px


class C:
    WB_GREY     = "#a0a0a0"
    WB_GREY_LT  = "#c8c8c8"
    WB_GREY_DK  = "#707070"
    BLACK       = "#000000"
    WHITE       = "#ffffff"
    WB_BLUE     = "#2040a0"
    WB_BLUE_LT  = "#5878c0"
    WB_BLUE_DK  = "#101830"

    # Active lister indicator (like original Quopus red/yellow highlight)
    ACTIVE_BG   = "#c83030"      # red
    ACTIVE_FG   = "#ffff00"      # yellow text (like "festplatte2" in screenshot)

    BTN_BLUE      = "#3050a8"
    BTN_BLUE_FG   = "#ffffff"
    BTN_PURPLE    = "#a840a8"
    BTN_PURPLE_FG = "#ffffff"
    BTN_BLACK     = "#000000"
    BTN_BLACK_FG  = "#ff8800"
    BTN_ORANGE    = "#ff8800"
    BTN_ORANGE_FG = "#000000"
    BTN_RED       = "#c83030"
    BTN_RED_FG    = "#ffffff"
    BTN_DEV       = "#2050a0"
    BTN_DEV_FG    = "#ffffff"
    BTN_MID       = "#a0a0a0"
    BTN_MID_FG    = "#000000"

    LISTER_BG   = "#a0a0a0"
    LISTER_FG   = "#000000"
    LISTER_DIR  = "#0000cc"
    SELECTED    = "#2040a0"
    SELECTED_FG = "#ffffff"
    TAGGED_BG   = "#ff8800"      # tagged files (orange, like selected in DOpus)
    TAGGED_FG   = "#000000"


def _contrast_fg(hex_bg):
    """Auto-pick white or black text based on background luminance."""
    h = hex_bg.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    # Perceptual luminance
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if lum > 140 else "#ffffff"


# Named button color presets. The legacy 7 keys (blue, purple, black, orange,
# red, dev, mid) come first for backwards compat with existing configs.
# Then 30+ additional color options covering warm/cool/accent tones.
_BUTTON_BG_PALETTE = {
    # Legacy / core
    "blue":       C.BTN_BLUE,        # #3050a8
    "purple":     C.BTN_PURPLE,      # #a840a8
    "black":      C.BTN_BLACK,       # #000000 (w/ orange text)
    "orange":     C.BTN_ORANGE,      # #ff8800
    "red":        C.BTN_RED,         # #c83030
    "dev":        C.BTN_DEV,         # #2050a0
    "mid":        C.BTN_MID,         # #a0a0a0

    # Blues
    "navy":       "#001f5a",
    "royal":      "#2040c0",
    "sky":        "#4da6ff",
    "teal":       "#1a9a9a",
    "cyan":       "#00bfd0",

    # Greens
    "darkgreen":  "#1e6e1e",
    "green":      "#2eaa2e",
    "lime":       "#8ce03a",
    "olive":      "#808000",
    "forest":     "#2a5a2a",

    # Yellows/oranges
    "yellow":     "#ffd000",
    "gold":       "#d4a820",
    "amber":      "#ff9f00",
    "coral":      "#ff6a4a",
    "brown":      "#804020",

    # Reds/pinks
    "crimson":    "#a01030",
    "maroon":     "#5a1020",
    "pink":       "#ff70b0",
    "magenta":    "#d030a0",
    "rose":       "#e86a8e",

    # Purples
    "violet":     "#7a40d0",
    "lavender":   "#b090d8",
    "indigo":     "#4040a0",
    "plum":       "#7a2a7a",

    # Grays
    "white":      "#f0f0f0",
    "lightgrey":  "#c8c8c8",
    "grey":       "#808080",
    "darkgrey":   "#404040",

    # Amiga / retro accents
    "wbblue":     "#0055aa",     # Workbench 1.x blue
    "wborange":   "#ff8800",
    "wbwhite":    "#eeeeee",
    "petscii":    "#7878ff",     # C64 light blue
    "c64brown":   "#553800",
}


BUTTON_STYLES = {
    name: (bg, _contrast_fg(bg)) for name, bg in _BUTTON_BG_PALETTE.items()
}

# Honor original FG overrides for the legacy keys (black button had orange
# text originally, that's a style choice not a contrast calc)
BUTTON_STYLES["black"]  = (C.BTN_BLACK,  C.BTN_BLACK_FG)   # black bg, orange fg
BUTTON_STYLES["orange"] = (C.BTN_ORANGE, C.BTN_ORANGE_FG)  # orange bg, black fg


def get_button_color_names():
    """Return all color keys in display order (legacy first)."""
    return list(_BUTTON_BG_PALETTE.keys())


_loaded_fonts = {}


def load_bundled_fonts(fonts_dir: Path):
    families = []
    if not fonts_dir.is_dir():
        return families
    for f in fonts_dir.iterdir():
        if f.suffix.lower() not in (".ttf", ".otf"):
            continue
        fid = QFontDatabase.addApplicationFont(str(f))
        if fid >= 0:
            for fam in QFontDatabase.applicationFontFamilies(fid):
                families.append(fam)
                _loaded_fonts[fam] = True
    return families


def get_topaz_font(size=11, scaled=True):
    """Return a QFont in the Topaz family at the given point
    size. If scaled is True (default), the size is multiplied
    by the user's app font scale factor - so a get_topaz_font(11)
    at 150% scale returns a 16-17pt font.

    Pass scaled=False if you specifically want the literal
    size (e.g. when the size is already user-controlled like
    in TextReader's +/- zoom).
    """
    if scaled:
        try:
            from .config import scaled_font_px
            size = scaled_font_px(size)
        except Exception:
            pass
    for name in ("Topaz-8", "Topaz New", "Topaz", "TopazPlus a1200 v1.0"):
        f = QFont(name)
        if f.exactMatch() or name in _loaded_fonts:
            f.setPointSize(size)
            f.setStyleHint(QFont.StyleHint.TypeWriter)
            return f
    f = QFont("Courier New")
    f.setPointSize(size)
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


def get_c64_font(size=14, scaled=True):
    """Return a QFont in a C64 family. See get_topaz_font for
    the `scaled` parameter behavior."""
    if scaled:
        try:
            from .config import scaled_font_px
            size = scaled_font_px(size)
        except Exception:
            pass
    for name in ("C64 Pro Mono", "C64 Pro", "C64 Elite Mono",
                 "PetMe64", "PetMe2Y", "Unscii", "Unscii 16"):
        f = QFont(name)
        if f.exactMatch() or name in _loaded_fonts:
            f.setPointSize(size)
            f.setStyleHint(QFont.StyleHint.TypeWriter)
            return f
    f = QFont("Courier New")
    f.setPointSize(size)
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


def get_mono_font(size=11, scaled=True):
    """Return the system default monospace font (Cascadia,
    Consolas, DejaVu Mono, Menlo, depending on platform) at
    the given point size.

    Used in the U64 streamer's hex/asm tables, the cell-editor
    line edit, and anywhere a monospace font is wanted but
    we don't care which specific family.

    With scaled=True (default) the size is multiplied by the
    user's app font scale - so the streamer's hex view, type
    line, and disassembly all grow/shrink with the global
    setting.
    """
    if scaled:
        try:
            from .config import scaled_font_px
            size = scaled_font_px(size)
        except Exception:
            pass
    try:
        from PyQt6.QtGui import QFontDatabase
        f = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont)
        f.setPointSize(size)
        return f
    except Exception:
        f = QFont("Courier New")
        f.setPointSize(size)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        return f


def has_c64_pro_mono():
    """Specifically C64 Pro Mono uses PUA E100-E2FF for all 256 screencodes."""
    for name in ("C64 Pro Mono", "C64 Pro"):
        if name in _loaded_fonts or QFont(name).exactMatch():
            return True
    return False


def button_qss(color_key):
    bg, fg = BUTTON_STYLES.get(color_key, (C.BTN_BLUE, C.BTN_BLUE_FG))
    return f"""
    QPushButton {{
        background-color: {bg};
        color: {fg};
        border: 1px solid {C.BLACK};
        padding: 1px 4px;
        font-family: "Topaz-8", "Topaz", "Courier New", monospace;
        font-size: {scaled_font_px(12)}px;
        font-weight: bold;
        min-height: 18px;
    }}
    QPushButton:pressed {{
        background-color: {C.BLACK};
        color: {bg};
    }}
    """


WB_TITLEBAR_INACTIVE_QSS = f"""
QLabel {{
    background-color: {C.WB_BLUE};
    color: {C.WHITE};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    font-weight: bold;
    padding: 2px 8px;
    border-top: 1px solid {C.WB_BLUE_LT};
    border-left: 1px solid {C.WB_BLUE_LT};
    border-right: 1px solid {C.BLACK};
    border-bottom: 1px solid {C.BLACK};
}}
"""

# Active lister: red background, yellow text (like Quopus "festplatte2" in screenshot)
WB_TITLEBAR_ACTIVE_QSS = f"""
QLabel {{
    background-color: {C.ACTIVE_BG};
    color: {C.ACTIVE_FG};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    font-weight: bold;
    padding: 2px 8px;
    border-top: 1px solid #ff6060;
    border-left: 1px solid #ff6060;
    border-right: 1px solid {C.BLACK};
    border-bottom: 1px solid {C.BLACK};
}}
"""

SCREEN_TITLEBAR_QSS = f"""
QLabel {{
    background-color: {C.WB_GREY};
    color: {C.BLACK};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    font-weight: bold;
    padding: 2px 8px;
    border-bottom: 1px solid {C.BLACK};
}}
"""

LISTER_QSS = f"""
QListView {{
    background-color: {C.LISTER_BG};
    color: {C.LISTER_FG};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    border: 1px solid {C.BLACK};
    selection-background-color: {C.SELECTED};
    selection-color: {C.SELECTED_FG};
    outline: none;
    alternate-background-color: {C.LISTER_BG};
    show-decoration-selected: 0;
}}
"""

PATH_EDIT_QSS = f"""
QLineEdit {{
    background-color: {C.WHITE};
    color: {C.BLACK};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    border: 1px solid {C.BLACK};
    padding: 1px 3px;
}}
"""

INFOBAR_QSS = f"""
QLabel {{
    background-color: {C.WB_GREY};
    color: {C.BLACK};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(11)}px;
    font-weight: bold;
    padding: 1px 6px;
    border-top: 1px solid {C.WB_GREY_DK};
    border-left: 1px solid {C.WB_GREY_DK};
    border-right: 1px solid {C.WB_GREY_LT};
    border-bottom: 1px solid {C.WB_GREY_LT};
}}
"""

STATUSBAR_QSS = f"""
QLabel {{
    background-color: {C.WB_BLUE};
    color: {C.WHITE};
    font-family: "Topaz-8", "Topaz", "Courier New", monospace;
    font-size: {scaled_font_px(12)}px;
    font-weight: bold;
    padding: 2px 6px;
    border: 1px solid {C.BLACK};
}}
"""

SCROLLBAR_QSS = f"""
QScrollBar:vertical {{
    background: {C.WB_GREY};
    width: 16px;
    border: 1px solid {C.BLACK};
}}
QScrollBar::handle:vertical {{
    background: {C.WHITE};
    border: 1px solid {C.BLACK};
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    background: {C.WB_GREY};
    border: 1px solid {C.BLACK};
    height: 14px;
}}
QScrollBar:horizontal {{
    background: {C.WB_GREY};
    height: 16px;
    border: 1px solid {C.BLACK};
}}
QScrollBar::handle:horizontal {{
    background: {C.WHITE};
    border: 1px solid {C.BLACK};
    min-width: 20px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    background: {C.WB_GREY};
    border: 1px solid {C.BLACK};
    width: 14px;
}}
"""


def fmt_size(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def fmt_blocks(n):
    """Format bytes as C64 disk blocks (256 bytes per block, 1541 D64
    convention). Always rounded UP - a 1-byte file still occupies a
    full block on the disk, like in CBM DOS. Returned as 'NNN bl'."""
    if n <= 0:
        return "0 bl"
    blocks = (int(n) + 255) // 256
    return f"{blocks} bl"
