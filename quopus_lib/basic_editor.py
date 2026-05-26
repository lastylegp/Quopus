"""BASIC v2 editor for the C64 with petcat-style PETSCII control
codes, syntax highlighting, validation, tokenization, and direct
send-and-run via the U64 REST API.

UI layout:
    +-- title ----------------------------------------+
    | [Open] [Save] [Validate] [Send & Run] [Close]   |
    +-- toolbar --------------------------------------+
    | line# | keywords | strings | REM | {ctrl codes} |
    +-- text editor (with syntax-coloured BASIC) -----+
    | 10 PRINT "{CLR}HELLO {RVS ON}WORLD{RVS OFF}"   |
    | 20 GOTO 10                                      |
    +-- status bar -----------------------------------+
    | line 2 col 18  |  validation: OK                |

PETSCII control codes accepted in strings:
    {CLR} {HOME} {RVS ON} {RVS OFF} {WHT} {RED} {CYAN} ...
    Color codes: BLK WHT RED CYAN PUR GRN BLU YEL ORG BRN PINK
                  DARK_GREY GREY LT_GREEN LT_BLUE LT_GREY
    Cursor:     UP DOWN LEFT RIGHT HOME CLR
    Format:     RVS_ON RVS_OFF
    Numeric:    {$XX} = raw PETSCII byte XX hex
                {NNN} = raw PETSCII byte NNN decimal (0-255)
    Repeat:     {3 SPACE} = 3 spaces, {5 RIGHT} = 5 cursor-right

Tokenizer produces a real C64 BASIC PRG file with 2-byte load
address $0801 + tokenized lines. Result is sendable as a PRG via
u64_run_prg() exactly like any compiled program.
"""

from __future__ import annotations

import re
from typing import List, Tuple, Optional

from PyQt6.QtCore import Qt, QRegularExpression, pyqtSignal
from PyQt6.QtGui import (
    QFont, QColor, QSyntaxHighlighter, QTextCharFormat,
    QFontDatabase, QKeySequence, QShortcut, QTextCursor,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QMessageBox, QFileDialog,
)
from .config import scaled_font_px


# ---------------------------------------------------------------------
# BASIC v2 keyword token table
# ---------------------------------------------------------------------
# Token values $80-$CB per the Commodore 64 Programmer's Reference
# Guide. Order matters for the tokenizer: longer keywords first so
# that e.g. 'GOSUB' matches before 'GO'.

BASIC_V2_TOKENS = [
    # (keyword, token_byte)
    ("END", 0x80), ("FOR", 0x81), ("NEXT", 0x82), ("DATA", 0x83),
    ("INPUT#", 0x84), ("INPUT", 0x85), ("DIM", 0x86), ("READ", 0x87),
    ("LET", 0x88), ("GOTO", 0x89), ("RUN", 0x8A), ("IF", 0x8B),
    ("RESTORE", 0x8C), ("GOSUB", 0x8D), ("RETURN", 0x8E),
    ("REM", 0x8F), ("STOP", 0x90), ("ON", 0x91), ("WAIT", 0x92),
    ("LOAD", 0x93), ("SAVE", 0x94), ("VERIFY", 0x95), ("DEF", 0x96),
    ("POKE", 0x97), ("PRINT#", 0x98), ("PRINT", 0x99), ("CONT", 0x9A),
    ("LIST", 0x9B), ("CLR", 0x9C), ("CMD", 0x9D), ("SYS", 0x9E),
    ("OPEN", 0x9F), ("CLOSE", 0xA0), ("GET", 0xA1), ("NEW", 0xA2),
    ("TAB(", 0xA3), ("TO", 0xA4), ("FN", 0xA5), ("SPC(", 0xA6),
    ("THEN", 0xA7), ("NOT", 0xA8), ("STEP", 0xA9),
    # Operators
    ("+", 0xAA), ("-", 0xAB), ("*", 0xAC), ("/", 0xAD), ("^", 0xAE),
    ("AND", 0xAF), ("OR", 0xB0), (">", 0xB1), ("=", 0xB2), ("<", 0xB3),
    # Functions
    ("SGN", 0xB4), ("INT", 0xB5), ("ABS", 0xB6), ("USR", 0xB7),
    ("FRE", 0xB8), ("POS", 0xB9), ("SQR", 0xBA), ("RND", 0xBB),
    ("LOG", 0xBC), ("EXP", 0xBD), ("COS", 0xBE), ("SIN", 0xBF),
    ("TAN", 0xC0), ("ATN", 0xC1), ("PEEK", 0xC2), ("LEN", 0xC3),
    ("STR$", 0xC4), ("VAL", 0xC5), ("ASC", 0xC6), ("CHR$", 0xC7),
    ("LEFT$", 0xC8), ("RIGHT$", 0xC9), ("MID$", 0xCA), ("GO", 0xCB),
]

# Sorted by descending keyword length for greedy match
BASIC_V2_TOKENS_SORTED = sorted(
    BASIC_V2_TOKENS, key=lambda kv: -len(kv[0]))


# ---------------------------------------------------------------------
# PETSCII control code names
# ---------------------------------------------------------------------
# Maps {NAME} to a raw PETSCII byte. Petcat-style names are
# case-insensitive; underscores are accepted as spaces.

PETSCII_CTRL_CODES = {
    # Control
    "STOP":     0x03,
    "WHT":      0x05,
    "WHITE":    0x05,
    "DISABLE_SHIFT": 0x08,
    "ENABLE_SHIFT":  0x09,
    "RETURN":   0x0D,
    "RET":      0x0D,
    "LOWER_CASE": 0x0E,
    "DOWN":     0x11,
    "CURSOR_DOWN": 0x11,
    "RVS_ON":   0x12,
    "RVSON":    0x12,
    "REVERSE_ON": 0x12,
    "HOME":     0x13,
    "DEL":      0x14,
    "DELETE":   0x14,
    "RED":      0x1C,
    "RIGHT":    0x1D,
    "CURSOR_RIGHT": 0x1D,
    "GRN":      0x1E,
    "GREEN":    0x1E,
    "BLU":      0x1F,
    "BLUE":     0x1F,
    "ORANGE":   0x81,
    "ORG":      0x81,
    "F1":       0x85,
    "F3":       0x86,
    "F5":       0x87,
    "F7":       0x88,
    "F2":       0x89,
    "F4":       0x8A,
    "F6":       0x8B,
    "F8":       0x8C,
    "SHIFT_RETURN": 0x8D,
    "UPPER_CASE":   0x8E,
    "BLK":      0x90,
    "BLACK":    0x90,
    "UP":       0x91,
    "CURSOR_UP": 0x91,
    "RVS_OFF":  0x92,
    "RVSOFF":   0x92,
    "REVERSE_OFF": 0x92,
    "CLR":      0x93,
    "CLEAR":    0x93,
    "INST":     0x94,
    "BROWN":    0x95,
    "BRN":      0x95,
    "PINK":     0x96,
    "LT_RED":   0x96,
    "DARK_GREY": 0x97,
    "DARK_GRAY": 0x97,
    "GREY":     0x98,
    "GRAY":     0x98,
    "LT_GREEN": 0x99,
    "LT_GRN":   0x99,
    "LT_BLUE":  0x9A,
    "LT_BLU":   0x9A,
    "LT_GREY":  0x9B,
    "LT_GRAY":  0x9B,
    "PUR":      0x9C,
    "PURPLE":   0x9C,
    "LEFT":     0x9D,
    "CURSOR_LEFT": 0x9D,
    "YEL":      0x9E,
    "YELLOW":   0x9E,
    "CYN":      0x9F,
    "CYAN":     0x9F,
    # Common synonyms
    "SPACE":    0x20,
    "SP":       0x20,
}


def _normalize_ctrl_name(name: str) -> str:
    """Lowercase + underscores -> uppercase + underscores
    canonical lookup form for PETSCII_CTRL_CODES."""
    return name.strip().upper().replace(" ", "_")


# Match {NAME}, {$XX}, {NNN}, or {N NAME} (repeated)
CTRL_RE = re.compile(r'\{([^{}]+)\}')
# Inside the braces we also accept "<count> <name>" form
COUNT_PREFIX_RE = re.compile(r'^(\d+)\s+(.+)$')


def expand_petscii_codes(text: str) -> Tuple[bytes, List[str]]:
    """Convert a BASIC source line containing {CLR}/{$93}/{5 SPACE}
    etc. into raw PETSCII bytes. Returns (bytes, errors_list).

    Outside of {} the input is converted character-by-character with
    a simple ASCII-to-PETSCII upper/lower mapping (the C64 default
    boot mode is upper/graphics, so unshifted A-Z = upper case PETSCII
    $41-$5A).

    Inside {} we recognize:
        - $XX = raw PETSCII byte XX hex (1-2 hex digits)
        - NNN = raw PETSCII byte NNN decimal (0-255)
        - NAME = look up PETSCII_CTRL_CODES
        - <count> <name|hex|dec> = repeat the resolved byte `count` times
    """
    errors = []
    out = bytearray()
    pos = 0
    n = len(text)
    while pos < n:
        m = CTRL_RE.search(text, pos)
        if m is None:
            # Rest is literal
            chunk = text[pos:]
            out.extend(_ascii_to_petscii(chunk))
            break
        # Literal up to this control code
        if m.start() > pos:
            out.extend(_ascii_to_petscii(text[pos:m.start()]))
        # Parse the control code
        body = m.group(1).strip()
        # Check for count-prefix
        cm = COUNT_PREFIX_RE.match(body)
        if cm:
            try:
                count = int(cm.group(1))
            except ValueError:
                errors.append(f"bad count in {{{body}}}")
                count = 1
            inner = cm.group(2).strip()
        else:
            count = 1
            inner = body
        byte = _resolve_ctrl_inner(inner, errors)
        if byte is not None:
            out.extend(bytes([byte]) * count)
        pos = m.end()
    return bytes(out), errors


def _resolve_ctrl_inner(inner: str, errors: list) -> Optional[int]:
    """Resolve one inner-of-braces token to a PETSCII byte."""
    # $XX hex form
    if inner.startswith("$"):
        try:
            v = int(inner[1:], 16)
            if 0 <= v <= 255:
                return v
        except ValueError:
            pass
        errors.append(f"bad hex byte: {{{inner}}}")
        return None
    # Pure decimal
    if inner.isdigit():
        v = int(inner)
        if 0 <= v <= 255:
            return v
        errors.append(f"byte out of range: {{{inner}}}")
        return None
    # Named code
    canonical = _normalize_ctrl_name(inner)
    if canonical in PETSCII_CTRL_CODES:
        return PETSCII_CTRL_CODES[canonical]
    errors.append(f"unknown control code: {{{inner}}}")
    return None


def _ascii_to_petscii(s: str) -> bytes:
    """Naive ASCII-to-PETSCII for unshifted upper/graphics mode.

    The C64 default boot mode shows uppercase A-Z when you type
    unshifted A-Z. In screen RAM these are PETSCII $41-$5A. So we
    just map directly with two adjustments:
        - lowercase a-z -> swap-case to $C1-$DA (PETSCII shifted
          letters = lowercase visually in shifted-mode tunes, but in
          BASIC source they're typically uppercase anyway)
        - keep printables in $20-$60 as-is
    For BASIC source the simplest mapping that round-trips through
    detokenize/list is upper.
    """
    out = bytearray()
    for ch in s:
        c = ord(ch)
        if 0x61 <= c <= 0x7A:
            # lowercase ascii -> petscii shifted ($C1-$DA, visible as
            # lowercase in shifted mode, uppercase in unshifted)
            out.append(c - 0x60 + 0xC0)
        elif 0x41 <= c <= 0x5A:
            # uppercase -> petscii unshifted graphics ($C1-$DA gives
            # uppercase in upper/gfx, lowercase in lower/upper)
            # but for normal BASIC source $41-$5A is fine
            out.append(c)
        elif c < 256:
            out.append(c)
        else:
            # Non-Latin: skip
            pass
    return bytes(out)


# ---------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------

class TokenizerError(Exception):
    """Raised when BASIC source can't be tokenized into a PRG."""


def tokenize_basic(source: str) -> Tuple[bytes, List[str]]:
    """Tokenize a BASIC v2 source listing into a runnable PRG.

    Source is split into lines. Each line must start with a line
    number followed by tokens, keywords, strings, numerics. Output
    PRG layout:
        $0801: load address (2 bytes little-endian = $01 $08)
        per line:
            next_addr (2 bytes LE) - address of next line
            line_num  (2 bytes LE)
            tokens... + NUL terminator
        terminating $00 $00 (empty next_addr)

    Returns (prg_bytes, warnings_list). Raises TokenizerError on
    fatal errors (bad line numbers, unclosed strings, etc.).
    """
    warnings = []
    parsed_lines = []   # list of (line_num, tokenized_bytes)
    last_line_num = -1

    for raw_line in source.splitlines():
        if not raw_line.strip():
            continue
        line_num, body = _parse_line_number(raw_line)
        if line_num is None:
            raise TokenizerError(
                f"line missing line number: {raw_line!r}")
        if line_num < 0 or line_num > 63999:
            raise TokenizerError(
                f"line number {line_num} out of range (0..63999)")
        if line_num <= last_line_num:
            warnings.append(
                f"line {line_num} not greater than previous "
                f"line {last_line_num} (BASIC will accept but "
                "LIST will be out of order)")
        last_line_num = line_num
        tokens = _tokenize_line_body(body, warnings)
        parsed_lines.append((line_num, tokens))

    # Build PRG
    out = bytearray()
    out.extend(b"\x01\x08")    # load address $0801
    cur_addr = 0x0801
    for line_num, tokens in parsed_lines:
        # 2 bytes next_addr + 2 bytes line_num + tokens + NUL
        line_size = 5 + len(tokens)
        next_addr = cur_addr + line_size
        out.append(next_addr & 0xFF)
        out.append((next_addr >> 8) & 0xFF)
        out.append(line_num & 0xFF)
        out.append((line_num >> 8) & 0xFF)
        out.extend(tokens)
        out.append(0)
        cur_addr = next_addr
    # Terminator: 2 NUL bytes at the next_addr slot of the next line
    out.append(0)
    out.append(0)
    return bytes(out), warnings


_LINE_NUM_RE = re.compile(r'^\s*(\d{1,5})\s*(.*)$')


def _parse_line_number(line: str):
    m = _LINE_NUM_RE.match(line)
    if not m:
        return None, None
    try:
        return int(m.group(1)), m.group(2)
    except ValueError:
        return None, None


def _tokenize_line_body(body: str, warnings: list) -> bytes:
    """Tokenize one body (everything after the line number)."""
    out = bytearray()
    i = 0
    n = len(body)
    in_string = False
    string_start = -1
    while i < n:
        ch = body[i]
        if in_string:
            if ch == '"':
                out.append(ord('"'))
                in_string = False
                i += 1
                continue
            if ch == "{":
                # PETSCII control code inside string
                close = body.find("}", i + 1)
                if close < 0:
                    warnings.append(
                        "unclosed control code in string "
                        f"starting at pos {i}")
                    out.append(ord(ch))
                    i += 1
                    continue
                inner = body[i + 1:close]
                cm = COUNT_PREFIX_RE.match(inner)
                if cm:
                    count = int(cm.group(1))
                    name = cm.group(2).strip()
                else:
                    count = 1
                    name = inner.strip()
                byte = _resolve_ctrl_inner(name, warnings)
                if byte is not None:
                    out.extend(bytes([byte]) * count)
                i = close + 1
                continue
            # Plain string char
            out.append(ord(ch) & 0xFF)
            i += 1
            continue
        # Not in string
        if ch == '"':
            out.append(ord('"'))
            in_string = True
            string_start = i
            i += 1
            continue
        if ch == ' ':
            out.append(ord(' '))
            i += 1
            continue
        # Try keyword match
        matched = False
        upper_body = body[i:].upper()
        for kw, token in BASIC_V2_TOKENS_SORTED:
            if upper_body.startswith(kw):
                # Special case: REM consumes the rest of the line
                # verbatim (no further tokenizing).
                out.append(token)
                i += len(kw)
                if token == 0x8F:   # REM
                    out.extend(body[i:].encode('latin-1',
                                                  errors='replace'))
                    return bytes(out)
                if token == 0x83:   # DATA - similar, rest is literal
                    # ... but DATA can contain : that ends the statement
                    rest_start = i
                    while i < n and body[i] != ':':
                        out.append(ord(body[i]))
                        i += 1
                    matched = True
                    break
                matched = True
                break
        if matched:
            continue
        # No keyword: pass char through, upper-case it (PETSCII
        # convention for variable names is uppercase)
        out.append(ord(ch.upper()) & 0xFF)
        i += 1
    if in_string:
        warnings.append(
            f"unclosed string starting at pos {string_start}")
    return bytes(out)


# ---------------------------------------------------------------------
# Syntax highlighter
# ---------------------------------------------------------------------

class _BasicHighlighter(QSyntaxHighlighter):
    """Colorize BASIC v2: line numbers, keywords, strings, REM, ctrl
    codes within strings. Designed for a dark editor background."""

    # Colors
    COL_LINENUM = QColor(180, 180, 180)
    COL_KEYWORD = QColor(255, 200, 80)
    COL_STRING  = QColor(180, 255, 180)
    COL_CTRL    = QColor(255, 120, 255)
    COL_REM     = QColor(120, 120, 120)
    COL_NUM     = QColor(150, 200, 255)

    def __init__(self, parent):
        super().__init__(parent)
        self._fmt_line   = self._mkfmt(self.COL_LINENUM, bold=True)
        self._fmt_kw     = self._mkfmt(self.COL_KEYWORD, bold=True)
        self._fmt_string = self._mkfmt(self.COL_STRING)
        self._fmt_ctrl   = self._mkfmt(self.COL_CTRL, bold=True)
        self._fmt_rem    = self._mkfmt(self.COL_REM, italic=True)
        self._fmt_num    = self._mkfmt(self.COL_NUM)
        # Build keyword regex
        kws = [re.escape(kw) for kw, _ in BASIC_V2_TOKENS_SORTED
               if kw[0].isalpha()]
        # Word-boundary on the right; left-side is implicit
        self._kw_re = re.compile(
            r'\b(' + '|'.join(kws) + r')\b',
            re.IGNORECASE)

    def _mkfmt(self, color, bold=False, italic=False):
        f = QTextCharFormat()
        f.setForeground(color)
        if bold:
            f.setFontWeight(QFont.Weight.Bold)
        if italic:
            f.setFontItalic(True)
        return f

    def highlightBlock(self, text):
        # Leading line number
        m = re.match(r'^\s*(\d+)', text)
        if m:
            self.setFormat(m.start(1), len(m.group(1)),
                            self._fmt_line)
        # REM: from REM keyword to end of line, colored as comment
        m_rem = re.search(r'\bREM\b', text, re.IGNORECASE)
        rem_start = m_rem.start() if m_rem else len(text)
        # Strings: " ... ", with ctrl codes inside
        i = 0
        while i < len(text):
            if i >= rem_start:
                break
            if text[i] == '"':
                end = text.find('"', i + 1)
                if end == -1:
                    end = len(text)
                else:
                    end += 1   # include closing quote
                # Format the whole string
                self.setFormat(i, end - i, self._fmt_string)
                # Then highlight ctrl codes
                for cm in re.finditer(r'\{[^{}]+\}',
                                          text[i:end]):
                    self.setFormat(i + cm.start(),
                                     cm.end() - cm.start(),
                                     self._fmt_ctrl)
                i = end
                continue
            i += 1
        # Keywords (outside strings and REM)
        for km in self._kw_re.finditer(text):
            ks, ke = km.start(), km.end()
            if ks >= rem_start:
                break
            # Skip if inside a string (check by looking at any
            # existing format - cheaper than re-scanning)
            existing = self.format(ks)
            if existing.foreground().color() == self.COL_STRING:
                continue
            self.setFormat(ks, ke - ks, self._fmt_kw)
        # REM trailing text
        if m_rem:
            self.setFormat(m_rem.start(), len(text) - m_rem.start(),
                            self._fmt_rem)


# ---------------------------------------------------------------------
# BASIC editor dialog
# ---------------------------------------------------------------------

class BasicEditorDialog(QDialog):
    """Modeless editor for C64 BASIC v2 programs.

    Workflow:
    1. Type or load a .bas file
    2. Click Validate to check tokenizability without sending
    3. Click Send & Run to tokenize + upload as PRG + auto-run
    4. Optionally Save as .prg or .bas

    The send_callback receives (prg_bytes, line_count) when the
    user clicks Send & Run; it's expected to do the actual U64 POST.
    """

    # Default sample so a fresh editor isn't blank
    DEFAULT_SOURCE = (
        '10 PRINT "{CLR}{WHT}HELLO {RVS_ON}WORLD{RVS_OFF}!"\n'
        '20 FOR I=1 TO 10\n'
        '30 PRINT I\n'
        '40 NEXT I\n'
        '50 GOTO 10\n'
    )

    sent = pyqtSignal(bytes, int)  # prg_bytes, line_count

    def __init__(self, parent=None, send_callback=None):
        super().__init__(parent)
        self.setWindowTitle("C64 BASIC v2 Editor")
        self.resize(720, 520)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "basic_editor")
        self._send_callback = send_callback

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(4)
        btn_new = QPushButton("New")
        btn_new.setFixedWidth(50)
        btn_new.clicked.connect(self._on_new)
        bar.addWidget(btn_new)
        btn_open = QPushButton("Open...")
        btn_open.setFixedWidth(70)
        btn_open.clicked.connect(self._on_open)
        bar.addWidget(btn_open)
        btn_save = QPushButton("Save...")
        btn_save.setFixedWidth(70)
        btn_save.setToolTip(
            "Save as .bas text or .prg tokenized BASIC. The\n"
            "format is picked from the extension you choose.")
        btn_save.clicked.connect(self._on_save)
        bar.addWidget(btn_save)
        bar.addSpacing(12)
        btn_validate = QPushButton("Validate")
        btn_validate.setFixedWidth(80)
        btn_validate.setToolTip(
            "Try tokenizing the source. Reports unbalanced\n"
            "strings, unknown control codes, descending line\n"
            "numbers, etc. Doesn't send anything to the C64.")
        btn_validate.clicked.connect(self._on_validate)
        bar.addWidget(btn_validate)
        btn_send = QPushButton("Send && Run")
        btn_send.setStyleSheet(
            "QPushButton { font-weight: bold; "
            "background-color: #4a8; color: white; }")
        btn_send.setFixedWidth(110)
        btn_send.setToolTip(
            "Tokenize, upload as PRG via REST API, and auto-run\n"
            "on the C64. Requires a configured U64 host.")
        btn_send.clicked.connect(self._on_send)
        bar.addWidget(btn_send)
        bar.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.setFixedWidth(60)
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        outer.addLayout(bar)

        # Help line
        help_lbl = QLabel(
            "PETSCII control codes inside strings: "
            "<code>{CLR} {HOME} {RVS_ON} {RVS_OFF} {WHT} {RED} "
            "{CYN} {YEL} {DOWN} {RIGHT} {$93} {147}</code> "
            "&middot; repeat: <code>{5 SPACE}</code>")
        help_lbl.setStyleSheet(
            "padding: 3px 6px; background: #303030; color: #ccc; "
            f"font-size: {scaled_font_px(10)}px;")
        help_lbl.setWordWrap(True)
        outer.addWidget(help_lbl)

        # Editor
        self.editor = QPlainTextEdit()
        # Use C64 Pro Mono if available, fall back to mono
        font = QFont()
        font.setFamilies([
            "Cascadia Mono", "Consolas", "DejaVu Sans Mono",
            "Liberation Mono", "monospace",
        ])
        font.setPixelSize(14)
        self.editor.setFont(font)
        self.editor.setStyleSheet(
            "QPlainTextEdit { background-color: #1e1e1e; "
            "color: #e0e0e0; }")
        self.editor.setTabStopDistance(40)
        self.editor.setPlainText(self.DEFAULT_SOURCE)
        outer.addWidget(self.editor, 1)
        self._highlighter = _BasicHighlighter(self.editor.document())

        # Status bar
        self.lbl_status = QLabel(" ready ")
        self.lbl_status.setStyleSheet(
            "padding: 4px; background: #303030; color: #ccc;")
        outer.addWidget(self.lbl_status)

        # Hotkeys
        QShortcut(QKeySequence.StandardKey.Save, self,
                    activated=self._on_save)
        QShortcut(QKeySequence.StandardKey.Open, self,
                    activated=self._on_open)
        QShortcut(QKeySequence("Ctrl+R"), self,
                    activated=self._on_send)
        QShortcut(QKeySequence("F5"), self,
                    activated=self._on_send)
        QShortcut(QKeySequence("F7"), self,
                    activated=self._on_validate)

    def _on_new(self):
        if self.editor.toPlainText().strip():
            reply = QMessageBox.question(
                self, "New",
                "Discard the current source?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.editor.setPlainText(self.DEFAULT_SOURCE)
        self.lbl_status.setText(" new ")

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open BASIC source",
            "", "BASIC files (*.bas *.txt *.b);;All files (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8',
                        errors='replace') as f:
                self.editor.setPlainText(f.read())
        except OSError as e:
            QMessageBox.warning(self, "Open", f"Failed:\n{e}")
            return
        self.lbl_status.setText(f" loaded: {path} ")

    def _on_save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save",
            "", "BASIC text (*.bas);;Tokenized PRG (*.prg);;"
                "All files (*)")
        if not path:
            return
        import os
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".prg":
                prg, warnings = tokenize_basic(
                    self.editor.toPlainText())
                with open(path, 'wb') as f:
                    f.write(prg)
                if warnings:
                    self.lbl_status.setText(
                        f" saved PRG with {len(warnings)} warnings ")
                else:
                    self.lbl_status.setText(
                        f" saved PRG ({len(prg)} bytes) ")
            else:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(self.editor.toPlainText())
                self.lbl_status.setText(f" saved {path} ")
        except (OSError, TokenizerError) as e:
            QMessageBox.warning(self, "Save", f"Failed:\n{e}")

    def _on_validate(self):
        try:
            prg, warnings = tokenize_basic(
                self.editor.toPlainText())
        except TokenizerError as e:
            QMessageBox.warning(
                self, "Validation failed", str(e))
            self.lbl_status.setText(f" error: {e} ")
            return
        if warnings:
            msg = (
                f"OK ({len(prg)} bytes) with "
                f"{len(warnings)} warnings:\n\n"
                + "\n".join("  " + w for w in warnings[:20]))
            if len(warnings) > 20:
                msg += f"\n  ... and {len(warnings) - 20} more"
            QMessageBox.information(self, "Validation - warnings",
                                       msg)
            self.lbl_status.setText(
                f" OK with warnings ({len(warnings)}) ")
        else:
            QMessageBox.information(
                self, "Validation - OK",
                f"Source tokenizes cleanly to a "
                f"{len(prg)}-byte PRG.")
            self.lbl_status.setText(
                f" valid: {len(prg)} bytes ")

    def _on_send(self):
        try:
            prg, warnings = tokenize_basic(
                self.editor.toPlainText())
        except TokenizerError as e:
            QMessageBox.warning(self, "Send failed",
                f"Tokenize error:\n{e}")
            return
        if warnings:
            reply = QMessageBox.warning(
                self, "Send with warnings",
                f"Tokenized to {len(prg)} bytes but with "
                f"{len(warnings)} warnings:\n\n"
                + "\n".join("  " + w for w in warnings[:10])
                + "\n\nSend anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        line_count = sum(
            1 for ln in self.editor.toPlainText().splitlines()
            if ln.strip())
        if self._send_callback is not None:
            try:
                self._send_callback(prg, line_count)
            except Exception as e:
                QMessageBox.warning(
                    self, "Send", f"Failed:\n{e}")
                return
            self.lbl_status.setText(
                f" sent {len(prg)} bytes ({line_count} lines) ")
        else:
            self.sent.emit(prg, line_count)
            self.lbl_status.setText(
                f" emit signal: {len(prg)} bytes ")
