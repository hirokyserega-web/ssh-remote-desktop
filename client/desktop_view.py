"""The remote-desktop view widget: renders frames and captures input.

Responsibilities:

* paint the latest decoded RGB frame, scaled to the widget while preserving
  aspect ratio and honouring the device pixel ratio (HiDPI / fractional
  scaling on Wayland) so the image stays crisp and coordinates stay correct;
* translate Qt mouse/keyboard events into protocol input messages, mapping
  widget coordinates back to server-screen coordinates;
* accept drag-and-drop of files to trigger an upload to the server's shared
  folder.

Keyboard pass-through: on X11 the parent window can grab the keyboard for full
pass-through (handled in the main window). On Wayland, global shortcuts cannot
be intercepted -- the toolbar exposes "send Ctrl+Alt+Del" / "send Super"
buttons to compensate (see :mod:`client.mainwindow`).
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QPointF, Signal, QTimer
from PySide6.QtGui import QImage, QPainter, QColor

from . import keymap_qt


class DesktopView(QWidget):
    #: emitted with a protocol input dict for the transport to send
    input_event = Signal(dict)
    #: emitted with a list of local file paths dropped on the view
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 240)
        self._image: QImage | None = None
        self._server_w = 0
        self._server_h = 0
        self._scale_to_window = True
        # destination rect of the drawn image (for coordinate mapping)
        self._draw_rect = (0, 0, 1, 1)
        # Connection-state overlay shown when no live frame is available.
        # None == "no signal" placeholder; a string == that message + spinner.
        self._overlay = None
        self._spinner_angle = 0
        self._spinner = QTimer(self)
        self._spinner.timeout.connect(self._tick_spinner)
        self._spinner.setInterval(80)

    # -- frame intake ------------------------------------------------------
    def set_frame(self, rgb_bytes: bytes, width: int, height: int):
        self._server_w, self._server_h = width, height
        img = QImage(rgb_bytes, width, height, width * 3, QImage.Format_RGB888)
        # QImage does not copy the buffer; keep a copy so it stays valid.
        self._image = img.copy()
        # A live frame clears any connection-state overlay.
        if self._overlay is not None:
            self._overlay = None
            self._spinner.stop()
        self.update()

    def set_scale_to_window(self, on: bool):
        self._scale_to_window = on
        self.update()

    # -- overlay / state --------------------------------------------------
    def set_overlay(self, text):
        """Show a connection-state overlay.

        ``None`` -> plain "no signal" placeholder (no spinner).
        A string -> that message plus an animated spinner (connecting /
        reconnecting). A live frame arriving clears the overlay.
        """
        self._overlay = text
        if text is not None:
            self._spinner.start()
        else:
            self._spinner.stop()
        self.update()

    def _tick_spinner(self):
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self.update()

    # -- painting ----------------------------------------------------------
    def paintEvent(self, _ev):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 18, 18))
        if self._image is None:
            self._paint_overlay(painter)
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        target = self._compute_target_rect()
        self._draw_rect = (target.x(), target.y(), target.width(), target.height())
        painter.drawImage(target, self._image)

    def _paint_overlay(self, painter):
        """Draw the placeholder / connection-state overlay with a spinner."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QConicalGradient
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        text = self._overlay or self.tr("Нет сигнала")
        painter.setPen(QColor(200, 200, 200))
        painter.drawText(rect, Qt.AlignCenter, text)
        # Animated spinner only when an explicit overlay (connecting…) is set.
        if self._overlay is not None:
            cx, cy = rect.width() / 2, rect.height() / 2 + 40
            r = 14
            grad = QConicalGradient(cx, cy, self._spinner_angle)
            grad.setColorAt(0.0, QColor(245, 180, 0, 255))
            grad.setColorAt(1.0, QColor(245, 180, 0, 0))
            painter.setBrush(grad)
            painter.setPen(QColor(245, 180, 0, 0))
            painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    def _compute_target_rect(self):
        from PySide6.QtCore import QRectF
        ww, wh = self.width(), self.height()
        iw, ih = self._server_w or 1, self._server_h or 1
        if not self._scale_to_window:
            return QRectF(0, 0, iw, ih)
        scale = min(ww / iw, wh / ih)
        dw, dh = iw * scale, ih * scale
        return QRectF((ww - dw) / 2, (wh - dh) / 2, dw, dh)

    # -- coordinate mapping ------------------------------------------------
    def _to_server(self, pos: QPointF):
        x0, y0, w, h = self._draw_rect
        if w <= 0 or h <= 0:
            return 0.0, 0.0
        rx = (pos.x() - x0) / w
        ry = (pos.y() - y0) / h
        rx = min(max(rx, 0.0), 1.0)
        ry = min(max(ry, 0.0), 1.0)
        # Normalized fractions in [0.0, 1.0] -- the server maps these to its
        # actual screen pixels (single scaling point, see server
        # ConnectionHandler._scale_coords). Returning pixel ints here was a bug:
        # the server re-scaled them and double-applied the geometry ratio.
        return round(rx, 6), round(ry, 6)

    # -- mouse -------------------------------------------------------------
    def mouseMoveEvent(self, ev):
        x, y = self._to_server(ev.position())
        self.input_event.emit({"t": "mouse_move", "x": x, "y": y})

    def _btn(self, button) -> int:
        return {Qt.LeftButton: 1, Qt.MiddleButton: 2, Qt.RightButton: 3}.get(button, 1)

    def mousePressEvent(self, ev):
        self.setFocus()
        self.input_event.emit({"t": "mouse_btn", "button": self._btn(ev.button()), "down": True})

    def mouseReleaseEvent(self, ev):
        self.input_event.emit({"t": "mouse_btn", "button": self._btn(ev.button()), "down": False})

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        dx = ev.angleDelta().x()
        self.input_event.emit({
            "t": "scroll",
            "dx": (1 if dx > 0 else -1) if dx else 0,
            "dy": (1 if dy > 0 else -1) if dy else 0,
        })

    # -- keyboard ----------------------------------------------------------
    def keyPressEvent(self, ev):
        self._emit_key(ev, True)

    def keyReleaseEvent(self, ev):
        self._emit_key(ev, False)

    def _emit_key(self, ev, down: bool):
        keysym = keymap_qt.qt_event_to_keysym(ev)
        if keysym is None:
            return
        mods = keymap_qt.qt_modifiers(ev.modifiers())
        self.input_event.emit({"t": "key", "keysym": keysym, "down": down, "mods": mods})

    # -- drag and drop -----------------------------------------------------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            ev.acceptProposedAction()

    # -- helper to inject synthetic key chords (toolbar buttons) -----------
    def send_chord(self, keysyms: list[str]):
        for ks in keysyms:
            self.input_event.emit({"t": "key", "keysym": ks, "down": True, "mods": []})
        for ks in reversed(keysyms):
            self.input_event.emit({"t": "key", "keysym": ks, "down": False, "mods": []})
