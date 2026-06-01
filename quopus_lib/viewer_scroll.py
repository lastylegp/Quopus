# date_time: 2026-06-01 18:32
"""Keyboard scrolling for viewer dialogs.

Every viewer (Text, Hex, PETSCII, Bitmap, Disasm, AmigaGuide, ...)
should let the user scroll with the arrow keys and Page Up / Page
Down / Home / End right after it opens - without having to click
into the content first or reach for the mouse wheel.

The trouble is that the different viewers use different scrollable
widgets:

  - QPlainTextEdit / QTextEdit / QTextBrowser already scroll on
    their own *if they hold keyboard focus* - but the dialogs
    often put focus on a search box or a combo, so the keys go
    nowhere useful.
  - QScrollArea (used for the rendered PETSCII / bitmap / charset
    images) does NOT scroll on arrow keys at all by default.

Rather than patch each viewer differently, this module installs a
single event filter on the dialog that intercepts the navigation
keys and drives whichever scrollable widget the viewer designates
as its "content". It works the same for text widgets and scroll
areas because every one of them is a QAbstractScrollArea with a
verticalScrollBar() / horizontalScrollBar().

Usage in a viewer dialog (one line, after the content widget
exists):

    from .viewer_scroll import enable_key_scrolling
    enable_key_scrolling(self, self.text_plain)

If the viewer can switch between several content widgets (e.g. the
TextReader flips between a plain-text view, a colour view and a
bitmap scroll area), pass a zero-argument callable that returns the
currently-visible one:

    enable_key_scrolling(self, lambda: self._current_scroll_widget())

The filter never swallows a key that the focused widget itself
wants (e.g. typing in the search box): it only acts when the
focused widget is not a text-entry / not the content already
handling the key.
"""
from PyQt6.QtCore import QObject, QEvent, Qt
from PyQt6.QtWidgets import (
    QAbstractScrollArea, QLineEdit, QComboBox, QPlainTextEdit,
    QTextEdit, QAbstractItemView, QApplication,
)


_NAV_KEYS = {
    Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End,
    Qt.Key.Key_Left, Qt.Key.Key_Right,
}


class _ScrollFilter(QObject):
    """Event filter that turns navigation key presses on a dialog
    into scrolling of the dialog's content widget."""

    def __init__(self, dialog, content):
        super().__init__(dialog)
        self._dialog = dialog
        # content may be a widget or a callable returning a widget.
        self._content = content

    def _resolve(self):
        c = self._content
        if callable(c):
            try:
                c = c()
            except Exception:
                return None
        return c

    def _scroll_target(self, widget):
        """Return the QAbstractScrollArea we should drive. Text
        widgets and QScrollArea both qualify; for anything else we
        walk up to the nearest scroll area."""
        w = widget
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                return w
            w = w.parent()
        return None

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.KeyPress:
            return False
        key = ev.key()
        if key not in _NAV_KEYS:
            return False

        # Don't hijack keys while the user is typing in a text
        # entry, a combo box, or an editable item view - those need
        # the arrows for their own cursor/selection movement.
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QComboBox)):
            return False
        # A focused text editor / item view handles its own arrows
        # and page keys correctly already - let it.
        if isinstance(focused, (QPlainTextEdit, QTextEdit,
                                QAbstractItemView)):
            return False

        content = self._resolve()
        if content is None:
            return False
        area = self._scroll_target(content)
        if area is None:
            return False

        # If the content is itself a text editor, the cleanest way
        # to scroll is to hand it focus and re-post the key, so it
        # moves its caret/page exactly as the user expects.
        if isinstance(content, (QPlainTextEdit, QTextEdit,
                                QAbstractItemView)):
            content.setFocus(Qt.FocusReason.OtherFocusReason)
            # Re-deliver this key to the now-focused content.
            QApplication.sendEvent(content, ev)
            return True

        # Otherwise drive the scroll bars of the scroll area
        # directly (QScrollArea around a rendered image, etc.).
        vbar = area.verticalScrollBar()
        hbar = area.horizontalScrollBar()
        step_v = max(1, vbar.singleStep())
        page_v = max(step_v, vbar.pageStep())
        step_h = max(1, hbar.singleStep())

        if key == Qt.Key.Key_Down:
            vbar.setValue(vbar.value() + step_v)
        elif key == Qt.Key.Key_Up:
            vbar.setValue(vbar.value() - step_v)
        elif key == Qt.Key.Key_PageDown:
            vbar.setValue(vbar.value() + page_v)
        elif key == Qt.Key.Key_PageUp:
            vbar.setValue(vbar.value() - page_v)
        elif key == Qt.Key.Key_Home:
            vbar.setValue(vbar.minimum())
        elif key == Qt.Key.Key_End:
            vbar.setValue(vbar.maximum())
        elif key == Qt.Key.Key_Right:
            hbar.setValue(hbar.value() + step_h)
        elif key == Qt.Key.Key_Left:
            hbar.setValue(hbar.value() - step_h)
        else:
            return False
        return True


def enable_key_scrolling(dialog, content, focus=True):
    """Install arrow / page / home / end keyboard scrolling on a
    viewer dialog.

    dialog  : the QDialog (or any QWidget) to listen on.
    content : the scrollable content widget, OR a zero-argument
              callable returning the currently-active one (for
              viewers that switch between several views).
    focus   : if True (default), give the content widget keyboard
              focus right away. A focused QPlainTextEdit / QTextEdit
              / QScrollArea scrolls natively on the arrow and page
              keys, so this alone fixes the common "have to click
              first" case. The event filter below is the backup for
              when focus is elsewhere (e.g. a search box) yet the
              user still presses a navigation key.

    Returns the filter object (kept alive by parenting it to the
    dialog; callers can ignore the return value).
    """
    filt = _ScrollFilter(dialog, content)
    dialog.installEventFilter(filt)
    try:
        dialog._kbd_scroll_filter = filt
    except Exception:
        pass
    if focus:
        try:
            w = content() if callable(content) else content
            if w is not None:
                # A QScrollArea needs an explicit focus policy to
                # accept the keyboard; text widgets already have one.
                from PyQt6.QtWidgets import QAbstractScrollArea
                if isinstance(w, QAbstractScrollArea):
                    w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                w.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception:
            pass
    return filt
