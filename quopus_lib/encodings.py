"""
Plain decoders (strip colors) + colored parsers (2D grid).

Colored parsers emit cells:
  ANSI:    {char: str, fg: #rgb, bg: #rgb, reverse: bool}
  PETSCII: {byte: raw byte, sc: screencode, fg, bg, reverse, charset}
"""
import re

from .petscii_tables import (
    C64_COLORS_HEX, PETSCII_COLOR_CODES, is_petscii_color,
    petscii_to_screencode, petscii_byte_to_unicode,
)


# ============================================================
# PLAIN TEXT
# ============================================================
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_CHARSET_RE = re.compile(r"\x1b[\(\)][A-Z]")


def amiga_to_unicode(raw_bytes):
    text = raw_bytes.decode("latin-1", errors="replace")
    text = _ANSI_RE.sub("", text)
    text = _ANSI_CHARSET_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").replace("\x0c", "\n\n")
    return text


def cp437_to_unicode(raw_bytes):
    try:
        text = raw_bytes.decode("cp437", errors="replace")
    except Exception:
        text = raw_bytes.decode("latin-1", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ============================================================
# ANSI COLOR PARSER
# ============================================================
ANSI_COLORS = [
    "#000000", "#aa0000", "#00aa00", "#aa5500",
    "#0000aa", "#aa00aa", "#00aaaa", "#aaaaaa",
    "#555555", "#ff5555", "#55ff55", "#ffff55",
    "#5555ff", "#ff55ff", "#55ffff", "#ffffff",
]


def _make_ansi_cell(ch=" ", fg="#aaaaaa", bg="#000000", reverse=False):
    return {"char": ch, "fg": fg, "bg": bg, "reverse": reverse}


def parse_ansi(raw_bytes, width=80, max_height=100000):
    """Parse Amiga/DOS ANSI art into 2D color cell grid."""
    grid = [[_make_ansi_cell() for _ in range(width)]]
    cx, cy = 0, 0
    cur_fg = "#aaaaaa"
    cur_bg = "#000000"
    bold = False
    reverse = False

    def ensure_row(y):
        while len(grid) <= y and len(grid) < max_height:
            grid.append([_make_ansi_cell(bg=cur_bg) for _ in range(width)])

    def clamp_cy():
        nonlocal cy
        if cy >= len(grid):
            cy = len(grid) - 1
        if cy < 0:
            cy = 0

    def apply_sgr(params):
        nonlocal cur_fg, cur_bg, bold, reverse
        if not params:
            params = [0]
        for p in params:
            if p == 0:
                cur_fg = "#aaaaaa"; cur_bg = "#000000"
                bold = False; reverse = False
            elif p == 1:
                bold = True
            elif p == 22:
                bold = False
            elif p == 7:
                reverse = True
            elif p == 27:
                reverse = False
            elif 30 <= p <= 37:
                idx = p - 30
                if bold:
                    idx += 8
                cur_fg = ANSI_COLORS[idx]
            elif 40 <= p <= 47:
                cur_bg = ANSI_COLORS[p - 40]
            elif 90 <= p <= 97:
                cur_fg = ANSI_COLORS[p - 90 + 8]
            elif 100 <= p <= 107:
                cur_bg = ANSI_COLORS[p - 100 + 8]
            elif p == 39:
                cur_fg = "#aaaaaa"
            elif p == 49:
                cur_bg = "#000000"

    i = 0
    n = len(raw_bytes)
    while i < n:
        b = raw_bytes[i]
        if b == 0x1B and i + 1 < n:
            nxt = raw_bytes[i + 1]
            if nxt == ord('['):
                j = i + 2
                params_str = ""
                while j < n:
                    c = raw_bytes[j]
                    if 0x30 <= c <= 0x3F or 0x20 <= c <= 0x2F:
                        params_str += chr(c); j += 1
                    elif 0x40 <= c <= 0x7E:
                        final = chr(c)
                        try:
                            params = [int(x) for x in params_str.replace("?", "").split(";") if x != ""]
                        except ValueError:
                            params = []
                        if final == 'm':
                            apply_sgr(params)
                        elif final in ('H', 'f'):
                            r = params[0] - 1 if len(params) >= 1 and params[0] > 0 else 0
                            c_ = params[1] - 1 if len(params) >= 2 and params[1] > 0 else 0
                            cy = max(0, r); cx = max(0, min(width - 1, c_))
                            ensure_row(cy)
                        elif final == 'A':
                            cy = max(0, cy - (params[0] if params else 1))
                        elif final == 'B':
                            cy += params[0] if params else 1; ensure_row(cy)
                        elif final == 'C':
                            cx = min(width - 1, cx + (params[0] if params else 1))
                        elif final == 'D':
                            cx = max(0, cx - (params[0] if params else 1))
                        elif final == 'J':
                            mode = params[0] if params else 0
                            if mode == 2:
                                for y in range(len(grid)):
                                    grid[y] = [_make_ansi_cell(bg=cur_bg) for _ in range(width)]
                                cx = 0; cy = 0
                            elif mode == 0:
                                for x in range(cx, width):
                                    grid[cy][x] = _make_ansi_cell(bg=cur_bg)
                                for y in range(cy + 1, len(grid)):
                                    grid[y] = [_make_ansi_cell(bg=cur_bg) for _ in range(width)]
                            elif mode == 1:
                                for y in range(0, cy):
                                    grid[y] = [_make_ansi_cell(bg=cur_bg) for _ in range(width)]
                                for x in range(0, cx + 1):
                                    grid[cy][x] = _make_ansi_cell(bg=cur_bg)
                        elif final == 'K':
                            mode = params[0] if params else 0
                            if mode == 0:
                                for x in range(cx, width):
                                    grid[cy][x] = _make_ansi_cell(bg=cur_bg)
                            elif mode == 1:
                                for x in range(0, cx + 1):
                                    grid[cy][x] = _make_ansi_cell(bg=cur_bg)
                            elif mode == 2:
                                grid[cy] = [_make_ansi_cell(bg=cur_bg) for _ in range(width)]
                        i = j + 1
                        break
                    else:
                        j += 1; break
                else:
                    i = n
                continue
            elif nxt in (ord('('), ord(')')) and i + 2 < n:
                i += 3; continue
            else:
                i += 2; continue

        if b == 0x0A: cx = 0; cy += 1; ensure_row(cy); clamp_cy(); i += 1; continue
        if b == 0x0D: cx = 0; i += 1; continue
        if b == 0x08: cx = max(0, cx - 1); i += 1; continue
        if b == 0x09: cx = min(width - 1, ((cx // 8) + 1) * 8); i += 1; continue
        if b == 0x0C: cx = 0; cy += 1; ensure_row(cy); clamp_cy(); i += 1; continue
        if b == 0x1A: break
        if b < 0x20: i += 1; continue

        try:
            ch = bytes([b]).decode("cp437")
        except Exception:
            ch = "?"

        ensure_row(cy)
        clamp_cy()
        if cx < width and 0 <= cy < len(grid):
            grid[cy][cx] = _make_ansi_cell(ch=ch, fg=cur_fg, bg=cur_bg, reverse=reverse)
        cx += 1
        if cx >= width:
            cx = 0; cy += 1; ensure_row(cy); clamp_cy()
        i += 1

    return grid, width, len(grid)


# ============================================================
# PETSCII COLOR PARSER (using exact CGTerm conversion)
# ============================================================
def parse_petscii(raw_bytes, width=40, max_height=10000,
                  initial_charset='lower'):
    """
    Parse a PETSCII byte stream into a 2D grid of color cells.
    
    Each cell contains:
      byte    - original PETSCII byte (0-255)
      sc      - screencode (via CGTerm conversion)
      fg      - foreground hex color
      bg      - background hex color (global screen bg at time of write)
      reverse - RVS flag
      charset - 'upper' or 'lower' at time of write

    Following Mario's petscii_parser.py exactly:
      - $02 + color byte = screen bg (CTRL-B)
      - $03 = bg black (CTRL-C BBS convention)
      - $0E/$8E = charset lower/upper
      - $12/$92 = RVS on/off
      - $13/$93 = HOME/CLEAR
      - $11/$91/$1D/$9D = cursor down/up/right/left
      - $0D/$8D = CR (resets RVS)
    """
    cur_fg_idx = 14     # light blue (C64 default)
    screen_bg_idx = 0   # black (BBS default)
    charset = initial_charset
    reverse = False
    awaiting_bg = False

    def new_cell(b=0x20, sc=0x20):
        return {
            "byte": b, "sc": sc,
            "fg": C64_COLORS_HEX[cur_fg_idx],
            "bg": C64_COLORS_HEX[screen_bg_idx],
            "reverse": False,
            "charset": charset,
        }

    grid = [[new_cell() for _ in range(width)]]
    cx, cy = 0, 0

    def ensure_row(y):
        while len(grid) <= y and len(grid) < max_height:
            grid.append([new_cell() for _ in range(width)])

    def clamp_cy():
        """Keep cy within the allocated grid; if we hit max_height,
        stop advancing and wrap back to the last row."""
        nonlocal cy
        if cy >= len(grid):
            cy = len(grid) - 1

    def newline():
        nonlocal cx, cy, reverse
        cx = 0
        cy += 1
        reverse = False
        ensure_row(cy)
        clamp_cy()

    for b in raw_bytes:
        # waiting for CTRL-B color param
        if awaiting_bg:
            awaiting_bg = False
            if b in PETSCII_COLOR_CODES:
                screen_bg_idx = PETSCII_COLOR_CODES[b]
                continue
            # else fall through

        # CTRL-B = screen bg follows
        if b == 0x02:
            awaiting_bg = True
            continue

        # CTRL-C = bg to black (BBS convention)
        if b == 0x03:
            screen_bg_idx = 0
            continue

        # BELL
        if b == 0x07:
            continue

        # Charset switches - THIS is what makes lower/upper toggle work
        if b == 0x0E:
            charset = 'lower'
            continue
        if b == 0x8E:
            charset = 'upper'
            continue

        # CR / LF
        if b in (0x0D, 0x8D):
            newline()
            continue

        # HOME
        if b == 0x13:
            cx = 0; cy = 0
            continue

        # CLEAR - in a normal C64 session, $93 wipes the screen. But when
        # *viewing* a .seq file as a static document, we preserve the
        # already-rendered content and insert a visual marker showing where
        # the screen clear happened, so the user can see both parts of the
        # file (the header config data before $93 and the actual screen
        # after $93).
        if b == 0x93:
            has_content = any(cell['byte'] != 0x20
                              for row in grid for cell in row)
            if has_content:
                # Insert marker row: "---[ CLEAR SCREEN ]---" with up arrow
                # to visually separate the pre-CLS config from post-CLS art.
                # Use byte 0x5E ('^') rendered as up-arrow in PETSCII font.
                ensure_row(len(grid))
                marker_row = grid[-1]
                marker_text = " ^ CLEAR SCREEN ^ ".center(width, '-')
                for i, ch in enumerate(marker_text[:width]):
                    marker_row[i] = {
                        "byte": ord(ch),
                        "sc": ord(ch),
                        "fg": C64_COLORS_HEX[1],  # white
                        "bg": C64_COLORS_HEX[screen_bg_idx],
                        "reverse": False,
                        "charset": charset,
                    }
                # Move cursor to new empty row below the marker
                ensure_row(len(grid))
                cy = len(grid) - 1
                cx = 0
            else:
                # Grid empty - just reset cursor to home
                cx = 0; cy = 0
            continue

        # Cursor moves
        if b == 0x11:
            cy += 1; ensure_row(cy); clamp_cy(); continue
        if b == 0x91:
            cy = max(0, cy - 1); continue
        if b == 0x1D:
            cx += 1
            if cx >= width:
                newline()
            continue
        if b == 0x9D:
            cx = max(0, cx - 1); continue

        # DEL
        if b == 0x14:
            if cx > 0:
                cx -= 1
                if 0 <= cy < len(grid):
                    grid[cy][cx] = new_cell()
            continue
        # INS
        if b == 0x94:
            if 0 <= cy < len(grid):
                line = grid[cy]
                for x in range(width - 1, cx, -1):
                    line[x] = line[x - 1]
                line[cx] = new_cell()
            continue

        # RVS
        if b == 0x12:
            reverse = True; continue
        if b == 0x92:
            reverse = False; continue

        # Colors (foreground)
        if is_petscii_color(b):
            cur_fg_idx = PETSCII_COLOR_CODES[b]
            continue

        # Skip other control bytes
        if b < 0x20:
            continue
        if 0x80 <= b < 0xA0:
            continue

        # Printable: 0x20-0x7F and 0xA0-0xFF
        if (0x20 <= b <= 0x7F) or (b >= 0xA0):
            sc = petscii_to_screencode(b)
            ensure_row(cy)
            clamp_cy()
            if cx < width and 0 <= cy < len(grid):
                grid[cy][cx] = {
                    "byte": b,
                    "sc": sc,
                    "fg": C64_COLORS_HEX[cur_fg_idx],
                    "bg": C64_COLORS_HEX[screen_bg_idx],
                    "reverse": reverse,
                    "charset": charset,
                }
            cx += 1
            if cx >= width:
                newline()

    return {
        "grid": grid,
        "width": width,
        "height": len(grid),
        "screen_bg": C64_COLORS_HEX[screen_bg_idx],
    }
