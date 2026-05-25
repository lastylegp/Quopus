"""Image viewer for PNG/JPG/GIF/BMP/WEBP and other Qt-supported formats.

Features:
- Fit-to-window (default)
- Zoom in/out with + / -
- Actual size (1:1)
- Animated GIFs (via QMovie)
- Shows dimensions, format, file size in status bar
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap, QMovie, QImageReader, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy,
)

from .palette import (
    C, button_qss, fmt_size, SCROLLBAR_QSS,
    WB_TITLEBAR_INACTIVE_QSS, INFOBAR_QSS,
)


# Extensions Qt supports out of the box (at least on most platforms)
IMAGE_EXTS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp',
    '.tif', '.tiff', '.ppm', '.pgm', '.pbm', '.xbm', '.xpm',
    '.ico', '.icns', '.svg', '.svgz',
}


def is_image(path) -> bool:
    p = Path(path)
    return p.suffix.lower() in IMAGE_EXTS


class ImageViewer(QDialog):
    """Quopus-styled image viewer."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Image: {self.path.name}")
        self.resize(900, 700)
        # Restore window geometry from last session
        from quopus_lib.window_state import install_window_state
        install_window_state(self, "image_viewer")
        self.setStyleSheet(f"QDialog {{ background-color: {C.WB_GREY}; }}")

        self._zoom = 1.0
        self._fit = True          # start in fit-to-window mode
        self._movie = None        # for animated GIFs
        self._pixmap = None       # for still images

        # Probe metadata first (cheap) without decoding the whole image
        reader = QImageReader(str(self.path))
        self._orig_size = reader.size()  # QSize, may be -1,-1 if unknown
        self._format = reader.format().data().decode('ascii', errors='ignore').upper()
        self._animated = reader.supportsAnimation()
        try:
            file_size = self.path.stat().st_size
        except Exception:
            file_size = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2); layout.setSpacing(2)

        # Title bar
        title = QLabel(
            f"  Image: {self.path.name}   "
            f"({self._format or '?'}, "
            f"{self._orig_size.width()}x{self._orig_size.height()}, "
            f"{fmt_size(file_size)})  "
        )
        title.setStyleSheet(WB_TITLEBAR_INACTIVE_QSS)
        layout.addWidget(title)

        # Toolbar
        tool = QHBoxLayout(); tool.setSpacing(2)

        self.btn_fit = QPushButton("Fit")
        self.btn_fit.setStyleSheet(button_qss("orange"))
        self.btn_fit.setFixedWidth(60)
        self.btn_fit.setToolTip("Fit to window")
        self.btn_fit.clicked.connect(self._fit_to_window)
        tool.addWidget(self.btn_fit)

        self.btn_actual = QPushButton("1:1")
        self.btn_actual.setStyleSheet(button_qss("orange"))
        self.btn_actual.setFixedWidth(50)
        self.btn_actual.setToolTip("Actual size (100%)")
        self.btn_actual.clicked.connect(self._actual_size)
        tool.addWidget(self.btn_actual)

        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_out.setStyleSheet(button_qss("mid"))
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_out.setToolTip("Zoom out (Ctrl+- or scroll wheel down)")
        self.btn_zoom_out.clicked.connect(lambda: self._apply_zoom(0.75))
        tool.addWidget(self.btn_zoom_out)

        self.lbl_zoom = QLabel(" 100% ")
        self.lbl_zoom.setStyleSheet(
            f"QLabel {{ background-color: {C.WHITE}; color: {C.BLACK}; "
            f"font-family: 'Topaz','Courier New',monospace; padding: 2px 8px; "
            f"border: 1px solid {C.BLACK}; min-width: 50px; }}")
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tool.addWidget(self.lbl_zoom)

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setStyleSheet(button_qss("mid"))
        self.btn_zoom_in.setFixedWidth(28)
        self.btn_zoom_in.setToolTip("Zoom in (Ctrl++ or scroll wheel up)")
        self.btn_zoom_in.clicked.connect(lambda: self._apply_zoom(1.3333))
        tool.addWidget(self.btn_zoom_in)

        tool.addStretch()

        self.btn_close = QPushButton("Close (Esc)")
        self.btn_close.setStyleSheet(button_qss("red"))
        self.btn_close.setFixedWidth(100)
        self.btn_close.clicked.connect(self.accept)
        tool.addWidget(self.btn_close)
        layout.addLayout(tool)

        # Scrollable image area - centered on black
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet(f"""
            QScrollArea {{ background-color: {C.BLACK};
                           border: 1px solid {C.BLACK}; }}
            {SCROLLBAR_QSS}
        """)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(f"background-color: {C.BLACK};")
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Ignored)
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)

        self.lbl_status = QLabel(" Ready ")
        self.lbl_status.setStyleSheet(INFOBAR_QSS)
        layout.addWidget(self.lbl_status)

        # Load the image
        self._load()

        # Hotkeys
        QShortcut(QKeySequence("Escape"), self, self.accept)
        QShortcut(QKeySequence("Ctrl++"), self, lambda: self._apply_zoom(1.3333))
        QShortcut(QKeySequence("Ctrl+="), self, lambda: self._apply_zoom(1.3333))
        QShortcut(QKeySequence("Ctrl+-"), self, lambda: self._apply_zoom(0.75))
        QShortcut(QKeySequence("Ctrl+0"), self, self._actual_size)
        QShortcut(QKeySequence("F"),      self, self._fit_to_window)

    def _load(self):
        """Load the image - use QMovie for animated formats, QPixmap otherwise."""
        if self._animated:
            self._movie = QMovie(str(self.path))
            if not self._movie.isValid():
                self._load_static_fallback()
                return
            self.image_label.setMovie(self._movie)
            self._movie.start()
            # For movies, first frame drives the size
            self._movie.jumpToFrame(0)
            self._orig_size = self._movie.currentPixmap().size()
            self.lbl_status.setText(
                f" Animated {self._format} | {self._orig_size.width()}x{self._orig_size.height()}"
                f" | {self._movie.frameCount()} frames ")
        else:
            self._load_static_fallback()

        # Initial fit after widget has a size
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(30, self._fit_to_window)

    def _load_static_fallback(self):
        pix = QPixmap(str(self.path))
        if pix.isNull():
            self.image_label.setText(
                f"<pre style='color:#ff8800;'>Cannot decode image:\n"
                f"{self.path}\n\nFormat: {self._format or 'unknown'}</pre>")
            self.lbl_status.setText(" Decode failed ")
            return
        self._pixmap = pix
        self._orig_size = pix.size()
        self.image_label.setPixmap(pix)
        self.lbl_status.setText(
            f" {self._format} | {pix.width()}x{pix.height()} | "
            f"{pix.depth()}-bit ")

    def _fit_to_window(self):
        """Scale image to fit the viewport without cropping."""
        if self._orig_size.width() <= 0 or self._orig_size.height() <= 0:
            return
        vp = self.scroll.viewport()
        avail_w = max(50, vp.width() - 4)
        avail_h = max(50, vp.height() - 4)
        sx = avail_w / self._orig_size.width()
        sy = avail_h / self._orig_size.height()
        self._zoom = min(sx, sy)
        self._fit = True
        self._apply_scale()

    def _actual_size(self):
        self._zoom = 1.0
        self._fit = False
        self._apply_scale()

    def _apply_zoom(self, factor):
        self._fit = False
        self._zoom = max(0.05, min(40.0, self._zoom * factor))
        self._apply_scale()

    def _apply_scale(self):
        if self._orig_size.width() <= 0:
            return
        new_w = max(1, int(self._orig_size.width() * self._zoom))
        new_h = max(1, int(self._orig_size.height() * self._zoom))

        if self._movie is not None:
            # QMovie scaling: setScaledSize rescales every frame as it plays
            self._movie.setScaledSize(QSize(new_w, new_h))
            self.image_label.resize(new_w, new_h)
        elif self._pixmap is not None:
            scaled = self._pixmap.scaled(
                new_w, new_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation
                if self._zoom < 4.0
                else Qt.TransformationMode.FastTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.resize(new_w, new_h)

        self.lbl_zoom.setText(f" {int(self._zoom * 100)}% ")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-fit on resize if we're in fit mode
        if self._fit:
            from PyQt6.QtCore import QTimer
            if not hasattr(self, '_fit_timer'):
                self._fit_timer = QTimer(self)
                self._fit_timer.setSingleShot(True)
                self._fit_timer.timeout.connect(self._fit_to_window)
            self._fit_timer.start(50)

    def wheelEvent(self, event):
        """Mouse wheel zooms (no modifier needed). Ctrl+wheel still
        works for users who're used to that convention. Zoom centers on
        the mouse cursor so scrolling in zooms toward what you're
        looking at, not just the top-left corner."""
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        factor = 1.2 if delta > 0 else 1 / 1.2

        # Zoom-to-cursor: figure out where in the image the cursor is,
        # apply the zoom, then scroll so that same image point ends up
        # under the cursor again.
        # Map the wheel position into image coordinates first.
        viewport = self.scroll.viewport()
        # Cursor position relative to viewport
        try:
            pos = event.position()  # PyQt6 returns QPointF
            mx = pos.x()
            my = pos.y()
        except Exception:
            pos = event.pos()
            mx = pos.x(); my = pos.y()
        # Account for the scroll area's outer frame: convert dialog -> viewport
        vp_origin = viewport.mapTo(self, viewport.pos())
        mx -= vp_origin.x()
        my -= vp_origin.y()

        # Where in the image is that, in image coordinates?
        if self._zoom > 0 and self._orig_size.width() > 0:
            scrolled_x = self.scroll.horizontalScrollBar().value() + mx
            scrolled_y = self.scroll.verticalScrollBar().value() + my
            img_x = scrolled_x / self._zoom
            img_y = scrolled_y / self._zoom
        else:
            img_x = img_y = 0

        # Apply the zoom (this updates self._zoom and rescales)
        self._apply_zoom(factor)

        # After zoom, scroll so the image point is back under the cursor
        if self._zoom > 0:
            new_scrolled_x = img_x * self._zoom - mx
            new_scrolled_y = img_y * self._zoom - my
            hbar = self.scroll.horizontalScrollBar()
            vbar = self.scroll.verticalScrollBar()
            hbar.setValue(int(new_scrolled_x))
            vbar.setValue(int(new_scrolled_y))

        event.accept()

    def closeEvent(self, ev):
        if self._movie is not None:
            self._movie.stop()
        super().closeEvent(ev)
