# date_time: 2026-06-05 19:59
"""
6502 disassembler with a dual-pane navigation viewer for C64 .prg / .bin files.

Layout:
  +----------------+----------------+
  | LEFT pane      | RIGHT pane     |
  | (primary)      | (preview)      |
  | follow-target  | jump-target    |
  +----------------+----------------+

Mouse model:
  Single-click on a branch/jmp/jsr operand in LEFT  -> RIGHT pane scrolls to target
  Double-click on a branch/jmp/jsr operand in LEFT  -> LEFT pane jumps to target
                                                       (current address pushed on history)
  Right double-click anywhere in RIGHT or LEFT      -> pop history (back)

The disassembler uses a simple linear scan from the load address. All 6502
documented opcodes are covered; illegal opcodes show as `.byte $XX`.
"""
from pathlib import Path
import re
import sys as _sys_module

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import (
    QFont, QTextCursor, QShortcut, QKeySequence, QTextCharFormat, QColor,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QTextEdit, QSplitter, QMessageBox,
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS, SCROLLBAR_QSS,
    button_qss, get_topaz_font,
)


def _safe_stderr_write(msg: str) -> None:
    """Write to stderr if it exists, no-op otherwise.

    PyInstaller --windowed builds (no console window) set
    sys.stderr to None, which makes every sys.stderr.write(...)
    raise 'NoneType has no attribute write'. This wrapper is the
    drop-in replacement: all module-internal logging goes through
    it so the frozen Quopus EXE doesn't crash when the
    disassembler tries to log a cache miss or an asm error.
    """
    try:
        s = _sys_module.stderr
        if s is not None:
            s.write(msg)
            try:
                s.flush()
            except Exception:
                pass
    except Exception:
        # Don't let logging blow up the caller.
        pass


def _safe_print_exc() -> None:
    """traceback.print_exc(file=sys.stderr) equivalent that's
    safe in frozen --windowed builds."""
    try:
        s = _sys_module.stderr
        if s is None:
            return
        import traceback as _tb
        _tb.print_exc(file=s)
    except Exception:
        pass


# =====================================================================
# 6502 Opcode Table
# =====================================================================
# Each entry: (mnemonic, addressing_mode, operand_size_in_bytes)
# Addressing modes:
#   imp = implied (no operand)
#   imm = immediate (#$nn)
#   zp  = zero page ($nn)
#   zpx = zero page,X
#   zpy = zero page,Y
#   abs = absolute ($nnnn)
#   abx = absolute,X
#   aby = absolute,Y
#   ind = indirect ($nnnn)
#   izx = (zp,X)
#   izy = (zp),Y
#   rel = relative branch (signed byte)
#   acc = accumulator (no operand bytes but written as "A")
OPCODES = {
    0x69: ("ADC", "imm", 1), 0x65: ("ADC", "zp",  1), 0x75: ("ADC", "zpx", 1),
    0x6D: ("ADC", "abs", 2), 0x7D: ("ADC", "abx", 2), 0x79: ("ADC", "aby", 2),
    0x61: ("ADC", "izx", 1), 0x71: ("ADC", "izy", 1),

    0x29: ("AND", "imm", 1), 0x25: ("AND", "zp",  1), 0x35: ("AND", "zpx", 1),
    0x2D: ("AND", "abs", 2), 0x3D: ("AND", "abx", 2), 0x39: ("AND", "aby", 2),
    0x21: ("AND", "izx", 1), 0x31: ("AND", "izy", 1),

    0x0A: ("ASL", "acc", 0), 0x06: ("ASL", "zp", 1), 0x16: ("ASL", "zpx", 1),
    0x0E: ("ASL", "abs", 2), 0x1E: ("ASL", "abx", 2),

    0x90: ("BCC", "rel", 1), 0xB0: ("BCS", "rel", 1),
    0xF0: ("BEQ", "rel", 1), 0x30: ("BMI", "rel", 1),
    0xD0: ("BNE", "rel", 1), 0x10: ("BPL", "rel", 1),
    0x50: ("BVC", "rel", 1), 0x70: ("BVS", "rel", 1),

    0x24: ("BIT", "zp", 1),  0x2C: ("BIT", "abs", 2),
    0x00: ("BRK", "imp", 0),

    0x18: ("CLC", "imp", 0), 0xD8: ("CLD", "imp", 0),
    0x58: ("CLI", "imp", 0), 0xB8: ("CLV", "imp", 0),

    0xC9: ("CMP", "imm", 1), 0xC5: ("CMP", "zp",  1), 0xD5: ("CMP", "zpx", 1),
    0xCD: ("CMP", "abs", 2), 0xDD: ("CMP", "abx", 2), 0xD9: ("CMP", "aby", 2),
    0xC1: ("CMP", "izx", 1), 0xD1: ("CMP", "izy", 1),

    0xE0: ("CPX", "imm", 1), 0xE4: ("CPX", "zp", 1), 0xEC: ("CPX", "abs", 2),
    0xC0: ("CPY", "imm", 1), 0xC4: ("CPY", "zp", 1), 0xCC: ("CPY", "abs", 2),

    0xC6: ("DEC", "zp", 1),  0xD6: ("DEC", "zpx", 1),
    0xCE: ("DEC", "abs", 2), 0xDE: ("DEC", "abx", 2),
    0xCA: ("DEX", "imp", 0), 0x88: ("DEY", "imp", 0),

    0x49: ("EOR", "imm", 1), 0x45: ("EOR", "zp",  1), 0x55: ("EOR", "zpx", 1),
    0x4D: ("EOR", "abs", 2), 0x5D: ("EOR", "abx", 2), 0x59: ("EOR", "aby", 2),
    0x41: ("EOR", "izx", 1), 0x51: ("EOR", "izy", 1),

    0xE6: ("INC", "zp", 1),  0xF6: ("INC", "zpx", 1),
    0xEE: ("INC", "abs", 2), 0xFE: ("INC", "abx", 2),
    0xE8: ("INX", "imp", 0), 0xC8: ("INY", "imp", 0),

    0x4C: ("JMP", "abs", 2), 0x6C: ("JMP", "ind", 2),
    0x20: ("JSR", "abs", 2),

    0xA9: ("LDA", "imm", 1), 0xA5: ("LDA", "zp",  1), 0xB5: ("LDA", "zpx", 1),
    0xAD: ("LDA", "abs", 2), 0xBD: ("LDA", "abx", 2), 0xB9: ("LDA", "aby", 2),
    0xA1: ("LDA", "izx", 1), 0xB1: ("LDA", "izy", 1),

    0xA2: ("LDX", "imm", 1), 0xA6: ("LDX", "zp",  1), 0xB6: ("LDX", "zpy", 1),
    0xAE: ("LDX", "abs", 2), 0xBE: ("LDX", "aby", 2),

    0xA0: ("LDY", "imm", 1), 0xA4: ("LDY", "zp",  1), 0xB4: ("LDY", "zpx", 1),
    0xAC: ("LDY", "abs", 2), 0xBC: ("LDY", "abx", 2),

    0x4A: ("LSR", "acc", 0), 0x46: ("LSR", "zp", 1), 0x56: ("LSR", "zpx", 1),
    0x4E: ("LSR", "abs", 2), 0x5E: ("LSR", "abx", 2),

    0xEA: ("NOP", "imp", 0),

    0x09: ("ORA", "imm", 1), 0x05: ("ORA", "zp",  1), 0x15: ("ORA", "zpx", 1),
    0x0D: ("ORA", "abs", 2), 0x1D: ("ORA", "abx", 2), 0x19: ("ORA", "aby", 2),
    0x01: ("ORA", "izx", 1), 0x11: ("ORA", "izy", 1),

    0x48: ("PHA", "imp", 0), 0x08: ("PHP", "imp", 0),
    0x68: ("PLA", "imp", 0), 0x28: ("PLP", "imp", 0),

    0x2A: ("ROL", "acc", 0), 0x26: ("ROL", "zp", 1), 0x36: ("ROL", "zpx", 1),
    0x2E: ("ROL", "abs", 2), 0x3E: ("ROL", "abx", 2),

    0x6A: ("ROR", "acc", 0), 0x66: ("ROR", "zp", 1), 0x76: ("ROR", "zpx", 1),
    0x6E: ("ROR", "abs", 2), 0x7E: ("ROR", "abx", 2),

    0x40: ("RTI", "imp", 0), 0x60: ("RTS", "imp", 0),

    0xE9: ("SBC", "imm", 1), 0xE5: ("SBC", "zp",  1), 0xF5: ("SBC", "zpx", 1),
    0xED: ("SBC", "abs", 2), 0xFD: ("SBC", "abx", 2), 0xF9: ("SBC", "aby", 2),
    0xE1: ("SBC", "izx", 1), 0xF1: ("SBC", "izy", 1),

    0x38: ("SEC", "imp", 0), 0xF8: ("SED", "imp", 0), 0x78: ("SEI", "imp", 0),

    0x85: ("STA", "zp",  1), 0x95: ("STA", "zpx", 1),
    0x8D: ("STA", "abs", 2), 0x9D: ("STA", "abx", 2), 0x99: ("STA", "aby", 2),
    0x81: ("STA", "izx", 1), 0x91: ("STA", "izy", 1),

    0x86: ("STX", "zp", 1),  0x96: ("STX", "zpy", 1), 0x8E: ("STX", "abs", 2),
    0x84: ("STY", "zp", 1),  0x94: ("STY", "zpx", 1), 0x8C: ("STY", "abs", 2),

    0xAA: ("TAX", "imp", 0), 0xA8: ("TAY", "imp", 0),
    0xBA: ("TSX", "imp", 0), 0x8A: ("TXA", "imp", 0),
    0x9A: ("TXS", "imp", 0), 0x98: ("TYA", "imp", 0),
}

# Instructions whose operand is a code address (used to make hyperlinks)
JUMP_OPS = {"JMP", "JSR"}
BRANCH_OPS = {"BCC", "BCS", "BEQ", "BMI", "BNE", "BPL", "BVC", "BVS",
               # 65C02 unconditional branch
               "BRA"}

# Illegal / undocumented 6502 opcodes. These are real CPU behaviours that
# software occasionally relies on, but most disassemblers treat them as
# data. Set show_illegal=True to use these mnemonics; otherwise they
# appear as `.byte $XX`.
ILLEGAL_OPCODES = {
    # NOPs (multi-byte)
    0x1A: ("NOP", "imp", 0), 0x3A: ("NOP", "imp", 0),
    0x5A: ("NOP", "imp", 0), 0x7A: ("NOP", "imp", 0),
    0xDA: ("NOP", "imp", 0), 0xFA: ("NOP", "imp", 0),
    0x80: ("NOP", "imm", 1), 0x82: ("NOP", "imm", 1),
    0x89: ("NOP", "imm", 1), 0xC2: ("NOP", "imm", 1),
    0xE2: ("NOP", "imm", 1),
    0x04: ("NOP", "zp",  1), 0x44: ("NOP", "zp",  1),
    0x64: ("NOP", "zp",  1),
    0x14: ("NOP", "zpx", 1), 0x34: ("NOP", "zpx", 1),
    0x54: ("NOP", "zpx", 1), 0x74: ("NOP", "zpx", 1),
    0xD4: ("NOP", "zpx", 1), 0xF4: ("NOP", "zpx", 1),
    0x0C: ("NOP", "abs", 2),
    0x1C: ("NOP", "abx", 2), 0x3C: ("NOP", "abx", 2),
    0x5C: ("NOP", "abx", 2), 0x7C: ("NOP", "abx", 2),
    0xDC: ("NOP", "abx", 2), 0xFC: ("NOP", "abx", 2),

    # LAX = LDA + LDX
    0xA7: ("LAX", "zp",  1), 0xB7: ("LAX", "zpy", 1),
    0xAF: ("LAX", "abs", 2), 0xBF: ("LAX", "aby", 2),
    0xA3: ("LAX", "izx", 1), 0xB3: ("LAX", "izy", 1),
    0xAB: ("LAX", "imm", 1),

    # SAX = STA & STX
    0x87: ("SAX", "zp",  1), 0x97: ("SAX", "zpy", 1),
    0x8F: ("SAX", "abs", 2), 0x83: ("SAX", "izx", 1),

    # DCP = DEC + CMP
    0xC7: ("DCP", "zp",  1), 0xD7: ("DCP", "zpx", 1),
    0xCF: ("DCP", "abs", 2), 0xDF: ("DCP", "abx", 2),
    0xDB: ("DCP", "aby", 2), 0xC3: ("DCP", "izx", 1),
    0xD3: ("DCP", "izy", 1),

    # ISC / ISB = INC + SBC
    0xE7: ("ISC", "zp",  1), 0xF7: ("ISC", "zpx", 1),
    0xEF: ("ISC", "abs", 2), 0xFF: ("ISC", "abx", 2),
    0xFB: ("ISC", "aby", 2), 0xE3: ("ISC", "izx", 1),
    0xF3: ("ISC", "izy", 1),

    # RLA = ROL + AND
    0x27: ("RLA", "zp",  1), 0x37: ("RLA", "zpx", 1),
    0x2F: ("RLA", "abs", 2), 0x3F: ("RLA", "abx", 2),
    0x3B: ("RLA", "aby", 2), 0x23: ("RLA", "izx", 1),
    0x33: ("RLA", "izy", 1),

    # RRA = ROR + ADC
    0x67: ("RRA", "zp",  1), 0x77: ("RRA", "zpx", 1),
    0x6F: ("RRA", "abs", 2), 0x7F: ("RRA", "abx", 2),
    0x7B: ("RRA", "aby", 2), 0x63: ("RRA", "izx", 1),
    0x73: ("RRA", "izy", 1),

    # SLO = ASL + ORA
    0x07: ("SLO", "zp",  1), 0x17: ("SLO", "zpx", 1),
    0x0F: ("SLO", "abs", 2), 0x1F: ("SLO", "abx", 2),
    0x1B: ("SLO", "aby", 2), 0x03: ("SLO", "izx", 1),
    0x13: ("SLO", "izy", 1),

    # SRE = LSR + EOR
    0x47: ("SRE", "zp",  1), 0x57: ("SRE", "zpx", 1),
    0x4F: ("SRE", "abs", 2), 0x5F: ("SRE", "abx", 2),
    0x5B: ("SRE", "aby", 2), 0x43: ("SRE", "izx", 1),
    0x53: ("SRE", "izy", 1),

    # ANC, ALR, ARR, AXS, XAA - immediate single-byte combos
    0x0B: ("ANC", "imm", 1), 0x2B: ("ANC", "imm", 1),
    0x4B: ("ALR", "imm", 1), 0x6B: ("ARR", "imm", 1),
    0xCB: ("AXS", "imm", 1), 0x8B: ("XAA", "imm", 1),

    # SBC duplicate
    0xEB: ("SBC", "imm", 1),

    # SHX, SHY, TAS, LAS, AHX (less common, store ops)
    0x9C: ("SHY", "abx", 2), 0x9E: ("SHX", "aby", 2),
    0x9B: ("TAS", "aby", 2), 0xBB: ("LAS", "aby", 2),
    0x9F: ("AHX", "aby", 2), 0x93: ("AHX", "izy", 1),

    # KIL / JAM (CPU lockup)
    0x02: ("JAM", "imp", 0), 0x12: ("JAM", "imp", 0),
    0x22: ("JAM", "imp", 0), 0x32: ("JAM", "imp", 0),
    0x42: ("JAM", "imp", 0), 0x52: ("JAM", "imp", 0),
    0x62: ("JAM", "imp", 0), 0x72: ("JAM", "imp", 0),
    0x92: ("JAM", "imp", 0), 0xB2: ("JAM", "imp", 0),
    0xD2: ("JAM", "imp", 0), 0xF2: ("JAM", "imp", 0),
}


# =====================================================================
# Disassembler core
# =====================================================================
class _DisasmLine:
    __slots__ = ('pc', 'bytes', 'mnemonic', 'mode', 'operand', 'target', 'comment')
    def __init__(self, pc, b, mnemonic, mode, operand, target=None, comment=""):
        self.pc = pc
        self.bytes = b               # list of int (1-3 bytes)
        self.mnemonic = mnemonic     # "LDA" etc, or ".BYTE" for data
        self.mode = mode             # addressing mode tag
        self.operand = operand       # display string
        self.target = target         # absolute target address for jumps/branches, else None
        self.comment = comment


# =====================================================================
# 65C02 extension opcodes (WDC, Rockwell, NMOS variants)
# =====================================================================
# These add new instructions (BRA, PHX/PHY, PLX/PLY, STZ, TRB, TSB,
# new addressing modes for existing opcodes like BIT immediate, JMP
# (abs,X), etc.) plus the Rockwell/WDC bit ops (BBR0-7, BBS0-7,
# RMB0-7, SMB0-7) and WDC-only WAI/STP.
# Common subset that real C64+ code uses (Plus/4 has 65C02-like CPU).
EXTRA_65C02 = {
    # New mnemonics
    0x80: ("BRA", "rel", 1),
    0xDA: ("PHX", "imp", 0),
    0x5A: ("PHY", "imp", 0),
    0xFA: ("PLX", "imp", 0),
    0x7A: ("PLY", "imp", 0),
    0x1A: ("INC", "acc", 0),     # INA / INC A
    0x3A: ("DEC", "acc", 0),     # DEA / DEC A
    0x64: ("STZ", "zp",  1),
    0x74: ("STZ", "zpx", 1),
    0x9C: ("STZ", "abs", 2),
    0x9E: ("STZ", "abx", 2),
    0x14: ("TRB", "zp",  1),
    0x1C: ("TRB", "abs", 2),
    0x04: ("TSB", "zp",  1),
    0x0C: ("TSB", "abs", 2),
    # New addressing modes for existing opcodes
    0x89: ("BIT", "imm", 1),
    0x34: ("BIT", "zpx", 1),
    0x3C: ("BIT", "abx", 2),
    0x12: ("ORA", "izp", 1),     # (zp) — non-indexed indirect zp
    0x32: ("AND", "izp", 1),
    0x52: ("EOR", "izp", 1),
    0x72: ("ADC", "izp", 1),
    0x92: ("STA", "izp", 1),
    0xB2: ("LDA", "izp", 1),
    0xD2: ("CMP", "izp", 1),
    0xF2: ("SBC", "izp", 1),
    0x7C: ("JMP", "iax", 2),     # (abs,X)
    # WDC-only WAI / STP
    0xCB: ("WAI", "imp", 0),
    0xDB: ("STP", "imp", 0),
    # Rockwell/WDC RMB0-7, SMB0-7 - reset/set single bit in zp
    # (each gets its own opcode; the bit number is part of the mnemonic)
    0x07: ("RMB0", "zp", 1), 0x17: ("RMB1", "zp", 1),
    0x27: ("RMB2", "zp", 1), 0x37: ("RMB3", "zp", 1),
    0x47: ("RMB4", "zp", 1), 0x57: ("RMB5", "zp", 1),
    0x67: ("RMB6", "zp", 1), 0x77: ("RMB7", "zp", 1),
    0x87: ("SMB0", "zp", 1), 0x97: ("SMB1", "zp", 1),
    0xA7: ("SMB2", "zp", 1), 0xB7: ("SMB3", "zp", 1),
    0xC7: ("SMB4", "zp", 1), 0xD7: ("SMB5", "zp", 1),
    0xE7: ("SMB6", "zp", 1), 0xF7: ("SMB7", "zp", 1),
    # BBR0-7, BBS0-7 - branch on bit reset/set in zp.
    # Format is `BBRx zp, target` with 3 bytes total.
    # We use a custom mode 'zprel' for these.
    0x0F: ("BBR0", "zprel", 2), 0x1F: ("BBR1", "zprel", 2),
    0x2F: ("BBR2", "zprel", 2), 0x3F: ("BBR3", "zprel", 2),
    0x4F: ("BBR4", "zprel", 2), 0x5F: ("BBR5", "zprel", 2),
    0x6F: ("BBR6", "zprel", 2), 0x7F: ("BBR7", "zprel", 2),
    0x8F: ("BBS0", "zprel", 2), 0x9F: ("BBS1", "zprel", 2),
    0xAF: ("BBS2", "zprel", 2), 0xBF: ("BBS3", "zprel", 2),
    0xCF: ("BBS4", "zprel", 2), 0xDF: ("BBS5", "zprel", 2),
    0xEF: ("BBS6", "zprel", 2), 0xFF: ("BBS7", "zprel", 2),
}


# =====================================================================
# Build reverse lookup: (mnemonic, mode) -> opcode.
# Used by the assembler. Includes 6502 + 65C02 (assembler accepts both).
# =====================================================================
_REV_OPCODES = {}
for _op, (_mn, _mode, _sz) in OPCODES.items():
    _REV_OPCODES[(_mn, _mode)] = _op
for _op, (_mn, _mode, _sz) in EXTRA_65C02.items():
    # 65C02 wins on conflicts (e.g. 0x1A): documented 6502 has no entry
    # for 0x1A, so this is fine.
    _REV_OPCODES[(_mn, _mode)] = _op


class AssemblerError(Exception):
    pass


def _parse_number(s):
    """Parse a 6502-style numeric operand. Accepts:
        $1234   = hex
        %1010   = binary
        1234    = decimal (rare)
    Returns int. Raises AssemblerError on bad input."""
    s = s.strip()
    if not s:
        raise AssemblerError("empty number")
    if s[0] == '$':
        try: return int(s[1:], 16)
        except ValueError: raise AssemblerError(f"bad hex: {s}")
    if s[0] == '%':
        try: return int(s[1:], 2)
        except ValueError: raise AssemblerError(f"bad binary: {s}")
    try: return int(s, 10)
    except ValueError: raise AssemblerError(f"bad number: {s}")


def _detect_mode(operand: str, mnemonic: str):
    """Figure out the addressing mode from an operand string.
    Returns (mode_tag, value_or_None). mnemonic is needed because
    branches (BCC etc.) take a different mode than absolute jumps
    even though the syntax looks identical."""
    o = operand.strip()
    if o == '' or o.upper() == 'A':
        # Implied or accumulator
        return ('acc' if o.upper() == 'A' else 'imp'), None

    # Branches always use relative mode
    if mnemonic in BRANCH_OPS:
        return 'rel', _parse_number(o)

    # Immediate: #$nn or #nn or #%nn
    if o.startswith('#'):
        return 'imm', _parse_number(o[1:])

    # Indirect: (...)
    if o.startswith('(') and o.endswith(')'):
        # Could be JMP ($nnnn), (zp,X), (zp),Y
        inner = o[1:-1].strip()
        # (zp,X)?
        if ',' in inner and inner.upper().endswith(',X'):
            return 'izx', _parse_number(inner[:-2].strip())
        # JMP ($nnnn)
        return 'ind', _parse_number(inner)
    # (zp),Y
    if o.startswith('(') and ')' in o and o.upper().endswith(',Y'):
        # e.g. ($20),Y
        idx = o.index(')')
        inner = o[1:idx].strip()
        return 'izy', _parse_number(inner)

    # Indexed: $nn,X or $nn,Y (and decimal/binary equivalents)
    if ',' in o:
        base, _, idx = o.rpartition(',')
        idx = idx.strip().upper()
        val = _parse_number(base.strip())
        if idx == 'X':
            return ('zpx', val) if val < 0x100 else ('abx', val)
        if idx == 'Y':
            return ('zpy', val) if val < 0x100 else ('aby', val)
        raise AssemblerError(f"bad index: {o}")

    # Plain absolute or zero-page
    val = _parse_number(o)
    return ('zp', val) if val < 0x100 else ('abs', val)


def _parse_label(s):
    """A label is an identifier: starts with letter, _, '.', or '@',
    followed by letters/digits/_. Returns the label name or None.

    The '.' and '@' prefixes are used by ACME/KickAss for local/cheap
    labels - we don't track local scope, so we just treat them as
    part of the label name. That means a `.loop` in one zone and
    `.loop` in another would collide, but in practice this works for
    most simple sources where local labels have unique names anyway.
    """
    if not s:
        return None
    if not (s[0].isalpha() or s[0] in '_.@'):
        return None
    for ch in s[1:]:
        if not (ch.isalnum() or ch == '_'):
            return None
    return s


def _eval_expr(expr: str, symbols: dict, pc: int):
    """Evaluate a numeric/label/arithmetic expression. Supports:
        $FF, %1010, 1234   - numeric literals
        'A'                - character literal (ASCII byte)
        label              - symbol lookup
        *                  - current pc
        + - * / %          - arithmetic (proper precedence)
        & | ^              - bitwise and/or/xor
        << >>              - bitwise shift
        <expr, >expr       - low/high byte (unary)
        ~expr, -expr       - bitwise NOT, negation (unary)
        ( ... )            - grouping
    Anything else raises AssemblerError."""
    expr = expr.strip()
    if not expr:
        raise AssemblerError("empty expression")

    # Strip a single layer of redundant outer parens, but only if they
    # really are matching (not '(a)+(b)').
    while expr.startswith('(') and expr.endswith(')'):
        depth = 0
        ok = True
        for i, ch in enumerate(expr[:-1]):
            if ch == '(': depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    # The opening ( closes before the end - not a wrapper
                    ok = False; break
        if ok:
            expr = expr[1:-1].strip()
            if not expr:
                raise AssemblerError("empty parens")
        else:
            break

    # Helper: scan from right to left at paren depth 0 for any of `ops`.
    # Returns the index of the rightmost match, or -1.
    def _split_at(operators):
        depth = 0
        in_q = None
        for i in range(len(expr) - 1, -1, -1):
            ch = expr[i]
            if in_q:
                if ch == in_q: in_q = None
                continue
            if ch in "'\"":
                in_q = ch; continue
            if ch == ')': depth += 1
            elif ch == '(': depth -= 1
            elif depth == 0:
                # Two-char operators (>>, <<, ==, !=, <=, >=, &&, ||)
                # take priority over one-char.
                if i > 0:
                    two = expr[i-1:i+1]
                    if two in operators:
                        return i - 1
                if ch in operators:
                    # Don't split if this char is the FIRST half of a
                    # 2-char operator (e.g. the `<` in `<<`)
                    if i + 1 < len(expr):
                        nxt = expr[i:i+2]
                        if nxt in ('<<', '>>', '==', '!=', '<=', '>=',
                                    '&&', '||'):
                            continue
                    if i == 0:
                        continue
                    prev = expr[i-1]
                    # Don't split if previous char makes this part of
                    # a 2-char op already (the '<' in '<<', '>' in '>>',
                    # '=' in '==', etc.)
                    if prev in '<>=!&|':
                        # But only if the combined `prev+ch` would be
                        # a known 2-char op
                        if prev + ch in ('<<', '>>', '==', '!=',
                                          '<=', '>=', '&&', '||'):
                            continue
                    if prev in '+-*/%&|^~<>(':
                        # unary minus etc - skip
                        continue
                    return i
        return -1


    # Operator precedence (low to high):
    #   1. | (bitwise or)
    #   2. ^ (bitwise xor)
    #   3. & (bitwise and)
    #   4. << >>
    #   5. + -
    #   6. * / %
    # We scan rightmost-first within each level so that left-to-right
    # associativity is preserved (e.g. 10-3-2 = (10-3)-2 = 5).
    LEVELS = (
        # Logical OR (lowest precedence)
        ('||',),
        # Logical AND
        ('&&',),
        # Comparison: == != <= >= < >
        ('==', '!='),
        ('<=', '>=', '<', '>'),
        # Bitwise
        ('|',),
        ('^',),
        ('&',),
        ('<<', '>>'),
        # Arithmetic
        ('+', '-'),
        ('*', '/', '%'),
    )
    for level in LEVELS:
        idx = _split_at(set(level))
        if idx < 0:
            continue
        # Determine operator length (1 or 2)
        op_len = 2 if expr[idx:idx+2] in level else 1
        op = expr[idx:idx+op_len]
        left_str = expr[:idx].strip()
        right_str = expr[idx+op_len:].strip()
        if not left_str or not right_str:
            continue   # Probably unary - skip this level
        L = _eval_expr(left_str, symbols, pc)
        R = _eval_expr(right_str, symbols, pc)
        if op == '+':  return L + R
        if op == '-':  return L - R
        if op == '*':  return L * R
        if op == '/':  return L // R if R != 0 else 0
        if op == '%':  return L % R if R != 0 else 0
        if op == '&':  return L & R
        if op == '|':  return L | R
        if op == '^':  return L ^ R
        if op == '<<': return L << R
        if op == '>>': return L >> R
        # Comparison ops return 1/0 like C
        if op == '==': return 1 if L == R else 0
        if op == '!=': return 1 if L != R else 0
        if op == '<':  return 1 if L < R else 0
        if op == '>':  return 1 if L > R else 0
        if op == '<=': return 1 if L <= R else 0
        if op == '>=': return 1 if L >= R else 0
        if op == '&&': return 1 if (L and R) else 0
        if op == '||': return 1 if (L or R) else 0

    # Unary prefixes
    if expr[0] == '<':
        return _eval_expr(expr[1:], symbols, pc) & 0xFF
    if expr[0] == '>':
        return (_eval_expr(expr[1:], symbols, pc) >> 8) & 0xFF
    if expr[0] == '~':
        return (~_eval_expr(expr[1:], symbols, pc)) & 0xFFFFFFFF
    if expr[0] == '-':
        return -_eval_expr(expr[1:], symbols, pc)
    if expr[0] == '+':
        return _eval_expr(expr[1:], symbols, pc)

    # Function call: name(args)
    # Supports a few common compile-time builtins from KickAss/64tass
    # plus any user-defined functions.
    fn_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)$', expr)
    if fn_match:
        fn_name_orig = fn_match.group(1)
        fn_name = fn_name_orig.lower()
        args_str = fn_match.group(2)
        # Split on commas at top-level paren depth 0
        args = []; cur = ''; pd = 0; iq = None
        for ch in args_str:
            if iq:
                cur += ch
                if ch == iq: iq = None
                continue
            if ch in '"\'':
                iq = ch; cur += ch; continue
            if ch == '(': pd += 1; cur += ch; continue
            if ch == ')': pd -= 1; cur += ch; continue
            if ch == ',' and pd == 0:
                args.append(cur); cur = ''; continue
            cur += ch
        if cur.strip(): args.append(cur)
        # User-defined function (case-sensitive)?
        user_result = _evaluate_user_function(
            fn_name_orig, args_str, symbols, pc)
        if user_result is not None:
            return user_result
        # Skip eager eval for functions that take strings/raw args
        if fn_name == 'len':
            if len(args) == 1:
                a = args[0].strip()
                if (len(a) >= 2 and a[0] in '"\''
                        and a[-1] == a[0]):
                    return len(a[1:-1])
            return len(args)
        argvals = [_eval_expr(a, symbols, pc) for a in args] if args else []
        if fn_name == 'abs' and len(argvals) == 1:
            return abs(argvals[0])
        if fn_name == 'min':
            return min(argvals)
        if fn_name == 'max':
            return max(argvals)
        if fn_name == 'floor' and len(argvals) == 1:
            return int(argvals[0])      # ints are already floored
        if fn_name == 'ceil' and len(argvals) == 1:
            v = argvals[0]
            return int(v) + (1 if v != int(v) and v > 0 else 0)
        if fn_name == 'round' and len(argvals) == 1:
            return int(round(argvals[0]))
        if fn_name == 'sqrt' and len(argvals) == 1:
            import math
            return int(math.sqrt(argvals[0]))
        if fn_name == 'sin' and len(argvals) == 1:
            import math
            return int(round(math.sin(argvals[0])))
        if fn_name == 'cos' and len(argvals) == 1:
            import math
            return int(round(math.cos(argvals[0])))
        if fn_name == 'tan' and len(argvals) == 1:
            import math
            return int(round(math.tan(argvals[0])))
        if fn_name == 'asin' and len(argvals) == 1:
            import math
            return int(round(math.asin(argvals[0])))
        if fn_name == 'acos' and len(argvals) == 1:
            import math
            return int(round(math.acos(argvals[0])))
        if fn_name == 'atan' and len(argvals) == 1:
            import math
            return int(round(math.atan(argvals[0])))
        if fn_name == 'log' and len(argvals) == 1:
            import math
            return int(round(math.log(argvals[0])))
        if fn_name == 'exp' and len(argvals) == 1:
            import math
            return int(round(math.exp(argvals[0])))
        if fn_name == 'pow' and len(argvals) == 2:
            return int(argvals[0] ** argvals[1])
        if fn_name == 'mod' and len(argvals) == 2:
            return argvals[0] % argvals[1] if argvals[1] != 0 else 0
        if fn_name == 'sgn' and len(argvals) == 1:
            v = argvals[0]
            return 1 if v > 0 else (-1 if v < 0 else 0)
        if fn_name in ('lo', 'low') and len(argvals) == 1:
            return argvals[0] & 0xFF
        if fn_name in ('hi', 'high') and len(argvals) == 1:
            return (argvals[0] >> 8) & 0xFF
        if fn_name in ('bk', 'bank') and len(argvals) == 1:
            return (argvals[0] >> 16) & 0xFF
        if fn_name == 'len':
            # Special: don't evaluate args as expressions - work on
            # the raw text. Length of a string literal (after parsing
            # escapes), or count of comma-separated items.
            if len(args) == 1:
                a = args[0].strip()
                if (len(a) >= 2 and a[0] in '"\''
                        and a[-1] == a[0]):
                    return len(a[1:-1])
            return len(args)
        if fn_name == 'random' and len(argvals) == 0:
            import random
            return random.randint(0, 0xFFFF)
        if fn_name == 'random' and len(argvals) == 1:
            import random
            return random.randint(0, argvals[0])
        if fn_name == 'random' and len(argvals) == 2:
            import random
            return random.randint(argvals[0], argvals[1])
        # Unknown function - fall through to label lookup so the
        # parens-trim layer handles it.

    # Numeric literals
    if expr[0] == '$':
        try: return int(expr[1:], 16)
        except ValueError: pass
    if expr[0] == '%':
        try: return int(expr[1:], 2)
        except ValueError: pass
    if expr[0].isdigit():
        # Try decimal, then hex (KickAss allows 0x prefix)
        if expr.lower().startswith('0x'):
            try: return int(expr[2:], 16)
            except ValueError: pass
        try: return int(expr, 10)
        except ValueError: pass
    if expr == '*':
        return pc

    # Character literal: 'A' or "A"  (single char)
    if len(expr) >= 3 and expr[0] in "'\"" and expr[-1] == expr[0]:
        inner = expr[1:-1]
        if len(inner) == 1:
            return ord(inner)

    # Label lookup. Allow:
    #   - simple: foo
    #   - leading '.' or '@' for ACME/KickAss local labels: .loop / @lbl
    #   - dotted scope: scope.label, scope.subscope.label
    label_name = expr.lstrip('.').lstrip('@')
    if label_name and (label_name[0].isalpha() or label_name[0] == '_'):
        # Identifier rest can contain letters, digits, _ AND dots for
        # qualified scope access.
        if all(c.isalnum() or c in '_.' for c in label_name[1:]):
            # Try the original name (with dot/@ prefix preserved)
            if expr in symbols:
                return symbols[expr]
            if label_name in symbols:
                return symbols[label_name]
            raise AssemblerError(f"undefined label: {expr!r}")

    raise AssemblerError(f"cannot parse expression: {expr!r}")


def _strip_block_comments(source: str) -> str:
    """Remove /* ... */ comments from the entire source. Block comments
    can span multiple lines (KickAssembler / C-style). Comments inside
    string literals are NOT stripped."""
    out = []
    i = 0
    in_quote = None
    in_block = False
    while i < len(source):
        ch = source[i]
        if in_block:
            if ch == '*' and i + 1 < len(source) and source[i+1] == '/':
                in_block = False
                i += 2
                continue
            # Preserve newlines so line numbers stay correct
            if ch == '\n':
                out.append('\n')
            i += 1
            continue
        if in_quote:
            out.append(ch)
            if ch == in_quote:
                in_quote = None
            elif ch == '\\' and i + 1 < len(source):
                # Skip escaped char inside string
                out.append(source[i+1]); i += 2; continue
            i += 1
            continue
        if ch in "'\"":
            in_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < len(source) and source[i+1] == '*':
            in_block = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _strip_comment(line: str) -> str:
    """Remove ;... and //... comments from a single line. `;` inside
    a quoted literal is preserved. Block comments /* ... */ should be
    pre-processed by _strip_block_comments before this is called."""
    src = line
    in_quote = None
    out = []
    i = 0
    while i < len(src):
        ch = src[i]
        if in_quote:
            out.append(ch)
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in "'\"":
            in_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == ';':
            break
        if ch == '/' and i + 1 < len(src) and src[i+1] == '/':
            break
        out.append(ch)
        i += 1
    return ''.join(out)


def _instruction_size(mnemonic: str, mode: str):
    """Return the number of bytes an instruction takes, given mnemonic
    and mode. Used during pass 1 to compute label addresses without
    actually resolving operand values (which may be forward labels)."""
    if mode in ('imp', 'acc'):
        return 1
    if mode in ('imm', 'zp', 'zpx', 'zpy', 'izx', 'izy', 'izp', 'rel'):
        return 2
    if mode in ('abs', 'abx', 'aby', 'ind', 'iax', 'zprel'):
        return 3
    return 1


def _detect_mode_size(operand: str, mnemonic: str, symbols=None, pc=0):
    """Like _detect_mode, but returns just (mode, size). Used in pass 1
    where we only need to know how big the instruction will be.
    Forward-reference labels are assumed to be 16-bit addresses (abs),
    so operand-size computations are conservative.

    Supports 64tass `@w` / `@b` width overrides:
        lda @w16      -> force 16-bit absolute (3-byte instruction)
        lda @b16      -> force 8-bit zero-page  (2-byte instruction)
    The override is stripped from the operand before mode detection."""
    o = operand.strip()
    # Width override: @w / @b prefix forces wide / byte width
    force_wide = False
    force_byte = False
    if o.lower().startswith('@w'):
        force_wide = True
        o = o[2:].strip()
    elif o.lower().startswith('@b'):
        force_byte = True
        o = o[2:].strip()
    if o == '' or o.upper() == 'A':
        return ('acc' if o.upper() == 'A' else 'imp'), 1
    # Rockwell BBR0-7 / BBS0-7 take `zp, target` as operand
    if mnemonic.startswith(('BBR', 'BBS')) and len(mnemonic) == 4 \
            and mnemonic[3].isdigit():
        return 'zprel', 3
    if mnemonic in BRANCH_OPS:
        return 'rel', 2
    if o.startswith('#'):
        return 'imm', 2
    # Indirect: (...)
    if o.startswith('('):
        # JMP ($nnnn), JMP ($nnnn,X), (zp,X), (zp),Y, (zp)
        if o.endswith(')'):
            inner = o[1:-1].strip()
            if ',' in inner and inner.upper().endswith(',X'):
                # Could be (zp,X) [izx, 2 bytes] or 65C02 (abs,X) [iax, 3]
                base = inner[:-2].strip()
                try:
                    val = _eval_expr(base, symbols or {}, pc)
                    if val < 0x100 and (mnemonic, 'izx') in _REV_OPCODES:
                        return 'izx', 2
                except AssemblerError:
                    pass
                # Default to izx unless mnemonic only supports iax (JMP)
                if (mnemonic, 'iax') in _REV_OPCODES \
                        and (mnemonic, 'izx') not in _REV_OPCODES:
                    return 'iax', 3
                return 'izx', 2
            # Plain (expr): could be JMP (abs) [ind, 3] or 65C02 (zp) [izp, 2]
            try:
                val = _eval_expr(inner, symbols or {}, pc)
                if val < 0x100 and (mnemonic, 'izp') in _REV_OPCODES:
                    return 'izp', 2
            except AssemblerError:
                pass
            return 'ind', 3
        if ')' in o and o.upper().endswith(',Y'):
            return 'izy', 2
        # Couldn't parse - guess
        return 'abs', 3
    # Indexed: $nn,X / $nn,Y / label,X / label,Y
    if ',' in o:
        base, _, idx = o.rpartition(',')
        idx_u = idx.strip().upper()
        # Try to evaluate base; if it fits in zp choose zp; else abs.
        # If it's a forward label, we don't know - default to abs.
        try:
            val = _eval_expr(base.strip(), symbols or {}, pc)
            if val < 0x100 and not force_wide:
                if (mnemonic, 'zpx') in _REV_OPCODES and idx_u == 'X':
                    return 'zpx', 2
                if (mnemonic, 'zpy') in _REV_OPCODES and idx_u == 'Y':
                    return 'zpy', 2
        except AssemblerError:
            pass
        if idx_u == 'X':
            return 'abx', 3
        if idx_u == 'Y':
            return 'aby', 3
    # Plain expression - try to size it
    try:
        val = _eval_expr(o, symbols or {}, pc)
        if val < 0x100 and (mnemonic, 'zp') in _REV_OPCODES \
                and not force_wide:
            return 'zp', 2
        if force_byte and (mnemonic, 'zp') in _REV_OPCODES:
            return 'zp', 2
        return 'abs', 3
    except AssemblerError:
        # forward label - assume abs (or zp if forced)
        if force_byte and (mnemonic, 'zp') in _REV_OPCODES:
            return 'zp', 2
        return 'abs', 3


def _assemble_one(mnemonic: str, operand: str, pc: int, symbols: dict):
    """Encode one instruction. Returns list of bytes. Resolves labels
    in `symbols`. Used in pass 2."""
    mode, _ = _detect_mode_size(operand, mnemonic, symbols, pc)

    # Strip 64tass width override - it's already encoded in the mode
    o = operand.strip()
    if o.lower().startswith(('@w', '@b')):
        o = o[2:].strip()

    # Special cases for indirect
    if o.startswith('(') and o.endswith(')') and mode in ('ind', 'izp'):
        # JMP ($nnnn) [ind, 16-bit] or 65C02 OP (zp) [izp, 8-bit]
        inner = o[1:-1].strip()
        value = _eval_expr(inner, symbols, pc)
    elif o.startswith('(') and o.endswith(')') and mode in ('izx', 'iax'):
        # OP ($nn,X) [izx] or 65C02 JMP ($nnnn,X) [iax]
        inner = o[1:-1].strip()
        inner = inner.rsplit(',', 1)[0].strip()
        value = _eval_expr(inner, symbols, pc)
    elif o.startswith('(') and ')' in o and o.upper().endswith(',Y') and mode == 'izy':
        idx = o.index(')')
        inner = o[1:idx].strip()
        value = _eval_expr(inner, symbols, pc)
    elif mode in ('zpx', 'zpy', 'abx', 'aby'):
        base, _, _idx = o.rpartition(',')
        value = _eval_expr(base.strip(), symbols, pc)
    elif mode == 'zprel':
        # Rockwell BBR/BBS: operand is "zp, target"
        parts = [p.strip() for p in o.rsplit(',', 1)]
        if len(parts) != 2:
            raise AssemblerError(
                f"{mnemonic}: expected 'zp, target' operand")
        zp_val = _eval_expr(parts[0], symbols, pc)
        tgt_val = _eval_expr(parts[1], symbols, pc)
        if not 0 <= zp_val <= 0xFF:
            raise AssemblerError(
                f"{mnemonic}: zp out of range: ${zp_val:X}")
        offset = tgt_val - (pc + 3)
        if offset < -128 or offset > 127:
            raise AssemblerError(
                f"{mnemonic}: branch out of range from ${pc:04X} to ${tgt_val:04X}")
        opcode = _REV_OPCODES[(mnemonic, mode)]
        return [opcode, zp_val & 0xFF, offset & 0xFF]
    elif mode == 'imm':
        value = _eval_expr(o[1:], symbols, pc)
    elif mode in ('imp', 'acc'):
        value = None
    else:
        value = _eval_expr(o, symbols, pc)

    # Find opcode - allow zp<->abs fallback
    candidates = [mode]
    if mode == 'zp' and (mnemonic, 'zp') not in _REV_OPCODES:
        candidates.append('abs')
    elif mode == 'abs' and (mnemonic, 'abs') not in _REV_OPCODES:
        candidates.append('zp')
    elif mode == 'zpx' and (mnemonic, 'zpx') not in _REV_OPCODES:
        candidates.append('abx')
    elif mode == 'abx' and (mnemonic, 'abx') not in _REV_OPCODES:
        candidates.append('zpx')
    elif mode == 'zpy' and (mnemonic, 'zpy') not in _REV_OPCODES:
        candidates.append('aby')
    elif mode == 'aby' and (mnemonic, 'aby') not in _REV_OPCODES:
        candidates.append('zpy')

    opcode = None
    chosen_mode = None
    for m in candidates:
        if (mnemonic, m) in _REV_OPCODES:
            opcode = _REV_OPCODES[(mnemonic, m)]
            chosen_mode = m
            break

    if opcode is None:
        raise AssemblerError(
            f"no encoding for {mnemonic} with mode {mode!r} "
            f"(operand: {operand!r})")

    # Encode bytes
    if chosen_mode in ('imp', 'acc'):
        return [opcode]
    if chosen_mode == 'imm':
        if not 0 <= value <= 0xFF:
            raise AssemblerError(f"immediate value out of range: {value}")
        return [opcode, value & 0xFF]
    if chosen_mode in ('zp', 'zpx', 'zpy', 'izx', 'izy', 'izp'):
        if not 0 <= value <= 0xFF:
            raise AssemblerError(f"zero-page value out of range: ${value:X}")
        return [opcode, value & 0xFF]
    if chosen_mode in ('abs', 'abx', 'aby', 'ind', 'iax'):
        if not 0 <= value <= 0xFFFF:
            raise AssemblerError(f"absolute value out of range: ${value:X}")
        return [opcode, value & 0xFF, (value >> 8) & 0xFF]
    if chosen_mode == 'rel':
        offset = value - (pc + 2)
        if offset < -128 or offset > 127:
            raise AssemblerError(
                f"branch out of range: from ${pc:04X} to ${value:04X} "
                f"(offset {offset:+d})")
        return [opcode, offset & 0xFF]
    raise AssemblerError(f"unsupported mode: {chosen_mode}")


def assemble_line(line: str, pc: int):
    """Single-line assembler. Kept for backwards compatibility but the
    main entry point is now `assemble()` which handles labels."""
    src = _strip_comment(line).strip()
    if not src or src.endswith(':'):
        return []
    parts = src.split(None, 1)
    mnemonic = parts[0].upper()
    operand = parts[1].strip() if len(parts) > 1 else ''
    if '.' in mnemonic:
        mnemonic = mnemonic.split('.', 1)[0]
    return _assemble_one(mnemonic, operand, pc, {})


def _split_args(s):
    """Split a comma-separated argument list, but respect quoted strings
    and brackets/parens so that '!byte "Hello, World", $00' yields
    ['"Hello, World"', '$00'] and `.fill 8, [$55, $aa]` yields
    ['8', '[$55, $aa]'].
    Returns the raw argument tokens with surrounding whitespace stripped."""
    out = []
    cur = []
    in_quote = None
    paren = 0
    bracket = 0
    for ch in s:
        if in_quote:
            cur.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in '"\'':
            in_quote = ch
            cur.append(ch)
            continue
        if ch == '(':
            paren += 1; cur.append(ch); continue
        if ch == ')':
            paren -= 1; cur.append(ch); continue
        if ch == '[':
            bracket += 1; cur.append(ch); continue
        if ch == ']':
            bracket -= 1; cur.append(ch); continue
        if ch == ',' and paren == 0 and bracket == 0:
            piece = ''.join(cur).strip()
            if piece:
                out.append(piece)
            cur = []
            continue
        cur.append(ch)
    piece = ''.join(cur).strip()
    if piece:
        out.append(piece)
    return out


def _parse_string_arg(s, lineno, raw):
    """Pull a quoted string out of an argument; strip the quotes.
    Allows trailing extra args like  !text "hello", $00  (only the
    first quoted token is returned as the string; further bytes need
    a separate !byte directive).

    Supports common backslash escapes:  \\n  \\r  \\t  \\0  \\\\  \\"
    \\'  and hex \\xHH form (e.g. "\\x05" -> single byte 0x05)."""
    s = s.strip()
    if not s or s[0] not in '"\'':
        raise AssemblerError(
            f"line {lineno}: text directive needs a quoted string\n  > {raw}")
    quote = s[0]
    # Find matching close-quote, respecting backslash escapes
    out = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == quote:
            return ''.join(out)
        if ch == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == 'n':   out.append('\n'); i += 2; continue
            if nxt == 'r':   out.append('\r'); i += 2; continue
            if nxt == 't':   out.append('\t'); i += 2; continue
            if nxt == '0':   out.append('\x00'); i += 2; continue
            if nxt == '\\':  out.append('\\'); i += 2; continue
            if nxt == '"':   out.append('"'); i += 2; continue
            if nxt == "'":   out.append("'"); i += 2; continue
            if nxt == 'x' and i + 3 < len(s):
                try:
                    byte = int(s[i+2:i+4], 16)
                    out.append(chr(byte))
                    i += 4
                    continue
                except ValueError:
                    pass
            # unknown escape - keep literal backslash
            out.append(ch); i += 1; continue
        out.append(ch); i += 1
    raise AssemblerError(
        f"line {lineno}: unterminated string\n  > {raw}")


# C64 screencode conversion table for letters and common chars.
# C64 screencodes differ from PETSCII: 'A' = $01 in screencode,
# 'A' = $41 in PETSCII (= ASCII).
def _to_screencode(ch):
    """Convert one character to a C64 screencode byte."""
    o = ord(ch)
    # Uppercase A-Z -> $01..$1A
    if 0x41 <= o <= 0x5A:
        return o - 0x40
    # Lowercase a-z -> $01..$1A (same as upper - no case in screencode)
    if 0x61 <= o <= 0x7A:
        return o - 0x60
    # Digits 0-9 -> $30..$39 (same as ASCII)
    if 0x30 <= o <= 0x39:
        return o
    # Space -> $20
    if o == 0x20:
        return 0x20
    # Most punctuation 0x20..0x3F maps directly
    if 0x20 <= o <= 0x3F:
        return o
    # @ -> $00, [ -> $1B, etc - fall back to PETSCII low byte for unknown
    if o == 0x40: return 0x00
    if o == 0x5B: return 0x1B
    if o == 0x5C: return 0x1C
    if o == 0x5D: return 0x1D
    if o == 0x5E: return 0x1E
    if o == 0x5F: return 0x1F
    return o & 0xFF


def _to_petscii(ch):
    """Convert one character to PETSCII byte (uppercase mode).
    For ASCII letters, uppercase stays uppercase, lowercase -> uppercase
    in the C64's lowercase-equivalent range."""
    o = ord(ch)
    # In uppercase PETSCII charset: A-Z = $C1..$DA, a-z is shifted version
    # Most assemblers just pass ASCII through which works for upper
    # case + digits + symbols. We mirror ACME's default behaviour: PetSCII
    # uppercase = ASCII uppercase, ASCII lowercase becomes uppercase.
    if 0x61 <= o <= 0x7A:    # a-z -> A-Z
        return o - 0x20
    if 0x41 <= o <= 0x5A:    # A-Z stays
        return o
    return o & 0xFF


def _encode_char(ch, conv_table):
    """Apply a text conversion table to a single character. Used for
    !text / .text / .ptext / .null / .shift / .shiftl etc."""
    if conv_table == 'pet':
        return _to_petscii(ch) & 0xFF
    if conv_table == 'scr':
        return _to_screencode(ch) & 0xFF
    return ord(ch) & 0xFF


def _expand_includes(source: str, source_dir, visited: set,
                       depth: int = 0) -> str:
    """Recursively inline ACME/KickAss/CA65 include directives.
    Recognised forms (case-insensitive, with or without whitespace
    before the quote):
        !source "file"
        !src    "file"
        .import source "file"
        #import "file"
        .include "file"
        !include "file"
    File path is resolved relative to `source_dir`. Cycles are
    suppressed via the `visited` set; depth is capped to 16."""
    from pathlib import Path as _P
    if depth >= 16:
        return source   # avoid runaway recursion
    out_lines = []
    for raw in source.splitlines():
        stripped = raw.strip()
        # Match directive that starts the line (allow leading
        # whitespace) and is followed by an optional 'source' keyword
        # (for .import) and a quoted filename.
        m = re.match(
            r'\s*(?:!source|!src|!include|\.include|\.import\s+source|#import|#importonce)'
            r'\s*"([^"]+)"',
            stripped, re.IGNORECASE)
        if not m:
            out_lines.append(raw)
            continue
        fname = m.group(1)
        target = (source_dir / fname).resolve()
        if str(target) in visited:
            out_lines.append(f'; [skipped recursive include: {fname}]')
            continue
        if not target.exists():
            out_lines.append(f'; [include not found: {fname}]')
            continue
        try:
            inner = target.read_text(encoding='utf-8', errors='replace')
        except Exception:
            try:
                inner = target.read_text(encoding='latin-1', errors='replace')
            except Exception as e:
                out_lines.append(f'; [include read error: {fname} - {e}]')
                continue
        # Strip block comments before recursing
        inner = _strip_block_comments(inner)
        new_visited = visited | {str(target)}
        inner = _expand_includes(
            inner, target.parent, new_visited, depth + 1)
        out_lines.append(f'; [BEGIN include: {fname}]')
        out_lines.extend(inner.splitlines())
        out_lines.append(f'; [END include: {fname}]')
    return '\n'.join(out_lines)


# =====================================================================
# Phase 3: Functions, Namespaces, Scopes, Pseudo-Commands
# =====================================================================
# These features require infrastructure that goes beyond simple text
# substitution. We add:
#   - A function registry (.function NAME(args) { ... return expr })
#     callable from any expression as `NAME(arg1, arg2)`.
#   - A namespace stack: while inside `.namespace NAME { ... }`, labels
#     defined are stored as `NAME.label`.
#   - Pseudo-commands which look like opcodes but expand to a body
#     (KickAss `.pseudocommand`).
#   - `.weak` symbols that can be overridden.

# Module-level registries. assemble() resets them at start of each call.
_USER_FUNCTIONS = {}
_PSEUDO_COMMANDS = {}
_WEAK_SYMBOLS = set()


def _lookup_symbol(name, symbols, ns_stack):
    """Look up a symbol respecting namespace nesting.
    Search order:
       1. Currently fully-qualified scope:  ns_stack . name
       2. Each enclosing scope, peeling one level off
       3. Global (no prefix)
    Returns the value or raises KeyError if not found."""
    if not name:
        raise KeyError(name)
    # Fully-qualified name with dots: try it directly first
    if '.' in name:
        if name in symbols:
            return symbols[name]
        # Also try the name relative to current scope
        for i in range(len(ns_stack), -1, -1):
            prefix = '.'.join(ns_stack[:i])
            cand = (prefix + '.' + name) if prefix else name
            if cand in symbols:
                return symbols[cand]
        raise KeyError(name)
    # Unqualified - peel back through the namespace stack
    for i in range(len(ns_stack), -1, -1):
        prefix = '.'.join(ns_stack[:i])
        cand = (prefix + '.' + name) if prefix else name
        if cand in symbols:
            return symbols[cand]
    raise KeyError(name)


def _evaluate_user_function(fname, args_str, symbols, pc):
    """Evaluate a user-defined function call. Returns the int result.
    Returns None if `fname` is not a registered function."""
    if fname not in _USER_FUNCTIONS:
        return None
    body_args, body_expr = _USER_FUNCTIONS[fname]
    # Split args (respecting nested parens / quotes)
    args = []; cur = ''; pd = 0; iq = None
    for ch in args_str:
        if iq:
            cur += ch
            if ch == iq: iq = None
            continue
        if ch in '"\'':
            iq = ch; cur += ch; continue
        if ch == '(': pd += 1; cur += ch; continue
        if ch == ')': pd -= 1; cur += ch; continue
        if ch == ',' and pd == 0:
            args.append(cur); cur = ''; continue
        cur += ch
    if cur.strip(): args.append(cur)
    if len(args) != len(body_args):
        raise AssemblerError(
            f"function {fname!r}: expected {len(body_args)} args, "
            f"got {len(args)}")
    # Bind parameters in a local symbol map and evaluate the body
    local_syms = dict(symbols)
    for pname, pval in zip(body_args, args):
        local_syms[pname] = _eval_expr(pval, symbols, pc)
    return _eval_expr(body_expr, local_syms, pc)


# =====================================================================
# Preprocessor: macros, loops, conditionals, anonymous labels
# =====================================================================
# Runs BEFORE the two-pass assembler. Expands macros, repeat/for
# blocks, evaluates conditional assembly, and rewrites anonymous
# labels into unique names. The output is a flat source text that the
# main assembler consumes.

# Limit how many lines a macro/loop expansion may produce. Without
# this, a buggy `.for i=0, i<99999, i+=1 { }` would lock the UI.
_MAX_EXPANDED_LINES = 200000


class _PreprocError(AssemblerError):
    pass


def _split_source_lines(source):
    """Split into a list of (orig_lineno, raw_line) pairs. Preserves
    blank lines."""
    return list(enumerate(source.splitlines(), 1))


def _extract_functions(source):
    """Find `.function NAME(args) { .return expr }` definitions and
    register them in _USER_FUNCTIONS, removing the def from source.
    KickAss-style only - 64tass functions use a different flow.

    The body must be a single expression (after `.return`); functions
    with multiple statements aren't supported by this simple
    implementation."""
    func_re = re.compile(
        r'^\s*\.function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*\{',
        re.MULTILINE)
    while True:
        m = func_re.search(source)
        if m is None: break
        name = m.group(1)
        params_raw = m.group(2).strip()
        params = [p.strip() for p in params_raw.split(',') if p.strip()]
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f".function {name!r}: missing closing brace")
        body = source[brace_pos+1:end]
        # Look for `.return expr`
        ret_m = re.search(r'\.return\s+(.+)', body)
        if ret_m:
            body_expr = ret_m.group(1).strip().rstrip(';')
            # Strip trailing semicolons and comments
            if ';' in body_expr:
                body_expr = body_expr[:body_expr.index(';')].strip()
        else:
            # No return - assume the entire body is one expression
            body_expr = body.strip().splitlines()[0].strip() if body.strip() else '0'
        _USER_FUNCTIONS[name] = (params, body_expr)
        # Remove from source, preserve line count
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + '\n' * n_lines + after
    return source


_STRUCTS = {}    # name -> [(field_name, field_size_bytes), ...]


def _extract_structs(source):
    """Find KickAss `.struct NAME { .word a; .byte b }` definitions
    and register them. The struct body is a list of `.byte` / `.word`
    declarations with a single name each. Each field becomes a
    (name, size) entry. Total size of the struct is the sum.

    Instantiation is handled separately by the `.dstruct` directive
    in the main parser."""
    s_re = re.compile(
        r'^\s*\.struct\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{', re.MULTILINE)
    while True:
        m = s_re.search(source)
        if m is None: break
        name = m.group(1)
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f".struct {name!r}: missing closing brace")
        body = source[brace_pos+1:end]
        fields = []
        for ln in body.splitlines():
            ln = _strip_comment(ln).strip()
            if not ln: continue
            ln = ln.rstrip(';').strip()
            tokens = ln.split(None, 1)
            if len(tokens) < 2: continue
            kind = tokens[0].lower()
            field_name = tokens[1].strip().split(',')[0].strip()
            if kind in ('.byte', '.db', '.char'):
                size = 1
            elif kind in ('.word', '.wo', '.int', '.sint', '.addr'):
                size = 2
            elif kind in ('.long', '.lint'):
                size = 3
            elif kind in ('.dword', '.dw', '.dint'):
                size = 4
            else:
                continue
            fields.append((field_name, size))
        _STRUCTS[name] = fields
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + '\n' * n_lines + after
    return source


def _extract_pseudo_commands(source):
    """Find KickAss `.pseudocommand NAME args { body }` definitions.
    A pseudo-command looks and is called like an opcode, but expands
    to its body when invoked.

    Calls are matched as the FIRST whitespace-separated token of a
    line and replaced with the body, with parameter substitution."""
    pc_re = re.compile(
        r'^\s*\.pseudocommand\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
        r'(?:([^{]*?))?\s*\{', re.MULTILINE)
    while True:
        m = pc_re.search(source)
        if m is None: break
        name = m.group(1)
        params_raw = (m.group(2) or '').strip()
        params = []
        if params_raw:
            for p in params_raw.split(','):
                p = p.strip().lstrip(':')
                if p:
                    params.append(p)
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f".pseudocommand {name!r}: missing brace")
        body = source[brace_pos+1:end].splitlines()
        _PSEUDO_COMMANDS[name] = (params, body)
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + '\n' * n_lines + after
    if not _PSEUDO_COMMANDS:
        return source
    # Now expand calls: lines that begin with a known pseudo-command
    # name, optionally followed by args.
    name_alt = '|'.join(re.escape(n) for n in _PSEUDO_COMMANDS)
    out = []
    for raw in source.splitlines():
        m = re.match(r'^(\s*)(' + name_alt + r')\b\s*(.*)$', raw)
        if m:
            indent = m.group(1)
            name = m.group(2)
            args_raw = m.group(3).strip()
            params, body = _PSEUDO_COMMANDS[name]
            args = _split_args(args_raw) if args_raw else []
            for body_ln in body:
                line = body_ln
                # Substitute :param with arg value
                for pname, pval in zip(params, args):
                    line = re.sub(
                        r':' + re.escape(pname) + r'\b', pval, line)
                    line = re.sub(
                        r'\b' + re.escape(pname) + r'\b', pval, line)
                out.append(indent + line)
            continue
        out.append(raw)
    return '\n'.join(out)



    """Find KickAss `.pseudocommand NAME args { body }` definitions.
    A pseudo-command looks and is called like an opcode, but expands
    to its body when invoked.

    Calls are matched as the FIRST whitespace-separated token of a
    line and replaced with the body, with parameter substitution."""
    pc_re = re.compile(
        r'^\s*\.pseudocommand\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
        r'(?:([^{]*?))?\s*\{', re.MULTILINE)
    while True:
        m = pc_re.search(source)
        if m is None: break
        name = m.group(1)
        params_raw = (m.group(2) or '').strip()
        params = []
        if params_raw:
            for p in params_raw.split(','):
                p = p.strip().lstrip(':')
                if p:
                    params.append(p)
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f".pseudocommand {name!r}: missing brace")
        body = source[brace_pos+1:end].splitlines()
        _PSEUDO_COMMANDS[name] = (params, body)
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + '\n' * n_lines + after
    if not _PSEUDO_COMMANDS:
        return source
    # Now expand calls: lines that begin with a known pseudo-command
    # name, optionally followed by args.
    name_alt = '|'.join(re.escape(n) for n in _PSEUDO_COMMANDS)
    out = []
    for raw in source.splitlines():
        m = re.match(r'^(\s*)(' + name_alt + r')\b\s*(.*)$', raw)
        if m:
            indent = m.group(1)
            name = m.group(2)
            args_raw = m.group(3).strip()
            params, body = _PSEUDO_COMMANDS[name]
            args = _split_args(args_raw) if args_raw else []
            for body_ln in body:
                line = body_ln
                # Substitute :param with arg value
                for pname, pval in zip(params, args):
                    line = re.sub(
                        r':' + re.escape(pname) + r'\b', pval, line)
                    line = re.sub(
                        r'\b' + re.escape(pname) + r'\b', pval, line)
                out.append(indent + line)
            continue
        out.append(raw)
    return '\n'.join(out)


def _extract_weak_symbols(source):
    """Find `.weak NAME = value` definitions and register them.
    These produce normal labels but with a flag that allows them to
    be silently overridden by a later non-weak definition."""
    out = []
    for line in source.splitlines():
        m = re.match(
            r'^\s*\.weak\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*(?:;.*)?$',
            line, re.IGNORECASE)
        if m:
            # Flag the symbol as weak by recording its name
            _WEAK_SYMBOLS.add(m.group(1))
            # Replace with a normal equate so pass 1 picks it up
            out.append(f'{m.group(1)} = {m.group(2)}')
        else:
            out.append(line)
    return '\n'.join(out)


def _preprocess(source: str, source_dir=None):
    """Run all pre-assembly passes and return the expanded source as
    a string. Each pass is independent and self-contained:

      1. Strip /* */ block comments
      2. Expand `!src` / `.include` / `#import`
      3. Track re-assignable variables (`.var x=`, `!set x=`, `x:=`)
         - replace their occurrences in subsequent code lines
      4. Collect macros (ACME `!macro N { }`, KickAss `.macro N() { }`,
         64tass `.macro N` ... `.endm`) and expand calls (`+N`, `:N()`,
         `#N`)
      5. Expand `.rept N { body }` / `!rept N { body }`
      6. Expand `.for var=START, var<END, var+=STEP { body }` and
         ACME `!for var, START, END { body }`
      7. Evaluate `.if expr { body } [.else { body }] (.endif)` and
         `!if expr { body } else { body }`
      8. Rename anonymous labels `+`/`-`/`++`/`--` to unique names

    All passes use simple text-based expansion - no AST involved.
    Multiple passes run iteratively until the source stabilises so
    that, e.g., a macro call inside a .for loop can be expanded.
    """
    source = _strip_block_comments(source)
    if source_dir is not None:
        source = _expand_includes(source, source_dir, set())

    # Extract user-defined functions and pseudo-commands BEFORE macros,
    # so they're available for use within macro bodies. These get
    # registered globally so _eval_expr can find them.
    source = _extract_functions(source)
    source = _extract_structs(source)
    source = _extract_pseudo_commands(source)
    source = _extract_weak_symbols(source)

    # Run the expansion pipeline up to N times until stable. Most
    # sources stabilise in 1-2 passes; we cap at 8 to catch macros
    # invoking macros invoking macros etc.
    last = None
    for _i in range(8):
        s2 = source
        s2 = _expand_macros(s2)
        s2 = _expand_rept(s2)
        s2 = _expand_for(s2)
        s2 = _expand_if(s2)
        s2 = _resolve_vars(s2)
        if s2 == source:
            break
        if s2 == last:
            break   # oscillation - bail out
        last = source
        source = s2

    source = _rename_anonymous_labels(source)
    return source


# =====================================================================
# Re-assignable variables: .var, !set, := operator
# =====================================================================
# 64tass:    var := value      ; or  .var var = value
# KickAss:   .var name = value
# ACME:      !set name = value
#
# We do a single forward walk: each definition records the new value;
# uses BEFORE the definition are not affected; uses AFTER the
# definition (until the next re-assignment) are textually substituted.

def _resolve_vars(source):
    """Walk the source linearly, tracking re-assignable variables and
    substituting their current value into subsequent code lines."""
    var_re = re.compile(
        r'^\s*(?:\.var|!set)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*(?:;.*)?$',
        re.IGNORECASE)
    # 64tass `name := expr` or `name = expr` (without .var prefix)
    # We can't generally substitute every `=` because real equates
    # use that too; only the `:=` form is unambiguously re-assignable.
    var_re_walrus = re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(.+?)\s*(?:;.*)?$')
    out = []
    current_vars = {}   # name -> string-value
    for line in source.splitlines():
        m = var_re.match(line) or var_re_walrus.match(line)
        if m:
            name = m.group(1)
            val_expr = m.group(2).strip()
            # Try to evaluate now; if it depends on labels we don't
            # know yet, we keep the expression text and substitute
            # later (the actual assembler will resolve at pass 2).
            try:
                val = _eval_expr_const(val_expr)
                current_vars[name] = str(val)
            except Exception:
                # Could be a label - store the expression as-is so
                # later uses get textual substitution.
                current_vars[name] = '(' + val_expr + ')'
            # Replace the definition line with a comment so the
            # assembler doesn't try to define a real symbol.
            out.append(f'; [var {name} = {val_expr}]')
            continue
        # Substitute occurrences of known variables. Wrap the
        # substituted text in parens so operator precedence stays sane.
        if current_vars:
            new_line = line
            for vname, vval in current_vars.items():
                # Only substitute as a whole-word identifier, not
                # inside `.macro foo` etc. (those are already gone by
                # the time we get here, but be safe.)
                new_line = re.sub(
                    r'\b' + re.escape(vname) + r'\b', vval, new_line)
            out.append(new_line)
        else:
            out.append(line)
    return '\n'.join(out)


def _stub_to_force_indent():
    """Placeholder so the previous comment block doesn't merge."""
    return None


# =====================================================================
# 1. Macro definitions and expansion
# =====================================================================
# We support three styles:
#
# ACME:
#     !macro NAME { ... }
#     !macro NAME .arg1, .arg2 { ... }
#     +NAME              ; call (no args)
#     +NAME 1, 2         ; call (with args)
#
# KickAssembler:
#     .macro NAME() { ... }
#     .macro NAME(arg1, arg2) { ... }
#     :NAME()            ; explicit call form
#     NAME()             ; implicit call (we accept both)
#
# 64tass:
#     .macro NAME              ; args via \1 \2 \3 ... \@
#         lda #\1
#     .endm
#     #NAME 1, 2         ; call form (# prefix)
#     NAME 1, 2          ; also accepted

_MACRO_RE_ACME = re.compile(
    r'^\s*!macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
    r'(?:([^{]*?))?\s*\{', re.MULTILINE)
_MACRO_RE_KICKASS = re.compile(
    r'^\s*\.macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*\{',
    re.MULTILINE)
_MACRO_RE_64TASS = re.compile(
    r'^\s*\.macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*$', re.MULTILINE)


def _find_brace_block_end(text, start_idx):
    """Given the index of an opening `{`, find the matching `}`.
    Respects string literals. Returns the index of the closing brace,
    or -1 if not found."""
    depth = 0
    in_q = None
    i = start_idx
    while i < len(text):
        ch = text[i]
        if in_q:
            if ch == in_q: in_q = None
            elif ch == '\\' and i + 1 < len(text):
                i += 2; continue
        elif ch in '"\'':
            in_q = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_word_block_end(lines, start_idx, end_kw):
    """For 64tass-style `.macro ... .endm`: scan lines starting at
    start_idx looking for the matching end keyword (case-insensitive).
    Returns the index of the line with the end keyword, or -1."""
    end_kws = end_kw if isinstance(end_kw, (set, tuple, list)) else {end_kw}
    end_kws = {k.lower() for k in end_kws}
    for j in range(start_idx, len(lines)):
        first = lines[j].strip().split(None, 1)
        if first and first[0].lower() in end_kws:
            return j
    return -1


def _expand_macros(source):
    """Locate macro definitions, remove them from the source, then
    expand all calls. Returns the rewritten source."""
    macros = {}   # name -> (params: list[str], body_lines: list[str], style)

    # ---- ACME `!macro NAME [params] { ... }` ----
    while True:
        m = _MACRO_RE_ACME.search(source)
        if m is None: break
        name = m.group(1)
        params_raw = (m.group(2) or '').strip()
        # Strip leading '.' from each param (ACME convention `.arg1`)
        params = []
        if params_raw:
            for p in params_raw.split(','):
                p = p.strip().lstrip('.')
                if p:
                    params.append(p)
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f"macro {name!r}: missing closing brace")
        body = source[brace_pos+1:end]
        macros[name] = (params, body.splitlines(), 'acme')
        # Remove the entire definition from the source; replace with
        # blank lines so line numbers don't shift much.
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + ('\n' * n_lines) + after

    # ---- KickAss `.macro NAME(args) { ... }` ----
    while True:
        m = _MACRO_RE_KICKASS.search(source)
        if m is None: break
        name = m.group(1)
        params_raw = m.group(2).strip()
        params = [p.strip() for p in params_raw.split(',') if p.strip()]
        brace_pos = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace_pos)
        if end < 0:
            raise _PreprocError(f"macro {name!r}: missing closing brace")
        body = source[brace_pos+1:end]
        macros[name] = (params, body.splitlines(), 'kickass')
        before = source[:m.start()]
        after = source[end+1:]
        n_lines = source[m.start():end+1].count('\n')
        source = before + ('\n' * n_lines) + after

    # ---- 64tass `.macro NAME` ... `.endm` ----
    lines = source.splitlines()
    out_lines = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _MACRO_RE_64TASS.match(ln)
        if m is not None:
            name = m.group(1)
            end_idx = _find_word_block_end(lines, i + 1, {'.endm'})
            if end_idx < 0:
                raise _PreprocError(f"macro {name!r}: missing .endm")
            body_lines = lines[i+1:end_idx]
            macros[name] = ([], body_lines, '64tass')
            # Skip definition; replace with blank lines
            for _k in range(end_idx - i + 1):
                out_lines.append('')
            i = end_idx + 1
            continue
        out_lines.append(ln)
        i += 1
    source = '\n'.join(out_lines)

    if not macros:
        return source

    # ---- Now expand calls ----
    # Build a regex that matches any of our macro names as a call.
    # Three call forms:
    #   ACME:     ^\s*\+NAME(\s+args)?$
    #   KickAss:  ^\s*:?NAME\((args)\)$    (also bare NAME() on a line)
    #   64tass:   ^\s*#?NAME(\s+args)?$
    #
    # Collisions between these are resolved by trying ACME first
    # (must have +), then KickAss (parens required), then 64tass.
    # Plain `NAME args` only matches if NAME is a known macro.
    name_alt = '|'.join(re.escape(n) for n in macros)
    if not name_alt:
        return source
    out_lines = []
    line_count = 0
    for raw in source.splitlines():
        # ACME call: +NAME [args]
        m = re.match(r'^\s*\+(' + name_alt + r')\b\s*(.*)$', raw)
        if m and m.group(1) in macros:
            name = m.group(1); args_raw = m.group(2).strip()
            expansion = _instantiate_macro(name, args_raw, macros)
            out_lines.extend(expansion)
            line_count += len(expansion)
            if line_count > _MAX_EXPANDED_LINES:
                raise _PreprocError("macro expansion blew up (>200k lines)")
            continue
        # KickAss call: :NAME(args)  or  NAME(args) at line start
        m = re.match(r'^\s*:?(' + name_alt + r')\s*\(([^)]*)\)\s*$', raw)
        if m and m.group(1) in macros:
            name = m.group(1); args_raw = m.group(2).strip()
            expansion = _instantiate_macro(name, args_raw, macros)
            out_lines.extend(expansion)
            line_count += len(expansion)
            if line_count > _MAX_EXPANDED_LINES:
                raise _PreprocError("macro expansion blew up (>200k lines)")
            continue
        # 64tass call: #NAME args   (with explicit #)
        m = re.match(r'^\s*#(' + name_alt + r')\b\s*(.*)$', raw)
        if m and m.group(1) in macros:
            name = m.group(1); args_raw = m.group(2).strip()
            expansion = _instantiate_macro(name, args_raw, macros)
            out_lines.extend(expansion)
            line_count += len(expansion)
            continue
        out_lines.append(raw)
    return '\n'.join(out_lines)


def _split_macro_args(s):
    """Split a macro argument list on commas, respecting string
    literals and parentheses. Returns a list of trimmed argument
    strings."""
    return _split_args(s)


def _instantiate_macro(name, args_raw, macros):
    """Substitute parameters in the macro body and return the
    expanded body lines."""
    params, body, style = macros[name]
    args = _split_macro_args(args_raw) if args_raw else []
    out = []
    for body_line in body:
        line = body_line
        if style == 'acme':
            # ACME: substitute each .arg name with the actual argument
            for pname, pval in zip(params, args):
                # Whole-word replace, prefixed with `.` (typical) and bare
                line = re.sub(r'\.' + re.escape(pname) + r'\b', pval, line)
                line = re.sub(r'\b' + re.escape(pname) + r'\b', pval, line)
        elif style == 'kickass':
            # KickAss: \name (or just name) substitution
            for pname, pval in zip(params, args):
                line = re.sub(r'\\' + re.escape(pname) + r'\b', pval, line)
                line = re.sub(r'\b' + re.escape(pname) + r'\b', pval, line)
        elif style == '64tass':
            # 64tass: \1, \2, ... and \@
            for idx, val in enumerate(args, 1):
                line = line.replace(f'\\{idx}', val)
            line = line.replace('\\@', ', '.join(args))
        out.append(line)
    return out


# =====================================================================
# 2. .rept / !rept / .repeat - block repetition
# =====================================================================
# Forms:
#   .rept N
#       ...
#   .next   (or  .endrept)
#
#   !rept N { ... }      (ACME)
#   .repeat N { ... }    (KickAss-ish - we accept both)

def _expand_rept(source):
    """Expand .rept / !rept blocks. Each iteration expands without
    setting a loop variable; for a parameterised loop use .for."""
    # ACME / KickAss style with braces:  !rept N { body }  or
    #   .rept N { body }  / .repeat N { body }
    m_re = re.compile(
        r'^[ \t]*(?:!rept|\.rept|\.repeat)\s+([^\{\n]+?)\s*\{',
        re.MULTILINE | re.IGNORECASE)
    while True:
        m = m_re.search(source)
        if m is None: break
        count_expr = m.group(1).strip()
        try:
            n = int(_eval_expr_const(count_expr))
        except Exception:
            raise _PreprocError(
                f".rept count not a constant: {count_expr!r}")
        if n < 0: n = 0
        brace = source.index('{', m.end() - 1)
        end = _find_brace_block_end(source, brace)
        if end < 0:
            raise _PreprocError(".rept: missing closing brace")
        body = source[brace+1:end]
        repeated = (body + '\n') * n
        before = source[:m.start()]
        after = source[end+1:]
        n_lines_def = source[m.start():end+1].count('\n')
        source = (before + '\n' * n_lines_def + '\n' + repeated + after)
        if source.count('\n') > _MAX_EXPANDED_LINES:
            raise _PreprocError(".rept expansion too large")

    # 64tass style: .rept N ... .next
    lines = source.splitlines()
    out = []; i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r'^\s*(?:\.rept|\.repeat)\s+(.+?)\s*$', ln,
                      re.IGNORECASE)
        if m:
            count_expr = m.group(1).strip()
            try:
                n = int(_eval_expr_const(count_expr))
            except Exception:
                out.append(ln); i += 1; continue
            end_idx = _find_word_block_end(
                lines, i + 1, {'.next', '.endrept'})
            if end_idx < 0:
                raise _PreprocError(".rept missing .next / .endrept")
            body = lines[i+1:end_idx]
            for _ in range(n):
                out.extend(body)
            i = end_idx + 1
            continue
        out.append(ln); i += 1
    return '\n'.join(out)


# =====================================================================
# 3. .for / !for - parametric loops
# =====================================================================
# Forms:
#   ACME:    !for var, start, end { body }     ; iterates var from start to end inclusive
#   KickAss: .for (var i = 0; i < 10; i++) { body }    ; we support a limited subset
#   64tass:  .for var = start, var < end, var += step
#                ...
#            .next

def _expand_for(source):
    """Expand .for / !for loops with a single iterator variable."""
    # ACME: !for var, start, end { body }
    m_re = re.compile(
        r'^[ \t]*!for\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'
        r'([^,\n]+?)\s*,\s*([^\{\n]+?)\s*\{',
        re.MULTILINE | re.IGNORECASE)
    while True:
        m = m_re.search(source)
        if m is None: break
        var = m.group(1)
        try:
            start = int(_eval_expr_const(m.group(2).strip()))
            end = int(_eval_expr_const(m.group(3).strip()))
        except Exception:
            raise _PreprocError(f"!for bounds not constant: {m.group(0)}")
        brace = source.index('{', m.end() - 1)
        end_brace = _find_brace_block_end(source, brace)
        if end_brace < 0:
            raise _PreprocError("!for missing closing brace")
        body = source[brace+1:end_brace]
        out = []
        step = 1 if end >= start else -1
        v = start
        while (step > 0 and v <= end) or (step < 0 and v >= end):
            inst = re.sub(r'\b' + re.escape(var) + r'\b', str(v), body)
            out.append(inst)
            v += step
            if len(out) > _MAX_EXPANDED_LINES:
                raise _PreprocError("!for expansion too large")
        before = source[:m.start()]
        after = source[end_brace+1:]
        n_lines_def = source[m.start():end_brace+1].count('\n')
        source = (before + '\n' * n_lines_def + '\n'.join(out) + after)

    # KickAss-ish: .for (var i = X; i < Y; i++) { body }
    m_re = re.compile(
        r'^[ \t]*\.for\s*\(\s*var\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
        r'([^;]+?)\s*;\s*[A-Za-z_][A-Za-z0-9_]*\s*([<>=!]+)\s*'
        r'([^;]+?)\s*;\s*[A-Za-z_][A-Za-z0-9_]*(\+\+|--|[+\-]=\s*[^)]+)\s*\)\s*\{',
        re.MULTILINE | re.IGNORECASE)
    while True:
        m = m_re.search(source)
        if m is None: break
        var = m.group(1)
        try:
            start = int(_eval_expr_const(m.group(2).strip()))
            limit = int(_eval_expr_const(m.group(4).strip()))
        except Exception:
            raise _PreprocError(
                f".for bounds not constant: {m.group(0)}")
        op = m.group(3)
        step_str = m.group(5).strip()
        if step_str == '++': step = 1
        elif step_str == '--': step = -1
        elif step_str.startswith('+='):
            step = int(_eval_expr_const(step_str[2:].strip()))
        elif step_str.startswith('-='):
            step = -int(_eval_expr_const(step_str[2:].strip()))
        else: step = 1
        brace = source.index('{', m.end() - 1)
        end_brace = _find_brace_block_end(source, brace)
        if end_brace < 0:
            raise _PreprocError(".for missing closing brace")
        body = source[brace+1:end_brace]
        out = []
        v = start
        while True:
            if op == '<' and not (v < limit): break
            if op == '<=' and not (v <= limit): break
            if op == '>' and not (v > limit): break
            if op == '>=' and not (v >= limit): break
            if op == '!=' and not (v != limit): break
            if op == '==' and not (v == limit): break
            inst = re.sub(r'\b' + re.escape(var) + r'\b', str(v), body)
            out.append(inst)
            v += step
            if len(out) > _MAX_EXPANDED_LINES:
                raise _PreprocError(".for expansion too large")
        before = source[:m.start()]
        after = source[end_brace+1:]
        n_lines_def = source[m.start():end_brace+1].count('\n')
        source = (before + '\n' * n_lines_def + '\n'.join(out) + after)

    # 64tass: .for var = start, var < end, var += step  ...  .next
    lines = source.splitlines()
    out = []; i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(
            r'^\s*\.for\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,]+),\s*'
            r'[A-Za-z_][A-Za-z0-9_]*\s*([<>=!]+)\s*([^,]+),\s*'
            r'[A-Za-z_][A-Za-z0-9_]*\s*([+\-]=)\s*(.+?)\s*$',
            ln, re.IGNORECASE)
        if m:
            var = m.group(1)
            try:
                start = int(_eval_expr_const(m.group(2).strip()))
                limit = int(_eval_expr_const(m.group(4).strip()))
                step = int(_eval_expr_const(m.group(6).strip()))
                if m.group(5) == '-=':
                    step = -step
            except Exception:
                out.append(ln); i += 1; continue
            op = m.group(3)
            end_idx = _find_word_block_end(lines, i + 1, {'.next'})
            if end_idx < 0:
                raise _PreprocError(".for missing .next")
            body = lines[i+1:end_idx]
            v = start
            while True:
                if op == '<' and not (v < limit): break
                if op == '<=' and not (v <= limit): break
                if op == '>' and not (v > limit): break
                if op == '>=' and not (v >= limit): break
                if op == '!=' and not (v != limit): break
                if op == '==' and not (v == limit): break
                for body_ln in body:
                    out.append(
                        re.sub(r'\b' + re.escape(var) + r'\b',
                                str(v), body_ln))
                v += step
                if len(out) > _MAX_EXPANDED_LINES:
                    raise _PreprocError(".for expansion too large")
            i = end_idx + 1
            continue
        out.append(ln); i += 1
    return '\n'.join(out)


# =====================================================================
# 4. .if / !if - conditional assembly with body evaluation
# =====================================================================

def _expand_if(source):
    """Evaluate compile-time conditionals and emit only the chosen
    branch. Supported forms:
        .if expr { body }
        .if expr { body } else { body }     (KickAss)
        !if expr { body }
        !if expr { body } else { body }
        .ifdef NAME { body }   .ifndef NAME { body }
    """
    # KickAss / ACME brace-form
    m_re = re.compile(
        r'^[ \t]*(\.if|!if|\.ifdef|!ifdef|\.ifndef|!ifndef)\s+([^\{\n]+?)\s*\{',
        re.MULTILINE | re.IGNORECASE)
    while True:
        m = m_re.search(source)
        if m is None: break
        kind = m.group(1).lower()
        cond_expr = m.group(2).strip()
        brace = source.index('{', m.end() - 1)
        end_brace = _find_brace_block_end(source, brace)
        if end_brace < 0:
            raise _PreprocError(".if missing closing brace")
        true_body = source[brace+1:end_brace]
        # Look for `else { ... }` immediately after
        rest = source[end_brace+1:]
        else_match = re.match(r'\s*else\s*\{', rest)
        false_body = ''
        consumed_extra = 0
        if else_match:
            else_brace = end_brace + 1 + else_match.end() - 1
            else_end = _find_brace_block_end(source, else_brace)
            if else_end < 0:
                raise _PreprocError(".if .else missing closing brace")
            false_body = source[else_brace+1:else_end]
            consumed_extra = else_end - end_brace
        # Evaluate condition (compile-time constants only)
        try:
            if kind in ('.ifdef', '!ifdef', '.ifndef', '!ifndef'):
                # We don't have a symbol table at this point - treat
                # it conservatively: assume defined for any plain label.
                # This is a known limitation of pure preprocessing.
                cond_true = bool(cond_expr)   # always true for any name
                if 'ndef' in kind:
                    cond_true = not cond_true
            else:
                cond_true = bool(_eval_expr_const(cond_expr))
        except Exception:
            # Conservative: keep both branches if we can't evaluate
            # (the actual assembler will then complain about duplicate
            # labels etc., which is at least informative).
            chosen = true_body
            cond_true = True
        chosen = true_body if cond_true else false_body
        before = source[:m.start()]
        total_replaced_end = end_brace + consumed_extra + 1
        after = source[total_replaced_end:]
        n_lines = source[m.start():total_replaced_end].count('\n')
        source = (before + '\n' * n_lines + chosen + after)
    return source


# =====================================================================
# 5. Constant expression evaluator (no symbol table)
# =====================================================================
# Used during preprocessing where labels/symbols aren't known yet.
# Only handles literal numbers, *, /, %, +, -, parens, <, >, ~, &, |, ^
# with full precedence. Falls back to raising if a name is referenced.

def _eval_expr_const(expr):
    """Evaluate an expression that uses only numeric literals and
    operators (no labels). Raises ValueError if a name is found."""
    return _eval_expr(expr, {}, 0)


# =====================================================================
# 6. Anonymous labels: + and - become unique names
# =====================================================================
# In ACME and 64tass, `+` is a forward-reference cheap label and `-`
# is a backward-reference one. They can repeat freely in the source.
# Examples:
#   - lda #0
#     bne -        ; jump to nearest preceding `-`
#     bra +        ; jump to nearest following `+`
#   + sta foo
#
# We pre-rename every anonymous label to a globally unique name
# (`__anon_fwd_<n>` / `__anon_bwd_<n>`) and rewrite all references to
# point at the appropriate one.

def _rename_anonymous_labels(source):
    """Rewrite `+`/`-` anonymous labels to unique names and update
    references to them."""
    lines = source.splitlines()
    # First pass: collect positions of anonymous label DEFINITIONS
    # An anonymous label DEFINITION is a line that starts (after
    # optional whitespace) with one or more `+` or `-` and nothing
    # else, OR `+` followed by a colon, OR multi-`+` (`++` `+++`).
    fwd_positions = []   # list of (line_idx, depth)
    bwd_positions = []
    for i, ln in enumerate(lines):
        s = _strip_comment(ln).strip()
        # Must be just +s or -s, possibly followed by `:` or by
        # whitespace+instruction
        m = re.match(r'^([+\-]+)(?:\s|$|:)', s)
        if m and len(m.group(1)) == len(s.rstrip(':').split()[0] if s.split() else ''):
            sym = m.group(1)
            depth = len(sym)
            if sym[0] == '+':
                fwd_positions.append((i, depth))
            else:
                bwd_positions.append((i, depth))
    if not fwd_positions and not bwd_positions:
        return source

    # Generate unique names
    fwd_names = {(i, d): f'__anon_f_{i}_{d}' for i, d in fwd_positions}
    bwd_names = {(i, d): f'__anon_b_{i}_{d}' for i, d in bwd_positions}

    out = []
    for i, ln in enumerate(lines):
        s = _strip_comment(ln).strip()
        m = re.match(r'^([+\-]+)(\s|$|:)', s)
        if m and len(m.group(1)) == len(s.rstrip(':').split()[0] if s.split() else ''):
            sym = m.group(1)
            depth = len(sym)
            if sym[0] == '+':
                key = (i, depth)
                name = fwd_names[key]
            else:
                key = (i, depth)
                name = bwd_names[key]
            # Replace the leading + or - sequence with the name
            rest = s[len(sym):].lstrip(':').lstrip()
            new_line = f'{name}: {rest}' if rest else f'{name}:'
            out.append(new_line)
        else:
            # Rewrite operand references: `bne -` -> `bne __anon_b_X_1`
            # We look for an opcode followed by a + or - of varying length.
            new_line = ln
            new_line = re.sub(
                r'(\b[a-zA-Z]{3}\s+)(\++)',
                lambda mm: mm.group(1) + _resolve_fwd(
                    i, len(mm.group(2)), fwd_positions, fwd_names),
                new_line)
            new_line = re.sub(
                r'(\b[a-zA-Z]{3}\s+)(\-+)',
                lambda mm: mm.group(1) + _resolve_bwd(
                    i, len(mm.group(2)), bwd_positions, bwd_names),
                new_line)
            out.append(new_line)
    return '\n'.join(out)


def _resolve_fwd(line_idx, depth, fwd_positions, fwd_names):
    """Find the nearest forward `+`-label at the requested depth."""
    candidates = [p for p in fwd_positions if p[0] > line_idx and p[1] == depth]
    if not candidates:
        return f'__anon_f_unresolved_{line_idx}_{depth}'
    candidates.sort(key=lambda p: p[0])
    return fwd_names[candidates[0]]


def _resolve_bwd(line_idx, depth, bwd_positions, bwd_names):
    """Find the nearest backward `-`-label at the requested depth."""
    candidates = [p for p in bwd_positions if p[0] < line_idx and p[1] == depth]
    if not candidates:
        return f'__anon_b_unresolved_{line_idx}_{depth}'
    candidates.sort(key=lambda p: -p[0])
    return bwd_names[candidates[0]]


# =====================================================================
# Re-assignable variables (.var, !set, := ::= +=)
# =====================================================================
# These are tracked via a forward-walk: a `.var x = N` line sets `x`
# to N; subsequent uses of `x` get textually replaced with the literal
# N (until a re-assignment changes it).
#
# Re-assignable variables are generally used for compile-time
# counters and toggles; conservatively we only substitute when the
# value is a compile-time constant. References to forward-defined
# real labels are NOT substituted - those resolve in the assembler.

# Note: handled simply enough that it's done inline above as part of
# the main parser via the existing equate logic - re-assignable
# variables in the strict sense would require a separate symbol
# scope. Phase 1 leaves this for now.


def assemble(source: str, base_addr: int, source_dir=None):
    """Two-pass assembler with label support and ACME / KickAssembler
    syntax support.

    Optional `source_dir` is a Path the assembler uses to resolve
    `!source`, `!src`, `!binary`, `!bin`, `.import`, `.importbinary`,
    `#import` directives. If None, those directives are silently
    skipped.

    Returns a tuple (bytes, origin_address). The origin may differ
    from base_addr if the source contains `*=` or `.org` directives.
    """
    from pathlib import Path as _P
    if source_dir is not None and not isinstance(source_dir, _P):
        source_dir = _P(source_dir)

    # Reset module-level registries for a fresh assemble call. These
    # accumulate as the preprocessor encounters definitions.
    global _USER_FUNCTIONS, _PSEUDO_COMMANDS, _WEAK_SYMBOLS, _STRUCTS
    _USER_FUNCTIONS = {}
    _PSEUDO_COMMANDS = {}
    _WEAK_SYMBOLS = set()
    _STRUCTS = {}

    # Run the preprocessor: strips block comments, expands !src
    # includes, expands macros, resolves .rept / .for loops, evaluates
    # .if/.endif blocks, and renames anonymous labels (+/-).
    source = _preprocess(source, source_dir)

    raw_lines = source.splitlines()

    # -------- Pass 1: collect labels and compute sizes ---------------
    symbols = {}
    # Namespace stack for scoped label qualification. While this stack
    # has entries, every label gets the dotted prefix of all entries.
    # E.g. inside `.namespace Foo { ... }`, label `bar` becomes `Foo.bar`.
    ns_stack = []
    pc = base_addr
    origin = base_addr
    seen_origin = False
    # ACME-style text conversion table for !text / !tx / .text / .asc.
    # Default is 'raw' (no conversion). !pet and !scr always override.
    conv_table = 'raw'
    parsed = []   # per-line: (lineno, raw, kind, payload, pc)
                  # kind in {'instr','data','label','equ','origin','blank'}

    # Block-skip state.
    # Two parallel systems run in tandem:
    #   skip_depth   -> KickAssembler {...} brace nesting
    #   word_depth   -> 64tass / TASM word-pair balance (e.g. .proc .. .pend)
    # While EITHER is > 0 we skip the line entirely.
    skip_depth = 0
    word_depth = 0

    # Helper: register a label honouring the namespace stack and
    # respecting weak-symbol override semantics. Returns the
    # qualified name actually stored.
    def _set_label(name, value, lineno, raw):
        qualified = ('.'.join(ns_stack) + '.' + name) if ns_stack else name
        if qualified in symbols:
            if symbols[qualified] == value:
                return qualified
            # Weak: real definition silently replaces weak one
            if qualified in _WEAK_SYMBOLS:
                _WEAK_SYMBOLS.discard(qualified)
                symbols[qualified] = value
                return qualified
            raise AssemblerError(
                f"line {lineno}: duplicate label {qualified!r} "
                f"(was ${symbols[qualified]:04X}, "
                f"now ${value:04X})\n  > {raw}")
        symbols[qualified] = value
        return qualified

    BLOCK_SKIP_DIRECTIVES = (
        # KickAss directives that open a `{ ... }` block:
        '.macro', '.pseudocommand', '.filenamespace',
        '.function', '.struct', '.enum', '.define',
        '.if', '.for', '.while', '.do', '.modify',
        '.segmentdef', '.segment',
        # ACME directives that open with `{` or otherwise need skipping:
        '!macro', '!for', '!do', '!if', '!ifdef', '!zone',
        '!source', '!src', '!pseudopc',
    )

    # KickAss `.namespace NAME { ... }` is special: body assembles as
    # normal, but labels inside get the `NAME.` prefix. We track this
    # via a separate stack synced with brace depth at line of opening.
    BRACE_SCOPE_OPENERS = ('.namespace', '.proc', '.block')
    # Stack of (brace_depth_at_open, scope_name)
    brace_ns_stack = []

    # 64tass / Turbo-Assembler word-pair openers. Two flavours:
    #
    # SCOPE_WRAPPERS - just open a label scope. The code INSIDE is
    # assembled normally; only the open/close keywords are skipped.
    # We track these separately because we DON'T want to skip body
    # lines, just absorb the framing words.
    #
    # CODE_SKIP_BLOCKS - the body is NOT assembled (e.g. .macro
    # definitions, .if false, .virtual stack-only blocks). When we
    # see one of these, we enter skip mode until the matching closer.
    SCOPE_WRAPPERS = {
        '.proc':       {'.pend'},
        '.block':      {'.bend'},
        '.section':    {'.send'},
        '.namespace':  {'.endn'},
        '.logical':    {'.here'},
    }
    CODE_SKIP_BLOCKS = {
        '.struct':     {'.endstruct', '.ends'},
        '.union':      {'.endu'},
        '.macro':      {'.endm'},
        '.segment':    {'.endsegment'},
        '.function':   {'.endf'},
        '.virtual':    {'.endv'},
        '.comment':    {'.endcomment', '.encomment'},
        '.switch':     {'.endswitch'},
        '.for':        {'.next'},
        '.rept':       {'.next', '.endrept'},
        '.repeat':     {'.next', '.endrept'},
        '.while':      {'.endw', '.next'},
        '.if':         {'.fi', '.endif', '.endc'},
        '.ifeq':       {'.fi', '.endif', '.endc'},
        '.ifne':       {'.fi', '.endif', '.endc'},
        '.ifmi':       {'.fi', '.endif', '.endc'},
        '.ifpl':       {'.fi', '.endif', '.endc'},
        '.ifdef':      {'.fi', '.endif', '.endc'},
        '.ifndef':     {'.fi', '.endif', '.endc'},
        # 64tass weak symbol overrides - body is assembled like normal
        # but conflicting symbols don't error; we treat the whole block
        # as silent-skip for simplicity.
        '.weak':       {'.endweak'},
        '.encode':     {'.endencode'},
    }
    SCOPE_OPENERS = set(SCOPE_WRAPPERS)
    SCOPE_CLOSERS = set()
    for v in SCOPE_WRAPPERS.values():
        SCOPE_CLOSERS |= v
    WORD_BLOCK_OPENERS = dict(CODE_SKIP_BLOCKS)
    WORD_BLOCK_CLOSERS = set()
    for v in WORD_BLOCK_OPENERS.values():
        WORD_BLOCK_CLOSERS |= v

    def _count_braces(line):
        """Count open/close braces in a line, ignoring those inside
        string literals."""
        op = cl = 0
        in_q = None
        for ch in line:
            if in_q:
                if ch == in_q: in_q = None
                continue
            if ch in '"\'':
                in_q = ch; continue
            if ch == '{': op += 1
            elif ch == '}': cl += 1
        return op, cl

    for lineno, raw in enumerate(raw_lines, 1):
        src = _strip_comment(raw).strip()
        if not src:
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # If we're currently skipping a block, just track depth changes
        # and continue. Both brace and word systems can be active.
        if skip_depth > 0 or word_depth > 0:
            op, cl = _count_braces(src)
            skip_depth = max(0, skip_depth + op - cl)
            # Word-based open/close detection for the current line
            first_word_lc = src.split(None, 1)[0].lower()
            if first_word_lc in WORD_BLOCK_OPENERS:
                word_depth += 1
            elif first_word_lc in WORD_BLOCK_CLOSERS:
                word_depth = max(0, word_depth - 1)
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # Detect a block-skip directive. Even if the directive does not
        # have its `{` on the same line, the next non-blank line might.
        # KickAss-style block-opener detection: only entered if the
        # line actually contains a `{`, otherwise let WORD_BLOCK_OPENERS
        # handle it (64tass/TASS-style word-pair). This avoids matching
        # `.if 0` (64tass) against KickAss-style `.if cond { ... }`.
        first_word = src.split(None, 1)[0].lower()

        # KickAss `.namespace NAME { ... }` (and similar `.proc`/`.block`
        # in KickAss flavor with braces) - body assembled normally, but
        # we push a namespace prefix that gets prepended to label names.
        if first_word in BRACE_SCOPE_OPENERS and '{' in src:
            tail = src.split(None, 2)
            scope_name = (tail[1].strip().rstrip('{').strip()
                            if len(tail) > 1 else f'__anon_{lineno}')
            if not scope_name or not (scope_name[0].isalpha() or
                                        scope_name[0] == '_'):
                scope_name = f'__anon_{lineno}'
            op, cl = _count_braces(src)
            ns_stack.append(scope_name)
            # Record the brace-depth at which this scope opened so we
            # know when to pop. We track via a sentinel pushed for each
            # additional brace.
            brace_ns_stack.append((skip_depth + op - cl, scope_name))
            # Note: we don't increase skip_depth - body is assembled.
            # But we DO still need to track close braces. We add a
            # parallel depth counter for namespaces only.
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # If we have open brace-namespaces, watch for the closing braces
        # so we can pop the namespace stack. We do this by counting net
        # closing braces on each line and popping when we hit our depth.
        if brace_ns_stack:
            op, cl = _count_braces(src)
            net = cl - op    # positive if closing
            while net > 0 and brace_ns_stack:
                # Pop one namespace per net close brace
                _, name = brace_ns_stack.pop()
                if ns_stack and ns_stack[-1] == name:
                    ns_stack.pop()
                net -= 1

        if first_word in BLOCK_SKIP_DIRECTIVES:
            op, cl = _count_braces(src)
            if op > 0 or cl > 0:
                # KickAss-style {...} block
                skip_depth += op - cl
                if skip_depth < 0: skip_depth = 0
                parsed.append((lineno, raw, 'blank', None, pc))
                continue
            # No braces on this line - might be a 64tass word-pair
            # block, fall through to WORD_BLOCK_OPENERS check below.

        # 64tass / Turbo-Assembler word-pair block markers. Unlike
        # KickAss these don't use `{ ... }` - they use named end words.
        # SCOPE openers/closers (.proc, .block, .section, .namespace,
        # .logical) are just skipped at code level - their body
        # assembles normally - but we DO track the namespace stack so
        # that labels defined inside get qualified names.
        if first_word in SCOPE_OPENERS:
            # Try to grab the scope name from the rest of the line
            tail = src.split(None, 1)
            if len(tail) > 1 and first_word in (
                    '.proc', '.block', '.namespace', '.section',
                    '!zone'):
                scope_name = tail[1].strip().split()[0]
                # Strip trailing punctuation that ACME/KickAss allow
                scope_name = scope_name.rstrip(':').rstrip('{').strip()
                if scope_name and (scope_name[0].isalpha() or
                                    scope_name[0] == '_'):
                    ns_stack.append(scope_name)
                else:
                    # Anonymous scope - use a generated name so labels
                    # inside still don't collide globally
                    ns_stack.append(f'__anon_scope_{lineno}')
            else:
                ns_stack.append(f'__anon_scope_{lineno}')
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if first_word in SCOPE_CLOSERS:
            if ns_stack:
                ns_stack.pop()
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if first_word in WORD_BLOCK_OPENERS:
            word_depth += 1
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if first_word in WORD_BLOCK_CLOSERS:
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # Origin directive: *=$0801    ACME / KickAss
        #                   *=$0801 "Block name"   KickAss with named block
        #                   .org $0801            CA65 / DASM
        #                   .pc = $1000           KickAss legacy notation
        if src.lstrip().startswith('*'):
            rest = src.lstrip()[1:].lstrip()
            if rest.startswith('='):
                addr_str = rest[1:].strip()
                # KickAss allows '*=$1000 "Block name"' - drop the
                # quoted block name; we don't track named blocks.
                if '"' in addr_str:
                    addr_str = addr_str[:addr_str.index('"')].strip()
                try:
                    addr = _eval_expr(addr_str, symbols, pc)
                except AssemblerError as e:
                    raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                pc = addr
                if not seen_origin:
                    origin = addr
                    seen_origin = True
                parsed.append((lineno, raw, 'origin', addr, pc))
                continue
        if src.lower().startswith('.org'):
            addr_str = src[4:].strip()
            try:
                addr = _eval_expr(addr_str, symbols, pc)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
            pc = addr
            if not seen_origin:
                origin = addr
                seen_origin = True
            parsed.append((lineno, raw, 'origin', addr, pc))
            continue
        # KickAss legacy notation: .pc = $1000
        if src.lower().startswith('.pc'):
            rest = src[3:].lstrip()
            if rest.startswith('='):
                addr_str = rest[1:].strip()
                if '"' in addr_str:
                    addr_str = addr_str[:addr_str.index('"')].strip()
                try:
                    addr = _eval_expr(addr_str, symbols, pc)
                except AssemblerError as e:
                    raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                pc = addr
                if not seen_origin:
                    origin = addr
                    seen_origin = True
                parsed.append((lineno, raw, 'origin', addr, pc))
                continue

        # KickAss-style equates: .const NAME = expr
        #                         .var NAME = expr
        #                         .label NAME = expr
        # Treat all three the same as our simple equate (no
        # re-assignment tracking, .var values are constant once seen).
        kick_equ = None
        for kw in ('.const', '.var', '.label'):
            if src.lower().startswith(kw):
                rest = src[len(kw):].lstrip()
                if '=' in rest:
                    name_part, _, value_part = rest.partition('=')
                    name = name_part.strip()
                    expr = value_part.strip()
                    if _parse_label(name) and expr:
                        kick_equ = (name, expr)
                        break
        if kick_equ is not None:
            name, expr = kick_equ
            try:
                val = _eval_expr(expr, symbols, pc)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
            symbols[name] = val
            parsed.append((lineno, raw, 'equ', (name, val), pc))
            continue

        # Equate: NAME = expr     or    NAME equ expr   or NAME .equ expr
        # (must have whitespace or = around it)
        equ_m = None
        # Try `=` form
        if '=' in src and not src.lstrip().startswith('*'):
            left, _, right = src.partition('=')
            left = left.strip()
            right = right.strip()
            if _parse_label(left) is not None and right:
                equ_m = (left, right)
        # Try `equ` form
        if equ_m is None:
            tokens = src.split(None, 2)
            if len(tokens) >= 3 and tokens[1].lower() in ('equ', '.equ'):
                if _parse_label(tokens[0]) is not None:
                    equ_m = (tokens[0], tokens[2])
        if equ_m is not None:
            name, expr = equ_m
            try:
                val = _eval_expr(expr, symbols, pc)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
            symbols[name] = val
            parsed.append((lineno, raw, 'equ', (name, val), pc))
            continue

        # Label: `name:` possibly followed by instruction on same line
        if ':' in src:
            colon_idx = src.index(':')
            possible_label = src[:colon_idx].strip()
            if _parse_label(possible_label) is not None:
                # Same-address duplicates tolerated; namespace-aware via
                # _set_label.
                _set_label(possible_label, pc, lineno, raw)
                # Instruction on same line after the label?
                rest = src[colon_idx+1:].strip()
                if not rest:
                    parsed.append((lineno, raw, 'label', possible_label, pc))
                    continue
                # else fall through and treat `rest` as the instruction
                src = rest

        # Implicit column-1 label OR indented local label.
        #   `name        instruction`     - column-1 label (DASM/TASM-ish)
        #   `\t.local    instruction`     - indented local label (ACME/KickAss)
        # The first token is treated as a label-defining mark when:
        #   - it's a valid identifier (possibly with '.' or '@' prefix)
        #   - it's NOT a known mnemonic
        #   - it's NOT a known directive name
        # For column-1 unprefixed labels we additionally require the
        # raw line to start without whitespace (so a stray `lda` typo
        # doesn't get silently swallowed).
        DIRECTIVE_PREFIXES = (
            # CA65/KickAss directives we care about (without the dot):
            'org', 'pc', 'byte', 'by', 'word', 'wo', 'dword', 'dw',
            'text', 'asc', 'fill', 'align', 'encoding',
            'const', 'var', 'label', 'cpu', 'namespace', 'filenamespace',
            'macro', 'pseudocommand', 'import', 'importonce',
            'importbinary', 'return', 'eval', 'if', 'else', 'elif',
            'for', 'while', 'do', 'print', 'printnow', 'error',
            'assert', 'function', 'define', 'struct', 'enum',
            'dstruct', 'dunion',
            'zp', 'lohifill', 'modify', 'errorif', 'warnif',
            'disk', 'segment', 'segmentdef', 'segmentout', 'file',
            'plugin', 'equ',
            # 64tass directives:
            'int', 'char', 'sint', 'long', 'lint', 'dint',
            'word', 'dword', 'addr', 'rta',
            'ptext', 'null', 'shift', 'shiftl',
            'binary', 'include', 'enc', 'cdef', 'edef',
            'proc', 'pend', 'block', 'bend', 'section', 'send',
            'logical', 'here', 'virtual', 'endv',
            'comment', 'endcomment', 'switch', 'endswitch',
            'fi', 'endif', 'endc', 'next', 'endrept',
            'endm', 'endf', 'endn', 'endu', 'endstruct', 'ends',
            'option', 'cerror', 'cwarn', 'lbl', 'goto',
            'databank', 'dpage', 'al', 'as', 'short',
            'endp', 'page', 'eor', 'proff', 'pron',
            'elseif', 'elsif', 'endpage', 'end',
            'rept', 'repeat', 'union', 'ifeq', 'ifne', 'ifmi', 'ifpl',
            'ifdef', 'ifndef', 'weak', 'endweak', 'encode', 'endencode',
            'pc', 'org', 'endw',
        )

        tokens_chk = src.split(None, 1)
        if tokens_chk:
            first = tokens_chk[0]
            first_lc = first.lower()
            label_candidate = None

            # Case A: column-1 (no leading whitespace) and the token
            # is a valid label identifier
            if raw and raw[0] not in ' \t':
                if (not first_lc.startswith(('!', '#', '*'))
                        and _parse_label(first) is not None):
                    # If '.foo', check that 'foo' isn't a real directive
                    if first_lc.startswith('.'):
                        if first_lc[1:] not in DIRECTIVE_PREFIXES:
                            label_candidate = first
                    else:
                        label_candidate = first

            # Case B: indented and starts with '.' or '@' - treat as
            # local label as long as it's not a known directive
            elif first_lc.startswith(('.', '@')):
                if (first_lc[1:] not in DIRECTIVE_PREFIXES
                        and _parse_label(first) is not None):
                    label_candidate = first

            if label_candidate is not None:
                # Is it actually a known mnemonic? Then it's not a label.
                known_mnemonics = {m for (m, _md) in _REV_OPCODES}
                if label_candidate.upper() not in known_mnemonics:
                    _set_label(label_candidate, pc, lineno, raw)
                    rest = tokens_chk[1].strip() if len(tokens_chk) > 1 else ''
                    if not rest:
                        parsed.append((lineno, raw, 'label',
                                        label_candidate, pc))
                        continue
                    src = rest

        # Pseudo-op: ACME-style !byte / !word / !text / !pet / !scr /
        # !fill / !align / !ct, plus DASM/CA65 aliases .byte / .word /
        # .text / .org / .db / .dw / .asc.
        # Tokenise to separate mnemonic from args (handle both `!byte`
        # and `.byte` consistently).
        # Tokenize: separate the directive/mnemonic from its arguments.
        # `!src"file.asm"` (no whitespace before the quote) is valid in
        # ACME, so we manually split on the first whitespace OR quote
        # OR `(` rather than relying on split(None, 1).
        first_break = len(src)
        for i, ch in enumerate(src):
            if ch.isspace() or ch in '"\'(':
                first_break = i
                break
        if first_break < len(src):
            mn_part = src[:first_break]
            args_str = src[first_break:].strip()
            if args_str.startswith(('(',)):
                # Don't strip quotes from a paren'd expression
                pass
        else:
            mn_part = src
            args_str = ''
        tokens = [mn_part] if not args_str else [mn_part, args_str]
        mn_lc = tokens[0].lower()

        # 64tass list literals as data directive operands:
        # `.byte [1, 2, 3]` is equivalent to `.byte 1, 2, 3`. Strip an
        # enclosing pair of `[ ... ]` here so all the data-directive
        # branches below can ignore the syntax. We only strip when the
        # whole args_str is one bracketed list (not e.g. `[1,2], 3`).
        if (args_str.startswith('[') and args_str.endswith(']')
                and mn_lc in ('!byte', '!by', '!8', '!08',
                                '.byte', '.db', '.by', '.char',
                                '!word', '!wo', '!16', '.word', '.wo',
                                '.sint', '.addr', '.int',
                                '!24', '.long', '.lint',
                                '!32', '.dword', '.dw', '.dint',
                                '.rta', '!text', '!tx', '.text', '.asc')):
            # Verify the brackets actually wrap everything (not a
            # mid-string `]`). We do this by tracking depth.
            depth = 0; ok = True
            for i, ch in enumerate(args_str[:-1]):
                if ch == '[': depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        ok = False; break
            if ok:
                args_str = args_str[1:-1].strip()
                tokens[1] = args_str

        # 64tass block-opener detection, fallthrough from label-prefix
        # case (e.g. `main .proc` where `main` was already taken as a
        # label and we're left with `.proc` as `src`).
        if mn_lc in SCOPE_OPENERS or mn_lc in SCOPE_CLOSERS:
            # Scope-only directive (.proc/.pend etc.) - skip the line
            # itself but continue assembling the body normally.
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if mn_lc in WORD_BLOCK_OPENERS:
            word_depth += 1
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if mn_lc in WORD_BLOCK_CLOSERS:
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # KickAss block-opener detection here too (label + .macro etc)
        if mn_lc in BLOCK_SKIP_DIRECTIVES:
            op, cl = _count_braces(src)
            skip_depth += op - cl
            if skip_depth < 0: skip_depth = 0
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # ---- byte data ----
        # ACME: !byte / !by / !8 / !08
        # Other: .byte / .db / KickAss .by
        # ---- .dstruct - instantiate a previously defined struct ----
        # Form: .dstruct StructName, val1, val2, val3
        if mn_lc == '.dstruct':
            items = _split_args(args_str)
            if not items:
                raise AssemblerError(
                    f"line {lineno}: .dstruct needs a struct name\n  > {raw}")
            sname = items[0].strip()
            if sname not in _STRUCTS:
                raise AssemblerError(
                    f"line {lineno}: unknown struct {sname!r}\n  > {raw}")
            fields = _STRUCTS[sname]
            field_vals = items[1:]
            total = sum(sz for _, sz in fields)
            parsed.append((lineno, raw, 'data',
                            ('dstruct', fields, field_vals), pc))
            pc += total
            continue
        # ---- byte data ----
        # ACME:    !byte / !by / !8 / !08
        # CA65/DASM: .byte / .db
        # KickAss: .by
        # 64tass:  .byte (signed: .char, also a separate spelling)
        if mn_lc in ('!byte', '!by', '!8', '!08',
                      '.byte', '.db', '.by', '.char'):
            items = _split_args(args_str)
            sz = len(items)
            parsed.append((lineno, raw, 'data', ('byte', items), pc))
            pc += sz
            continue
        # ---- word data (16-bit) ----
        # ACME:    !word / !wo / !16
        # CA65/DASM: .word
        # KickAss: .word / .wo
        # 64tass:  .word (unsigned), .sint (signed), .addr (synonym),
        #          .rta (return address: stores value-1)
        # Note: KickAss `.dw` is 32-bit, NOT 16-bit, handled below.
        if mn_lc in ('!word', '!wo', '!16',
                      '.word', '.wo', '.sint', '.addr', '.int'):
            items = _split_args(args_str)
            sz = len(items) * 2
            parsed.append((lineno, raw, 'data', ('word', items), pc))
            pc += sz
            continue
        # 64tass .rta : like .word but stores VALUE-1 (used for
        # building jump tables consumed by RTS-trick dispatch)
        if mn_lc == '.rta':
            items = _split_args(args_str)
            sz = len(items) * 2
            parsed.append((lineno, raw, 'data', ('rta', items), pc))
            pc += sz
            continue
        # ---- 24-bit / 32-bit values ----
        # ACME:    !24, !32
        # KickAss: .dword (32-bit), .dw (32-bit alias)
        # 64tass:  .long (24-bit unsigned), .lint (24-bit signed),
        #          .dword (32-bit), .dint (32-bit signed)
        if mn_lc in ('!24', '.long', '.lint'):
            items = _split_args(args_str)
            sz = len(items) * 3
            parsed.append((lineno, raw, 'data', ('word24', items), pc))
            pc += sz
            continue
        if mn_lc in ('!32', '.dword', '.dw', '.dint'):
            items = _split_args(args_str)
            sz = len(items) * 4
            parsed.append((lineno, raw, 'data', ('word32', items), pc))
            pc += sz
            continue
        # ---- text data ----
        # ACME:    !text / !tx
        # CA65/DASM: .text / .asc
        # 64tass:  .text  (current encoding)
        # The current conversion table is honoured: raw / pet / scr.
        if mn_lc in ('!text', '!tx', '.text', '.asc'):
            txt = _parse_string_arg(args_str, lineno, raw)
            sz = len(txt)
            parsed.append((lineno, raw, 'data',
                            ('text', txt, conv_table), pc))
            pc += sz
            continue
        # ---- !pet : always PETSCII regardless of !convtab ----
        if mn_lc in ('!pet',):
            txt = _parse_string_arg(args_str, lineno, raw)
            sz = len(txt)
            parsed.append((lineno, raw, 'data', ('text', txt, 'pet'), pc))
            pc += sz
            continue
        # ---- !scr : always C64 screencode ----
        if mn_lc in ('!scr', '!screen'):
            txt = _parse_string_arg(args_str, lineno, raw)
            sz = len(txt)
            parsed.append((lineno, raw, 'data', ('text', txt, 'scr'), pc))
            pc += sz
            continue
        # ---- !raw : raw / no conversion (alias of !text in raw mode) ----
        if mn_lc in ('!raw',):
            txt = _parse_string_arg(args_str, lineno, raw)
            sz = len(txt)
            parsed.append((lineno, raw, 'data', ('text', txt, 'raw'), pc))
            pc += sz
            continue
        # ---- 64tass: .null - text terminated with a $00 byte ----
        if mn_lc == '.null':
            txt = _parse_string_arg(args_str, lineno, raw)
            parsed.append((lineno, raw, 'data',
                            ('null', txt, conv_table), pc))
            pc += len(txt) + 1
            continue
        # ---- 64tass: .ptext - Pascal-style string (length byte first) ----
        if mn_lc == '.ptext':
            txt = _parse_string_arg(args_str, lineno, raw)
            if len(txt) > 255:
                raise AssemblerError(
                    f"line {lineno}: .ptext string too long ({len(txt)})\n  > {raw}")
            parsed.append((lineno, raw, 'data',
                            ('ptext', txt, conv_table), pc))
            pc += len(txt) + 1
            continue
        # ---- 64tass: .shift - 7-bit text, last byte has high bit set ----
        if mn_lc == '.shift':
            txt = _parse_string_arg(args_str, lineno, raw)
            parsed.append((lineno, raw, 'data',
                            ('shift', txt, conv_table), pc))
            pc += len(txt)
            continue
        # ---- 64tass: .shiftl - 7-bit shifted left, last gets bit 0 set ----
        if mn_lc == '.shiftl':
            txt = _parse_string_arg(args_str, lineno, raw)
            parsed.append((lineno, raw, 'data',
                            ('shiftl', txt, conv_table), pc))
            pc += len(txt)
            continue
        # ---- Binary file inclusion ----
        # ACME:    !binary "file" [,size[,offset]]
        # 64tass:  .binary "file" [,offset[,size]]   <-- args swapped!
        # KickAss: .importbinary "file" [, offset[, size]]
        # We accept all three with their respective argument order.
        if mn_lc in ('!binary', '!bin', '.binary', '.importbinary'):
            if source_dir is None:
                # Skip silently if we can't resolve files
                parsed.append((lineno, raw, 'blank', None, pc))
                continue
            # Use keep-empty split: ACME's `!binary "f",,offs` has an
            # explicitly empty size argument that we must NOT collapse.
            items = []
            cur = []; in_q = None
            for ch in args_str:
                if in_q:
                    cur.append(ch)
                    if ch == in_q: in_q = None
                    continue
                if ch in '"\'':
                    in_q = ch; cur.append(ch); continue
                if ch == ',':
                    items.append(''.join(cur).strip())
                    cur = []
                    continue
                cur.append(ch)
            items.append(''.join(cur).strip())
            if not items or not items[0]:
                raise AssemblerError(
                    f"line {lineno}: binary directive needs a filename\n  > {raw}")
            fname = items[0].strip().strip('"\'')
            from pathlib import Path as _P
            target = (source_dir / fname).resolve()
            if not target.exists():
                raise AssemblerError(
                    f"line {lineno}: binary file not found: {fname}\n  > {raw}")
            try:
                data = target.read_bytes()
            except Exception as e:
                raise AssemblerError(
                    f"line {lineno}: cannot read {fname}: {e}\n  > {raw}")
            # ACME: !binary "file", size, offset
            # 64tass / KickAss: .binary "file", offset, size
            if mn_lc in ('!binary', '!bin'):
                size_str = items[1] if len(items) > 1 else None
                offs_str = items[2] if len(items) > 2 else None
            else:   # 64tass / KickAss order
                offs_str = items[1] if len(items) > 1 else None
                size_str = items[2] if len(items) > 2 else None
            offset = 0
            if offs_str:
                try:
                    offset = _eval_expr(offs_str, symbols, pc)
                except AssemblerError:
                    pass
            if offset < 0: offset = 0
            data = data[offset:]
            if size_str:
                try:
                    size = _eval_expr(size_str, symbols, pc)
                    data = data[:size]
                except AssemblerError:
                    pass
            parsed.append((lineno, raw, 'data',
                            ('binary', bytes(data)), pc))
            pc += len(data)
            continue
        # ---- !convtab / !ct  : change current text conversion table ----
        # Forms accepted:  !convtab pet     !ct scr     !ct_pet     !ct_scr
        if mn_lc in ('!convtab', '!ct'):
            arg = args_str.strip().lower()
            if arg in ('pet', 'raw', 'scr'):
                conv_table = arg
                parsed.append((lineno, raw, 'blank', None, pc))
                continue
            raise AssemblerError(
                f"line {lineno}: !convtab needs pet, raw, or scr\n  > {raw}")
        # !ct_pet, !ct_raw, !ct_scr - shorthand
        if mn_lc in ('!ct_pet', '!ct_raw', '!ct_scr'):
            conv_table = mn_lc[4:]
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # ---- 64tass .enc : equivalent to !convtab / .encoding ----
        # Common encoding names: none (raw), screen (scr), petscii (pet)
        if mn_lc == '.enc':
            arg = args_str.strip().strip('"\'').lower()
            if arg in ('screen', 'screencode', 'screencode_mixed',
                        'screencode_upper', 'scr'):
                conv_table = 'scr'
            elif arg in ('petscii', 'petscii_mixed', 'petscii_upper',
                         'pet', 'cbm'):
                conv_table = 'pet'
            elif arg in ('none', 'raw', 'ascii'):
                conv_table = 'raw'
            else:
                # Unknown - keep current, don't error (other tools may
                # define custom encodings)
                pass
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # ---- 64tass .ptext : like .text but with leading length byte ----
        if mn_lc == '.ptext':
            txt = _parse_string_arg(args_str, lineno, raw)
            if len(txt) > 255:
                raise AssemblerError(
                    f"line {lineno}: .ptext too long ({len(txt)} > 255)\n  > {raw}")
            sz = len(txt) + 1
            parsed.append((lineno, raw, 'data',
                            ('ptext', txt, conv_table), pc))
            pc += sz
            continue
        # ---- 64tass .null : like .text but with trailing $00 ----
        if mn_lc == '.null':
            txt = _parse_string_arg(args_str, lineno, raw)
            sz = len(txt) + 1
            parsed.append((lineno, raw, 'data',
                            ('null', txt, conv_table), pc))
            pc += sz
            continue
        # ---- 64tass .shift : 7-bit text, MSB set on last byte ----
        if mn_lc == '.shift':
            txt = _parse_string_arg(args_str, lineno, raw)
            if not txt:
                continue
            sz = len(txt)
            parsed.append((lineno, raw, 'data',
                            ('shift', txt, conv_table), pc))
            pc += sz
            continue
        # ---- 64tass .shiftl : 7-bit text shifted left, last byte MSB
        if mn_lc == '.shiftl':
            txt = _parse_string_arg(args_str, lineno, raw)
            if not txt:
                continue
            sz = len(txt)
            parsed.append((lineno, raw, 'data',
                            ('shiftl', txt, conv_table), pc))
            pc += sz
            continue
        # ---- !fill / .fill : fill N bytes with value (default 0) ----
        # KickAss: .fill N, expr   (expr can reference iterator i;
        #                            we don't support that, just literal value)
        if mn_lc in ('!fill', '!fi', '.fill'):
            items = _split_args(args_str)
            if not items:
                raise AssemblerError(
                    f"line {lineno}: !fill needs a count\n  > {raw}")
            try:
                count = _eval_expr(items[0], symbols, pc)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: !fill count: {e}\n  > {raw}")
            if count < 0:
                raise AssemblerError(
                    f"line {lineno}: !fill count negative\n  > {raw}")
            fill_val = items[1] if len(items) > 1 else '$00'
            parsed.append((lineno, raw, 'data',
                            ('fill', count, fill_val), pc))
            pc += count
            continue
        # ---- !align / .align : pad until (pc & AND) == EQUAL ----
        # KickAss: .align N      (N is a power of 2; align to next multiple)
        if mn_lc in ('!align', '.align'):
            items = _split_args(args_str)
            if not items:
                raise AssemblerError(
                    f"line {lineno}: !align needs argument\n  > {raw}")
            # KickAss form: just one arg = power-of-2 alignment
            if len(items) == 1:
                try:
                    N = _eval_expr(items[0], symbols, pc)
                except AssemblerError as e:
                    raise AssemblerError(f"line {lineno}: .align: {e}\n  > {raw}")
                if N <= 0:
                    raise AssemblerError(
                        f"line {lineno}: .align needs positive power of 2\n  > {raw}")
                target_pc = pc
                if target_pc % N != 0:
                    target_pc += N - (target_pc % N)
                count = target_pc - pc
                parsed.append((lineno, raw, 'data',
                                ('fill', count, '$00'), pc))
                pc += count
                continue
            # ACME form: AND_VAL, EQUAL_VAL [, fill]
            try:
                and_val = _eval_expr(items[0], symbols, pc)
                eq_val = _eval_expr(items[1], symbols, pc)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: !align: {e}\n  > {raw}")
            fill_val = items[2] if len(items) > 2 else '$00'
            target_pc = pc
            while (target_pc & and_val) != eq_val:
                target_pc += 1
            count = target_pc - pc
            parsed.append((lineno, raw, 'data',
                            ('fill', count, fill_val), pc))
            pc += count
            continue
        # ---- KickAss .encoding : like !ct but with named values
        # Forms: .encoding "screencode_mixed" / "screencode_upper"
        #         .encoding "petscii_mixed" / "petscii_upper"
        if mn_lc == '.encoding':
            arg = args_str.strip().strip('"\'').lower()
            if arg.startswith('screencode'):
                conv_table = 'scr'
            elif arg.startswith('petscii'):
                conv_table = 'pet'
            else:
                conv_table = 'raw'
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # ---- !zone / !sl : ignored (we don't track local label scope) ----
        # Both ACME and KickAss directives that we silently skip:
        # they keep simple files assembling without implementing the
        # full feature. Some of these would need nested {} block parsing
        # which is beyond this mini-assembler.
        # ---- Compile-time message directives ----
        # `.print expr [, expr...]`  - echo to stderr, useful for
        #     debugging compile-time values (KickAss / 64tass).
        # `.error msg`              - emit an error and stop assembly.
        # `.warn msg`               - print a warning.
        # `.assert cond, msg`       - fail if cond evaluates to 0.
        if mn_lc in ('.print', '.printnow', '!warn'):
            # Try to evaluate any expressions; print them to stderr.
            try:
                items = _split_args(args_str)
                rendered = []
                for it in items:
                    it = it.strip()
                    if it.startswith('"') and it.endswith('"'):
                        rendered.append(it[1:-1])
                    else:
                        try:
                            v = _eval_expr(it, symbols, pc)
                            rendered.append(f"{v} (${v:X})")
                        except AssemblerError:
                            rendered.append(it)
                msg = ' '.join(rendered)
                _safe_stderr_write(f"[asm line {lineno}] {msg}\n")
            except Exception:
                pass
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if mn_lc in ('.error', '!error', '.cerror'):
            msg = args_str.strip().strip('"\'')
            raise AssemblerError(
                f"line {lineno}: user error: {msg}\n  > {raw}")
        if mn_lc in ('.warn', '.cwarn'):
            msg = args_str.strip().strip('"\'')
            _safe_stderr_write(f"[asm WARNING line {lineno}] {msg}\n")
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if mn_lc == '.assert':
            items = _split_args(args_str)
            if items:
                try:
                    cond = _eval_expr(items[0], symbols, pc)
                except AssemblerError:
                    cond = 1   # can't eval -> assume true
                if not cond:
                    msg = items[1].strip().strip('"\'') if len(items) > 1 else 'assertion failed'
                    raise AssemblerError(
                        f"line {lineno}: assertion failed: {msg}\n  > {raw}")
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # `.errorif cond, msg` and `.warnif cond, msg` (KickAss)
        if mn_lc == '.errorif':
            items = _split_args(args_str)
            if items:
                try:
                    cond = _eval_expr(items[0], symbols, pc)
                    if cond:
                        msg = items[1].strip().strip('"\'') if len(items) > 1 else 'errorif'
                        raise AssemblerError(
                            f"line {lineno}: .errorif: {msg}\n  > {raw}")
                except AssemblerError as e:
                    raise
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        if mn_lc == '.warnif':
            items = _split_args(args_str)
            if items:
                try:
                    cond = _eval_expr(items[0], symbols, pc)
                    if cond:
                        msg = items[1].strip().strip('"\'') if len(items) > 1 else 'warnif'
                        _safe_stderr_write(f"[asm WARNING line {lineno}] {msg}\n")
                except AssemblerError:
                    pass
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        SILENT_SKIPS = {
            # ACME
            '!zone', '!sl', '!source', '!src', '!to', '!cpu',
            '!set', '!if', '!ifdef', '!for', '!do', '!macro',
            '!initmem', '!pseudopc',
            # KickAssembler directives
            '.cpu', '.namespace', '.filenamespace', '.macro',
            '.pseudocommand', '.import', '.importonce',
            '.return', '.eval', '.if', '.else', '.elif',
            '.for', '.while', '.do',
            '.function', '.define', '.struct', '.enum',
            '.zp', '.lohifill', '.modify',
            '.disk', '.segment', '.segmentdef', '.segmentout',
            '.file', '.plugin',
            # KickAss preprocessor (lines starting with #)
            '#import', '#importonce', '#importif', '#define', '#undef',
            '#if', '#elif', '#else', '#endif',
            # 64tass directives (single-line; block ones handled by
            # WORD_BLOCK_OPENERS above)
            '.option', '.lbl', '.goto',
            '.databank', '.dpage', '.al', '.as', '.short',
            '.endp', '.page', '.eor', '.proff', '.pron',
            '.cdef', '.edef',
            '.elseif', '.elsif', '.endpage',
        }
        if mn_lc in SILENT_SKIPS:
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # 64tass `.end` terminates the assembly. Anything after is ignored.
        if mn_lc == '.end':
            break
        # KickAss block directives may leave bare `{` and `}` lines
        # behind. Skip these so they don't error out as unknown
        # mnemonics.
        if src in ('{', '}'):
            parsed.append((lineno, raw, 'blank', None, pc))
            continue
        # Skip lines starting with `}` followed by directives like
        # `} else {`  -  these come from .if blocks we already skipped.
        if src.startswith('}'):
            parsed.append((lineno, raw, 'blank', None, pc))
            continue

        # Plain instruction
        parts = src.split(None, 1)
        mnemonic = parts[0].upper()
        operand = parts[1].strip() if len(parts) > 1 else ''
        if '.' in mnemonic:
            mnemonic = mnemonic.split('.', 1)[0]
        if (mnemonic, 'imp') not in _REV_OPCODES \
                and mnemonic not in {m for m, _mode in _REV_OPCODES}:
            raise AssemblerError(
                f"line {lineno}: unknown mnemonic {mnemonic!r}\n  > {raw}")
        try:
            mode, sz = _detect_mode_size(operand, mnemonic, symbols, pc)
        except AssemblerError as e:
            raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
        parsed.append((lineno, raw, 'instr',
                        (mnemonic, operand), pc))
        pc += sz

    # -------- Pass 2: emit bytes -------------------------------------
    out = bytearray()
    cur_pc = origin
    for lineno, raw, kind, payload, line_pc in parsed:
        if kind in ('blank', 'label', 'equ'):
            continue
        if kind == 'origin':
            new_pc = payload
            if new_pc < cur_pc:
                # Backward jump: truncate output and continue. This
                # mirrors how ACME's pseudopc / virtual blocks behave
                # when re-using memory ranges - effectively the second
                # `*=` overwrites bytes already emitted.
                # We truncate but only down to `origin` (we never
                # forget the file's start). If the new pc is before
                # origin, that's a real error.
                if new_pc < origin:
                    raise AssemblerError(
                        f"line {lineno}: origin ${new_pc:04X} is before "
                        f"the assembly start ${origin:04X}\n  > {raw}")
                # Truncate
                truncate_to = new_pc - origin
                if truncate_to < 0: truncate_to = 0
                del out[truncate_to:]
                cur_pc = new_pc
                continue
            out.extend(b'\x00' * (new_pc - cur_pc))
            cur_pc = new_pc
            continue
        if kind == 'data':
            payload_data = payload
            kind2 = payload_data[0]
            if kind2 == 'byte':
                items = payload_data[1]
                for it in items:
                    # Allow 'X' character literals
                    if (len(it) >= 3 and it[0] in '"\'' and it[-1] == it[0]
                            and len(it) - 2 == 1):
                        v = ord(it[1])
                    else:
                        try:
                            v = _eval_expr(it, symbols, cur_pc)
                        except AssemblerError as e:
                            raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                    # Accept signed range too: -128..127 wraps to 128..255
                    if -0x80 <= v <= 0xFF:
                        out.append(v & 0xFF); cur_pc += 1
                    else:
                        raise AssemblerError(
                            f"line {lineno}: byte value out of range: ${v:X} ({v})\n  > {raw}")
            elif kind2 == 'word':
                items = payload_data[1]
                for it in items:
                    try:
                        v = _eval_expr(it, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                    # Allow signed range too
                    if -0x8000 <= v <= 0xFFFF:
                        out.append(v & 0xFF); cur_pc += 1
                        out.append((v >> 8) & 0xFF); cur_pc += 1
                    else:
                        raise AssemblerError(
                            f"line {lineno}: word value out of range: ${v:X}\n  > {raw}")
            elif kind2 == 'word24':
                items = payload_data[1]
                for it in items:
                    try:
                        v = _eval_expr(it, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                    if -0x800000 <= v <= 0xFFFFFF:
                        v &= 0xFFFFFF
                        out.append(v & 0xFF); cur_pc += 1
                        out.append((v >> 8) & 0xFF); cur_pc += 1
                        out.append((v >> 16) & 0xFF); cur_pc += 1
                    else:
                        raise AssemblerError(
                            f"line {lineno}: 24-bit value out of range: ${v:X}\n  > {raw}")
            elif kind2 == 'word32':
                items = payload_data[1]
                for it in items:
                    try:
                        v = _eval_expr(it, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                    if -0x80000000 <= v <= 0xFFFFFFFF:
                        v &= 0xFFFFFFFF
                        for shift in (0, 8, 16, 24):
                            out.append((v >> shift) & 0xFF); cur_pc += 1
                    else:
                        raise AssemblerError(
                            f"line {lineno}: 32-bit value out of range\n  > {raw}")
            elif kind2 == 'text':
                txt = payload_data[1]
                conv = payload_data[2] if len(payload_data) >= 3 else 'raw'
                for ch in txt:
                    out.append(_encode_char(ch, conv)); cur_pc += 1
            elif kind2 == 'binary':
                # Bytes from an external file
                data = payload_data[1]
                out.extend(data)
                cur_pc += len(data)
            elif kind2 == 'dstruct':
                fields = payload_data[1]
                vals = payload_data[2]
                # Pad missing values with empty (-> 0)
                while len(vals) < len(fields):
                    vals.append('0')
                for (fname, fsize), vexpr in zip(fields, vals):
                    try:
                        v = _eval_expr(vexpr, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(
                            f"line {lineno}: dstruct field {fname!r}: {e}\n  > {raw}")
                    for shift in range(fsize):
                        out.append((v >> (shift * 8)) & 0xFF)
                        cur_pc += 1
            elif kind2 == 'rta':
                # 64tass: store value-1 as 16-bit LE (RTS-trick tables)
                items = payload_data[1]
                for it in items:
                    try:
                        v = _eval_expr(it, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
                    v = (v - 1) & 0xFFFF
                    out.append(v & 0xFF); cur_pc += 1
                    out.append((v >> 8) & 0xFF); cur_pc += 1
            elif kind2 == 'ptext':
                # 64tass: leading length byte, then text
                txt = payload_data[1]
                conv = payload_data[2] if len(payload_data) >= 3 else 'raw'
                out.append(len(txt) & 0xFF); cur_pc += 1
                for ch in txt:
                    out.append(_encode_char(ch, conv)); cur_pc += 1
            elif kind2 == 'null':
                # 64tass: text + trailing $00
                txt = payload_data[1]
                conv = payload_data[2] if len(payload_data) >= 3 else 'raw'
                for ch in txt:
                    out.append(_encode_char(ch, conv)); cur_pc += 1
                out.append(0x00); cur_pc += 1
            elif kind2 == 'shift':
                # 64tass: 7-bit text, MSB set on last byte
                txt = payload_data[1]
                conv = payload_data[2] if len(payload_data) >= 3 else 'raw'
                last = len(txt) - 1
                for i, ch in enumerate(txt):
                    b = _encode_char(ch, conv) & 0x7F
                    if i == last:
                        b |= 0x80
                    out.append(b); cur_pc += 1
            elif kind2 == 'shiftl':
                # 64tass: 7-bit text shifted left, MSB set on last
                txt = payload_data[1]
                conv = payload_data[2] if len(payload_data) >= 3 else 'raw'
                last = len(txt) - 1
                for i, ch in enumerate(txt):
                    b = (_encode_char(ch, conv) << 1) & 0xFE
                    if i == last:
                        b |= 0x01
                    out.append(b); cur_pc += 1
            elif kind2 == 'fill':
                count = payload_data[1]
                fill_expr = payload_data[2]
                # 64tass: .fill 8, [$55, $aa]  -> alternating pattern
                if fill_expr.strip().startswith('['):
                    # Parse the bracket list
                    inner = fill_expr.strip().lstrip('[').rstrip(']')
                    pieces = _split_args(inner)
                    pattern = []
                    for p in pieces:
                        try:
                            pattern.append(
                                _eval_expr(p, symbols, cur_pc) & 0xFF)
                        except AssemblerError as e:
                            raise AssemblerError(
                                f"line {lineno}: .fill pattern: {e}\n  > {raw}")
                    if not pattern:
                        pattern = [0]
                    for i in range(count):
                        out.append(pattern[i % len(pattern)])
                        cur_pc += 1
                else:
                    try:
                        fill_val = _eval_expr(fill_expr, symbols, cur_pc)
                    except AssemblerError as e:
                        raise AssemblerError(f"line {lineno}: !fill: {e}\n  > {raw}")
                    for _ in range(count):
                        out.append(fill_val & 0xFF); cur_pc += 1
            continue
        if kind == 'instr':
            mnemonic, operand = payload
            try:
                bs = _assemble_one(mnemonic, operand, cur_pc, symbols)
            except AssemblerError as e:
                raise AssemblerError(f"line {lineno}: {e}\n  > {raw}")
            out.extend(bs); cur_pc += len(bs)
            continue
    return bytes(out), origin


# =====================================================================
# 6502 Disassembler core
# =====================================================================
def disassemble(data: bytes, base_addr: int, show_illegal: bool = False) -> list:
    """Linear-scan disassembly starting at base_addr.
    `data` is the raw bytes to disassemble (no PRG header).
    Returns a list of _DisasmLine. Bytes that don't match a known
    opcode emit a .byte data line.

    If show_illegal=True, undocumented 6502 opcodes (LAX, SAX, DCP,
    ISC, RLA, RRA, SLO, SRE, ANC, ALR, ARR, AXS, NOP variants, JAM,
    SHX, SHY, TAS, LAS, AHX, XAA) are decoded as proper mnemonics
    instead of `.byte $XX`. Illegal mnemonics are tagged with a
    leading `*` to make them visually distinct.
    """
    lines = []
    pc = base_addr
    i = 0
    while i < len(data):
        op = data[i]
        info = OPCODES.get(op)
        is_illegal = False
        if info is None and show_illegal:
            info = ILLEGAL_OPCODES.get(op)
            is_illegal = info is not None
        if info is None:
            # Unknown opcode - emit as raw data byte
            lines.append(_DisasmLine(
                pc, [op], ".byte", "data", f"${op:02X}"))
            pc += 1; i += 1
            continue
        mnemonic, mode, sz = info
        # Check we have enough bytes for the operand
        if i + 1 + sz > len(data):
            lines.append(_DisasmLine(
                pc, [op], ".byte", "data", f"${op:02X}",
                comment="truncated"))
            pc += 1; i += 1
            continue
        operand_bytes = data[i+1 : i+1+sz]
        operand_str, target = _format_operand(mode, operand_bytes, pc, sz)
        # Mark illegal opcodes with a leading asterisk so they stand out
        display_mn = ("*" + mnemonic) if is_illegal else mnemonic
        lines.append(_DisasmLine(
            pc, [op] + list(operand_bytes), display_mn, mode,
            operand_str, target=target))
        pc += 1 + sz
        i += 1 + sz
    return lines


def _format_operand(mode, ob, pc, sz):
    """Returns (operand_str, target_or_None)."""
    if mode == "imp":
        return "", None
    if mode == "acc":
        return "A", None
    if mode == "imm":
        return f"#${ob[0]:02X}", None
    if mode == "zp":
        return f"${ob[0]:02X}", None
    if mode == "zpx":
        return f"${ob[0]:02X},X", None
    if mode == "zpy":
        return f"${ob[0]:02X},Y", None
    if mode == "izx":
        return f"(${ob[0]:02X},X)", None
    if mode == "izy":
        return f"(${ob[0]:02X}),Y", None
    if mode == "abs":
        addr = ob[0] | (ob[1] << 8)
        return f"${addr:04X}", addr
    if mode == "abx":
        addr = ob[0] | (ob[1] << 8)
        return f"${addr:04X},X", addr
    if mode == "aby":
        addr = ob[0] | (ob[1] << 8)
        return f"${addr:04X},Y", addr
    if mode == "ind":
        addr = ob[0] | (ob[1] << 8)
        return f"(${addr:04X})", addr
    if mode == "rel":
        offset = ob[0]
        if offset >= 0x80:
            offset -= 0x100
        target = (pc + 2 + offset) & 0xFFFF
        return f"${target:04X}", target
    return "?", None


def build_render_data(lines):
    """Phase 1 of rendering: build the plain text + format-ranges from
    a list of disassembly lines. The result is a pure-Python tuple
    that can be cached or pickled - no Qt objects involved.

    Returns: (full_text, anchor_ranges, link_ranges, color_ranges)
        full_text         str, the complete disassembly text
        anchor_ranges     [(start, length, anchor_name), ...]
        link_ranges       [(start, length, href), ...]
        color_ranges      [(start, length, tag), ...]
    """
    anchor_ranges = []
    link_ranges = []
    color_ranges = []
    parts = []
    pos = 0
    for ln in lines:
        # Address (4 chars)
        anchor_ranges.append((pos, 4, f"a{ln.pc:04X}"))
        color_ranges.append((pos, 4, 'addr'))
        parts.append(f"{ln.pc:04X}"); pos += 4
        parts.append("  ");           pos += 2
        # Bytes column (always 9 chars)
        bs = " ".join(f"{b:02X}" for b in ln.bytes)
        bs_padded = f"{bs:<9}"
        color_ranges.append((pos, 9, 'bytes'))
        parts.append(bs_padded); pos += 9
        parts.append(" ");       pos += 1
        # Mnemonic (always 6 chars)
        mn_padded = f"{ln.mnemonic:<6}"
        color_ranges.append((pos, 6,
                              'data' if ln.mnemonic == '.byte' else 'op'))
        parts.append(mn_padded); pos += 6
        parts.append(" ");        pos += 1
        # Operand (with optional jump-link)
        if ln.mnemonic == ".byte":
            color_ranges.append((pos, len(ln.operand), 'data'))
            parts.append(ln.operand); pos += len(ln.operand)
        elif ln.target is not None:
            m = re.match(r'(\$[0-9A-Fa-f]+)(.*)', ln.operand)
            if m:
                ap, rest = m.group(1), m.group(2)
                link_ranges.append((pos, len(ap), f"addr:{ln.target:04X}"))
                parts.append(ap); pos += len(ap)
                if rest:
                    color_ranges.append((pos, len(rest), 'operand'))
                    parts.append(rest); pos += len(rest)
            else:
                parts.append(ln.operand); pos += len(ln.operand)
        else:
            color_ranges.append((pos, len(ln.operand), 'operand'))
            parts.append(ln.operand); pos += len(ln.operand)
        if ln.comment:
            cmt = f" ; {ln.comment}"
            color_ranges.append((pos, len(cmt), 'data'))
            parts.append(cmt); pos += len(cmt)
        parts.append("\n"); pos += 1
    return "".join(parts), anchor_ranges, link_ranges, color_ranges


def apply_render_data(render_data, document):
    """Phase 2 of rendering: take the cached render-data tuple from
    build_render_data and apply it to a QTextDocument. This is the
    Qt-touching part.

    Performance note: each setCharFormat call is expensive (~100us
    on average). For a 30000-line disassembly, applying every color
    range plus every anchor name takes ~20 seconds. To keep the UI
    snappy:
      * On files under 5000 lines we apply all formats (color +
        anchors + links).
      * Above that we skip color formats AND anchors (jump_to uses
        direct cursor positioning which doesn't need anchors at all).
        Click-jump links are still applied because they're sparse
        (only on JMP/JSR/branch operands)."""
    from PyQt6.QtGui import (
        QTextCharFormat, QColor, QTextCursor, QFont,
    )
    full_text, anchor_ranges, link_ranges, color_ranges = render_data
    document.clear()
    f = QFont()
    f.setFamilies(["Topaz-8", "Cascadia Mono", "Consolas",
                    "Courier New"])
    f.setPixelSize(13)
    f.setStyleHint(QFont.StyleHint.Monospace)
    document.setDefaultFont(f)
    document.setPlainText(full_text)

    # Heavy-formatting threshold. ~5000 lines = ~5000 anchors and
    # ~20000 color ranges = ~5 seconds total. Above that we skip both.
    is_big = len(anchor_ranges) > 5000

    # Pre-build the formats once
    f_addr = QTextCharFormat();    f_addr.setForeground(QColor("#88ccff"))
    f_bytes = QTextCharFormat();   f_bytes.setForeground(QColor("#666666"))
    f_op = QTextCharFormat();      f_op.setForeground(QColor("#ffaa44"))
    f_data = QTextCharFormat();    f_data.setForeground(QColor("#888888"))
    f_operand = QTextCharFormat(); f_operand.setForeground(QColor("#bbbbbb"))
    f_link_base = QTextCharFormat()
    f_link_base.setForeground(QColor("#7faaff"))
    f_link_base.setFontUnderline(True)

    fmt_map = {
        'addr':    f_addr,
        'bytes':   f_bytes,
        'op':      f_op,
        'data':    f_data,
        'operand': f_operand,
    }

    cur = QTextCursor(document)
    cur.beginEditBlock()
    try:
        if not is_big:
            for start, length, tag in color_ranges:
                cur.setPosition(start)
                cur.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cur.setCharFormat(fmt_map[tag])
            for start, length, name in anchor_ranges:
                cur.setPosition(start)
                cur.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                af = QTextCharFormat(f_addr)
                af.setAnchor(True)
                af.setAnchorNames([name])
                cur.setCharFormat(af)
        # Click-jump links are always applied (sparse - only on JMP/
        # JSR/branch operands).
        for start, length, href in link_ranges:
            cur.setPosition(start)
            cur.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            lf = QTextCharFormat(f_link_base)
            lf.setAnchor(True)
            lf.setAnchorHref(href)
            cur.setCharFormat(lf)
    finally:
        cur.endEditBlock()


# =====================================================================
# Render cache
# =====================================================================
# Strategy: when we disassemble + render a PRG, we save the heavy
# pre-computed data (lines + render_data) keyed by an MD5 hash of the
# raw file bytes plus the show_illegal toggle. Next time the user opens
# the same file, we skip re-disassembling and skip re-walking the lines
# to build render_data - both can take seconds on a 32K binary.
#
# Cache invalidates automatically: when a user edits the EDIT copy,
# its bytes change, the MD5 changes, the cache miss triggers a re-render
# and a fresh cache entry is written.
#
# Cache file format: pickle dump of:
#     {'lines': [...], 'render_data': (text, anchors, links, colors)}

import hashlib as _hashlib
import pickle as _pickle
from .config import scaled_font_px


def _cache_key(file_bytes, show_illegal):
    """Compute a cache key for a file's disassembly. Includes the
    show_illegal toggle plus a version byte so old cache files from
    earlier code revisions don't get reused."""
    h = _hashlib.md5()
    # Cache format version. Bump this whenever the on-disk layout
    # changes (lines structure, render_data tuple shape, etc.).
    h.update(b'v2')
    h.update(file_bytes)
    h.update(b'\x01' if show_illegal else b'\x00')
    return h.hexdigest()


def _cache_path(key):
    """Resolve the on-disk path for a cache entry."""
    try:
        from .config import CACHE_DIR
    except Exception:
        # Fallback when config module isn't available
        from pathlib import Path
        CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
    cache_dir = CACHE_DIR / "disasm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.pkl"


def cache_lookup(file_bytes, show_illegal):
    """Return cached (lines, render_data) for these file bytes, or
    None if no cache entry exists."""
    key = _cache_key(file_bytes, show_illegal)
    p = _cache_path(key)
    if not p.exists():
        _safe_stderr_write(f"[cache] miss: {p.name}\n")
        return None
    try:
        with open(p, 'rb') as f:
            data = _pickle.load(f)
        _safe_stderr_write(
            f"[cache] hit: {p.name} ({p.stat().st_size} bytes)\n")
        return data['lines'], data['render_data']
    except Exception as e:
        _safe_stderr_write(f"[cache] lookup corrupt at {p}: {e}\n")
        try: p.unlink()
        except Exception: pass
        return None


def cache_store(file_bytes, show_illegal, lines, render_data):
    """Persist disassembled lines + render_data for future use."""
    key = _cache_key(file_bytes, show_illegal)
    p = _cache_path(key)
    try:
        with open(p, 'wb') as f:
            _pickle.dump({'lines': lines,
                          'render_data': render_data}, f,
                         protocol=_pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        import traceback as _tb
        _safe_stderr_write(f"[cache] store failed at {p}: {e}\n")
        _safe_print_exc()
        raise


def render_to_document(lines, document):
    """Backwards-compatible: build the render data on the fly and
    apply it. Used when no cache is available or the caller doesn't
    want caching."""
    render_data = build_render_data(lines)
    apply_render_data(render_data, document)


# Old HTML renderer kept for reference but not used in the viewer
def lines_to_html(lines: list) -> str:
    """Render disassembly lines to HTML with clickable jump targets.
    Note: this is much slower than render_to_document for large inputs;
    kept here only for testing/export."""
    out = ['<html><head><style>'
           'html,body{margin:0;padding:0;background:#000;color:#dddddd;}'
           'pre{margin:0;padding:0;'
           'font-family:"Topaz-8","Cascadia Mono","Consolas","Courier New",monospace;'
           f'font-size: {scaled_font_px(13)}px;line-height:1.0;}}'
           'a.j{color:#7faaff;text-decoration:underline;}'
           'a.j:hover{color:#ffff80;}'
           'span.op{color:#ffaa44;}'
           'span.data{color:#888888;}'
           'span.addr{color:#88ccff;}'
           'span.bytes{color:#666666;}'
           '</style></head><body><pre>']
    for ln in lines:
        addr_html = f'<a name="a{ln.pc:04X}"></a><span class="addr">{ln.pc:04X}</span>'
        bs = " ".join(f"{b:02X}" for b in ln.bytes)
        bs_html = f'<span class="bytes">{bs:<9}</span>'
        if ln.mnemonic == ".byte":
            mn_html = f'<span class="data">.byte</span>'
            op_html = f'<span class="data">{ln.operand}</span>'
        else:
            mn_html = f'<span class="op">{ln.mnemonic}</span>'
            if ln.target is not None and (ln.mnemonic in JUMP_OPS
                                           or ln.mnemonic in BRANCH_OPS):
                op_html = _wrap_addr_in_operand(ln.operand, ln.target)
            else:
                op_html = ln.operand
        comment = f' ; {ln.comment}' if ln.comment else ''
        out.append(f'{addr_html}  {bs_html} {mn_html} {op_html}{comment}\n')
    out.append('</pre></body></html>')
    return ''.join(out)


def _wrap_addr_in_operand(operand_str, target):
    """Wrap the $NNNN portion of an operand string in an <a>."""
    m = re.match(r'(\$[0-9A-Fa-f]+)(.*)', operand_str)
    if not m:
        return operand_str
    addr_part, rest = m.group(1), m.group(2)
    return f'<a class="j" href="addr:{target:04X}">{addr_part}</a>{rest}'


# =====================================================================
# Detection
# =====================================================================
def is_c64_binary(path: Path) -> bool:
    """Heuristic: file extension OR PRG-style 2-byte header pointing into
    a sensible C64 RAM range."""
    ext = path.suffix.lower()
    if ext in ('.prg', '.bin', '.crt', '.tap', '.sid'):
        return True
    # Check first 2 bytes if very small file
    try:
        if path.stat().st_size < 2:
            return False
        with open(path, 'rb') as f:
            head = f.read(2)
        if len(head) == 2:
            load_addr = head[0] | (head[1] << 8)
            # PRG load addresses are typically in 0x0200..0xFFFF for C64
            if 0x0200 <= load_addr <= 0xFFFF:
                return ext in ('.prg', '.bin', '.crt', '.tap', '.sid')
    except Exception:
        pass
    return False


def load_prg(path: Path):
    """Load a C64 PRG/SID/CRT/TAP file. Returns (data_without_header, load_addr).
    Different formats have different headers we need to skip."""
    raw = path.read_bytes()
    ext = path.suffix.lower()

    # --- SID (PSID/RSID) ---
    if ext == '.sid' and len(raw) >= 0x7c and raw[:4] in (b'PSID', b'RSID'):
        # SID header - find data offset and load address
        data_offset = (raw[6] << 8) | raw[7]
        load_addr_in_header = (raw[8] << 8) | raw[9]
        if load_addr_in_header == 0:
            # Load address is the first 2 bytes after the header (PRG style)
            if data_offset + 2 <= len(raw):
                load_addr = raw[data_offset] | (raw[data_offset + 1] << 8)
                return raw[data_offset + 2:], load_addr
        return raw[data_offset:], load_addr_in_header

    # --- CRT (cartridge image) ---
    # Header layout (64 bytes): "C64 CARTRIDGE   " (16) + header_len (4) +
    # version (2) + hardware_type (2) + EXROM (1) + GAME (1) + ...
    # Followed by CHIP packets: "CHIP" (4) + total_len (4) + chip_type (2)
    # + bank (2) + load_addr (2) + rom_size (2) + ROM data.
    if ext == '.crt' and len(raw) >= 0x40 and raw[:16] == b'C64 CARTRIDGE   ':
        header_len = (raw[0x10] << 24) | (raw[0x11] << 16) | (raw[0x12] << 8) | raw[0x13]
        if header_len < 0x20: header_len = 0x40    # safety
        # First CHIP packet
        if header_len + 16 <= len(raw) and raw[header_len:header_len+4] == b'CHIP':
            packet_len = ((raw[header_len+4] << 24) | (raw[header_len+5] << 16)
                           | (raw[header_len+6] << 8) | raw[header_len+7])
            load_addr = (raw[header_len+0xC] << 8) | raw[header_len+0xD]
            rom_size = (raw[header_len+0xE] << 8) | raw[header_len+0xF]
            data_start = header_len + 0x10
            data = raw[data_start:data_start + rom_size]
            return data, load_addr
        # Fallback - just skip the cartridge header
        return raw[header_len:], 0x8000

    # --- TAP (tape image) ---
    # Header (20 bytes): "C64-TAPE-RAW" (12) + version (1) + reserved (3) +
    # data_size (4 LE). The body is pulse-width data, not directly executable
    # code, but we can still show it. Use $0801 as a sensible "load addr" so
    # users see hex addresses.
    if ext == '.tap' and len(raw) >= 20 and raw[:12] == b'C64-TAPE-RAW':
        return raw[20:], 0x0801

    # Plain PRG: first 2 bytes = load address (little-endian)
    if len(raw) >= 2:
        load_addr = raw[0] | (raw[1] << 8)
        return raw[2:], load_addr
    return raw, 0


# =====================================================================
# Dual-Pane Viewer
# =====================================================================
class _DisasmPane(QTextEdit):
    """A single disassembly pane. Uses QTextEdit (not QTextBrowser) to
    avoid the built-in auto-navigation behaviour. Anchor clicks are
    detected manually in mousePressEvent / mouseDoubleClickEvent."""

    def __init__(self, owner, side):
        super().__init__()
        self.owner = owner    # parent C64DisasmViewer
        self.side = side      # "left" or "right"
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(f"""
            QTextEdit {{
                background-color: #000000;
                color: #dddddd;
                border: 1px solid #444;
            }}
            {SCROLLBAR_QSS}
        """)
        self._click_pos = None
        # Track click that's part of a double-click so we can suppress
        # the single-click logic.
        self._suppress_single = False

    def _href_at(self, pos):
        """Return the anchor href under a given QPoint, or empty string."""
        cursor = self.cursorForPosition(pos)
        fmt = cursor.charFormat()
        if fmt.isAnchor():
            return fmt.anchorHref()
        return ""

    def mouseMoveEvent(self, event):
        # Show the pointing-hand cursor when hovering over a clickable link.
        href = self._href_at(event.pos())
        if href.startswith("addr:"):
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            href = self._href_at(event.pos())
            if href.startswith("addr:"):
                # Defer the single-click decision so a follow-up double-
                # click can override it.
                from PyQt6.QtCore import QTimer
                self._click_pos = event.pos()
                self._pending_href = href
                self._suppress_single = False
                QTimer.singleShot(220, self._fire_single_if_pending)
                event.accept()
                return
        super().mousePressEvent(event)

    def _fire_single_if_pending(self):
        if self._suppress_single:
            self._suppress_single = False
            self._pending_href = None
            return
        href = getattr(self, '_pending_href', None)
        if href:
            self.owner.handle_anchor_click(
                self.side, QUrl(href), double=False)
        self._pending_href = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            # Right-double-click = back in history
            self.owner.go_back(self.side)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            href = self._href_at(event.pos())
            if href.startswith("addr:"):
                # Cancel any pending single-click, fire double directly
                self._suppress_single = True
                self._pending_href = None
                self.owner.handle_anchor_click(
                    self.side, QUrl(href), double=True)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def jump_to(self, addr):
        """Scroll so the given address appears at the TOP of the
        viewport. If no instruction starts exactly at `addr`, fall
        back to the closest one at or before. Uses direct cursor
        positioning instead of anchors so we don't need to register
        thousands of anchor ranges in the QTextDocument."""
        owner = self.owner
        if owner is None:
            return
        # Resolve the requested address to one we actually have a line for
        if hasattr(owner, '_resolve_anchor'):
            resolved = owner._resolve_anchor(addr)
            if resolved is not None:
                addr = resolved
        # Pick the right list of lines for this side
        lines = owner.lines if self.side == 'left' else owner.edit_lines
        # Binary search for the line index
        import bisect
        if not hasattr(self, '_pc_list'):
            self._pc_list = [ln.pc for ln in lines]
        pcs = self._pc_list
        idx = bisect.bisect_right(pcs, addr) - 1
        if idx < 0:
            idx = 0
        block = self.document().findBlockByNumber(idx)
        if not block.isValid():
            return
        cur = QTextCursor(block)
        self.setTextCursor(cur)
        # Place the target line at the TOP of the viewport.  We
        # compute the y-offset of this block in document coordinates
        # and set the vertical scroll bar to that value.  This is
        # different from ensureCursorVisible() which only guarantees
        # the cursor is somewhere in the viewport (often at the
        # bottom when the cursor moves down).
        try:
            layout = self.document().documentLayout()
            rect = layout.blockBoundingRect(block)
            target_y = int(rect.top())
            sb = self.verticalScrollBar()
            # Clamp so we don't try to scroll past the document end
            target_y = max(0, min(target_y, sb.maximum()))
            sb.setValue(target_y)
        except Exception:
            # Fallback to ensure-visible if anything in the layout
            # query fails for some reason
            self.ensureCursorVisible()


class C64DisasmViewer(QDialog):
    """Dual-pane 6502 disassembly viewer with click-to-jump navigation."""

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"C64 Disasm: {self.path.name}")
        self.resize(1200, 800)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "c64_disasm")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        # ---- Edit-copy: the RIGHT pane loads from a side-by-side file
        # whose contents the user can hex-edit. Original (LEFT) stays
        # read-only. Edit-copy lives next to the original as
        #   <stem>.edit<ext>   e.g. game.prg -> game.edit.prg
        self.edit_path = self.path.parent / f"{self.path.stem}.edit{self.path.suffix}"
        if not self.edit_path.exists():
            try:
                self.edit_path.write_bytes(self.path.read_bytes())
            except Exception as e:
                QMessageBox.warning(
                    self, "C64 Disasm",
                    f"Could not create edit copy {self.edit_path.name}:\n{e}\n\n"
                    "The right pane will be read-only.")
                self.edit_path = None

        # Read persisted user preference for illegal-opcode display
        show_illegal = False
        try:
            w = parent
            while w is not None and not hasattr(w, 'config'):
                w = w.parent()
            if w and hasattr(w, 'config'):
                show_illegal = bool(w.config.get('c64_show_illegal', False))
        except Exception:
            pass
        self._show_illegal = show_illegal

        try:
            data, load_addr = load_prg(self.path)
            # Cache key uses RAW file bytes (not the PRG/SID/CRT-
            # stripped data) - this is what `_render_pane` will hash
            # later when it stores. Keeping both sides on the same
            # source-of-truth is critical or lookups will miss
            # against keys that the store wrote.
            try:
                left_raw = self.path.read_bytes()
            except Exception:
                left_raw = data
            cached = cache_lookup(left_raw, show_illegal)
            if cached is not None:
                self.lines, self._left_render_data = cached
            else:
                self.lines = disassemble(data, load_addr,
                                           show_illegal=show_illegal)
                self._left_render_data = None
            # Right pane uses the edit copy (may be the same content
            # initially, but the user can modify it byte-by-byte).
            if self.edit_path and self.edit_path.exists():
                edit_data, edit_load = load_prg(self.edit_path)
                try:
                    right_raw = self.edit_path.read_bytes()
                except Exception:
                    right_raw = edit_data
                cached = cache_lookup(right_raw, show_illegal)
                if cached is not None:
                    self.edit_lines, self._right_render_data = cached
                else:
                    self.edit_lines = disassemble(
                        edit_data, edit_load, show_illegal=show_illegal)
                    self._right_render_data = None
                self.edit_load_addr = edit_load
                self.edit_data = bytearray(edit_data)
            else:
                self.edit_lines = list(self.lines)
                self._right_render_data = self._left_render_data
                self.edit_load_addr = load_addr
                self.edit_data = bytearray(data)
            self._truncated = False
        except Exception as e:
            QMessageBox.critical(self, "C64 Disasm", str(e))
            self._dead = True
            return

        self._dead = False
        self.load_addr = load_addr
        # Per-pane history stacks for back-navigation
        self._history = {"left": [], "right": []}
        # Compare state - filled by _compare(), navigated by F3
        self._compare_diffs = []   # list of addresses where panes differ
        self._compare_idx = -1     # current position in _compare_diffs

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(2)

        # Title bar
        title_text = (f"  6502 Disasm  -  {self.path.name}  "
                      f"-  Load: ${load_addr:04X}  "
                      f"-  {len(self.lines)} instructions  ")
        self.title_lbl = QLabel(title_text)
        self.title_lbl.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(self.title_lbl)

        # Toolbar
        from PyQt6.QtWidgets import QWidget as _W, QSizePolicy
        bar = _W()
        tb = QHBoxLayout(bar)
        tb.setContentsMargins(0, 0, 0, 0); tb.setSpacing(3)
        for label, slot, color in [
            ("Top",         self._go_top,       "blue"),
            ("Sync",        self._sync_panes,   "blue"),
            ("Back L",      lambda: self.go_back("left"),  "purple"),
            ("Back R",      lambda: self.go_back("right"), "purple"),
            ("Edit (F2)",   self._edit_bytes,   "orange"),
            ("Run in C64",  self._run_emulator, "green"),
            ("Emu Config",  self._configure_emulator, "green"),
            ("Compare",     self._compare,      "yellow"),
            ("Find Src",    lambda: self._find(prompt=True, side="left"),  "teal"),
            ("Find Edit",   lambda: self._find(prompt=True, side="right"), "teal"),
            ("Close",       self.accept,        "red"),
        ]:
            b = QPushButton(label)
            b.setStyleSheet(button_qss(color))
            b.setFixedHeight(26)
            # Calculate width from text so the label always fits with a
            # reasonable padding. setMinimumWidth + Fixed-policy means
            # the layout cannot shrink the button below this width.
            fm = b.fontMetrics()
            min_w = fm.horizontalAdvance(label) + 32
            b.setMinimumWidth(min_w)
            b.setSizePolicy(QSizePolicy.Policy.Fixed,
                             QSizePolicy.Policy.Fixed)
            b.clicked.connect(slot)
            tb.addWidget(b)
        # Illegal-opcodes toggle (checkbox)
        from PyQt6.QtWidgets import QCheckBox
        self.cb_illegal = QCheckBox("Illegal opcodes")
        # Block toggled signal during initial state set so we don't
        # trigger a re-disassembly that's already done above.
        self.cb_illegal.blockSignals(True)
        self.cb_illegal.setChecked(self._show_illegal)
        self.cb_illegal.blockSignals(False)
        self.cb_illegal.setStyleSheet(
            "QCheckBox { color: #000000; padding: 0 6px;"
            " font-weight: bold; }")
        self.cb_illegal.setToolTip(
            "Decode undocumented 6502 opcodes (LAX, SAX, DCP, ISC, RLA, "
            "RRA, SLO, SRE, NOP variants, ...) instead of showing them "
            "as .byte data. Illegal mnemonics are prefixed with *.")
        self.cb_illegal.toggled.connect(self._on_illegal_toggled)
        tb.addWidget(self.cb_illegal)
        # Help label
        help_lbl = QLabel(
            "  LEFT=read-only, RIGHT=edit copy  |  "
            "F2=edit bytes  |  F5=run in emulator  ")
        help_lbl.setStyleSheet("color: #000000; font-weight: bold;")
        tb.addWidget(help_lbl)
        tb.addStretch()
        root.addWidget(bar)

        # Splitter with two disasm panes, each in a container with a
        # small header showing what the pane contains
        from PyQt6.QtWidgets import QWidget as _PaneW
        splitter = QSplitter(Qt.Orientation.Horizontal)

        def _wrap(pane, header_text, header_color):
            container = _PaneW()
            v = QVBoxLayout(container)
            v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
            hdr = QLabel(header_text)
            hdr.setStyleSheet(
                f"QLabel {{ background-color: {header_color}; color: #000;"
                f" padding: 2px 6px; font-weight: bold; }}")
            hdr.setFixedHeight(20)
            v.addWidget(hdr)
            v.addWidget(pane, 1)
            return container

        self.left_pane = _DisasmPane(self, "left")
        self.right_pane = _DisasmPane(self, "right")
        left_label = f" ORIGINAL  -  {self.path.name}  (read-only) "
        edit_name = self.edit_path.name if self.edit_path else "<none>"
        right_label = f" EDIT COPY  -  {edit_name}  (F2 to edit, F5 to run) "
        splitter.addWidget(_wrap(self.left_pane, left_label, "#88aaff"))
        splitter.addWidget(_wrap(self.right_pane, right_label, "#ffcc44"))
        splitter.setSizes([600, 600])
        root.addWidget(splitter, 1)

        # Status bar
        last_addr = self.lines[-1].pc if self.lines else load_addr
        trunc_note = ""
        edit_note = (f"  |  Edit copy: {self.edit_path.name}"
                     if self.edit_path else "  |  No edit copy (read-only)")
        self.status = QLabel(
            f" Disassembled {len(self.lines)} instructions "
            f"from ${load_addr:04X} to ${last_addr:04X}{trunc_note}{edit_note} ")
        self.status.setStyleSheet(INFOBAR_QSS)
        self.status.setFixedHeight(20)
        # Prevent the status bar from forcing the dialog wider when
        # very long text is set (e.g. after assembling many bytes).
        # Ignored size hint = the layout decides the width based on
        # surrounding widgets, not the label's contents.
        from PyQt6.QtWidgets import QSizePolicy as _SP
        self.status.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Fixed)
        # Truncate visible text rather than overflow when too long
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(False)
        root.addWidget(self.status)

        # Show window first, then a "Rendering Memory Data..." overlay
        # while the QTextDocument is built.  show() is non-blocking; we
        # then call processEvents so the dialog is actually drawn before
        # render_to_document blocks for a few seconds on big files.
        self.show()
        self._notice = QLabel(" Rendering Memory Data, please wait... ", self)
        self._notice.setStyleSheet(
            "QLabel { background-color: #ffcc00; color: #000000;"
            " border: 2px solid #000000; padding: 12px 24px;"
            f" font-size: {scaled_font_px(14)}px; font-weight: bold; }}")
        self._notice.adjustSize()
        # Center the notice label over the splitter area
        self._notice.move(
            (self.width() - self._notice.width()) // 2,
            (self.height() - self._notice.height()) // 2)
        self._notice.raise_()
        self._notice.show()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

        # Now do the heavy work
        try:
            self._render_pane('left')
            QApplication.processEvents()
            self._render_pane('right')
        finally:
            self._notice.hide()
            self._notice.deleteLater()
            self._notice = None

        # Hotkeys
        QShortcut(QKeySequence("Esc"),       self, self.accept)
        QShortcut(QKeySequence("Ctrl+F"),    self, self._find)
        QShortcut(QKeySequence("F3"),        self, self._f3_pressed)
        QShortcut(QKeySequence("F2"),        self, self._edit_bytes)
        QShortcut(QKeySequence("F5"),        self, self._run_emulator)
        QShortcut(QKeySequence("Home"),      self, self._go_top)
        QShortcut(QKeySequence("Backspace"), self, lambda: self.go_back("left"))

        # Start at top
        self.left_pane.jump_to(load_addr)
        self.right_pane.jump_to(load_addr)

    def _render_pane(self, side):
        """Render the named pane ('left' or 'right') using cached
        render_data if available, otherwise build fresh and cache.

        After this call, the pane's QTextDocument is fully up to date
        with self.lines / self.edit_lines."""
        if side == 'left':
            lines = self.lines
            doc = self.left_pane.document()
            cached_rd = self._left_render_data
            # Cache key uses RAW file bytes - same as the lookup in
            # __init__ - so the keys match and we get a real cache hit
            # on the next open.
            try:
                file_bytes = self.path.read_bytes()
            except Exception as e:
                _safe_stderr_write(f"[cache] left read_bytes failed: {e}\n")
                file_bytes = None
        else:
            lines = self.edit_lines
            doc = self.right_pane.document()
            cached_rd = self._right_render_data
            # Right pane: use the edit-copy file bytes if it exists.
            # If not (no edit copy), fall back to the original.
            try:
                target = self.edit_path if (
                    self.edit_path and self.edit_path.exists()) else self.path
                file_bytes = target.read_bytes()
            except Exception as e:
                _safe_stderr_write(f"[cache] right read_bytes failed: {e}\n")
                file_bytes = None
        if cached_rd is not None:
            apply_render_data(cached_rd, doc)
            return
        # Build the render data fresh
        rd = build_render_data(lines)
        apply_render_data(rd, doc)
        if side == 'left':
            self._left_render_data = rd
        else:
            self._right_render_data = rd
        # Persist cache for next open. Surface failures to stderr so
        # the user sees them in the terminal during development.
        if file_bytes is not None:
            try:
                cache_store(file_bytes, self._show_illegal, lines, rd)
                _p = _cache_path(_cache_key(file_bytes,
                                              self._show_illegal))
                _safe_stderr_write(
                    f"[cache] {side}: wrote {_p.name} "
                    f"({_p.stat().st_size} bytes)\n")
            except Exception as e:
                import traceback as _tb
                _safe_stderr_write(
                    f"[cache] save failed for {side}: {e}\n")
                _safe_print_exc()
        elif file_bytes is None:
            _safe_stderr_write(f"[cache] {side}: skipped (no file bytes)\n")

    def _resolve_anchor(self, addr):
        """Map a target address to the closest disassembled instruction
        address. If addr lands inside an instruction, returns the start
        of that instruction. If addr is outside the disassembled range,
        returns None (caller falls back to whatever scrollToAnchor does).
        """
        if not self.lines:
            return None
        # Build / cache a sorted list of instruction PCs once
        if not hasattr(self, '_pc_list'):
            self._pc_list = [ln.pc for ln in self.lines]
        pcs = self._pc_list
        # Quick range check
        if addr < pcs[0] or addr > pcs[-1] + 3:
            return None
        # Binary search for the largest pc <= addr
        from bisect import bisect_right
        idx = bisect_right(pcs, addr) - 1
        if idx < 0:
            return None
        return pcs[idx]

    # --- click routing --------------------------------------------------
    def handle_anchor_click(self, side, url, double):
        """Single-click in LEFT  -> scroll RIGHT pane to target (preview).
                                    RIGHT pane's history gets a push so
                                    "Back R" can undo the preview.
        Double-click in LEFT  -> jump LEFT pane (push LEFT history).
        Single-click in RIGHT -> scroll LEFT pane (preview), push LEFT history.
        Double-click in RIGHT -> jump RIGHT pane (push RIGHT history).

        History entries store the address that was visible at top of
        the pane just before the jump, so "Back" returns to where you
        were even if the document gets re-rendered.
        """
        s = url.toString()
        if not s.startswith("addr:"):
            return
        try:
            target = int(s[5:], 16)
        except ValueError:
            return
        if double:
            # Double-click follows in the same pane
            pane = self.left_pane if side == "left" else self.right_pane
            self._push_history(side, pane)
            pane.jump_to(target)
            self.status.setText(f" {side.upper()} jumped to ${target:04X} ")
        else:
            # Single-click sends to the OTHER pane as preview, and we
            # push that other pane's history so user can undo with Back.
            other_side = "right" if side == "left" else "left"
            other_pane = self.right_pane if side == "left" else self.left_pane
            self._push_history(other_side, other_pane)
            other_pane.jump_to(target)
            self.status.setText(
                f" Preview ${target:04X} in {other_side.upper()} pane "
                f"(double-click to follow) ")

    def _current_top_address(self, pane):
        """Return the address of the line currently at the top of the
        pane's viewport. Used so 'Back' can restore the user's view
        independently of scrollbar pixel positions."""
        # cursorForPosition at viewport top-left
        cur = pane.cursorForPosition(pane.viewport().rect().topLeft())
        # Read the first 4 chars of the line - that's the hex address
        block = cur.block()
        text = block.text()
        if len(text) >= 4:
            try:
                return int(text[:4], 16)
            except ValueError:
                pass
        return None

    def _push_history(self, side, pane):
        """Remember the address currently visible at the top of `pane`,
        so go_back can return there."""
        addr = self._current_top_address(pane)
        if addr is None:
            return
        # Avoid pushing the same address twice in a row
        hist = self._history[side]
        if hist and hist[-1] == addr:
            return
        hist.append(addr)
        if len(hist) > 200:
            self._history[side] = hist[-100:]

    def go_back(self, side):
        if not self._history[side]:
            self.status.setText(f" {side.upper()} pane: no history ")
            return
        addr = self._history[side].pop()
        pane = self.left_pane if side == "left" else self.right_pane
        pane.jump_to(addr)
        self.status.setText(f" {side.upper()} pane went back to ${addr:04X} ")

    # --- toolbar actions ------------------------------------------------
    def _go_top(self):
        self.left_pane.jump_to(self.load_addr)
        self.right_pane.jump_to(self.edit_load_addr)

    def _sync_panes(self):
        """Make the right pane scroll to match the left pane."""
        self.right_pane.verticalScrollBar().setValue(
            self.left_pane.verticalScrollBar().value())
        self.status.setText(" Synced RIGHT to LEFT ")

    # ---- Byte editing in the right (edit-copy) pane ------------------
    def _edit_bytes(self):
        """Open the edit dialog for the right pane's currently selected
        line. The dialog supports two modes:
          - Hex bytes: replace the instruction byte-for-byte
          - Assembly: type 6502 source (multiple lines) starting at the
            line's address; the assembled bytes overwrite the file.
        Either mode persists the edit to the .edit copy and re-renders
        the right pane.
        """
        if not self.edit_path or not self.edit_path.exists():
            QMessageBox.information(self, "Edit",
                "Edit copy is not available - right pane is read-only.")
            return
        # Try to find the line under the cursor in the right pane.
        # If the cursor isn't on a disassembly line, fall back to the
        # first line of the file, leave hex/asm fields empty, and let
        # the user type the target address into the "Edit address"
        # field instead.
        cur = self.right_pane.textCursor()
        block = cur.block()
        text = block.text()
        addr = None
        if len(text) >= 4:
            try:
                addr = int(text[:4], 16)
            except ValueError:
                addr = None

        ln = None
        if addr is not None:
            for i, candidate in enumerate(self.edit_lines):
                if candidate.pc == addr:
                    ln = candidate
                    break

        # If no concrete instruction found, default to the load address
        # so the user has a reasonable starting point to override.
        if ln is None:
            addr = self.edit_load_addr
            current_hex = ""
            current_asm = ""
        else:
            current_hex = " ".join(f"{b:02X}" for b in ln.bytes)
            current_asm = (f"{ln.mnemonic.lstrip('*'):<6} {ln.operand}".rstrip()
                            if ln.mnemonic != '.byte'
                            else f".byte {ln.operand}")

        # File-offset is computed at apply-time after the user has
        # confirmed (and possibly changed) the edit address. Format
        # editability is the only thing we can check upfront.
        if self.edit_path.suffix.lower() in ('.crt', '.tap', '.sid'):
            self.status.setText(
                f" {self.edit_path.suffix} format is not editable in-place ")
            return

        # Build the dialog with a tab widget: Hex | Assembly
        from PyQt6.QtWidgets import (
            QDialog as _D, QVBoxLayout as _V, QHBoxLayout as _H,
            QLineEdit, QTabWidget, QPlainTextEdit, QLabel as _L,
        )
        dlg = _D(self)
        dlg.setWindowTitle(f"Edit at ${addr:04X}")
        dlg.resize(750, 520)
        v = _V(dlg)

        # Header row: address (overridable) + current instruction info
        if ln is not None:
            info_lbl = _L(f"Current at ${addr:04X}: {current_asm}   ({current_hex})")
        else:
            info_lbl = _L(
                "No instruction selected. Set the target address below "
                "and enter your code; bytes there will be overwritten.")
        info_lbl.setStyleSheet("font-weight: bold; padding: 4px;")
        v.addWidget(info_lbl)

        addr_row = _H()
        addr_row.addWidget(_L("Edit address:"))
        addr_edit = QLineEdit(f"${addr:04X}")
        addr_edit.setStyleSheet("font-family: 'Cascadia Mono', monospace;")
        addr_edit.setMaximumWidth(120)
        addr_row.addWidget(addr_edit)
        addr_help = _L("(can be outside the original PRG range; "
                        "file will be extended with $00 padding)")
        addr_help.setStyleSheet(f"color: #000000; font-size: {scaled_font_px(11)}px;")
        addr_row.addWidget(addr_help)
        addr_row.addStretch()
        v.addLayout(addr_row)

        tabs = QTabWidget()

        # ---- Hex tab ---------------------------------------------------
        from PyQt6.QtWidgets import QWidget as _W2
        hex_tab = _W2()
        hv = _V(hex_tab)
        hv.addWidget(_L("Hex bytes (space-separated):"))
        hex_edit = QLineEdit(current_hex)
        hex_edit.setStyleSheet("font-family: 'Cascadia Mono', monospace;")
        hv.addWidget(hex_edit)
        if ln is not None:
            hv.addWidget(_L(
                f"Original instruction is {len(ln.bytes)} byte(s).\n"
                "Entering more or fewer bytes will shift subsequent code."))
        else:
            hv.addWidget(_L(
                "No instruction selected - bytes will be written at the\n"
                "address shown above (file extended with $00 if needed)."))
        hv.addStretch()
        tabs.addTab(hex_tab, "Hex bytes")

        # ---- Assembly tab ---------------------------------------------
        asm_tab = _W2()
        av = _V(asm_tab)
        av.addWidget(_L(
            f"6502 assembly source. Code is written to ${addr:04X} in the "
            f"edit copy. Use *=$XXXX or .org to override:"))

        # Mini-toolbar: Load .asm / Save .asm
        toolbar_row = _H()
        bt_load = QPushButton("Load .asm...")
        bt_load.setStyleSheet(button_qss("blue"))
        bt_save = QPushButton("Save .asm...")
        bt_save.setStyleSheet(button_qss("blue"))
        toolbar_row.addWidget(bt_load)
        toolbar_row.addWidget(bt_save)
        toolbar_row.addStretch()
        av.addLayout(toolbar_row)

        asm_edit = QPlainTextEdit()
        asm_edit.setPlaceholderText(
            "; ACME-style 6502 assembly\n"
            "        LDA #$00\n"
            "        STA $D020\n"
            "loop:   INC $D021\n"
            "        JMP loop\n")
        asm_edit.setPlainText(current_asm)
        asm_edit.setStyleSheet(
            "font-family: 'Cascadia Mono', 'Consolas', monospace;"
            " font-size: 12pt;")
        av.addWidget(asm_edit, 1)

        def _do_load():
            from PyQt6.QtWidgets import QFileDialog
            sel, _ = QFileDialog.getOpenFileName(
                dlg, "Load assembly source",
                str(self.path.parent),
                "Assembly source (*.asm *.s *.a *.txt);;All files (*)")
            if not sel:
                return
            sel_path = Path(sel)
            try:
                # Try utf-8 first, fall back to latin-1 for ACME files
                # written on older systems.
                try:
                    txt = sel_path.read_text(encoding='utf-8')
                except UnicodeDecodeError:
                    txt = sel_path.read_text(encoding='latin-1')
            except Exception as e:
                QMessageBox.warning(dlg, "Load",
                    f"Could not read file:\n{e}")
                return
            asm_edit.setPlainText(txt)
            # Remember directory so includes can be resolved later
            self._loaded_asm_dir = sel_path.parent
            self.status.setText(f" Loaded {sel_path.name} into editor ")

        def _do_save():
            from PyQt6.QtWidgets import QFileDialog
            sel, _ = QFileDialog.getSaveFileName(
                dlg, "Save assembly source",
                str(self.path.parent / (self.path.stem + ".asm")),
                "Assembly source (*.asm *.s *.a);;All files (*)")
            if not sel:
                return
            try:
                Path(sel).write_text(asm_edit.toPlainText(),
                                       encoding='utf-8')
            except Exception as e:
                QMessageBox.warning(dlg, "Save",
                    f"Could not write file:\n{e}")
                return
            self.status.setText(f" Saved source to {Path(sel).name} ")

        bt_load.clicked.connect(_do_load)
        bt_save.clicked.connect(_do_save)

        asm_help = _L(
            "Numbers: $FF (hex), %1010 (binary), 12 (decimal)\n"
            "Modes:   LDA #$00 / LDA $20 / LDA $1234 / LDA $20,X / "
            "LDA ($20,X) / LDA ($20),Y\n"
            "Labels:  loop:  ...  BNE loop    (forward refs OK)\n"
            "Equates: SCREEN = $0400          (or:  SCREEN equ $0400)\n"
            "Origin:  *=$0801                  (or:  .org $0801)\n"
            "Data:    .byte $01, $02, 'A'      .word $1234, label   .text \"hi\"\n"
            "Modifiers: <expr (lowbyte), >expr (highbyte), * (current pc)")
        asm_help.setStyleSheet(f"color: #000000; font-size: {scaled_font_px(11)}px;")
        av.addWidget(asm_help)
        tabs.addTab(asm_tab, "Assembly")

        v.addWidget(tabs, 1)

        # Buttons
        btn_row = _H()
        btn_row.addStretch()
        bt_apply = QPushButton("Apply")
        bt_apply.setStyleSheet(button_qss("green"))
        bt_cancel = QPushButton("Cancel")
        bt_cancel.setStyleSheet(button_qss("red"))
        bt_apply.clicked.connect(dlg.accept)
        bt_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(bt_apply); btn_row.addWidget(bt_cancel)
        v.addLayout(btn_row)

        # Default to Assembly tab if user double-clicked an instruction
        # line; Hex tab is mostly for poking specific byte values.
        tabs.setCurrentIndex(1)
        asm_edit.setFocus()
        # Select all the placeholder text so typing replaces it
        asm_edit.selectAll()

        # Loop the dialog: on assembly errors we re-open it with the
        # user's source code preserved so they can fix the syntax.
        new_bytes = None
        replace_len = len(ln.bytes) if ln is not None else 0
        while True:
            if dlg.exec() != _D.DialogCode.Accepted:
                return

            # Parse the edit-address from the override field. Allow
            # forms: $1234  /  1234  /  4660 (decimal)  /  %0001001000110100
            addr_text = addr_edit.text().strip()
            try:
                if addr_text.startswith('$'):
                    apply_addr = int(addr_text[1:], 16)
                elif addr_text.startswith('%'):
                    apply_addr = int(addr_text[1:], 2)
                elif all(c in '0123456789abcdefABCDEF' for c in addr_text):
                    # Treat hex if it has any A-F; otherwise hex (most common)
                    apply_addr = int(addr_text, 16)
                else:
                    apply_addr = int(addr_text, 10)
            except ValueError:
                QMessageBox.warning(self, "Edit",
                    f"Bad edit address: {addr_text!r}")
                continue
            if not 0 <= apply_addr <= 0xFFFF:
                QMessageBox.warning(self, "Edit",
                    f"Edit address out of range: ${apply_addr:X}")
                continue

            if tabs.currentIndex() == 0:
                # Hex mode
                try:
                    tokens = hex_edit.text().replace(',', ' ').split()
                    new_bytes = bytes(int(t, 16) for t in tokens)
                except ValueError:
                    QMessageBox.warning(self, "Edit",
                        f"Invalid hex input: {hex_edit.text()!r}\n\n"
                        "Click OK to fix it.")
                    continue
                if not new_bytes:
                    return
                addr = apply_addr
                break
            else:
                # Assembly mode - assemble() returns (bytes, origin)
                # where origin may differ from `apply_addr` if the
                # source contains a *=... or .org directive.
                # source_dir is set by Load-.asm-button so includes can
                # be resolved relative to the loaded file's location.
                # If no file was loaded, use the directory of the PRG
                # being edited - users sometimes drop .asm next to it.
                src_dir = (getattr(self, '_loaded_asm_dir', None)
                            or self.path.parent)
                try:
                    new_bytes, asm_origin = assemble(
                        asm_edit.toPlainText(), apply_addr,
                        source_dir=src_dir)
                except AssemblerError as e:
                    QMessageBox.warning(self, "Assembly error",
                        f"{e}\n\nClick OK to return to the editor and fix it.")
                    asm_edit.setFocus()
                    continue
                if not new_bytes:
                    self.status.setText(" No code produced ")
                    return
                addr = asm_origin
                break

        # Resolve file offset, allowing extension past the end of file
        file_offset = self._addr_to_file_offset(addr, allow_extend=True)
        if file_offset is None:
            QMessageBox.warning(self, "Edit",
                f"Cannot write to ${addr:04X}: format not editable, "
                f"or below load address ${self.edit_load_addr:04X}.")
            return

        # Length warning
        if len(new_bytes) != replace_len:
            ans = QMessageBox.question(
                self, "Length differs",
                f"Original at ${addr:04X} is {replace_len} byte(s), "
                f"new code is {len(new_bytes)} byte(s).\n\n"
                "The extra bytes will OVERWRITE the bytes that follow "
                "in the file (or shorter input leaves the trailing "
                "bytes untouched).\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

        # Apply: write into the file at file_offset, simply overwriting
        # the requested length and any following bytes if longer.
        raw = bytearray(self.edit_path.read_bytes())
        # Make sure file is big enough; pad with $00 if writing past end
        end_offset = file_offset + len(new_bytes)
        if end_offset > len(raw):
            raw.extend(b'\x00' * (end_offset - len(raw)))
        raw[file_offset:file_offset + len(new_bytes)] = new_bytes

        try:
            self.edit_path.write_bytes(bytes(raw))
        except Exception as e:
            QMessageBox.critical(self, "Edit", f"Could not save: {e}")
            return

        # Re-disassemble the edit copy and refresh the right pane
        try:
            edit_data, edit_load = load_prg(self.edit_path)
            self.edit_data = bytearray(edit_data)
            self.edit_load_addr = edit_load
            self.edit_lines = disassemble(
                edit_data, edit_load,
                show_illegal=getattr(self, '_show_illegal', False))
            scroll_val = self.right_pane.verticalScrollBar().value()
            # Edit changed the bytes -> invalidate the in-memory render
            # cache for the right pane. _render_pane will rebuild and
            # write a fresh on-disk cache entry under the new MD5.
            self._right_render_data = None
            self._render_pane('right')
            if hasattr(self.right_pane, '_pc_list'):
                del self.right_pane._pc_list
            self.right_pane.verticalScrollBar().setValue(scroll_val)
            # Compare highlights become stale after an edit - clear them
            self._clear_compare()
            # Build a status string. Long byte sequences (e.g. when
            # assembling a multi-hundred-byte source) blow out the
            # status bar width and drag the entire dialog wider, so
            # truncate the displayed hex once it gets long.
            new_hex_str = ' '.join(f'{b:02X}' for b in new_bytes)
            if len(new_hex_str) > 60:
                new_hex_str = new_hex_str[:60] + '...'
            self.status.setText(
                f" ${addr:04X} <- {len(new_bytes)} byte(s): {new_hex_str}"
                f"  [saved to {self.edit_path.name}] ")
        except Exception as e:
            QMessageBox.warning(self, "Edit",
                f"File saved but re-disasm failed: {e}")

    def _addr_to_file_offset(self, addr, allow_extend=False):
        """Convert a disassembled address to a byte offset inside the
        edit_path file (including the 2-byte PRG load-addr header).

        If `allow_extend` is True, addresses beyond the current end of
        the file return a valid offset anyway. The caller is then
        responsible for padding the file (we typically pad with $00
        when actually writing the new bytes).

        Returns None if the format is not editable in-place (CRT, TAP,
        SID) or the address is below the load address."""
        ext = self.edit_path.suffix.lower()
        if ext == '.crt':
            return None    # CRT not supported for in-place edit
        if ext == '.tap':
            return None
        if ext == '.sid':
            # SID has variable header; would need careful offset calc
            return None
        # PRG / BIN: 2-byte load addr header + (addr - load_addr) bytes
        if addr < self.edit_load_addr:
            return None
        offset = 2 + (addr - self.edit_load_addr)
        if allow_extend:
            return offset
        size = self.edit_path.stat().st_size
        if offset >= size:
            return None
        return offset

    def _on_illegal_toggled(self, checked):
        """Re-disassemble both panes with the new illegal-opcodes setting.
        Persists the choice in config so it sticks across sessions."""
        # Save scroll positions
        l_scroll = self.left_pane.verticalScrollBar().value()
        r_scroll = self.right_pane.verticalScrollBar().value()
        # Re-disassemble both sides
        try:
            data, load_addr = load_prg(self.path)
            # Use raw file bytes for cache key (same key the store side
            # uses in _render_pane).
            try:
                left_raw = self.path.read_bytes()
            except Exception:
                left_raw = data
            cached = cache_lookup(left_raw, checked)
            if cached is not None:
                self.lines, self._left_render_data = cached
            else:
                self.lines = disassemble(data, load_addr,
                                          show_illegal=checked)
                self._left_render_data = None
            if self.edit_path and self.edit_path.exists():
                edit_data, edit_load = load_prg(self.edit_path)
                self.edit_data = bytearray(edit_data)
                self.edit_load_addr = edit_load
                try:
                    right_raw = self.edit_path.read_bytes()
                except Exception:
                    right_raw = edit_data
                cached = cache_lookup(right_raw, checked)
                if cached is not None:
                    self.edit_lines, self._right_render_data = cached
                else:
                    self.edit_lines = disassemble(
                        edit_data, edit_load, show_illegal=checked)
                    self._right_render_data = None
            else:
                self.edit_lines = list(self.lines)
                self._right_render_data = self._left_render_data
            self._show_illegal = checked
            from PyQt6.QtWidgets import QApplication
            self._render_pane('left')
            QApplication.processEvents()
            self._render_pane('right')
            # Invalidate PC caches
            for p in (self.left_pane, self.right_pane):
                if hasattr(p, '_pc_list'): del p._pc_list
            # Restore scroll positions
            self.left_pane.verticalScrollBar().setValue(l_scroll)
            self.right_pane.verticalScrollBar().setValue(r_scroll)
            # Compare highlights become stale after re-disasm
            self._clear_compare()
        except Exception as e:
            QMessageBox.warning(self, "Disasm",
                f"Re-disassembly failed: {e}")
            return
        # Persist
        try:
            main = self._main_window()
            if main and hasattr(main, 'config'):
                main.config['c64_show_illegal'] = bool(checked)
                from .config import save_config
                save_config(main.config)
        except Exception:
            pass
        msg = ("Illegal opcodes shown" if checked
               else "Illegal opcodes hidden (treated as .byte)")
        self.status.setText(f" {msg} ")

    def _main_window(self):
        """Walk up parents to find the main Quopus window with a config."""
        w = self.parent()
        while w is not None and not hasattr(w, 'config'):
            w = w.parent()
        return w

    def _configure_emulator(self):
        """Configuration dialog for the C64 emulator path and command-line
        arguments. Stored persistently in quopus.cfg as:
            c64_emulator      - path to the emulator executable
            c64_emulator_args - extra args before the file path; tokens:
                                {file} = full path to edit copy
        Default args if empty: just '{file}'
        """
        main = self._main_window()
        if main is None or not hasattr(main, 'config'):
            QMessageBox.warning(self, "Emu Config",
                "Cannot find main window config.")
            return
        cur_path = main.config.get('c64_emulator', '')
        cur_args = main.config.get('c64_emulator_args', '{file}')

        from PyQt6.QtWidgets import (
            QDialog as _D, QVBoxLayout as _V, QHBoxLayout as _H,
            QLineEdit, QFileDialog, QFormLayout,
        )
        dlg = _D(self)
        dlg.setWindowTitle("C64 Emulator Configuration")
        dlg.resize(680, 220)
        v = _V(dlg)
        form = QFormLayout()

        # Path row
        row1 = _H()
        ed_path = QLineEdit(cur_path)
        ed_path.setPlaceholderText(r"e.g. C:\VICE\x64sc.exe")
        btn_browse = QPushButton("Browse...")
        btn_browse.setStyleSheet(button_qss("blue"))
        def _browse():
            sel, _ = QFileDialog.getOpenFileName(
                dlg, "Select C64 emulator executable", cur_path,
                "Executables (*.exe);;All files (*)")
            if sel:
                ed_path.setText(sel)
        btn_browse.clicked.connect(_browse)
        row1.addWidget(ed_path, 1); row1.addWidget(btn_browse)
        from PyQt6.QtWidgets import QWidget as _W2
        row1w = _W2(); row1w.setLayout(row1)
        form.addRow("Executable:", row1w)

        # Args row
        ed_args = QLineEdit(cur_args)
        ed_args.setPlaceholderText("{file}")
        form.addRow("Arguments:", ed_args)

        # Suppress-VICE-exit-confirm checkbox. VICE 3.x has a
        # "Confirm on exit" dialog that pops up every time the
        # user closes the emulator window - annoying when you're
        # doing rapid run-edit-run cycles. The CLI flag
        # '+confirmonexit' (with PLUS sign) disables it for that
        # session without changing the user's saved vice.ini.
        # We inject it right before {file} so it applies to the
        # specific invocation.
        from PyQt6.QtWidgets import QCheckBox
        cb_noconfirm = QCheckBox(
            "Skip VICE 'Confirm on exit' dialog "
            "(adds +confirmonexit before {file})")
        cb_noconfirm.setChecked(
            "+confirmonexit" in cur_args)
        cb_noconfirm.setToolTip(
            "VICE 3.x pops up a 'Do you really want to exit?' \n"
            "dialog when you close the emulator window. The\n"
            "+confirmonexit flag disables this for this session\n"
            "only (your saved vice.ini settings are untouched).")
        form.addRow("", cb_noconfirm)

        # Help text
        help_lbl = QLabel(
            "Tokens you can use in Arguments:\n"
            "    {file}  - full path to the .edit.prg copy\n"
            "    {name}  - just the filename without path\n"
            "    {dir}   - directory containing the file\n\n"
            "Examples:\n"
            "  VICE:    {file}\n"
            "  VICE w/ autostart prg: -autostart {file}\n"
            "  Hoxs64:  {file}\n"
            "  CCS64:   {file} -fullscreen")
        help_lbl.setStyleSheet(f"color: #000000; font-size: {scaled_font_px(11)}px;")
        form.addRow(help_lbl)

        v.addLayout(form)

        # Buttons
        btn_row = _H()
        btn_row.addStretch()
        bt_save = QPushButton("Save")
        bt_save.setStyleSheet(button_qss("green"))
        bt_cancel = QPushButton("Cancel")
        bt_cancel.setStyleSheet(button_qss("red"))
        bt_save.clicked.connect(dlg.accept)
        bt_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(bt_save); btn_row.addWidget(bt_cancel)
        v.addLayout(btn_row)

        if dlg.exec() != _D.DialogCode.Accepted:
            return
        new_path = ed_path.text().strip()
        new_args = ed_args.text().strip() or '{file}'
        # Apply the suppress-confirm checkbox to the args
        # string. Inject +confirmonexit right before {file} if
        # checked AND not already there; strip it out if
        # unchecked AND present.
        if cb_noconfirm.isChecked():
            if "+confirmonexit" not in new_args:
                if "{file}" in new_args:
                    new_args = new_args.replace(
                        "{file}", "+confirmonexit {file}")
                else:
                    # User has args without {file} token (rare) -
                    # append at end since there's no anchor.
                    new_args = (
                        new_args + " +confirmonexit").strip()
        else:
            # Strip any standalone '+confirmonexit' token and
            # any spurious double-spaces left behind.
            import re as _re
            new_args = _re.sub(
                r"\s*\+confirmonexit\s*", " ", new_args).strip()
            new_args = _re.sub(r"\s+", " ", new_args)
            if not new_args:
                new_args = "{file}"
        main.config['c64_emulator'] = new_path
        main.config['c64_emulator_args'] = new_args
        try:
            from .config import save_config
            save_config(main.config)
            self.status.setText(" Emulator configuration saved ")
        except Exception as e:
            QMessageBox.warning(self, "Emu Config",
                f"Could not save config: {e}")

    # ---- Run in emulator -----------------------------------------------
    def _run_emulator(self):
        """Launch the configured C64 emulator with the edit copy.
        Path and args come from config['c64_emulator'] and
        config['c64_emulator_args']. If no path is set, prompts for it.
        Args support {file}, {name}, {dir} tokens."""
        if not self.edit_path or not self.edit_path.exists():
            QMessageBox.information(self, "Run",
                "Edit copy is not available - cannot run.")
            return
        main = self._main_window()
        emu_path = (main.config.get('c64_emulator', '').strip()
                     if main and hasattr(main, 'config') else '')
        emu_args = (main.config.get('c64_emulator_args', '{file}').strip()
                     if main and hasattr(main, 'config') else '{file}')

        if not emu_path or not Path(emu_path).exists():
            ans = QMessageBox.question(
                self, "C64 Emulator",
                "No C64 emulator configured yet.\n\n"
                "Open the configuration dialog now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
            self._configure_emulator()
            # Re-read after config dialog
            emu_path = (main.config.get('c64_emulator', '').strip()
                         if main else '')
            emu_args = (main.config.get('c64_emulator_args', '{file}').strip()
                         if main else '{file}')
            if not emu_path or not Path(emu_path).exists():
                return

        # Build the argument list. We split FIRST (before token expansion)
        # so the user-typed quoting in the template is honoured, then
        # expand tokens per-argument. That way a path containing spaces
        # like "pe reloaded.edit.prg" stays a single argument even if
        # the user just wrote "{file}" without quotes.
        import shlex
        try:
            template_args = shlex.split(emu_args, posix=False)
        except Exception:
            template_args = emu_args.split()

        def _expand(s):
            return (s.replace('{file}', str(self.edit_path))
                     .replace('{name}', self.edit_path.name)
                     .replace('{dir}',  str(self.edit_path.parent)))

        arg_list = [_expand(a) for a in template_args]
        # If after expansion an argument still has a leading and
        # trailing quote (because the user wrote "{file}" in args),
        # strip the quotes - subprocess.Popen quotes per-argument
        # itself on Windows.
        cleaned = []
        for a in arg_list:
            if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
                a = a[1:-1]
            cleaned.append(a)
        arg_list = cleaned
        full_cmd = [emu_path] + arg_list

        # Launch detached
        import subprocess, sys
        try:
            if sys.platform == 'win32':
                DETACHED = 0x00000008
                subprocess.Popen(
                    full_cmd,
                    creationflags=DETACHED | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True)
            else:
                subprocess.Popen(
                    full_cmd,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True)
            self.status.setText(
                f" Launched: {Path(emu_path).name} {' '.join(arg_list)} ")
        except Exception as e:
            QMessageBox.warning(self, "Run",
                f"Could not launch emulator:\n{e}\n\nCommand: {full_cmd}")

    # ---- F3 hotkey routing --------------------------------------------
    def _f3_pressed(self):
        """F3 = next diff if compare is active, else find-next."""
        if self._compare_diffs:
            self._next_diff()
        else:
            self._find(prompt=False)

    # ---- Compare ------------------------------------------------------
    def _compare(self):
        """Walk both disassembled lists in parallel and collect every
        address where the bytes differ. Highlight differing lines in
        both panes with a red background. F3 jumps to the next diff."""
        # Build a map address -> bytes for each side
        left_map = {ln.pc: bytes(ln.bytes) for ln in self.lines}
        right_map = {ln.pc: bytes(ln.bytes) for ln in self.edit_lines}
        all_addrs = sorted(set(left_map) | set(right_map))
        diffs = []
        for a in all_addrs:
            l = left_map.get(a)
            r = right_map.get(a)
            if l != r:
                diffs.append(a)

        # Highlight lines in both panes
        self._highlight_diffs(self.left_pane,  diffs, left_map, right_map, "left")
        self._highlight_diffs(self.right_pane, diffs, left_map, right_map, "right")

        self._compare_diffs = diffs
        self._compare_idx = -1

        if not diffs:
            self.status.setText(" No differences between LEFT and RIGHT ")
            return

        # Jump to first diff in both panes (synchronised)
        self._compare_idx = 0
        self._goto_diff(diffs[0])
        self.status.setText(
            f" Diff 1/{len(diffs)} at ${diffs[0]:04X}  -  "
            f"F3 = next diff, click Compare again to clear ")

    def _next_diff(self):
        """F3 while compare is active: jump to next diff in both panes."""
        if not self._compare_diffs:
            return
        self._compare_idx = (self._compare_idx + 1) % len(self._compare_diffs)
        addr = self._compare_diffs[self._compare_idx]
        self._goto_diff(addr)
        n = len(self._compare_diffs)
        wrapped = " (wrapped)" if self._compare_idx == 0 and n > 1 else ""
        self.status.setText(
            f" Diff {self._compare_idx + 1}/{n} at ${addr:04X}{wrapped} ")

    def _goto_diff(self, addr):
        """Scroll BOTH panes to a diff address. Unlike find/F3, compare
        navigation is synchronised because the user is comparing the
        same address on both sides."""
        self.left_pane.jump_to(addr)
        self.right_pane.jump_to(addr)

    def _highlight_diffs(self, pane, diff_addrs, left_map, right_map, side):
        """Apply a red background to every line in `pane` whose address
        is in diff_addrs. Lines that exist on this side but not the
        other get a slightly different shade so user sees insertions /
        deletions."""
        from PyQt6.QtGui import QTextCursor, QTextCharFormat, QColor
        from PyQt6.QtWidgets import QTextEdit
        # Use ExtraSelection so the background stays even when the user
        # clicks elsewhere (regular setCharFormat fights with our color
        # tokens). ExtraSelections also auto-clear when we set [] again.
        if not diff_addrs:
            pane.setExtraSelections([])
            return

        diff_set = set(diff_addrs)
        # Map address -> block number for fast lookup
        doc = pane.document()
        # Build a per-line lookup once: address (from first 4 chars)
        # -> QTextBlock
        block = doc.firstBlock()
        addr_to_block = {}
        while block.isValid():
            text = block.text()
            if len(text) >= 4:
                try:
                    a = int(text[:4], 16)
                    addr_to_block[a] = block
                except ValueError:
                    pass
            block = block.next()

        sels = []
        # Two shades: lines present on both sides but with different
        # bytes get dark-red; lines that exist on this side only get
        # an orange shade.
        red = QColor(80, 0, 0)        # both sides differ
        orange = QColor(80, 40, 0)    # only present on this side
        own_map = left_map if side == "left" else right_map
        other_map = right_map if side == "left" else left_map
        for a in diff_addrs:
            blk = addr_to_block.get(a)
            if blk is None:
                continue
            # Pick shade
            if a in own_map and a in other_map:
                color = red
            else:
                color = orange
            sel = QTextEdit.ExtraSelection()
            cur = QTextCursor(blk)
            cur.select(QTextCursor.SelectionType.LineUnderCursor)
            sel.cursor = cur
            sel.format = QTextCharFormat()
            sel.format.setBackground(color)
            sel.format.setProperty(
                QTextCharFormat.Property.FullWidthSelection, True)
            sels.append(sel)
        pane.setExtraSelections(sels)

    def _clear_compare(self):
        self._compare_diffs = []
        self._compare_idx = -1
        self.left_pane.setExtraSelections([])
        self.right_pane.setExtraSelections([])

    def _find(self, prompt=True, side=None):
        """Find dialog. Searches the LEFT or RIGHT pane.

        side='left'/'right' explicitly picks a pane (used by the
        toolbar's Find Src / Find Edit buttons).
        side=None falls back to whichever pane has keyboard focus,
        defaulting to LEFT - this is what F3 (find next) uses, so it
        continues in the same pane the user last searched.

        prompt=True  -> always open the input dialog
        prompt=False -> skip the dialog, repeat the last search (F3)
        """
        # Pick pane explicitly or by focus
        if side == 'left':
            search_pane = self.left_pane
        elif side == 'right':
            search_pane = self.right_pane
        elif self.right_pane.hasFocus():
            search_pane = self.right_pane
        else:
            # F3 from right pane after Find Edit: remember which pane
            # the previous search used so F3 stays in it.
            search_pane = getattr(self, '_last_search_pane', self.left_pane)
        # Remember for the next F3
        self._last_search_pane = search_pane

        if prompt:
            # ALWAYS show the input dialog when called from the Find
            # button. The cached _last_search is only used by F3 (which
            # passes prompt=False).
            from PyQt6.QtWidgets import (
                QDialog as _D, QVBoxLayout as _V, QHBoxLayout as _H,
                QLineEdit, QLabel as _L,
            )
            dlg = _D(self)
            dlg.setWindowTitle(
                f"Find in {'EDIT' if search_pane is self.right_pane else 'SOURCE'} pane")
            dlg.setModal(True)
            dlg.resize(480, 130)
            v = _V(dlg)
            v.addWidget(_L("Search text (e.g. LDA, STA $0314, JSR $FFD2):"))
            ed = QLineEdit(getattr(self, '_last_search', ''))
            ed.selectAll()
            v.addWidget(ed)
            row = _H(); row.addStretch()
            ok_btn = QPushButton("Find")
            ok_btn.setStyleSheet(button_qss("teal"))
            ok_btn.setDefault(True)
            ok_btn.clicked.connect(dlg.accept)
            cancel_btn = QPushButton("Cancel")
            cancel_btn.setStyleSheet(button_qss("red"))
            cancel_btn.clicked.connect(dlg.reject)
            row.addWidget(ok_btn); row.addWidget(cancel_btn)
            v.addLayout(row)
            ed.setFocus()
            if dlg.exec() != _D.DialogCode.Accepted:
                return
            text = ed.text()
            if not text:
                return
            self._last_search = text
            # Reset cursor to start of pane so the new query begins
            # from the top
            cur = search_pane.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            search_pane.setTextCursor(cur)
        else:
            # F3 (find next): use the cached query
            text = getattr(self, '_last_search', '')
            if not text:
                # No previous search - prompt for one
                self._find(prompt=True)
                return

        # Whitespace-tolerant text search via QRegularExpression.
        # The user always wants a TEXT search here; if they want to
        # jump to a hex address they can click a hyperlink in the
        # disassembly or use the address override in the Edit dialog.
        from PyQt6.QtCore import QRegularExpression
        parts = text.split()
        if not parts:
            return
        pattern = r'\s+'.join(re.escape(p) for p in parts)
        regex = QRegularExpression(
            pattern,
            QRegularExpression.PatternOption.CaseInsensitiveOption)
        if not regex.isValid():
            self.status.setText(f" Bad search pattern: {text!r} ")
            return

        full_text = search_pane.document().toPlainText()
        cur = search_pane.textCursor()
        # Skip past current selection if any so F3 actually moves
        start_pos = cur.selectionEnd() if cur.hasSelection() else cur.position()

        def _first_match_from(start):
            it = regex.globalMatch(full_text, start)
            if it.hasNext():
                return it.next()
            return None

        m = _first_match_from(start_pos)
        wrapped = False
        if m is None or not m.hasMatch():
            m = _first_match_from(0)
            wrapped = True

        if m is None or not m.hasMatch():
            self.status.setText(f" Not found in "
                                 f"{'RIGHT' if search_pane is self.right_pane else 'LEFT'} "
                                 f"pane: {text!r} ")
            return

        match_start = m.capturedStart()
        match_end = m.capturedEnd()
        new_cur = QTextCursor(search_pane.document())
        new_cur.setPosition(match_start)
        new_cur.setPosition(match_end, QTextCursor.MoveMode.KeepAnchor)
        search_pane.setTextCursor(new_cur)
        search_pane.ensureCursorVisible()
        prefix = "Found (wrapped): " if wrapped else "Found: "
        side = "RIGHT" if search_pane is self.right_pane else "LEFT"
        self.status.setText(f" {prefix}{text!r} in {side} pane  -  F3 for next ")


# ---------------------------------------------------------------------
# Module-level Emulator-Helpers
# ---------------------------------------------------------------------
# Diese beiden Funktionen sind aus der C64DisasmViewer-Klasse rausgezogene
# Versionen von _run_emulator und _configure_emulator. Damit koennen
# F3-View / F4-Edit / Run-Buttons und das File-Association-Dialog den
# gleichen Code aufrufen ohne den Disasm-Viewer instantiieren zu muessen.
# Config-Keys sind dieselben:
#   c64_emulator      - path to the emulator executable
#   c64_emulator_args - args template with {file} / {name} / {dir} tokens

def show_c64_emu_config_dialog(parent, config, save_callback=None):
    """Zeige den C64-Emulator-Konfigurationsdialog. Schreibt
    c64_emulator und c64_emulator_args in config; ruft save_callback
    auf falls gesetzt (typisch: lambda: save_config(mw.config)).

    Returns True wenn der User auf Save geklickt hat, sonst False.
    """
    import os
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog,
        QFormLayout, QLabel, QPushButton, QWidget, QMessageBox,
    )
    cur_path = config.get('c64_emulator', '') or ''
    cur_args = config.get('c64_emulator_args', '{file}') or '{file}'

    dlg = QDialog(parent)
    dlg.setWindowTitle("C64 Emulator Configuration")
    dlg.resize(680, 230)
    v = QVBoxLayout(dlg)
    form = QFormLayout()

    # Path row
    row1 = QHBoxLayout()
    ed_path = QLineEdit(cur_path)
    ed_path.setPlaceholderText(r"e.g. C:\VICE\x64sc.exe")
    btn_browse = QPushButton("Browse...")
    btn_browse.setStyleSheet(button_qss("blue"))
    def _browse():
        sel, _ = QFileDialog.getOpenFileName(
            dlg, "Select C64 emulator executable", cur_path,
            "Executables (*.exe);;All files (*)")
        if sel:
            ed_path.setText(sel)
    btn_browse.clicked.connect(_browse)
    row1.addWidget(ed_path, 1)
    row1.addWidget(btn_browse)
    row1w = QWidget(); row1w.setLayout(row1)
    form.addRow("Executable:", row1w)

    # Args row
    ed_args = QLineEdit(cur_args)
    ed_args.setPlaceholderText("{file}")
    form.addRow("Arguments:", ed_args)

    # Suppress-VICE-exit-confirm checkbox. VICE 3.x pops up
    # "Confirm on exit" every time the user closes the window;
    # +confirmonexit disables it for this invocation. Checked
    # state is derived from whether the current args string
    # already contains the token.
    from PyQt6.QtWidgets import QCheckBox
    cb_noconfirm = QCheckBox(
        "Skip VICE 'Confirm on exit' dialog "
        "(adds +confirmonexit before {file})")
    cb_noconfirm.setChecked("+confirmonexit" in cur_args)
    cb_noconfirm.setToolTip(
        "VICE 3.x asks 'Do you really want to exit?' every time\n"
        "the emulator window is closed. The +confirmonexit flag\n"
        "(with plus sign) disables that prompt for this session\n"
        "only - your saved vice.ini is left alone.")
    form.addRow("", cb_noconfirm)

    help_lbl = QLabel(
        "Tokens you can use in Arguments:\n"
        "    {file}  - full path to the file\n"
        "    {name}  - filename without path\n"
        "    {dir}   - directory containing the file\n\n"
        "Examples:\n"
        "  Plain VICE:        {file}\n"
        "  VICE w/ autostart: -autostart {file}\n"
        "  VICE w/ binary monitor (needs VICE 3.5+):\n"
        "                     -binarymonitor -autostart {file}\n"
        "  Hoxs64:            {file}\n"
        "  CCS64:             {file} -fullscreen\n\n"
        "If VICE complains 'Unknown option -binarymonitor', your "
        "VICE is older\nthan 3.5 - the binary monitor was introduced "
        "in that version. Update VICE\nfor live memory dump support, "
        "or drop the option to just autostart the file.")
    help_lbl.setStyleSheet(f"color: #000000; font-size: {scaled_font_px(11)}px;")
    form.addRow(help_lbl)
    v.addLayout(form)

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    bt_save = QPushButton("Save")
    bt_save.setStyleSheet(button_qss("green"))
    bt_cancel = QPushButton("Cancel")
    bt_cancel.setStyleSheet(button_qss("red"))
    bt_save.clicked.connect(dlg.accept)
    bt_cancel.clicked.connect(dlg.reject)
    btn_row.addWidget(bt_save)
    btn_row.addWidget(bt_cancel)
    v.addLayout(btn_row)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    new_path = ed_path.text().strip()
    new_args = ed_args.text().strip() or '{file}'
    # Inject/remove +confirmonexit based on the checkbox state.
    # Same logic as the other emu-config dialog (in
    # C64DisasmViewer) - keep both in sync.
    if cb_noconfirm.isChecked():
        if "+confirmonexit" not in new_args:
            if "{file}" in new_args:
                new_args = new_args.replace(
                    "{file}", "+confirmonexit {file}")
            else:
                new_args = (
                    new_args + " +confirmonexit").strip()
    else:
        import re as _re
        new_args = _re.sub(
            r"\s*\+confirmonexit\s*", " ", new_args).strip()
        new_args = _re.sub(r"\s+", " ", new_args)
        if not new_args:
            new_args = "{file}"
    config['c64_emulator'] = new_path
    config['c64_emulator_args'] = new_args
    if save_callback is not None:
        try:
            save_callback()
        except Exception as e:
            QMessageBox.warning(parent, "Emu Config",
                                  f"Could not save config: {e}")
            return False
    return True


def run_in_c64_emulator(filepath, parent, config, save_callback=None):
    """Starte den konfigurierten C64-Emulator mit `filepath`. Falls
    kein Emulator konfiguriert ist, oeffnet sich der Config-Dialog
    zuerst.

    `filepath` kann pathlib.Path oder str sein. Wird detached
    gestartet (kein blocking) damit Quopus weiterlaeuft.

    Args-Template aus config['c64_emulator_args'] - kennt {file},
    {name}, {dir} Tokens.

    Returns True wenn der Emulator gestartet wurde, sonst False.
    """
    import shlex
    import subprocess
    import sys
    from pathlib import Path
    from PyQt6.QtWidgets import QMessageBox

    fp = Path(str(filepath))
    if not fp.exists():
        QMessageBox.warning(parent, "Run in emulator",
                              f"File does not exist:\n{fp}")
        return False

    emu_path = (config.get('c64_emulator', '') or '').strip()
    emu_args = (config.get('c64_emulator_args', '{file}')
                  or '{file}').strip()

    # No emulator configured yet -> open config dialog and retry once.
    if not emu_path or not Path(emu_path).exists():
        ans = QMessageBox.question(
            parent, "C64 Emulator",
            "No C64 emulator configured yet.\n\n"
            "Open the configuration dialog now?",
            QMessageBox.StandardButton.Yes
              | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return False
        if not show_c64_emu_config_dialog(parent, config, save_callback):
            return False
        emu_path = (config.get('c64_emulator', '') or '').strip()
        emu_args = (config.get('c64_emulator_args', '{file}')
                      or '{file}').strip()
        if not emu_path or not Path(emu_path).exists():
            return False

    # Args-Template aufteilen, dann Tokens pro Argument expandieren.
    # Wir splitten zuerst (vor der Token-Expansion) damit User-typed
    # Quoting im Template erhalten bleibt.
    try:
        template_args = shlex.split(emu_args, posix=False)
    except Exception:
        template_args = emu_args.split()

    def _expand(s):
        return (s.replace('{file}', str(fp))
                  .replace('{name}', fp.name)
                  .replace('{dir}',  str(fp.parent)))

    arg_list = [_expand(a) for a in template_args]
    # Strip leading/trailing quotes left over from "{file}" templates
    cleaned = []
    for a in arg_list:
        if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
            a = a[1:-1]
        cleaned.append(a)
    arg_list = cleaned

    full_cmd = [emu_path] + arg_list

    try:
        if sys.platform == 'win32':
            DETACHED = 0x00000008
            subprocess.Popen(
                full_cmd,
                creationflags=(DETACHED
                                  | subprocess.CREATE_NEW_PROCESS_GROUP),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True)
        else:
            subprocess.Popen(
                full_cmd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True)
        return True
    except Exception as e:
        QMessageBox.warning(parent, "Run in emulator",
                              f"Could not launch emulator:\n{e}\n\n"
                              f"Command: {full_cmd}")
        return False


# ---------------------------------------------------------------------
# Memory-reference cross-analysis ("Find references")
# ---------------------------------------------------------------------
# Static disassembly of a RAM dump, then for each instruction:
# does it touch the watched address?  We classify the access as
# read / write / rmw.
#
# Caveat: this is a *linear* disassembly from base_addr, so bytes
# inside data regions get reinterpreted as instructions and may
# produce false positives. Users typically restrict the dump range
# to known code areas, or accept the noise.

# Mnemonics that READ from a memory operand (no write back).
# We strip the leading "*" used to mark illegal opcodes so the
# classification still works for those.
_MN_READ = frozenset({
    "LDA", "LDX", "LDY", "CMP", "CPX", "CPY", "BIT",
    "ADC", "SBC", "AND", "ORA", "EOR",
    # Illegal opcodes that load
    "LAX", "LAS", "ANC", "ALR", "ARR", "AXS", "XAA",
})

# Mnemonics that only WRITE to a memory operand (no read first).
_MN_WRITE = frozenset({
    "STA", "STX", "STY", "STZ",
    # Illegal opcodes that store
    "SAX", "AHX", "SHX", "SHY", "TAS",
})

# Read-Modify-Write: reads, computes, writes back. These count as
# *both* read and write of the address.
_MN_RMW = frozenset({
    "ASL", "LSR", "ROL", "ROR", "INC", "DEC", "TRB", "TSB",
    # Illegal opcodes that are RMW
    "SLO", "SRE", "RLA", "RRA", "ISC", "DCP",
})

# Indirect-jump reads two bytes at the operand to fetch the new PC.
# That's still technically a read of the target address (and target+1).
_MN_INDJMP = frozenset({"JMP"})    # mode "ind" only


class MemoryReference:
    """A single instruction that touches the watched address.

    Fields:
        pc           PC of the instruction
        bytes        raw bytes of the instruction (list of int)
        mnemonic     "LDA", "STA", "INC" etc (with leading * if illegal)
        operand_str  rendered operand, e.g. "$C000", "$BF80,Y"
        mode         addressing mode tag ("abs", "abx", "izy"...)
        access       "read", "write", "rmw", or "indjmp"
        exact        True if this instruction definitely touches the
                      watched address; False if it's a guess based on an
                      indexed mode where the index could push it there.
        note         optional extra info ("via $FB ptr +Y", "X range
                      [$00..$FF] hits target", etc)
    """
    __slots__ = ("pc", "bytes", "mnemonic", "operand_str", "mode",
                  "access", "exact", "note")
    def __init__(self, pc, b, mn, op, mode, access, exact=True, note=""):
        self.pc = pc
        self.bytes = b
        self.mnemonic = mn
        self.operand_str = op
        self.mode = mode
        self.access = access
        self.exact = exact
        self.note = note


def _zp_indirect_pointer(dump, base_addr, zp_addr):
    """Resolve a zeropage pointer ($zp / $zp+1) into the absolute
    16-bit address it currently holds. dump = bytes, base_addr is
    the dump's start address. Returns None if zp+0 or zp+1 isn't
    covered by the dump.
    """
    off0 = (zp_addr - base_addr) & 0xFFFF
    off1 = (zp_addr + 1 - base_addr) & 0xFFFF
    if off0 >= len(dump) or off1 >= len(dump):
        return None
    return dump[off0] | (dump[off1] << 8)


def find_references(data, base_addr, target_addr, *,
                      show_illegal=False):
    """Statically analyse a memory dump, returning every instruction
    that accesses `target_addr`.

    Indexed modes are approximated: `LDA $BF80,Y` is reported as a
    POSSIBLE read of $C000 (with note "Y in [$80..$FF]") because we
    don't know Y. The user sees these as "fuzzy" matches and decides.

    Indirect zeropage modes (`LDA ($FB),Y`) are resolved using the
    pointer value currently in the dump - that's a snapshot, but
    usually accurate for the moment the user clicked.

    Returns a list of MemoryReference, in PC order.
    """
    lines = disassemble(data, base_addr, show_illegal=show_illegal)
    hits = []
    target = target_addr & 0xFFFF

    for ln in lines:
        # Klassifikation: nur Mnemonics die wirklich Memory anfassen
        mn = ln.mnemonic.lstrip("*")    # *SLO -> SLO
        access = None
        if mn in _MN_READ:
            access = "read"
        elif mn in _MN_WRITE:
            access = "write"
        elif mn in _MN_RMW:
            access = "rmw"
        elif mn in _MN_INDJMP and ln.mode == "ind":
            access = "indjmp"
        else:
            continue

        # Modes ohne Memory-Operand abhaken (z.B. LDA #$nn = imm)
        m = ln.mode
        if m in ("imp", "acc", "imm", "rel", "data"):
            continue

        # --- Direct absolute ---
        if m == "abs":
            # ln.target ist die direkte Adresse
            if ln.target == target:
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=True))
            continue

        # --- Indexed absolute (abs,X / abs,Y) ---
        if m in ("abx", "aby"):
            base = ln.target
            # Die Adresse koennte base..base+$FF abdecken.
            # exact-Match nur wenn target genau base ist UND wir
            # davon ausgehen dass der Index 0 sein KOENNTE. Wir
            # melden alle Faelle wo target in [base, base+$FF] liegt
            # mit exact=False, plus den index-Wert den's brauchen
            # wuerde.
            idx = (target - base) & 0xFFFF
            if 0 <= idx <= 0xFF:
                idx_reg = "X" if m == "abx" else "Y"
                exact = (idx == 0)
                note = (f"if {idx_reg}=${idx:02X}" if not exact
                          else f"direct hit when {idx_reg}=$00")
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=exact, note=note))
            continue

        # --- Zero-page direct ---
        if m == "zp":
            # operand byte ist ln.bytes[1]
            if len(ln.bytes) >= 2 and ln.bytes[1] == (target & 0xFF) \
                    and target < 0x100:
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=True))
            continue

        # --- Zero-page indexed (zp,X / zp,Y) ---
        if m in ("zpx", "zpy"):
            if target >= 0x100:
                continue   # zp-indexed kann nicht > $FF treffen (wraps)
            if len(ln.bytes) < 2:
                continue
            base = ln.bytes[1]
            idx = (target - base) & 0xFF
            if 0 <= idx <= 0xFF:
                idx_reg = "X" if m == "zpx" else "Y"
                exact = (idx == 0)
                note = (f"if {idx_reg}=${idx:02X}" if not exact
                          else f"direct hit when {idx_reg}=$00")
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=exact, note=note))
            continue

        # --- Indirect indexed: LDA ($FB),Y ---
        if m == "izy":
            if len(ln.bytes) < 2:
                continue
            zp = ln.bytes[1]
            ptr = _zp_indirect_pointer(data, base_addr, zp)
            if ptr is None:
                # Pointer not in dump - we can't resolve, but emit a
                # tentative match so the user knows there's an
                # indirect access through this ZP location.
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=False,
                    note=f"via $({zp:02X}),Y - ptr outside dump"))
                continue
            idx = (target - ptr) & 0xFFFF
            if 0 <= idx <= 0xFF:
                exact = (idx == 0)
                note = (f"ptr $({zp:02X})=${ptr:04X}, Y=${idx:02X}"
                          if not exact else
                          f"ptr $({zp:02X})=${ptr:04X}, direct hit when Y=$00")
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=exact, note=note))
            continue

        # --- Indexed indirect: LDA ($FB,X) ---
        # X waehlt eine zp-Adresse, dann wird die als Pointer benutzt.
        # Ohne X-Wert koennen wir das nur fuer X=0 aufloesen; alles
        # andere ist Spekulation. Wir melden es immer als fuzzy mit
        # dem aktuellen X=0 Wert.
        if m == "izx":
            if len(ln.bytes) < 2:
                continue
            zp = ln.bytes[1]
            ptr = _zp_indirect_pointer(data, base_addr, zp)
            if ptr == target:
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, access, exact=False,
                    note=f"if X=$00: ptr $({zp:02X},X)=${ptr:04X}"))
            continue

        # --- JMP (abs) - reads 2 bytes from operand as new PC ---
        if m == "ind":
            base = ln.target
            if base == target or ((base + 1) & 0xFFFF) == target:
                hits.append(MemoryReference(
                    ln.pc, list(ln.bytes), ln.mnemonic, ln.operand,
                    m, "read", exact=True,
                    note="jump vector read"))
            continue

    return hits


# ---------------------------------------------------------------------
# Pattern-Suche: Code-Stellen die einen Wert laden / manipulieren
# ---------------------------------------------------------------------
# Anders als find_references() (sucht nach Memory-Operanden auf eine
# Adresse), suchen die folgenden Funktionen Code-Patterns die mit einem
# konkreten BYTE-WERT arbeiten - immediate-Loads, Vergleiche, Counter-
# Modifikationen. Nuetzlich um Cheats zu finden: "wo wird 3 (Anzahl
# Leben) als initial-Wert geladen?", "wo wird der Lifecount runter-
# gezaehlt?".


class CodePatternHit:
    """Ein Pattern-Treffer bei der Code-Suche.

    Felder:
        pc            PC der Instruktion (oder der ersten Instr in
                       einem Multi-Instruction-Pattern)
        bytes         Roh-Bytes der gefundenen Instr-Sequenz
        kind          Pattern-Klassifikation ("imm_load", "imm_cmp",
                       "counter_inc", "counter_dec", "store_imm",
                       "compare_branch")
        description   Lesbarer Beschreibungstext mit Mnemonics
        note          Optionaler Zusatz-Hinweis (z.B. "branches to $XXXX")
    """
    __slots__ = ("pc", "bytes", "kind", "description", "note")
    def __init__(self, pc, b, kind, desc, note=""):
        self.pc = pc
        self.bytes = b
        self.kind = kind
        self.description = desc
        self.note = note


# Pattern 1: Immediate Loads/Compares mit einem konkreten Wert
# Opcode-Map: alle Opcodes mit 2-byte Form "OP value" wo der Operand
# ein 8-bit Immediate ist. Wir bauen die Map einmal global auf.
# Mnemonic-Lookup: damit wir bei einem Treffer das Mnemonic ausgeben
# koennen ohne den ganzen Disassembler durchzuiterieren.
_IMM_OPCODES = {
    0xA9: "LDA", 0xA2: "LDX", 0xA0: "LDY",
    0xC9: "CMP", 0xE0: "CPX", 0xC0: "CPY",
    # Adds/Subs gegen konstanten Wert (z.B. SBC #$01 zum decrementen)
    0x69: "ADC", 0xE9: "SBC",
    # Logische immediates - selten relevant fuer cheat-search aber
    # vollstaendigkeitshalber dabei
    0x29: "AND", 0x09: "ORA", 0x49: "EOR",
}


def find_value_loads(data, base_addr, value):
    """Suche alle Code-Stellen wo der byte `value` als immediate
    geladen oder verglichen wird (LDA/LDX/LDY #$VV, CMP/CPX/CPY #$VV,
    ADC/SBC/AND/ORA/EOR #$VV).

    Wir scannen nicht-disassemblierend: gehen byte-fuer-byte den Dump
    durch und prufen bei jedem Index ob `data[i]` ein bekanntes Imm-
    Opcode ist und `data[i+1] == value`. Das ist robust gegen "weiss
    nicht wo Code anfaengt" - Treffer kommen auch wenn der Linear-
    Disasm das Pattern verpassen wuerde (Code im Datenbereich z.B.).
    Wir akzeptieren dafuer false positives bei zufaellig passenden
    Bytefolgen.

    Returns list[CodePatternHit].
    """
    hits = []
    v = value & 0xFF
    n = len(data)
    for i in range(n - 1):
        op = data[i]
        if op in _IMM_OPCODES and data[i + 1] == v:
            mn = _IMM_OPCODES[op]
            pc = (base_addr + i) & 0xFFFF
            hits.append(CodePatternHit(
                pc, [op, v], "imm_load",
                f"{mn} #${v:02X}",
                note=f"loads/compares value ${v:02X}"))
    return hits


# Pattern 2: Counter-Operations auf eine Adresse
# Wir kombinieren Multi-Instruction-Patterns. Das ist heuristisch:
# wir scannen byte-fuer-byte und matchen Sequenzen.
#
# Patterns die wir erkennen:
#
# Single-Instr Patterns:
#   EE LL HH                INC $HHLL
#   CE LL HH                DEC $HHLL
#   E6 LL                   INC $LL (zp)
#   C6 LL                   DEC $LL (zp)
#   FE LL HH                INC $HHLL,X
#   DE LL HH                DEC $HHLL,X
#
# Two-Instr Patterns (LDA imm + STA):
#   A9 VV  8D LL HH         LDA #$VV / STA $HHLL  (initial set)
#   A9 VV  85 LL            LDA #$VV / STA $LL    (zp)
#
# Compare+Branch Patterns:
#   AD LL HH  C9 VV  F0/D0 ?? = LDA $HHLL / CMP #$VV / BEQ/BNE
#   A5 LL     C9 VV  F0/D0 ?? = LDA $LL  / CMP #$VV / BEQ/BNE (zp)


def _addr_le(data, idx):
    """Lies eine 16-bit-LE-Adresse aus data ab idx."""
    return data[idx] | (data[idx + 1] << 8)


def find_counter_ops(data, base_addr, target_addr):
    """Pattern-Suche: alle Code-Stellen die auf `target_addr` als
    Counter operieren - inc/dec, set-to-immediate, compare-and-branch.

    Returns dict mit zwei Listen:
        {"mods":     [CodePatternHit, ...],   # Wert wird veraendert
         "compares": [CodePatternHit, ...]}   # Wert wird verglichen +
                                              #   ggf. gebrancht
    """
    mods = []
    compares = []
    target = target_addr & 0xFFFF
    tgt_lo = target & 0xFF
    tgt_hi = (target >> 8) & 0xFF
    is_zp = (target < 0x100)

    n = len(data)
    i = 0
    while i < n:
        op = data[i]

        # --- INC/DEC absolut ---
        if op in (0xEE, 0xCE) and i + 2 < n:
            if _addr_le(data, i + 1) == target:
                mn = "INC" if op == 0xEE else "DEC"
                pc = (base_addr + i) & 0xFFFF
                mods.append(CodePatternHit(
                    pc, list(data[i:i+3]), "counter_" + mn.lower(),
                    f"{mn} ${target:04X}",
                    note=("counter +1" if op == 0xEE else "counter -1")))
                i += 3
                continue

        # --- INC/DEC absolut,X ---
        if op in (0xFE, 0xDE) and i + 2 < n:
            # Bei abs,X koennte die Basis target oder target-N sein -
            # wir matchen nur "base == target" (X=0 Treffer); fuer X>0
            # waere das fuzzy und macht zu viel Rauschen.
            if _addr_le(data, i + 1) == target:
                mn = "INC" if op == 0xFE else "DEC"
                pc = (base_addr + i) & 0xFFFF
                mods.append(CodePatternHit(
                    pc, list(data[i:i+3]), "counter_" + mn.lower(),
                    f"{mn} ${target:04X},X",
                    note=f"{mn} indexed (X-relative, exact at X=0)"))
                i += 3
                continue

        # --- INC/DEC zp ---
        if is_zp and op in (0xE6, 0xC6) and i + 1 < n:
            if data[i + 1] == tgt_lo:
                mn = "INC" if op == 0xE6 else "DEC"
                pc = (base_addr + i) & 0xFFFF
                mods.append(CodePatternHit(
                    pc, list(data[i:i+2]), "counter_" + mn.lower(),
                    f"{mn} ${target:02X}",
                    note=("counter +1" if op == 0xE6 else "counter -1")))
                i += 2
                continue

        # --- LDA #$VV / STA $target (initial set / overwrite) ---
        if op == 0xA9 and i + 4 < n:
            if data[i + 2] == 0x8D and _addr_le(data, i + 3) == target:
                # LDA #$VV at i, STA $target at i+2
                val = data[i + 1]
                pc = (base_addr + i) & 0xFFFF
                mods.append(CodePatternHit(
                    pc, list(data[i:i+5]), "store_imm",
                    f"LDA #${val:02X} / STA ${target:04X}",
                    note=f"sets ${target:04X} = ${val:02X}"))
                i += 5
                continue

        # --- LDA #$VV / STA $LL (zp) ---
        if is_zp and op == 0xA9 and i + 3 < n:
            if data[i + 2] == 0x85 and data[i + 3] == tgt_lo:
                val = data[i + 1]
                pc = (base_addr + i) & 0xFFFF
                mods.append(CodePatternHit(
                    pc, list(data[i:i+4]), "store_imm",
                    f"LDA #${val:02X} / STA ${target:02X}",
                    note=f"sets ${target:02X} = ${val:02X}"))
                i += 4
                continue

        # --- LDA $target / CMP #$VV / BEQ/BNE ?? ---
        # Das volle Pattern ist 7 byte: AD LL HH C9 VV Fx YY
        if op == 0xAD and i + 6 < n:
            if (_addr_le(data, i + 1) == target
                    and data[i + 3] == 0xC9
                    and data[i + 5] in (0xF0, 0xD0, 0x10, 0x30)):
                # Branch-Mnemonics:
                # F0=BEQ, D0=BNE, 10=BPL, 30=BMI
                val = data[i + 4]
                br_op = data[i + 5]
                br_off = data[i + 6]
                if br_off >= 0x80:
                    br_off -= 0x100
                br_target = (base_addr + i + 7 + br_off) & 0xFFFF
                br_mn = {0xF0: "BEQ", 0xD0: "BNE",
                           0x10: "BPL", 0x30: "BMI"}[br_op]
                pc = (base_addr + i) & 0xFFFF
                compares.append(CodePatternHit(
                    pc, list(data[i:i+7]), "compare_branch",
                    f"LDA ${target:04X} / CMP #${val:02X} / "
                    f"{br_mn} ${br_target:04X}",
                    note=f"compares with ${val:02X}, branches to "
                          f"${br_target:04X}"))
                i += 7
                continue
            # Kuerzeres Pattern: LDA + CMP ohne Branch
            if (_addr_le(data, i + 1) == target
                    and data[i + 3] == 0xC9):
                val = data[i + 4]
                pc = (base_addr + i) & 0xFFFF
                compares.append(CodePatternHit(
                    pc, list(data[i:i+5]), "compare",
                    f"LDA ${target:04X} / CMP #${val:02X}",
                    note=f"compares with ${val:02X} (no immediate "
                          f"branch)"))
                i += 5
                continue

        # --- LDA $LL (zp) / CMP #$VV / BEQ/BNE ?? ---
        if is_zp and op == 0xA5 and i + 5 < n:
            if (data[i + 1] == tgt_lo
                    and data[i + 2] == 0xC9
                    and data[i + 4] in (0xF0, 0xD0, 0x10, 0x30)):
                val = data[i + 3]
                br_op = data[i + 4]
                br_off = data[i + 5]
                if br_off >= 0x80:
                    br_off -= 0x100
                br_target = (base_addr + i + 6 + br_off) & 0xFFFF
                br_mn = {0xF0: "BEQ", 0xD0: "BNE",
                           0x10: "BPL", 0x30: "BMI"}[br_op]
                pc = (base_addr + i) & 0xFFFF
                compares.append(CodePatternHit(
                    pc, list(data[i:i+6]), "compare_branch",
                    f"LDA ${target:02X} / CMP #${val:02X} / "
                    f"{br_mn} ${br_target:04X}",
                    note=f"compares with ${val:02X}, branches to "
                          f"${br_target:04X}"))
                i += 6
                continue

        i += 1

    return {"mods": mods, "compares": compares}
