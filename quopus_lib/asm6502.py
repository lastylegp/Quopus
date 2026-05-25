"""Mini 6502/6510 assembler for FindDialog's Assembly search mode.

Converts source lines like:
    lda #$00
    sta $d021
    rts

into a byte pattern that can be searched in raw binary files.

Wildcards in operands let you match any value:
    lda #?         -> A9 ??       (any immediate value)
    sta $??        -> 85 ??       (any zero-page address)
    sta $??d?      -> 8D ?D ??    (last operand-byte known, others wild)
    jsr $????      -> 20 ?? ??    (any 16-bit absolute target)
    jmp ($????)    -> 6C ?? ??    (any indirect target)

Returns a list of either int (0-255) or None (wildcard) per byte.
The FindDialog converts that into a wildcard-aware byte search.

Why we don't just call into c64_disasm's existing infrastructure:
that module is built for top-down DISASSEMBLY of whole programs,
not bottom-up assembly of individual lines. Building the inverse
table (mnemonic+mode -> opcode) is one line; reusing the disasm
machinery would be far more complex.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------
# Reverse opcode table (mnemonic, mode) -> opcode byte
# Built lazily on first use from c64_disasm.OPCODES.
# ---------------------------------------------------------------------
_REVERSE: dict[tuple[str, str], int] | None = None


def _reverse_table():
    global _REVERSE
    if _REVERSE is not None:
        return _REVERSE
    from .c64_disasm import OPCODES
    rev: dict[tuple[str, str], int] = {}
    for opc, (mnem, mode, _sz) in OPCODES.items():
        rev[(mnem.upper(), mode)] = opc
    _REVERSE = rev
    return rev


# Operand-size lookup per addressing mode (in bytes, NOT including
# the opcode byte itself).
_MODE_SIZE = {
    "imp": 0, "acc": 0,
    "imm": 1, "zp": 1, "zpx": 1, "zpy": 1,
    "izx": 1, "izy": 1, "rel": 1,
    "abs": 2, "abx": 2, "aby": 2, "ind": 2,
}


# ---------------------------------------------------------------------
# Operand parsing
# ---------------------------------------------------------------------
class AsmError(ValueError):
    """Raised on malformed assembly input. Carries a line number
    (1-based) when known."""
    def __init__(self, msg, line=None):
        super().__init__(msg)
        self.line = line


# Match a number with optional wildcards.
# Examples: "00", "FF", "1234", "??", "1?", "?A"
# We support hex digits and '?' as wildcard nibbles.
_HEX_WILD = re.compile(r'^[0-9A-Fa-f?]+$')


def _parse_number(token: str, expect_bytes: int):
    """Parse a hex number token into a list of (byte_value | None)
    of length `expect_bytes`. The token is the part after `$` or `#$`.

    expect_bytes:
        1 = parse as 8-bit (1 or 2 hex chars)
        2 = parse as 16-bit (3 or 4 hex chars)

    Wildcards: '?' replaces any nibble, making that byte position
    None in the output. If ALL nibbles in a byte are '?', the whole
    byte is a wildcard. If only one nibble is '?', the byte is still
    a wildcard (we can't represent half-byte matches).

    Returns: list of ints (0..255) or None values, in little-endian
    order (the order they appear in the assembled output)."""
    if not token or not _HEX_WILD.match(token):
        raise AsmError(f"Invalid hex/wildcard: {token!r}")
    n_chars = len(token)
    if expect_bytes == 1:
        if n_chars not in (1, 2):
            raise AsmError(f"Expected 8-bit value, got {token!r}")
        # Pad single-char to 2 chars on the left
        s = token.zfill(2)
        if '?' in s:
            return [None]
        return [int(s, 16)]
    elif expect_bytes == 2:
        if n_chars not in (1, 2, 3, 4):
            raise AsmError(f"Expected 16-bit value, got {token!r}")
        s = token.zfill(4)
        # Split into high+low; emit little-endian (low first)
        hi_s, lo_s = s[0:2], s[2:4]
        lo = None if '?' in lo_s else int(lo_s, 16)
        hi = None if '?' in hi_s else int(hi_s, 16)
        return [lo, hi]
    else:
        raise AsmError(f"Unsupported byte count: {expect_bytes}")


# Pure wildcard: '?' alone or '$?' '$??' '$????' etc.
_PURE_WILD_RE = re.compile(r'^\??\$?\?+$')


def _parse_operand(operand: str):
    """Parse the operand part of an instruction line and return
    (mode, [byte_values]) where byte_values can include None for
    wildcards. mode is the c64_disasm-style addressing-mode string.

    Accepted forms:
        (empty)        -> "imp"  (no bytes)
        "A"            -> "acc"  (no bytes)
        "#$xx"         -> "imm"  [xx]
        "#?"           -> "imm"  [None]
        "$xx"          -> "zp"   [xx]              (1-byte hex -> ZP)
        "$xxxx"        -> "abs"  [lo, hi]          (2-byte hex -> abs)
        "$xx,X"        -> "zpx"  [xx]
        "$xxxx,X"      -> "abx"  [lo, hi]
        "$xx,Y"        -> "zpy"  [xx]
        "$xxxx,Y"      -> "aby"  [lo, hi]
        "($xxxx)"      -> "ind"  [lo, hi]
        "($xx,X)"      -> "izx"  [xx]
        "($xx),Y"      -> "izy"  [xx]
    Wildcards: '?' as the value, e.g. "lda #?", "sta $????", "lda $?d??"

    The branch instructions (BNE, BEQ, etc.) use mode 'rel' but the
    parser here will see them as 'zp' (one-byte). Caller is
    responsible for distinguishing - if the mnemonic is a branch and
    we got 'zp' or 'abs', we promote to 'rel' with a wildcard byte
    (we can't compute the branch offset without knowing the source
    address; users can wildcard it explicitly with $?? if they want)."""
    op = operand.strip()
    if not op:
        return "imp", []
    if op.upper() == "A":
        return "acc", []

    # Immediate
    if op.startswith('#'):
        rest = op[1:].lstrip()
        if rest.startswith('$'):
            rest = rest[1:]
        return "imm", _parse_number(rest, 1)

    # Indexed-indirect (zp,X) / indirect-indexed (zp),Y
    if op.startswith('('):
        # Strip the outer parens; we'll look for matching pattern
        if op.endswith(',Y') or op.endswith(',y'):
            # ($xx),Y -> izy
            inner = op[1:-2].rstrip()    # everything before ),Y
            if not inner.endswith(')'):
                raise AsmError(f"Malformed indirect-Y: {op!r}")
            inner = inner[:-1]            # drop ')'
            if inner.startswith('$'):
                inner = inner[1:]
            return "izy", _parse_number(inner, 1)
        elif op.endswith(')'):
            # ($xx,X) -> izx, or ($xxxx) -> ind
            inner = op[1:-1]
            if inner.upper().endswith(',X'):
                inner = inner[:-2].rstrip()
                if inner.startswith('$'):
                    inner = inner[1:]
                return "izx", _parse_number(inner, 1)
            else:
                if inner.startswith('$'):
                    inner = inner[1:]
                return "ind", _parse_number(inner, 2)
        else:
            raise AsmError(f"Malformed indirect operand: {op!r}")

    # Indexed addressing (,X / ,Y suffixes)
    idx = None
    if op.upper().endswith(',X'):
        op = op[:-2].rstrip(); idx = 'X'
    elif op.upper().endswith(',Y'):
        op = op[:-2].rstrip(); idx = 'Y'

    # Now should be a $-prefixed hex number (or wildcard)
    if not op.startswith('$'):
        raise AsmError(f"Expected hex operand starting with $: {op!r}")
    body = op[1:]
    if not _HEX_WILD.match(body):
        raise AsmError(f"Invalid operand body: {body!r}")

    n = len(body)
    # 1-2 hex chars -> ZP-class; 3-4 hex chars -> absolute-class
    if n <= 2:
        if idx == 'X': return "zpx", _parse_number(body, 1)
        if idx == 'Y': return "zpy", _parse_number(body, 1)
        return "zp", _parse_number(body, 1)
    elif n <= 4:
        if idx == 'X': return "abx", _parse_number(body, 2)
        if idx == 'Y': return "aby", _parse_number(body, 2)
        return "abs", _parse_number(body, 2)
    else:
        raise AsmError(f"Operand too long: {body!r}")


# Branch mnemonics use 'rel' addressing - signed byte offset.
_BRANCHES = {"BCC", "BCS", "BEQ", "BNE", "BMI", "BPL", "BVC", "BVS"}


# ---------------------------------------------------------------------
# Line tokenisation
# ---------------------------------------------------------------------
def _strip_comment(line: str) -> str:
    """Remove comments (after ; or //) but preserve $ literals."""
    # Find first ';' or '//' that isn't inside parens
    i = 0; in_paren = 0
    while i < len(line):
        c = line[i]
        if c == '(':
            in_paren += 1
        elif c == ')' and in_paren > 0:
            in_paren -= 1
        elif c == ';' and in_paren == 0:
            return line[:i]
        elif c == '/' and i + 1 < len(line) and line[i+1] == '/' \
                and in_paren == 0:
            return line[:i]
        i += 1
    return line


_LINE_RE = re.compile(
    r'^\s*([A-Za-z]{3})\b\s*(.*?)\s*$')


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
def assemble_pattern(source: str) -> list:
    """Translate multi-line 6502 assembly source into a byte pattern.

    Returns a list whose elements are either int (0-255) for known
    bytes or None for wildcards. Suitable for passing to a
    wildcard-aware substring search.

    Raises AsmError on syntax or unknown-mnemonic problems.
    Empty lines and lines containing only a comment are skipped.

    Example:
        assemble_pattern('''
            lda #$00
            sta $d021
        ''')
        -> [0xA9, 0x00, 0x8D, 0x21, 0xD0]

    Example with wildcards:
        assemble_pattern('lda #?\\nsta $d021')
        -> [0xA9, None, 0x8D, 0x21, 0xD0]
    """
    rev = _reverse_table()
    out: list[Optional[int]] = []
    for line_no, raw in enumerate(source.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            raise AsmError(
                f"Line {line_no}: can't parse: {raw!r}", line=line_no)
        mnem = m.group(1).upper()
        operand = m.group(2).strip()
        try:
            mode, operand_bytes = _parse_operand(operand)
        except AsmError as e:
            e.line = line_no
            raise

        # Branch promotion: if the mnemonic is a branch and we got
        # zp-class, switch to rel mode (the operand is a signed
        # offset, but the user typically can't predict it - they'll
        # wildcard it).
        if mnem in _BRANCHES and mode in ("zp", "abs"):
            # Promote to rel; collapse 16-bit to a single wildcard byte
            if mode == "abs":
                operand_bytes = [None]
            mode = "rel"

        # acc form has no operand_bytes; some old syntaxes allow
        # `lsr` / `asl` / `rol` / `ror` without 'A' to mean acc,
        # so if mode is 'imp' but the mnemonic only exists in 'acc'
        # form, promote.
        if (mnem, mode) not in rev and mode == "imp" \
                and (mnem, "acc") in rev:
            mode = "acc"

        opc = rev.get((mnem, mode))
        if opc is None:
            raise AsmError(
                f"Line {line_no}: unknown instruction or addressing "
                f"mode: {mnem} {operand} (mode={mode})", line=line_no)

        out.append(opc)
        out.extend(operand_bytes)
    if not out:
        raise AsmError("No instructions assembled (empty input?)")
    return out


def format_pattern_hex(pattern: list) -> str:
    """Render a byte+wildcard pattern as a human-readable hex string,
    e.g. [0xA9, None, 0x8D, 0x21, 0xD0] -> 'A9 ?? 8D 21 D0'.
    Used for display in the FindDialog status line."""
    return ' '.join('??' if b is None else f'{b:02X}' for b in pattern)


def search_pattern_in_bytes(pattern: list, data: bytes) -> bool:
    """Return True if the wildcard pattern occurs in data.

    Pure-byte patterns (no None) take a fast path via bytes.find.
    Mixed patterns use a linear scan with per-byte comparison."""
    if not pattern:
        return False
    n = len(data)
    m = len(pattern)
    if m > n:
        return False
    # Fast path - no wildcards
    if all(b is not None for b in pattern):
        return bytes(pattern) in data
    # Wildcard path - try every starting position. Use the first
    # known byte as an anchor to skip impossible positions.
    anchor_idx = next((i for i, b in enumerate(pattern)
                          if b is not None), 0)
    anchor = pattern[anchor_idx] if anchor_idx < m else None
    if anchor is None:
        # All wildcards - degenerate case; matches any data of size m
        return n >= m
    pos = 0
    while pos <= n - m:
        if data[pos + anchor_idx] != anchor:
            pos += 1
            continue
        ok = True
        for j in range(m):
            b = pattern[j]
            if b is None:
                continue
            if data[pos + j] != b:
                ok = False
                break
        if ok:
            return True
        pos += 1
    return False
