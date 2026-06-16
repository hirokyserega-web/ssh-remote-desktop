"""Main application window: toolbar, desktop view, clipboard sync, dialogs.

Wires the :class:`Transport` to the :class:`DesktopView` and provides:

* toolbar actions: connect/disconnect, fullscreen, file manager, SSH keys,
  and the "send special combo" menu (Ctrl+Alt+Del, Super, Alt+Tab) needed
  because Wayland clients cannot globally grab those.
* two-way clipboard sync via ``QClipboard`` with origin-based loop protection
  and a size cap (privacy toggle honoured),
* decode worker that turns incoming video packets into ``QImage`` frames,
* status bar showing connection/session state.

GUI updates triggered from the transport thread are marshalled onto the Qt
thread with queued signals.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal, QObject, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QStatusBar, QMessageBox, QMenu,
)

from common import messages
from .transport import Transport
from .decoder import Decoder
from .desktop_view import DesktopView
from .connect_dialog import ConnectDialog
from .files_dialog import FilesDialog
from .keys_dialog import KeysDialog
from .files import SFTPTransfer

log = logging.getLogger("rd.client.window")

CLIP_MAX_DEFAULT = 1 * 1024 * 1024


class _Bridge(QObject):
    """Carries cross-thread events from the transport to the GUI thread."""

    video = Signal(bytes, int)
    session = Signal(dict)
    clipboard = Signal(dict)
    state = Signal(str, str)


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.transport: Transport | None = None
        self.decoder = Decoder()
        self._clip_guard = None     # last clipboard text we set (loop protection)
        self._server_screen = tuple(cfg.geometry)

        self.setWindowTitle("SSH Remote Desktop")
        self.resize(1280, 800)

        self.view = DesktopView()
        self.view.input_event.connect(self._on_input_event)
        self.view.files_dropped.connect(self._on_files_dropped)
        self.setCentralWidget(self.view)

        self.bridge = _Bridge()
        self.bridge.video.connect(self._on_video_gui)
        self.bridge.session.connect(self._on_session_gui)
        self.bridge.clipboard.connect(self._on_clipboard_gui)
        self.bridge.state.connect(self._on_state_gui)

        self._build_toolbar()
        self.setStatusBar(QStatusBar())
        self._set_status("не подключено")

        # Clipboard monitoring (client -> server).
        self._clipboard = QGuiApplication.clipboard()
        self._clipboard.dataChanged.connect(self._on_local_clipboard)

        # Heartbeat + stats timer.
        self._hb = QTimer(self)
        self._hb.timeout.connect(self._heartbeat)
        self._hb.start(3000)

    # -- toolbar -----------------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_connect = QAction("Подключиться", self)
        self.act_connect.triggered.connect(self.toggle_connect)
        tb.addAction(self.act_connect)

        self.act_full = QAction("Полный экран", self)
        self.act_full.setCheckable(True)
        self.act_full.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_full)

        act_files = QAction("Файлы", self)
        act_files.triggered.connect(self._open_files)
        tb.addAction(act_files)

        act_keys = QAction("SSH-ключи", self)
        act_keys.triggered.connect(self._open_keys)
        tb.addAction(act_keys)

        # Special-combo menu (essential on Wayland where global grab is denied).
        self.combo_menu = QMenu("Спец. сочетания", self)
        for label, combo in [
            ("Ctrl+Alt+Del", ["Control_L", "Alt_L", "Delete"]),
            ("Super (Win)", ["Super_L"]),
            ("Alt+Tab", ["Alt_L", "Tab"]),
            ("Ctrl+W", ["Control_L", "w"]),
        ]:
            a = QAction(label, self)
            a.triggered.connect(lambda checked=False, c=combo: self._send_combo(c))
            self.combo_menu.addAction(a)
        act_combo = QAction("Спец. сочетания", self)
        act_combo.setMenu(self.combo_menu)
        tb.addAction(act_combo)

        # Clipboard privacy toggle.
        self.act_clip = QAction("Синхр. буфер", self)
        self.act_clip.setCheckable(True)
        self.act_clip.setChecked(self.cfg.clipboard_enabled)
        tb.addAction(self.act_clip)

    # -- connection --------------------------------------------------------
    def toggle_connect(self):
        if self.transport is not None:
            self.disconnect_session()
            return
        dlg = ConnectDialog(self.cfg, self)
        if dlg.exec() != ConnectDialog.Accepted:
            return
        password = dlg.password()
        self._start_transport(password)

    def _start_transport(self, password):
        self.transport = Transport(self.cfg, password=password)
        self.transport.on_video = lambda data, flags: self.bridge.video.emit(data, flags)
        self.transport.on_session = lambda msg: self.bridge.session.emit(msg)
        self.transport.on_clipboard = lambda msg: self.bridge.clipboard.emit(msg)
        self.transport.on_state = lambda s, d: self.bridge.state.emit(s, d)
        self.transport.start()
        self.act_connect.setText("Отключиться")

    def disconnect_session(self):
        if self.transport is not None:
            self.transport.stop()
            self.transport = None
        self.act_connect.setText("Подключиться")
        self._set_status("отключено")

    # -- video -------------------------------------------------------------
    def _on_video_gui(self, data: bytes, flags: int):
        result = self.decoder.decode(data, flags)
        if result is not None:
            width, height, rgb = result
            self.view.set_frame(rgb, width, height)

    def _on_session_gui(self, msg: dict):
        self._server_screen = tuple(msg.get("screen", self.cfg.geometry))
        self.decoder.reset()
        self._set_status(
            f"сессия {msg.get('session_id')} | backend={msg.get('backend')} "
            f"| {self._server_screen[0]}x{self._server_screen[1]} | {msg.get('fps')} FPS"
        )

    # -- input -------------------------------------------------------------
    def _on_input_event(self, obj: dict):
        if self.transport is not None:
            self.transport.send_input(obj)

    def _send_combo(self, names):
        """Press the keys in order, then release in reverse."""
        if self.transport is None:
            return
        for name in names:
            self.transport.send_input(messages.key(name, True, []))
        for name in reversed(names):
            self.transport.send_input(messages.key(name, False, []))

    # -- clipboard ---------------------------------------------------------
    def _on_local_clipboard(self):
        if not self.act_clip.isChecked() or self.transport is None:
            return
        text = self._clipboard.text()
        if not text or text == self._clip_guard:
            return
        if len(text.encode("utf-8")) > self.cfg.clipboard_max_bytes:
            return
        self._clip_guard = text
        self.transport.send_clipboard(messages.clipboard("text", text, origin="client"))

    def _on_clipboard_gui(self, msg: dict):
        if not self.act_clip.isChecked():
            return
        if msg.get("format") != "text" or msg.get("origin") == "client":
            return
        text = msg.get("data", "")
        self._clip_guard = text  # so we don't echo it straight back
        self._clipboard.setText(text)

    # -- state / heartbeat -------------------------------------------------
    def _on_state_gui(self, state: str, detail: str):
        self._set_status(f"{state} {detail}".strip())
        if state == "connected":
            # Tell the server our current view size for coordinate scaling.
            if self.transport is not None:
                self.transport.send_control({"t": "resize", "view": list(self._view_size())})

    def _view_size(self):
        return (self.view.width(), self.view.height())

    def _heartbeat(self):
        if self.transport is not None:
            self.transport.send_control(messages.ping())

    def _set_status(self, text: str):
        self.statusBar().showMessage(f"● {text}")

    # -- dialogs / fullscreen ----------------------------------------------
    def _toggle_fullscreen(self, checked):
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()

    def _open_files(self):
        if self.transport is None:
            QMessageBox.information(self, "Файлы", "Сначала подключитесь.")
            return
        FilesDialog(self.transport, self.cfg, self).exec()

    def _open_keys(self):
        KeysDialog(self.cfg, self).exec()

    def _on_files_dropped(self, files):
        if self.transport is None:
            QMessageBox.information(self, "Файлы", "Сначала подключитесь.")
            return
        if not self.cfg.files_enabled:
            return
        import os as _os
        transfer = SFTPTransfer(self.transport)
        for path in files:
            name = _os.path.basename(path)
            try:
                fut = self.transport.run_coro(transfer.upload(path, name))
                fut.add_done_callback(
                    lambda f, n=name: self.bridge.state.emit("файл загружен", n)
                )
                self._set_status(f"загрузка {name}…")
            except Exception as exc:
                self._set_status(f"ошибка загрузки: {exc}")

    def closeEvent(self, event):
        self.disconnect_session()
        super().closeEvent(event)
