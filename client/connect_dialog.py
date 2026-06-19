"""Connection dialog: host, user, auth method, session options.

On open it populates the host field with a dropdown of the last fe"""

from __future__ import annotations

import os
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QLineEdit, QSpinBox,
    QComboBox, QPushButton, QCheckBox, QLabel, QFileDialog, QDialogButtonBox,
)
from PySide6.QtWidgets import QMessageBox


class ConnectDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Подключение")
        self.resize(460, 420)
        self._password: str | None = None

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.setInsertPolicy(QComboBox.NoInsert)
        self.host.lineEdit().setText(cfg.host)
        for h in self._load_recent_hosts():
            self.host.addItem(h)
        form.addRow("Хост:", self.host)

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.port)
        form.addRow("Порт:", self.port)

        self.user = QLineEdit(cfg.user)
        form.addRow("Пользователь:", self.user)

        self.auth = QComboBox()
        self.auth.addItems(["key", "password", "agent"])
        self.auth.setCurrentText(cfg.auth)
        self.auth.currentTextChanged.connect(self._auth_changed)
        form.addRow("Аутентификация:", self.auth)

        key_row = QHBoxLayout()
        self.key_path = QLineEdit(cfg.key_path)
        browse = QPushButton("…")
        browse.setMaximumWidth(34)
        browse.clicked.connect(self._browse_key)
        key_row.addWidget(self.key_path)
        key_row.addWidget(browse)
        self.key_row_label = QLabel("Приватный ключ:")
        form.addRow(self.key_row_label, key_row)

        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret_label = QLabel("Пароль / passphrase:")
        form.addRow(self.secret_label, self.secret)

        self.codec = QComboBox()
        self.codec.addItems(["h264", "jpeg"])
        self.codec.setCurrentText(cfg.codec)
        form.addRow("Кодек:", self.codec)

        self.geometry = QLineEdit(f"{cfg.geometry[0]}x{cfg.geometry[1]}")
        form.addRow("Разрешение сессии:", self.geometry)

        self.persistent = QCheckBox("Сохранять сессию для переподключения")
        self.persistent.setChecked(cfg.persistent)
        form.addRow("", self.persistent)

        self.fullscreen = QCheckBox("Запустить в полноэкранном режиме")
        self.fullscreen.setChecked(cfg.start_fullscreen)
        form.addRow("", self.fullscreen)

        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Подключиться")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._auth_changed(self.auth.currentText())

    def _auth_changed(self, method: str):
        is_key = method == "key"
        is_pw = method == "password"
        self.key_path.setEnabled(is_key)
        self.key_row_label.setEnabled(is_key)
        if is_pw:
            self.secret_label.setText("Пароль:")
        else:
            self.secret_label.setText("Passphrase ключа:")
        self.secret.setEnabled(is_key or is_pw)

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать приватный ключ",
                                              os.path.expanduser("~"))
        if path:
            self.key_path.setText(path)

    def _recent_hosts_path(self) -> Path:
        d = Path(os.path.expanduser("~/.config/ssh-remote-desktop"))
        d.mkdir(parents=True, exist_ok=True)
        return d / "recent_hosts.json"

    def _load_recent_hosts(self) -> list[str]:
        try:
            p = self._recent_hosts_path()
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(h) for h in data][:10]
        except Exception:
            pass
        return []

    def _save_recent_host(self, host: str):
        hosts = self._load_recent_hosts()
        if host in hosts:
            hosts.remove(host)
        hosts.insert(0, host)
        hosts = hosts[:10]
        try:
            self._recent_hosts_path().write_text(
                json.dumps(hosts, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def apply_to_config(self):
        self.cfg.host = self.host.currentText().strip()
        self.cfg.port = self.port.value()
        self.cfg.user = self.user.text().strip()
        self.cfg.auth = self.auth.currentText()
        self.cfg.key_path = self.key_path.text().strip()
        self.cfg.codec = self.codec.currentText()
        self.cfg.persistent = self.persistent.isChecked()
        self.cfg.start_fullscreen = self.fullscreen.isChecked()
        try:
            w, h = self.geometry.text().lower().split("x")
            self.cfg.geometry = (int(w), int(h))
        except Exception:
            pass
        self._password = self.secret.text() or None
        return self.cfg, self._password

    def accept(self):
        host = self.host.currentText().strip()
        user = self.user.text().strip()
        if not host:
            QMessageBox.warning(self, "Подключение", "Укажите хост.")
            return
        if not user:
            QMessageBox.warning(self, "Подключение", "Укажите пользователя.")
            return
        self.apply_to_config()
        self._save_recent_host(host)
        super().accept()

    def password(self) -> str | None:
        return self._password
