"""rd-server-gui entry point: PySide6 control panel for the SSH RD server.

Run as ``python -m server_gui`` or via the ``rd-server-gui`` console script.

Features
--------
* Edit ``server.toml`` (host/port/backend/limits/codec/auth toggles/run_as_user/
  logging) with validation. Atomic save; never persists secrets.
* Start / stop / restart the server — via systemd when the unit is installed,
  else via ``rd-server --daemon/--stop``. Live status (running/stopped, PID,
  port) + tail of the server log.
* "Autostart at boot" toggle for the systemd unit.
* Optional system tray (Open / Start / Stop / Quit) with
  ``--tray`` / ``--minimized`` flags and a "minimize to tray on close" prefs
  toggle. Falls back to a plain window when no tray is available.

Reuses :mod:`client.theme` (light/dark/system) and :mod:`client.i18n` (RU/EN).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure the project root is importable when run as ``python -m server_gui``
# from a checkout (matches the tests' sys.path setup).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget)

from client import i18n
from client.theme import apply_theme
from server_gui.controller import (
    BACKENDS, CODECS, ConfigController, ConfigError, GuiPrefs, LOG_LEVELS,
    ServerGuiConfig, ServiceController, pick_controller,
    tail_log,
)

TRAY_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAQElEQVR4nO3PMQEAIAzAMOKf"
    "i6BJzMDgFwBIVEBEqwt4A0REJiJyAxFdARF9ARH9ARRAgIiICABATfQBRO8PMaP9WwAAAABJ"
    "RU5ErkJggg=="
)


def _tr(s: str) -> str:
    return i18n.tr(s)


def _make_tray_icon() -> QIcon:
    """A tiny inline icon so we don't need an on-disk asset to show a tray."""
    import base64
    from PySide6.QtGui import QPixmap, QImage
    img = QImage.fromData(base64.b64decode(TRAY_ICON_B64))
    return QIcon(QPixmap.fromImage(img))


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class ServerGuiWindow(QMainWindow):
    def __init__(self, config_path: str, prefs: GuiPrefs,
                 *, start_minimized: bool = False, use_tray: bool = True):
        super().__init__()
        self.config_path = config_path
        self.prefs = prefs
        self.ctrl = ConfigController(config_path)
        # The service controller is picked per current state (systemd if the
        # unit is installed, else daemon-mode fallback).
        self._svc: ServiceController | None = None
        self._refresh_svc()

        self.setWindowTitle(_tr("Настройки сервера"))
        self.resize(640, 720)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- config form ---------------------------------------------------
        root.addWidget(self._build_form_group())

        # --- server control ------------------------------------------------
        root.addWidget(self._build_control_group())

        # --- log tail ------------------------------------------------------
        root.addWidget(self._build_log_group(), 1)

        # --- bottom bar: save + tray toggle --------------------------------
        root.addLayout(self._build_bottom_bar())

        self._load_form_from_config()

        # --- status polling timer ------------------------------------------
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()

        # --- tray ----------------------------------------------------------
        self.tray = None
        self._tray_available = use_tray and self._init_tray()

        # Minimize-to-tray behaviour
        self._quitting = False

        if start_minimized and self._tray_available:
            self.hide()
            if self.tray is not None:
                self.tray.show()
        else:
            self.show()

        self._refresh_status()

    # -- form ----------------------------------------------------------------
    def _build_form_group(self) -> QGroupBox:
        gb = QGroupBox(_tr("Настройки сервера"))
        form = QFormLayout(gb)

        # Network
        self.e_host = QLineEdit()
        self.e_port = QSpinBox()
        self.e_port.setRange(1, 65535)
        self.cb_backend = QComboBox()
        self.cb_backend.addItems(list(BACKENDS))
        form.addRow(_tr("Хост:"), self.e_host)
        form.addRow(_tr("Порт:"), self.e_port)
        form.addRow(_tr("Бэкенд:"), self.cb_backend)

        # Sessions
        self.e_max_sessions = QSpinBox()
        self.e_max_sessions.setRange(0, 100000)
        self.e_idle_timeout = QSpinBox()
        self.e_idle_timeout.setRange(0, 86400 * 7)
        form.addRow(_tr("Макс. сессий:"), self.e_max_sessions)
        form.addRow(_tr("Таймаут простоя (с):"), self.e_idle_timeout)

        # Encoding
        self.cb_codec = QComboBox()
        self.cb_codec.addItems(list(CODECS))
        self.e_fps = QSpinBox()
        self.e_fps.setRange(0, 240)
        self.e_bitrate = QSpinBox()
        self.e_bitrate.setRange(0, 100000)
        form.addRow(_tr("Кодек:"), self.cb_codec)
        form.addRow(_tr("FPS:"), self.e_fps)
        form.addRow(_tr("Битрейт (кбит/с):"), self.e_bitrate)

        # Files
        self.e_shared_dir = QLineEdit()
        self.btn_browse = QPushButton(_tr("Обзор…"))
        self.btn_browse.clicked.connect(self._browse_shared_dir)
        sd_row = QHBoxLayout()
        sd_row.addWidget(self.e_shared_dir, 1)
        sd_row.addWidget(self.btn_browse)
        sd_w = QWidget()
        sd_w.setLayout(sd_row)
        form.addRow(_tr("Общая папка:"), sd_w)

        # Auth
        self.chk_allow_password = QCheckBox(_tr("Разрешить пароль"))
        self.chk_allow_publickey = QCheckBox(_tr("Разрешить публичный ключ"))
        self.chk_run_as_user = QCheckBox(_tr("Запускать от имени пользователя"))
        form.addRow(self.chk_allow_password)
        form.addRow(self.chk_allow_publickey)
        form.addRow(self.chk_run_as_user)

        # Logging
        self.cb_log_level = QComboBox()
        self.cb_log_level.addItems(list(LOG_LEVELS))
        self.e_log_file = QLineEdit()
        form.addRow(_tr("Уровень журнала:"), self.cb_log_level)
        form.addRow(_tr("Файл журнала:"), self.e_log_file)

        return gb

    def _build_control_group(self) -> QGroupBox:
        gb = QGroupBox(_tr("Управление сервером"))
        v = QVBoxLayout(gb)

        # Status row
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel(_tr("Состояние:")))
        self.lbl_state = QLabel(_tr("Неизвестно"))
        self.lbl_state.setStyleSheet("font-weight: bold;")
        status_row.addWidget(self.lbl_state, 1)
        status_row.addWidget(QLabel(_tr("PID:")))
        self.lbl_pid = QLabel("—")
        status_row.addWidget(self.lbl_pid)
        v.addLayout(status_row)

        # Managed-by hint
        self.lbl_managed = QLabel("")
        self.lbl_managed.setStyleSheet("color: gray;")
        v.addWidget(self.lbl_managed)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton(_tr("Старт"))
        self.btn_stop = QPushButton(_tr("Стоп"))
        self.btn_restart = QPushButton(_tr("Перезапуск"))
        self.btn_refresh = QPushButton(_tr("Обновить статус"))
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_refresh):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_restart.clicked.connect(self._on_restart)
        self.btn_refresh.clicked.connect(self._refresh_status)

        # Autostart toggle
        self.chk_autostart = QCheckBox(_tr("Автозапуск при загрузке"))
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        v.addWidget(self.chk_autostart)

        return gb

    def _build_log_group(self) -> QGroupBox:
        gb = QGroupBox(_tr("Лог сервера"))
        v = QVBoxLayout(gb)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        v.addWidget(self.log_view)
        return gb

    def _build_bottom_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.chk_minimize_tray = QCheckBox(_tr("Свернуть в трей при закрытии"))
        self.chk_minimize_tray.setChecked(self.prefs.minimize_to_tray)
        self.chk_minimize_tray.toggled.connect(self._on_minimize_tray_toggled)
        row.addWidget(self.chk_minimize_tray)
        row.addStretch(1)
        self.btn_save = QPushButton(_tr("Применить и сохранить"))
        self.btn_save.clicked.connect(self._on_save)
        row.addWidget(self.btn_save)
        return row

    # -- load / save ---------------------------------------------------------
    def _load_form_from_config(self) -> None:
        try:
            self.cfg = self.ctrl.load()
        except ConfigError as exc:
            QMessageBox.critical(self, _tr("Ошибка"), str(exc))
            self.cfg = ServerGuiConfig()
        self.e_host.setText(self.cfg.host)
        self.e_port.setValue(self.cfg.port)
        self.cb_backend.setCurrentText(self.cfg.backend)
        self.e_max_sessions.setValue(self.cfg.max_sessions)
        self.e_idle_timeout.setValue(self.cfg.idle_timeout)
        self.cb_codec.setCurrentText(self.cfg.codec)
        self.e_fps.setValue(self.cfg.fps)
        self.e_bitrate.setValue(self.cfg.bitrate_kbps)
        self.e_shared_dir.setText(self.cfg.shared_dir)
        self.chk_allow_password.setChecked(self.cfg.allow_password)
        self.chk_allow_publickey.setChecked(self.cfg.allow_publickey)
        self.chk_run_as_user.setChecked(self.cfg.run_as_user)
        self.cb_log_level.setCurrentText(self.cfg.log_level)
        self.e_log_file.setText(self.cfg.log_file)

    def _collect_form(self) -> ServerGuiConfig:
        return ServerGuiConfig(
            host=self.e_host.text().strip() or "0.0.0.0",
            port=self.e_port.value(),
            backend=self.cb_backend.currentText(),
            max_sessions=self.e_max_sessions.value(),
            idle_timeout=self.e_idle_timeout.value(),
            codec=self.cb_codec.currentText(),
            fps=self.e_fps.value(),
            bitrate_kbps=self.e_bitrate.value(),
            shared_dir=self.e_shared_dir.text().strip(),
            allow_password=self.chk_allow_password.isChecked(),
            allow_publickey=self.chk_allow_publickey.isChecked(),
            run_as_user=self.chk_run_as_user.isChecked(),
            log_level=self.cb_log_level.currentText(),
            log_file=self.e_log_file.text().strip(),
        )

    def _on_save(self) -> None:
        cfg = self._collect_form()
        try:
            self.ctrl.save(cfg)
            self.cfg = cfg
            self._refresh_svc()
            self.statusBar().showMessage(_tr("Настройки сохранены"), 3000)
        except ConfigError as exc:
            QMessageBox.warning(self, _tr("Настройки невалидны"), str(exc))

    # -- shared dir browse ---------------------------------------------------
    def _browse_shared_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, _tr("Общая папка:"))
        if d:
            self.e_shared_dir.setText(d)

    # -- service control -----------------------------------------------------
    def _refresh_svc(self) -> None:
        try:
            self._svc = pick_controller(self.cfg)
        except Exception:
            self._svc = None

    def _state_label(self, state: str) -> str:
        if state == "running":
            return _tr("Запущен")
        if state == "stopped":
            return _tr("Остановлен")
        return _tr("Неизвестно")

    def _refresh_status(self) -> None:
        if self._svc is None:
            self.lbl_state.setText(_tr("Не установлен"))
            self.lbl_pid.setText("—")
            self.lbl_managed.setText("")
            self.chk_autostart.setEnabled(False)
            return
        st = self._svc.state()
        self.lbl_state.setText(self._state_label(st.state))
        self.lbl_pid.setText(str(st.pid) if st.pid else "—")
        self.lbl_managed.setText(
            _tr("Управление: systemd") if st.managed_by == "systemd"
            else _tr("Управление: демон") if st.managed_by == "daemon"
            else ""
        )
        # Autostart checkbox reflects systemd-managed state. Block signals so
        # we don't trigger a toggle when we're just reflecting reality.
        self.chk_autostart.blockSignals(True)
        self.chk_autostart.setEnabled(st.managed_by == "systemd")
        self.chk_autostart.setChecked(st.autostart)
        self.chk_autostart.blockSignals(False)
        # Log tail
        if st.managed_by == "systemd":
            from server_gui.controller import journalctl_tail
            self.log_view.setPlainText(journalctl_tail())
        else:
            log_path = self.cfg.log_file or ""
            if log_path:
                self.log_view.setPlainText(tail_log(os.path.expanduser(log_path)))
            elif st.managed_by == "daemon":
                # Daemon default pidfile dir is the config dir; no log file →
                # show a hint instead of an empty pane.
                self.log_view.setPlainText("")

    def _on_start(self) -> None:
        if self._svc and self._svc.start():
            self.statusBar().showMessage(_tr("Сервер запущен"), 3000)
        else:
            QMessageBox.warning(self, _tr("Ошибка"), _tr("Не удалось запустить сервер"))
        self._refresh_status()

    def _on_stop(self) -> None:
        if self._svc and self._svc.stop():
            self.statusBar().showMessage(_tr("Сервер остановлен"), 3000)
        else:
            QMessageBox.warning(self, _tr("Ошибка"), _tr("Не удалось остановить сервер"))
        self._refresh_status()

    def _on_restart(self) -> None:
        if self._svc and self._svc.restart():
            self.statusBar().showMessage(_tr("Сервер перезапущен"), 3000)
        else:
            QMessageBox.warning(self, _tr("Ошибка"), _tr("Не удалось перезапустить сервер"))
        self._refresh_status()

    def _on_autostart_toggled(self, on: bool) -> None:
        if self._svc is None or self._svc.name != "systemd":
            self.statusBar().showMessage(
                _tr("Системный юнит не установлен — автозапуск недоступен"), 4000)
            return
        if self._svc.enable_autostart(on):
            self.statusBar().showMessage(
                _tr("Автозапуск включён") if on else _tr("Автозапуск выключен"), 3000)
        self._refresh_status()

    def _on_minimize_tray_toggled(self, on: bool) -> None:
        self.prefs.minimize_to_tray = on
        self.prefs.save()

    # -- tray ----------------------------------------------------------------
    def _init_tray(self) -> bool:
        from PySide6.QtWidgets import QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        try:
            self.tray = QSystemTrayIcon(_make_tray_icon(), self)
            self.tray.setToolTip(_tr("Настройки сервера"))
            menu = _TrayMenu(self)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()
            return True
        except Exception:
            self.tray = None
            return False

    def _on_tray_activated(self, reason) -> None:
        # Double-click (Trigger) reveals the window.
        from PySide6.QtWidgets import QSystemTrayIcon
        if reason == QSystemTrayIcon.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # -- close handling ------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            self.prefs.save()
            event.accept()
            return
        # If tray is available and minimize-to-tray is on, hide instead of exit.
        if self._tray_available and self.prefs.minimize_to_tray:
            event.ignore()
            self.hide()
            if self.tray is not None:
                self.tray.show()
            return
        # No tray (or tray off): save prefs and exit normally.
        self._timer.stop()
        self.prefs.save()
        event.accept()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()
        QApplication.quit()


class _TrayMenu(QWidget):
    """Context menu for the tray icon: Open / Start / Stop / Quit."""

    def __init__(self, win: ServerGuiWindow):
        super().__init__()
        from PySide6.QtWidgets import QMenu
        self.win = win
        self.menu = QMenu()
        a_open = QAction(_tr("Открыть"), self.menu)
        a_open.triggered.connect(win._show_from_tray)
        a_start = QAction(_tr("Старт"), self.menu)
        a_start.triggered.connect(win._on_start)
        a_stop = QAction(_tr("Стоп"), self.menu)
        a_stop.triggered.connect(win._on_stop)
        a_quit = QAction(_tr("Выход"), self.menu)
        a_quit.triggered.connect(win.quit_app)
        self.menu.addAction(a_open)
        self.menu.addSeparator()
        self.menu.addAction(a_start)
        self.menu.addAction(a_stop)
        self.menu.addSeparator()
        self.menu.addAction(a_quit)

    def popup(self, *a, **kw):  # noqa: D401
        self.menu.popup(*a, **kw)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rd-server-gui",
                                description="SSH Remote Desktop server control panel")
    p.add_argument("--config", help="path to server.toml")
    p.add_argument("--tray", action="store_true",
                   help="enable the system tray (default unless --no-tray)")
    p.add_argument("--no-tray", dest="tray", action="store_false",
                   help="disable the system tray")
    p.add_argument("--minimized", action="store_true",
                   help="start hidden in the tray (implies --tray)")
    p.set_defaults(tray=True)
    return p


def _default_config_path() -> str:
    return os.path.expanduser("~/.config/ssh-remote-desktop/server.toml")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.minimized:
        args.tray = True

    # Headless / offscreen friendly: only force the offscreen Qt platform
    # when there is no display, so real desktops get a normal window (Qt
    # auto-selects xcb/wayland). Unconditionally defaulting to offscreen
    # hid the window on every desktop where QT_QPA_PLATFORM was unset.
    if not os.environ.get("QT_QPA_PLATFORM"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                or os.environ.get("XDG_SESSION_TYPE") in ("x11", "wayland")):
            os.environ["QT_QPA_PLATFORM"] = "offscreen"

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("rd-server-gui")

    prefs = GuiPrefs.load()
    apply_theme(app, prefs.theme)
    i18n.set_language(app, prefs.language)

    win = ServerGuiWindow(
        args.config or _default_config_path(),
        prefs,
        start_minimized=args.minimized,
        use_tray=args.tray,
    )

    # Keep a reference so the window isn't GC'd.
    app._win = win  # type: ignore[attr-defined]

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
