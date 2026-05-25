"""
AmigaGuide hypertext viewer.
Supports the most common AmigaGuide markup:
  @DATABASE / @AUTHOR / @VERSION / @TITLE / @$VER (header)
  @NODE name "title" ... @ENDNODE
  @TOC nodename, @PREV nodename, @NEXT nodename, @INDEX nodename
  @{ "label" LINK "node" [line] }    text->node link
  @{ "label" GUIDE "file/node" }     cross-file link
  @{ "label" SYSTEM "cmd" }          shown but inert (we won't run shell)
  @{ "label" RX  "..." }             shown but inert
  @{b}/@{ub}                         bold on/off
  @{i}/@{ui}                         italic on/off
  @{u}/@{uu}                         underline on/off
  @{fg COLOR} / @{bg COLOR}          foreground / background highlight
  @REM ...                           comment line, hidden
  Empty inline brace tags @{ } are dropped.

Navigation:
  Click any link, or press Enter on the focused link
  Back / Forward buttons + Alt+Left/Right
  Contents (jumps to MAIN/TOC), Index, Help
  Esc closes the viewer
"""
import re
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import (
    QTextDocument, QFont, QShortcut, QKeySequence, QTextCursor,
    QAction,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QMessageBox,
)

from .palette import (
    C, WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS, SCROLLBAR_QSS,
    button_qss, get_topaz_font,
)


# =====================================================================
# Parser
# =====================================================================
class _Node:
    __slots__ = ('name', 'title', 'lines', 'prev', 'next_', 'toc', 'index')
    def __init__(self, name, title=""):
        self.name = name
        self.title = title or name
        self.lines = []          # raw body text
        self.prev = None
        self.next_ = None
        self.toc = None
        self.index = None


class AmigaGuideDoc:
    """Parses an AmigaGuide file into nodes + global properties."""

    HEADER_TAGS = (
        "DATABASE", "AUTHOR", "VERSION", "$VER", "TITLE",
        "TOC", "INDEX", "HELP", "MASTER", "WIDTH", "FONT",
        "WORDWRAP", "SMARTWRAP", "TAB",
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        self.nodes = {}                # lower-case-name -> _Node
        self.first_node_name = None    # name of first defined node
        self.title = self.path.name
        self.author = ""
        self.version = ""
        self.global_toc = None         # filename or "name"
        self.global_index = None
        self.global_help = None
        self._parse()

    # ---- helpers ----
    @staticmethod
    def _decode(data: bytes) -> str:
        # AmigaGuide is normally ISO-8859-1
        for enc in ("utf-8", "iso-8859-1", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("iso-8859-1", errors="replace")

    def _parse(self):
        try:
            data = self.path.read_bytes()
        except Exception as e:
            raise RuntimeError(f"Cannot read file: {e}")
        text = self._decode(data)

        # Normalize newlines
        text = text.replace('\r\n', '\n').replace('\r', '\n')

        cur_node = None
        for raw_line in text.split('\n'):
            line = raw_line.rstrip()

            # Lines starting with '@' are commands (only when in column 0).
            # Everything else is body text and belongs to the current node.
            if line.startswith('@'):
                if self._handle_command(line, cur_node) is True:
                    # node started
                    cur_node = self._last_started_node
                    continue
                # @ENDNODE
                if line.upper().startswith('@ENDNODE'):
                    cur_node = None
                    continue
                # Other global / header commands
                continue

            if cur_node is not None:
                cur_node.lines.append(line)
            # If outside nodes, ignore body text (front matter)

        # If no explicit MAIN/TOC found, fall back to first defined node
        if not self.global_toc and self.first_node_name:
            self.global_toc = self.first_node_name

    def _handle_command(self, line, cur_node):
        """Process a global @-command. Returns True if a new node started."""
        upper = line.upper()
        # --- @NODE name "title"  OR  @NODE "name with spaces" ["title"]
        if upper.startswith('@NODE'):
            rest = line[len('@NODE'):].strip()
            # Try quoted name first, then bareword name
            m = re.match(r'"([^"]+)"\s*(?:"([^"]*)")?', rest)
            if not m:
                m = re.match(r'(\S+)\s*(?:"([^"]*)")?', rest)
            if not m:
                return False
            name = m.group(1)
            title = m.group(2) or name
            n = _Node(name, title)
            self.nodes[name.lower()] = n
            if self.first_node_name is None:
                self.first_node_name = name
            self._last_started_node = n
            return True

        # --- per-node nav (set on currently open node)
        for tag, attr in (("@PREV", "prev"), ("@NEXT", "next_"),
                          ("@TOC", "toc"), ("@INDEX", "index")):
            if upper.startswith(tag):
                val = line[len(tag):].strip().strip('"')
                if cur_node is not None:
                    setattr(cur_node, attr, val)
                else:
                    # global TOC/INDEX/HELP
                    if tag == "@TOC":   self.global_toc = val
                    if tag == "@INDEX": self.global_index = val
                return False

        if upper.startswith('@HELP'):
            self.global_help = line[len('@HELP'):].strip().strip('"')
            return False
        if upper.startswith('@TITLE'):
            self.title = line[len('@TITLE'):].strip().strip('"')
            return False
        if upper.startswith('@AUTHOR'):
            self.author = line[len('@AUTHOR'):].strip().strip('"')
            return False
        if upper.startswith('@VERSION') or upper.startswith('@$VER'):
            self.version = line[line.find(' ')+1:].strip().strip('"')
            return False
        if upper.startswith('@REM'):
            return False
        if upper.startswith('@DATABASE'):
            db = line[len('@DATABASE'):].strip().strip('"')
            if db:
                self.title = db
            return False
        # Other tags we silently ignore (FONT, WIDTH, etc.)
        return False


# =====================================================================
# Inline-markup -> HTML converter
# =====================================================================
def _esc(s):
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;'))


# AmigaGuide colour names -> high-contrast palette tuned for our black
# viewer background. The original Amiga colours rely on the system palette
# at runtime; reproducing them literally (e.g. yellow-on-yellow) makes
# text unreadable here, so we map every name to a colour that contrasts
# well on black.
_AG_COLORS = {
    "TEXT":       "#dddddd",
    "SHINE":      "#ffffff",
    "SHADOW":     "#888888",
    "FILL":       "#888888",
    "FILLTEXT":   "#ffffff",
    "BACKGROUND": "#dddddd",
    "HIGHLIGHT":  "#ffff80",   # soft yellow, readable on black
    "DARK":       "#aaaaaa",
    "LIGHT":      "#ffffff",
}


def render_node_html(node: _Node) -> str:
    """Convert a node's body to HTML with embedded links + styling."""
    body = '\n'.join(node.lines)

    # We'll walk the text and emit HTML chunks. AmigaGuide commands look
    # like @{...}, possibly nested with quoted args.
    out = []
    i = 0
    n = len(body)
    style_stack = []   # list of currently-open html tags to close later
    fg = None
    bg = None

    def open_tag(tag):
        out.append(f"<{tag}>")
        style_stack.append(tag)

    def close_tag(tag):
        # Close any inner tags first, then this one (simple pairing)
        if tag in style_stack:
            # close from top until tag found
            inner = []
            while style_stack and style_stack[-1] != tag:
                t = style_stack.pop()
                out.append(f"</{t}>")
                inner.append(t)
            if style_stack and style_stack[-1] == tag:
                t = style_stack.pop()
                out.append(f"</{t}>")
            # reopen the inner tags so the stack is consistent
            for t in reversed(inner):
                out.append(f"<{t}>")
                style_stack.append(t)

    while i < n:
        ch = body[i]
        if ch == '@' and i + 1 < n and body[i+1] == '{':
            # parse the tag content up to matching }
            j = i + 2
            depth = 1
            in_quote = False
            while j < n and depth > 0:
                cj = body[j]
                if cj == '"' and (j == 0 or body[j-1] != '\\'):
                    in_quote = not in_quote
                elif not in_quote:
                    if cj == '{':
                        depth += 1
                    elif cj == '}':
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            inner = body[i+2:j]
            i = j + 1
            html = _convert_tag(inner)
            if html is not None:
                out.append(html)
            continue

        # Plain char
        if ch == '\n':
            out.append('<br>\n')
            i += 1
            continue
        if ch == ' ':
            out.append('&nbsp;')
            i += 1
            continue
        out.append(_esc(ch))
        i += 1

    # Close any still-open style tags
    while style_stack:
        t = style_stack.pop()
        out.append(f"</{t}>")
    return ''.join(out)


def _convert_tag(inner: str):
    """Convert one @{...} tag's interior to HTML. None if it should be
    completely omitted from output."""
    s = inner.strip()
    if not s:
        return ""

    upper = s.upper()
    # Style on/off
    if upper == "B":  return "<b>"
    if upper == "UB": return "</b>"
    if upper == "I":  return "<i>"
    if upper == "UI": return "</i>"
    if upper == "U":  return "<u>"
    if upper == "UU": return "</u>"
    if upper == "PLAIN" or upper == "STDFORMAT":
        return "</b></i></u>"   # best-effort reset
    if upper.startswith("FG "):
        col = upper[3:].strip()
        return f'<span style="color:{_AG_COLORS.get(col, col.lower())}">'
    if upper.startswith("BG "):
        # Background colour tags are intentionally ignored - re-creating the
        # Amiga palette literally produces near-illegible text (yellow on
        # yellow, dark grey on dark grey, etc). We just open an empty span
        # so the matching close-tag still pairs up.
        return '<span>'
    if upper == "FG" or upper == "BG":
        return '</span>'

    # Link form. AmigaGuide allows the target to be quoted OR unquoted:
    #   @{"label" LINK "node" [line]}      classic
    #   @{"label" LINK node [line]}        also valid - target is a bareword
    #   @{"label" GUIDE "file/node"}
    #   @{"label" SYSTEM "cmd"}
    #   @{"label" RX "rexx"}
    m = re.match(
        r'\s*"([^"]*)"\s+([A-Za-z]+)\s+'
        r'(?:"([^"]*)"|(\S+?))'           # quoted OR bareword target
        r'\s*(\d*)\s*$', s)
    if m:
        label, kind, target_q, target_u, lineno = m.groups()
        target = target_q if target_q is not None else target_u
        kind_u = kind.upper()
        url = ""
        if kind_u == "LINK":
            url = f"node:{target}"
            if lineno:
                url += f"#{lineno}"
        elif kind_u == "GUIDE":
            url = f"guide:{target}"
        elif kind_u == "SYSTEM":
            return f'<span style="color:#888;text-decoration:underline">{_esc(label)}</span>'
        elif kind_u == "RX" or kind_u == "RXS":
            return f'<span style="color:#888;text-decoration:underline">{_esc(label)}</span>'
        elif kind_u in ("BEEP", "QUIT", "CLOSE"):
            return f'<span style="color:#888">{_esc(label)}</span>'
        else:
            url = f"node:{target}"
        return (f'<a href="{_esc(url)}" '
                f'style="color:#7faaff;text-decoration:underline">'
                f'{_esc(label)}</a>')

    # Form without quoted target (rare): "label" close window etc.
    m = re.match(r'\s*"([^"]*)"\s+([A-Za-z]+)\s*$', s)
    if m:
        label, kind = m.groups()
        return f'<span style="color:#888">{_esc(label)}</span>'

    # Unknown tag - just drop
    return None


# =====================================================================
# Viewer dialog
# =====================================================================
class AmigaGuideViewer(QDialog):
    """Hypertext viewer for AmigaGuide files."""

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"AmigaGuide: {self.path.name}")
        self.resize(900, 640)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "amigaguide_viewer")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        # Parse
        try:
            self.doc = AmigaGuideDoc(self.path)
        except Exception as e:
            QMessageBox.critical(self, "AmigaGuide", str(e))
            self._dead = True
            return

        self._dead = False
        self._history = []         # (node_name, scroll_pos)
        self._forward = []
        self._current_node = None

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2); root.setSpacing(2)

        # Title bar
        title_text = f"  AmigaGuide  -  {self.doc.title}  "
        if self.doc.author:
            title_text += f"  by {self.doc.author}  "
        if self.doc.version:
            title_text += f"  ({self.doc.version})  "
        self.title_lbl = QLabel(title_text)
        self.title_lbl.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        root.addWidget(self.title_lbl)

        # Toolbar
        from PyQt6.QtWidgets import QWidget as _W
        bar_w = _W()
        tb = QHBoxLayout(bar_w)
        tb.setContentsMargins(0, 0, 0, 0); tb.setSpacing(2)
        for label, slot, color in [
            ("Contents",  self._go_contents, "blue"),
            ("Index",     self._go_index,    "blue"),
            ("Help",      self._go_help,     "blue"),
            ("Back",      self._go_back,     "purple"),
            ("Forward",   self._go_forward,  "purple"),
            ("Prev",      self._go_prev,     "orange"),
            ("Next",      self._go_next,     "orange"),
            ("Retrace",   self._go_retrace,  "orange"),
            ("Find",      self._find,        "teal"),
            ("Close",     self.accept,       "red"),
        ]:
            b = QPushButton(label)
            b.setStyleSheet(button_qss(color))
            b.setFixedHeight(22); b.setMinimumWidth(70)
            b.clicked.connect(slot)
            tb.addWidget(b)
        tb.addStretch()
        root.addWidget(bar_w)

        # The text browser - rich text + clickable links
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)   # we handle navigation manually
        self.browser.anchorClicked.connect(self._on_link_clicked)
        self.browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: #000000; color: #cccccc;
                font-family: "Topaz-8","Topaz","Courier New",monospace;
                font-size: 13px;
                border: 1px solid {C.BLACK};
                padding: 6px;
            }}
            {SCROLLBAR_QSS}
        """)
        f = get_topaz_font(13)
        self.browser.setFont(f)
        root.addWidget(self.browser, 1)

        # Status bar
        self.status = QLabel("")
        self.status.setStyleSheet(INFOBAR_QSS)
        self.status.setFixedHeight(20)
        root.addWidget(self.status)

        # Hotkeys
        QShortcut(QKeySequence("Esc"),         self, self.accept)
        QShortcut(QKeySequence("Alt+Left"),    self, self._go_back)
        QShortcut(QKeySequence("Alt+Right"),   self, self._go_forward)
        QShortcut(QKeySequence("Backspace"),   self, self._go_back)
        QShortcut(QKeySequence("Home"),        self, self._go_contents)
        QShortcut(QKeySequence("F1"),          self, self._go_help)
        QShortcut(QKeySequence("Ctrl+F"),      self, self._find)

        # Open MAIN node (or the first one)
        start = self.doc.global_toc or self.doc.first_node_name or "MAIN"
        self.show_node(start, push_history=False)

    # -----------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------
    def show_node(self, name, push_history=True, line_no=None):
        if not name:
            return
        node = self.doc.nodes.get(name.lower())
        if node is None:
            self.status.setText(f" Node not found: {name} ")
            return
        if push_history and self._current_node:
            self._history.append((self._current_node, self.browser.verticalScrollBar().value()))
            self._forward.clear()
        self._current_node = name
        html = render_node_html(node)
        # Wrap so the body has known background
        html_full = (
            f'<body style="background-color:#000000;color:#cccccc;'
            f'font-family:Topaz,Courier New,monospace;font-size:13px;">'
            f'{html}</body>'
        )
        self.browser.setHtml(html_full)
        # Update title
        self.title_lbl.setText(
            f"  AmigaGuide  -  {self.doc.title}  -  [{node.title}]  ")
        self.status.setText(
            f" Node: {node.name} | "
            f"prev: {node.prev or '—'}  next: {node.next_ or '—'}  "
            f"toc: {node.toc or self.doc.global_toc or '—'}")
        if line_no:
            try:
                ln = int(line_no)
                # Move cursor to the n-th line
                cur = self.browser.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.Start)
                for _ in range(ln - 1):
                    cur.movePosition(QTextCursor.MoveOperation.Down)
                self.browser.setTextCursor(cur)
                self.browser.ensureCursorVisible()
            except Exception:
                pass
        else:
            self.browser.verticalScrollBar().setValue(0)

    def _on_link_clicked(self, url: QUrl):
        s = url.toString()
        if s.startswith('node:'):
            target = s[5:]
            line = None
            if '#' in target:
                target, line = target.split('#', 1)
            self.show_node(target, line_no=line)
        elif s.startswith('guide:'):
            target = s[6:]
            # guide:file.guide/Node form
            file_part = target
            node_part = None
            if '/' in target:
                file_part, node_part = target.rsplit('/', 1)
            new_path = (self.path.parent / file_part)
            if not new_path.is_file():
                # try literal as relative path with original capitalisation
                self.status.setText(f" External guide not found: {file_part} ")
                return
            try:
                new_doc = AmigaGuideDoc(new_path)
            except Exception as e:
                QMessageBox.warning(self, "AmigaGuide",
                                     f"Cannot open {new_path.name}: {e}")
                return
            self.path = new_path
            self.doc = new_doc
            self._history.clear(); self._forward.clear()
            start = node_part or self.doc.global_toc or self.doc.first_node_name
            self.show_node(start, push_history=False)
        elif s.startswith('http'):
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    def _go_contents(self):
        target = (self._current_node and
                  self.doc.nodes.get(self._current_node.lower()) and
                  self.doc.nodes[self._current_node.lower()].toc) \
                 or self.doc.global_toc \
                 or self.doc.first_node_name
        if target:
            self.show_node(target)

    def _go_index(self):
        target = (self._current_node and
                  self.doc.nodes.get(self._current_node.lower()) and
                  self.doc.nodes[self._current_node.lower()].index) \
                 or self.doc.global_index
        if target:
            self.show_node(target)
        else:
            self.status.setText(" No Index node defined ")

    def _go_help(self):
        target = self.doc.global_help
        if target:
            self.show_node(target)
        else:
            self.status.setText(" No Help node defined ")

    def _go_prev(self):
        node = self.doc.nodes.get((self._current_node or "").lower())
        if node and node.prev:
            self.show_node(node.prev)
        else:
            self.status.setText(" No previous node ")

    def _go_next(self):
        node = self.doc.nodes.get((self._current_node or "").lower())
        if node and node.next_:
            self.show_node(node.next_)
        else:
            self.status.setText(" No next node ")

    def _go_back(self):
        if not self._history:
            self.status.setText(" No history ")
            return
        target, scroll = self._history.pop()
        self._forward.append(
            (self._current_node, self.browser.verticalScrollBar().value()))
        self._current_node = None   # avoid re-pushing
        self.show_node(target, push_history=False)
        self.browser.verticalScrollBar().setValue(scroll)

    def _go_forward(self):
        if not self._forward:
            self.status.setText(" No forward history ")
            return
        target, scroll = self._forward.pop()
        if self._current_node:
            self._history.append(
                (self._current_node, self.browser.verticalScrollBar().value()))
        self._current_node = None
        self.show_node(target, push_history=False)
        self.browser.verticalScrollBar().setValue(scroll)

    def _go_retrace(self):
        # Same as Back but don't add to forward
        if self._history:
            target, scroll = self._history.pop()
            self._current_node = None
            self.show_node(target, push_history=False)
            self.browser.verticalScrollBar().setValue(scroll)

    def _find(self):
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Find", "Search text:")
        if not ok or not text: return
        if not self.browser.find(text):
            # Wrap to top and try again
            cur = self.browser.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            self.browser.setTextCursor(cur)
            if not self.browser.find(text):
                self.status.setText(f" Not found: {text!r} ")
            else:
                self.status.setText(f" Found (wrapped): {text!r} ")
        else:
            self.status.setText(f" Found: {text!r} ")
