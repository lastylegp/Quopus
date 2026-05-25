"""ASCII <-> PETSCII text file conversion.

Based on the PETSCII table from https://www.c64-wiki.de/wiki/PETSCII-Tabelle

Core rules:
  - ASCII A-Z (0x41-0x5A)   <->   PETSCII a-z  (0x41-0x5A are LOWERCASE in PETSCII)
  - ASCII a-z (0x61-0x7A)   <->   PETSCII A-Z  (0x61-0x7A are GRAPHICS/UPPERCASE in PETSCII)
  - Digits, punctuation, space: 0x20-0x3F identical in both encodings
  - [ \\ ] are the same (0x5B-0x5D)
  - Newlines: ASCII \\n or \\r\\n -> PETSCII 0x0D (CR); PETSCII 0x0D -> ASCII \\n
  - Graphics characters (0x80-0xFF in PETSCII, non-ASCII range): SKIPPED
  - Color/control codes ($05, $1C-$1F, $81, $90, $95-$9F, etc.): SKIPPED
  - Cursor control codes ($11, $1D, $91, $9D, etc.): SKIPPED

Direction:
  - 'lower' mode: ASCII upper + lower both mapped sensibly. Matches
    typical BBS/SEQ files where both cases are used.
  - 'upper' mode: Only uppercase PETSCII chars (no case swap), fits
    stock C64 boot charset.
"""
from __future__ import annotations


# PETSCII control codes we strip when converting PETSCII -> ASCII
# These are things like cursor moves, color changes, RVS on/off, CLR screen.
# (Specific codes taken from the C64-Wiki PETSCII table.)
_PETSCII_CONTROL_CODES = set()
# 0x00-0x1F: mostly control codes. Keep only 0x0D (CR), 0x0A (LF).
for c in range(0x00, 0x20):
    if c not in (0x0A, 0x0D):
        _PETSCII_CONTROL_CODES.add(c)
# 0x80-0x9F: function key codes, color codes, cursor codes, CLR, INST, etc.
for c in range(0x80, 0xA0):
    _PETSCII_CONTROL_CODES.add(c)

# Graphics characters in PETSCII (text-file conversion skips these)
_PETSCII_GRAPHICS = set()
for c in range(0x60, 0x80):          # $60-$7F: graphics in PETSCII (conflict w/ ASCII lowercase)
    _PETSCII_GRAPHICS.add(c)
for c in range(0xA0, 0x100):         # $A0-$FF: graphics + shifted chars
    _PETSCII_GRAPHICS.add(c)


def ascii_to_petscii(data: bytes, mode: str = 'mixed') -> bytes:
    """
    Convert ASCII/Latin-1 bytes to PETSCII.

    mode='mixed': case-swap (ASCII upper ↔ PETSCII lower, ASCII lower ↔ PETSCII upper)
                  → works with C64 in lower/upper ('print chr$(14)') mode
    mode='upper': ASCII upper-only; lowercase ASCII gets converted to
                  PETSCII uppercase ($41-$5A)
                  → works with stock C64 uppercase/graphics mode
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        # CR/LF handling: normalize to PETSCII CR ($0D)
        if b == 0x0D:  # CR
            out.append(0x0D)
            # Swallow following LF to avoid doubling newlines
            if i + 1 < n and data[i + 1] == 0x0A:
                i += 1
        elif b == 0x0A:  # bare LF
            out.append(0x0D)
        elif 0x20 <= b <= 0x40:
            # space, punctuation, digits, @ - identical in both encodings
            out.append(b)
        elif 0x41 <= b <= 0x5A:
            # ASCII 'A'-'Z' (uppercase)
            if mode == 'mixed':
                # → PETSCII shifted range $C1-$DA (appears uppercase in mixed mode)
                out.append(b - 0x41 + 0xC1)
            else:  # 'upper'
                out.append(b)   # stays $41-$5A in upper-only C64 mode
        elif 0x5B <= b <= 0x5F:
            # [ \ ] ^ _  -- identical
            out.append(b)
        elif 0x61 <= b <= 0x7A:
            # ASCII 'a'-'z' (lowercase)
            if mode == 'mixed':
                # → PETSCII $41-$5A (displays as lowercase in mixed mode)
                out.append(b - 0x61 + 0x41)
            else:
                # 'upper' mode: C64 has no lower case; map to PETSCII uppercase
                out.append(b - 0x61 + 0x41)
        elif b == 0x09:
            # Tab -> space (PETSCII has no real tab in text context)
            out.append(0x20)
        else:
            # Unknown / graphics / extended range: skip silently
            pass
        i += 1
    return bytes(out)


def petscii_to_ascii(data: bytes, mode: str = 'mixed') -> bytes:
    """
    Convert PETSCII bytes to ASCII, ignoring graphics and control codes.

    mode='mixed':        $41-$5A → lowercase, $C1-$DA → uppercase
    mode='upper':        $41-$5A → UPPERCASE, $60-$7F reverse → UPPERCASE
    mode='hybrid':       $41-$5A → UPPERCASE, $60-$7F reverse → lowercase
    mode='hybrid-smart': same as 'hybrid', plus post-processing to turn
                         shadow-style patterns ('sUBS', 'hOT') into normal
                         capitalization ('Subs', 'Hot'). Useful for BBS
                         screens with 3D-letter effects.
    """
    raw_mode = 'hybrid' if mode == 'hybrid-smart' else mode
    out = _petscii_to_ascii_raw(data, raw_mode)
    if mode == 'hybrid-smart':
        out = _smart_recase(out)
    return out


def _petscii_to_ascii_raw(data: bytes, mode: str) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x0D:   # CR → LF for unix-like text
            out.append(0x0A)
        elif b == 0x0A: # bare LF passthrough
            out.append(0x0A)
        elif b in _PETSCII_CONTROL_CODES:
            # skip cursor moves, color changes, function keys, CLR, RVS on/off ...
            continue
        elif 0x20 <= b <= 0x40:
            # digits, punctuation, space, @ - identical
            out.append(b)
        elif 0x41 <= b <= 0x5A:
            # $41-$5A: mixed = lowercase; upper/hybrid = uppercase
            if mode == 'mixed':
                out.append(b + 0x20)
            else:
                out.append(b)
        elif 0x5B <= b <= 0x5F:
            # [ \ ] ^ _  - identical
            out.append(b)
        elif 0x60 <= b <= 0x7F:
            # Reverse-video or graphics depending on mode:
            # - upper:  map to uppercase letters (same text, just inverted)
            # - hybrid: map to LOWERCASE letters (differentiate from plain UC)
            # - mixed:  skip as graphics
            if mode == 'upper':
                mapped = b - 0x20
                if 0x41 <= mapped <= 0x5F:
                    out.append(mapped)
            elif mode == 'hybrid':
                mapped = b - 0x20
                if 0x41 <= mapped <= 0x5A:
                    out.append(mapped + 0x20)   # to lowercase
                elif 0x40 == mapped or 0x5B <= mapped <= 0x5F:
                    out.append(mapped)
            # mixed: skip
        elif 0xC1 <= b <= 0xDA:
            # PETSCII shifted uppercase letters
            if mode == 'mixed':
                out.append(b - 0xC1 + 0x41)
            else:
                continue
        elif b in _PETSCII_GRAPHICS:
            continue
        else:
            continue
    return bytes(out)


def _smart_recase(data: bytes) -> bytes:
    """
    Post-process hybrid output: detect shadow/3D-letter patterns and
    restore natural word capitalization.

    Pattern recognized: a word that starts with a single lowercase letter
    followed by one or more uppercase letters (like 'sUBS' or 'hOT').
    These come from BBS-screens where the first letter is drawn in reverse
    video as a shadow over the main uppercase glyph.

    Rewrites 'sUBS' → 'Subs', 'hOT' → 'Hot', 'tOP' → 'Top'.
    Leaves 'all-UPPERCASE' words alone, leaves 'all-lowercase' words alone,
    leaves normally-capitalized words alone.
    """
    import re
    s = data.decode('latin-1', errors='replace')

    def fix(m):
        word = m.group(0)
        first = word[0]
        rest  = word[1:]
        # only apply if first is lowercase and rest is all uppercase
        if first.islower() and rest.isupper() and len(rest) >= 1:
            return first.upper() + rest.lower()
        return word

    # A "word" here: one lowercase letter followed by >=1 uppercase letter,
    # bounded by non-letter characters.
    s = re.sub(r'\b[a-z][A-Z]+\b', fix, s)
    return s.encode('latin-1', errors='replace')


def detect_encoding(data: bytes) -> str:
    """
    Heuristic: return 'petscii' or 'ascii' based on byte statistics.
    Used to pre-select the 'direction' in the converter UI.
    """
    if not data:
        return 'ascii'
    sample = data[:4096]
    lowercase_ascii = sum(1 for b in sample if 0x61 <= b <= 0x7A)
    petscii_shifted = sum(1 for b in sample if 0xC1 <= b <= 0xDA)
    petscii_graphics = sum(1 for b in sample if 0xA0 <= b <= 0xBF or 0xE0 <= b <= 0xFF)
    cr_count = sum(1 for b in sample if b == 0x0D)
    lf_count = sum(1 for b in sample if b == 0x0A)
    # Lots of high bytes → probably PETSCII
    if petscii_shifted + petscii_graphics > len(sample) * 0.05:
        return 'petscii'
    # CR without LF → probably PETSCII (C64 uses CR only)
    if cr_count > 0 and lf_count == 0:
        return 'petscii'
    # Mostly printable low-ASCII → ascii
    return 'ascii'


def detect_charset_mode(data: bytes) -> str:
    """
    For a PETSCII file, guess whether it was written in 'mixed'
    (lower/upper toggle, via chr$(14)) or 'upper' (graphics/uppercase,
    the default C64 power-on charset) mode.

    Rule of thumb:
      - 'mixed' files use $C1-$DA for uppercase letters and $41-$5A for lowercase.
      - 'upper' files use $41-$5A for uppercase and have NO lowercase letters
        (they substitute graphics in the $60-$7F range for small shapes).

    So: if shifted-uppercase bytes ($C1-$DA) are rare but $41-$5A bytes are
    common, the file is in 'upper' mode.
    """
    if not data:
        return 'mixed'
    sample = data[:8192]
    shifted  = sum(1 for b in sample if 0xC1 <= b <= 0xDA)   # PETSCII uppercase in mixed
    unshifted = sum(1 for b in sample if 0x41 <= b <= 0x5A)  # could be either
    # If unshifted letters dominate and shifted are <=10% of unshifted → upper mode
    if unshifted > 20 and shifted < unshifted * 0.1:
        return 'upper'
    return 'mixed'
