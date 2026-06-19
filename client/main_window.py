"""Main application window: toolbar, desktop view, clipboard sync, dialogs.

Wires the :class:`Transport` to the :class:`DesktopView` and provides:

* a grouped toolbar with icons, shortcuts and tooltips (connect / fullscreen /
  files / keys / special combos / clipboard / preferences);
* a status bar with a coloured state indicator (offline / connecting / online /
  error / reconnecting) and live metrics (FPS, bitrate, RTT, resolution,
  session / host);
* a connection-state overlay on the viewport (Подключение… / reconnect) instead
  of a static "no signal" placeholder;
* two-way clipboard sync via ``QClipboard`` with origin-based loop protection
  and a size cap (privacy toggle honoured);
* decode worker that turns incoming video packets into ``QImage`` frames.

GUI updates triggered from the transport thread are marshalled onto the Qt
thread with queued signals.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Signal, QObject, QTimer, QSize
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QStatusBar, QMessageBox, QMenu, QLabel,
)
from PySide6.QtWidgets import QStyle

from common import messages
from .transport import Transport
from .decoder import Decoder
from .desktop_view import DesktopView
from .connect_dialog import ConnectDialog
from .files_dialog import FilesDialog
from .keys_dialog import KeysDialog
from .host_key_dialog import HostKeyDialog
from .files import SFTPTransfer

log = logging.getLogger("rd.client.window")

CLIP_MAX_DEFAULT = 1 * 1024 * 1024

# Status-bar state colours.
_STATE_COLORS = {
    "offline": "#9aa0a6",
    "connecting": "#f5b400",
    "connected": "#34a853",
    "reconnecting": "#f5b400",
    "error": "#ea4335",
    "failed": "#ea4335",
    "disconnected": "#9aa0a6",
}


class _Bridge(QObject):
    """Carries cross-thread events from the transport to the GUI thread."""

    video = Signal(bytes, int)
    session = Signal(dict)
    clipboard = Signal(dict)
    state = Signal(str, str)
    host_key = Signal(str, int, str, bool)


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.transport: Transport | None = None
        self.decoder = Decoder()
        self._clip_guard = None     # last clipboard text we set (loop protection)
        self._server_screen = tuple(cfg.geometry)
        self._state = "offline"

        self.setWindowTitle(self.tr("SSH Remote Desktop"))
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
        self.bridge.host_key.connect(self._on_host_key_gui)

        self._build_toolbar()
        self._build_statusbar()
        self._set_state("offline")

        # Clipboard monitoring (client -> server).
        self._clipboard = QGuiApplication.clipboard()
        self._clipboard.dataChanged.connect(self._on_local_clipboard)

        # Heartbeat timer (ping/pong + stats reporting to the server).
        self._hb = QTimer(self)
        self._hb.timeout.connect(self._heartbeat)
        self._hb.start(3000)

        # Live-metrics timer: FPS / bitrate / RTT display refresh.
        self._frame_count = 0
        self._byte_count = 0
        self._metrics_t0 = time.monotonic()
        self._metrics = QTimer(self)
        self._metrics.timeout.connect(self._refresh_metrics)
        self._metrics.start(1000)

    # -- helpers -----------------------------------------------------------
    def _std_icon(self, sp):
        return self.style().standardIcon(sp)

    def _add_action(self, tb, text, *, icon=None, shortcut=None, tooltip=None,
                    checkable=False, checked=False):
        act = QAction(text, self)
        if icon is not None:
            act.setIcon(icon)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut))
            act.setToolTip(f"{text} ({QKeySequence(shortcut).toString()})")
        elif tooltip is not None:
            act.setToolTip(tooltip)
        else:
            act.setToolTip(text)
        if checkable:
            act.setCheckable(True)
            act.setChecked(checked)
        tb.addAction(act)
        return act

    # -- toolbar -----------------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar(self.tr("main"))
        tb.setMovable(False)
        tb.setIconSize(tb.iconSize().expandedTo(QSize(20, 20)))
        self.addToolBar(tb)

        # --- group: connection ---
        self.act_connect = QAction(self.tr("Подключиться"), self)
        self.act_connect.setIcon(self._std_icon(QStyle.SP_MediaPlay))
        self.act_connect.setShortcut(QKeySequence("Ctrl+Return"))
        self.act_connect.setToolTip(self.tr("Подключиться (Ctrl+Return)"))
        self.act_connect.triggered.connect(self.toggle_connect)
        tb.addAction(self.act_connect)
        # A dedicated shortcut also works when the toolbar action is disabled.
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.toggle_connect)

        # --- group: view ---
        tb.addSeparator()
        self.act_full = QAction(self.tr("Полный экран"), self)
        self.act_full.setIcon(self._std_icon(QStyle.SP_TitleBarMaxButton))
        self.act_full.setShortcut(QKeySequence("F11"))
        self.act_full.setCheckable(True)
        self.act_full.setChecked(self.cfg.start_fullscreen)
        self.act_full.setToolTip(self.tr("Полный экран (F11)"))
        self.act_full.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_full)

        # --- group: tools (files, keys) ---
        tb.addSeparator()
        act_files = QAction(self.tr("Файлы"), self)
        act_files.setIcon(self._std_icon(QStyle.SP_DirOpenIcon))
        act_files.setShortcut(QKeySequence("Ctrl+M"))
        act_files.setToolTip(self.tr("Файловый менеджер (Ctrl+M)"))
        act_files.triggered.connect(self._open_files)
        tb.addAction(act_files)

        act_keys = QAction(self.tr("SSH-ключи"), self)
        act_keys.setIcon(self._std_icon(QStyle.SP_FileDialogListView))
        act_keys.setShortcut(QKeySequence("Ctrl+K"))
        act_keys.setToolTip(self.tr("Менеджер SSH-ключей (Ctrl+K)"))
        act_keys.triggered.connect(self._open_keys)
        tb.addAction(act_keys)

        # --- group: special combos (essential on Wayland) ---
        tb.addSeparator()
        self.combo_menu = QMenu(self.tr("Спец. сочетания"), self)
        for label, combo in [
            ("Ctrl+Alt+Del", ["Control_L", "Alt_L", "Delete"]),
            ("Super (Win)", ["Super_L"]),
            ("Alt+Tab", ["Alt_L", "Tab"]),
            ("Ctrl+W", ["Control_L", "w"]),
        ]:
            a = QAction(label, self)
            a.triggered.connect(lambda checked=False, c=combo: self._send_combo(c))
            self.combo_menu.addAction(a)
        act_combo = QAction(self.tr("Спец. сочетания"), self)
        act_combo.setIcon(self._std_icon(QStyle.SP_FileDialogDetailedView))
        act_combo.setMenu(self.combo_menu)
        act_combo.setToolTip(self.tr("Отправить системное сочетание клавиш"))
        tb.addAction(act_combo)

        # --- group: options (clipboard, preferences) ---
        tb.addSeparator()
        self.act_clip = QAction(self.tr("Синхр. буфер"), self)
        self.act_clip.setIcon(self._std_icon(QStyle.SP_MediaPause))
        self.act_clip.setCheckable(True)
        self.act_clip.setChecked(self.cfg.clipboard_enabled)
        self.act_clip.setToolTip(self.tr("Двусторонняя синхронизация буфера обмена"))
        tb.addAction(self.act_clip)

        act_prefs = QAction(self.tr("Настройки"), self)
        act_prefs.setIcon(self._std_icon(QStyle.SP_FileDialogInfoView))
        act_prefs.setShortcut(QKeySequence("Ctrl+,"))
        act_prefs.setToolTip(self.tr("Настройки (Ctrl+,)"))
        act_prefs.triggered.connect(self._open_prefs)
        tb.addAction(act_prefs)

    # -- status bar --------------------------------------------------------
    def _build_statusbar(self):
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.state_label = QLabel("●")
        self.state_label.setStyleSheet("padding: 0 6px;")
        bar.addPermanentWidget(self.state_label)
        self.metrics_label = QLabel("")
        self.metrics_label.setStyleSheet("padding: 0 6px; color: #9aa0a6;")
        bar.addPermanentWidget(self.metrics_label)

    def _set_state(self, state: str):
        self._state = state
        labels = {
            "offline": self.tr("не подключено"),
            "connecting": self.tr("подключение…"),
            "connected": self.tr("подключено"),
            "reconnecting": self.tr("переподключение…"),
            "error": self.tr("ошибка"),
            "failed": self.tr("ошибка"),
            "disconnected": self.tr("отключено"),
        }
        color = _STATE_COLORS.get(state, "#9aa0a6")
        text = labels.get(state, state)
        self.state_label.setText(f"● {text}")
        self.state_label.setStyleSheet(
            f"padding: 0 6px; color: {color}; font-weight: 600;"
        )
        # Drive the viewport overlay.
        if state in ("connecting", "reconnecting"):
            self.view.set_overlay(self.tr("Подключение…"))
        elif state == "connected":
            self.view.set_overlay(None)
        elif state in ("error", "failed"):
            self.view.set_overlay(self.tr("Ошибка соединения"))
        else:
            self.view.set_overlay(self.tr("Нет сигнала"))
        # Update the connect button label.
        if self.transport is not None:
            self.act_connect.setText(self.tr("Отключиться"))
            self.act_connect.setIcon(self._std_icon(QStyle.SP_MediaStop))
        else:
            self.act_connect.setText(self.tr("Подключиться"))
            self.act_connect.setIcon(self._std_icon(QStyle.SP_MediaPlay))

    def _set_status(self, text: str):
        """Transient message in the status bar (left side)."""
        self.statusBar().showMessage(text, 5000)

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
        self.transport.view_size_provider = self._view_size
        self.transport.on_video = lambda data, flags: self.bridge.video.emit(data, flags)
        self.transport.on_session = lambda msg: self.bridge.session.emit(msg)
        self.transport.on_clipboard = lambda msg: self.bridge.clipboard.emit(msg)
        self.transport.on_state = lambda s, d: self.bridge.state.emit(s, d)
        self.transport.on_host_key = lambda host, port, fp, changed: self.bridge.host_key.emit(host, port, fp, changed)
        self.transport.start()
        self._set_state("connecting")

    def disconnect_session(self):
        if self.transport is not None:
            self.transport.stop()
            self.transport = None
        self._set_state("disconnected")

    # -- video -------------------------------------------------------------
    def _on_video_gui(self, data: bytes, flags: int):
        self._frame_count += 1
        self._byte_count += len(data)
        result = self.decoder.decode(data, flags)
        if result is not None:
            width, height, rgb = result
            self.view.set_frame(rgb, width, height)

    def _on_session_gui(self, msg: dict):
        self._server_screen = tuple(msg.get("screen", self.cfg.geometry))
        codec = msg.get("codec") or self.cfg.codec
        self.decoder.reset(codec)
        self._set_status(
            self.tr("сессия {} | backend={} | {}x{} | {} FPS").format(
                msg.get("session_id"), msg.get("backend"),
                self._server_screen[0], self._server_screen[1], msg.get("fps"))
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

    # -- state / heartbeat / metrics --------------------------------------
    def _on_state_gui(self, state: str, detail: str):
        self._set_state(state)
        if detail:
            self._set_status(f"{detail}")
        if state == "connected":
            if self.transport is not None:
                self.transport.send_control({"t": "resize", "view": list(self._view_size())})

    def _view_size(self):
        return (self.view.width(), self.view.height())

    def _heartbeat(self):
        if self.transport is not None:
            self.transport.heartbeat()

    def _refresh_metrics(self):
        """Update the live metrics label (FPS / bitrate / RTT / res / host)."""
        now = time.monotonic()
        dt = max(0.001, now - self._metrics_t0)
        self._metrics_t0 = now
        fps = self._frame_count / dt
        bitrate_kbps = (self._byte_count * 8 / 1000) / dt
        self._frame_count = 0
        self._byte_count = 0

        rtt = 0.0
        loss = 0.0
        if self.transport is not None:
            st = self.transport.get_stats()
            rtt = st.get("rtt_ms", 0.0)
            loss = st.get("loss", 0.0)

        host = getattr(self.cfg, "host", "") or ""
        sid = ""
        if self.transport is not None and self.transport.session_info:
            sid = self.transport.session_info.get("session_id", "")
        w, h = self._server_screen

        parts = [
            f"{fps:.0f} FPS",
            f"{bitrate_kbps/1000:.1f} Mbps" if bitrate_kbps >= 1000 else f"{bitrate_kbps:.0f} kbps",
            f"RTT {rtt:.0f} ms" if rtt else "RTT —",
            f"{w}x{h}",
        ]
        if loss > 0.001:
            parts.append(f"loss {loss*100:.0f}%")
        if host:
            parts.append(host)
        if sid:
            parts.append(f"#{sid}")
        self.metrics_label.setText("  ·  ".join(parts))

    # -- dialogs / fullscreen ----------------------------------------------
    def _toggle_fullscreen(self, checked):
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()

    def _open_files(self):
        if self.transport is None:
            QMessageBox.information(self, self.tr("Файлы"), self.tr("Сначала подключитесь."))
            return
        FilesDialog(self.transport, self.cfg, self).exec()

    def _open_keys(self):
        KeysDialog(self.cfg, self).exec()

    def _open_prefs(self):
        from .preferences_dialog import PreferencesDialog
        PreferencesDialog(self.cfg, self).exec()
        # Apply theme/language immediately (they may have changed).
        from . import theme as _theme
        from . import i18n as _i18n
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        _theme.apply_theme(app, getattr(self.cfg, "theme", "dark"))
        _i18n.set_language(app, getattr(self.cfg, "language", "ru"))
        # Re-translate dynamic labels.
        self._set_state(self._state)

    def _on_host_key_gui(self, host, port, fingerprint, changed):
        dialog = HostKeyDialog(host, port, fingerprint, changed, self)
        result = dialog.exec()
        if result == HostKeyDialog.Accepted:
            self.transport.confirm_host_key(True, dialog.remember())
        elif result == HostKeyDialog.Rejected:
            self.transport.confirm_host_key(False, remember=False)

    def _on_files_dropped(self, files):
        if self.transport is None:
            QMessageBox.information(self, self.tr("Файлы"), self.tr("Сначала подключитесь."))
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
                self._set_status(self.tr("загрузка {}…").format(name))
            except Exception as exc:
                self._set_status(self.tr("ошибка загрузки: {}").format(exc))

    def closeEvent(self, event):
        self.disconnect_session()
        super().closeEvent(event)
