"""
Exact PETSCII tables from Mario's petscii_charset.py and petscii_screencode.py.
Embedded here to keep quopus_lib self-contained.
"""

# C64 palette as hex strings (for Qt)
C64_COLORS_HEX = [
    "#000000",  # 0 Black
    "#ffffff",  # 1 White
    "#880000",  # 2 Red
    "#aaffee",  # 3 Cyan
    "#cc44cc",  # 4 Purple
    "#00cc55",  # 5 Green
    "#0000aa",  # 6 Blue
    "#eeee77",  # 7 Yellow
    "#dd8855",  # 8 Orange
    "#664400",  # 9 Brown
    "#ff7777",  # 10 Light Red
    "#333333",  # 11 Dark Grey
    "#777777",  # 12 Grey
    "#aaff66",  # 13 Light Green
    "#0088ff",  # 14 Light Blue
    "#bbbbbb",  # 15 Light Grey
]


# PETSCII color codes byte -> palette index
PETSCII_COLOR_CODES = {
    0x05: 1,   # WHITE
    0x1C: 2,   # RED
    0x1E: 5,   # GREEN
    0x1F: 6,   # BLUE
    0x81: 8,   # ORANGE
    0x90: 0,   # BLACK
    0x95: 9,   # BROWN
    0x96: 10,  # LT_RED
    0x97: 11,  # GREY1
    0x98: 12,  # GREY2
    0x99: 13,  # LT_GREEN
    0x9A: 14,  # LT_BLUE
    0x9B: 15,  # GREY3
    0x9C: 4,   # PURPLE
    0x9E: 7,   # YELLOW
    0x9F: 3,   # CYAN
}


def is_petscii_color(b):
    return b in PETSCII_COLOR_CODES


# ============================================================
# CGTerm-exact PETSCII -> SCREENCODE conversion
# From Mario's petscii_screencode.py (CGTerm kernal.c line 10-18)
# ============================================================
_SCCONV = [
    128,   # 0: 0x00-0x1F
    0,     # 1: 0x20-0x3F
    -64,   # 2: 0x40-0x5F
    -32,   # 3: 0x60-0x7F
    64,    # 4: 0x80-0x9F
    -64,   # 5: 0xA0-0xBF
    -128,  # 6: 0xC0-0xDF
    -128,  # 7: 0xE0-0xFF
]

_SCREENCODE_TABLE = []
for _c in range(256):
    _sc = (_c + _SCCONV[_c // 32]) & 0xFF
    _SCREENCODE_TABLE.append(_sc)
_SCREENCODE_TABLE[255] = 94   # special case from CGTerm


def petscii_to_screencode(petscii_byte):
    """Convert PETSCII byte to C64 screencode (CGTerm exact)."""
    return _SCREENCODE_TABLE[petscii_byte & 0xFF]


# ============================================================
# PETSCII -> Unicode mapping for FALLBACK rendering
# (when C64 Pro Mono is NOT available)
# ============================================================

# UPPER/GRAPHICS charset
PETSCII_TO_UNICODE_UPPER = {
    0x20: ' ', 0x21: '!', 0x22: '"', 0x23: '#', 0x24: '$',
    0x25: '%', 0x26: '&', 0x27: "'", 0x28: '(', 0x29: ')',
    0x2A: '*', 0x2B: '+', 0x2C: ',', 0x2D: '-', 0x2E: '.',
    0x2F: '/',
    0x30: '0', 0x31: '1', 0x32: '2', 0x33: '3', 0x34: '4',
    0x35: '5', 0x36: '6', 0x37: '7', 0x38: '8', 0x39: '9',
    0x3A: ':', 0x3B: ';', 0x3C: '<', 0x3D: '=', 0x3E: '>',
    0x3F: '?', 0x40: '@',
    # UPPERCASE A-Z in upper mode
    0x41: 'A', 0x42: 'B', 0x43: 'C', 0x44: 'D', 0x45: 'E',
    0x46: 'F', 0x47: 'G', 0x48: 'H', 0x49: 'I', 0x4A: 'J',
    0x4B: 'K', 0x4C: 'L', 0x4D: 'M', 0x4E: 'N', 0x4F: 'O',
    0x50: 'P', 0x51: 'Q', 0x52: 'R', 0x53: 'S', 0x54: 'T',
    0x55: 'U', 0x56: 'V', 0x57: 'W', 0x58: 'X', 0x59: 'Y',
    0x5A: 'Z',
    0x5B: '[', 0x5C: '£', 0x5D: ']', 0x5E: '↑', 0x5F: '←',
    # Graphics 0x60-0x7F
    0x60: '─', 0x61: '♠', 0x62: '│', 0x63: '─',
    0x64: '─', 0x65: '─', 0x66: '─', 0x67: '│',
    0x68: '│', 0x69: '╮', 0x6A: '╰', 0x6B: '╯',
    0x6C: '└', 0x6D: '╲', 0x6E: '╱', 0x6F: '└',
    0x70: '└', 0x71: '●', 0x72: '─', 0x73: '♥',
    0x74: '│', 0x75: '╭', 0x76: '╳', 0x77: '○',
    0x78: '♣', 0x79: '│', 0x7A: '♦', 0x7B: '┼',
    0x7C: '▒', 0x7D: '│', 0x7E: 'π', 0x7F: '◥',
    # Graphics 0xA0-0xFF
    0xA0: ' ', 0xA1: '▌', 0xA2: '▄', 0xA3: '▔',
    0xA4: '▁', 0xA5: '▏', 0xA6: '▒', 0xA7: '▕',
    0xA8: '▓', 0xA9: '◤', 0xAA: '▒', 0xAB: '├',
    0xAC: '▗', 0xAD: '└', 0xAE: '┐', 0xAF: '▂',
    0xB0: '┌', 0xB1: '┴', 0xB2: '┬', 0xB3: '┤',
    0xB4: '▎', 0xB5: '▍', 0xB6: '▕', 0xB7: '▔',
    0xB8: '▔', 0xB9: '▃', 0xBA: '✓', 0xBB: '▖',
    0xBC: '▝', 0xBD: '┘', 0xBE: '▘', 0xBF: '▚',
    # Shifted uppercase mirror (0xC1-0xDA = shifted graphics in upper mode)
    0xC0: '━',
    0xDB: '█', 0xDC: '▎', 0xDD: '▐', 0xDE: '▀', 0xDF: '▄',
    0xE0: '░',
}

# LOWER/UPPER charset (BBS default)
# Lowercase letters take 0x41-0x5A, shifted chars 0xC1-0xDA are uppercase
PETSCII_TO_UNICODE_LOWER = dict(PETSCII_TO_UNICODE_UPPER)
PETSCII_TO_UNICODE_LOWER.update({
    0x41: 'a', 0x42: 'b', 0x43: 'c', 0x44: 'd', 0x45: 'e',
    0x46: 'f', 0x47: 'g', 0x48: 'h', 0x49: 'i', 0x4A: 'j',
    0x4B: 'k', 0x4C: 'l', 0x4D: 'm', 0x4E: 'n', 0x4F: 'o',
    0x50: 'p', 0x51: 'q', 0x52: 'r', 0x53: 's', 0x54: 't',
    0x55: 'u', 0x56: 'v', 0x57: 'w', 0x58: 'x', 0x59: 'y',
    0x5A: 'z',
    0xC1: 'A', 0xC2: 'B', 0xC3: 'C', 0xC4: 'D', 0xC5: 'E',
    0xC6: 'F', 0xC7: 'G', 0xC8: 'H', 0xC9: 'I', 0xCA: 'J',
    0xCB: 'K', 0xCC: 'L', 0xCD: 'M', 0xCE: 'N', 0xCF: 'O',
    0xD0: 'P', 0xD1: 'Q', 0xD2: 'R', 0xD3: 'S', 0xD4: 'T',
    0xD5: 'U', 0xD6: 'V', 0xD7: 'W', 0xD8: 'X', 0xD9: 'Y',
    0xDA: 'Z',
})


def petscii_byte_to_unicode(b, charset='lower'):
    """Fallback mapping when C64 Pro Mono is not available."""
    table = PETSCII_TO_UNICODE_LOWER if charset == 'lower' else PETSCII_TO_UNICODE_UPPER
    return table.get(b, '?')
