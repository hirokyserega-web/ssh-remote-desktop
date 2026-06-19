"""Preferences dialog: theme, language, default codec, quality, key path.

Changes apply immediately (theme + language switch live) and are persisted to
the client config file so they survive restarts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QSpinBox, QLineEdit,
    QPushButton, QHBoxLayout, QDialogButtonBox, QFileDialog,
)


def _config_path() -> Path:
    d = Path(os.path.expanduser("~/.config/ssh-remote-desktop"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "client.toml"


class PreferencesDialog(QDialog):
    def __init__(self, cfg, app, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.app = app
        self.setWindowTitle(self.tr("Настройки"))
        self.resize(420, 300)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.theme = QComboBox()
        self.theme.addItems(["system", "light", "dark"])
        self.theme.setCurrentText(cfg.theme if cfg.theme in ("system", "light", "dark") else "system")
        form.addRow(self.tr("Тема:"), self.theme)

        self.language = QComboBox()
        self.language.addItems(["ru", "en"])
        self.language.setCurrentText(cfg.language if cfg.language in ("ru", "en") else "ru")
        form.addRow(self.tr("Язык:"), self.language)

        self.codec = QComboBox()
        self.codec.addItems(["h264", "h265", "jpeg"])
        self.codec.setCurrentText(cfg.codec if cfg.codec in ("h264", "h265", "jpeg") else "h264")
        form.addRow(self.tr("Кодек по умолчанию:"), self.codec)

        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(int(getattr(cfg, "jpeg_quality", 80)))
        self.quality.setSuffix(" %")
        form.addRow(self.tr("Качество JPEG:"), self.quality)

        key_row = QHBoxLayout()
        self.key_path = QLineEdit(cfg.key_path)
        browse = QPushButton("…")
        browse.setMaximumWidth(34)
        browse.clicked.connect(self._browse_key)
        key_row.addWidget(self.key_path)
        key_row.addWidget(browse)
        form.addRow(self.tr("Путь к ключу:"), key_row)

        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("ОК"))
        buttons.button(QDialogButtonBox.Cancel).setText(self.tr("Отмена"))
        buttons.button(QDialogButtonBox.Apply).setText(self.tr("Применить"))
        buttons.accepted.connect(self._apply_and_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        root.addWidget(buttons)

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Выбрать приватный ключ"), os.path.expanduser("~"))
        if path:
            self.key_path.setText(path)

    def _apply(self):
        self.cfg.theme = self.theme.currentText()
        self.cfg.language = self.language.currentText()
        self.cfg.codec = self.codec.currentText()
        self.cfg.jpeg_quality = self.quality.value()
        self.cfg.key_path = self.key_path.text().strip()
        # Live theme + language switch.
        try:
            from .theme import apply_theme
            apply_theme(self.app, self.cfg.theme)
        except Exception:
            pass
        try:
            from .i18n import set_language
            set_language(self.app, self.cfg.language)
        except Exception:
            pass
        self._persist()

    def _apply_and_accept(self):
        self._apply()
        self.accept()

    def _persist(self):
        """Write the UI prefs back to the client config file (JSON-formatted)."""
        try:
            path = _config_path()
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data["theme"] = self.cfg.theme
            data["language"] = self.cfg.language
            data["codec"] = self.cfg.codec
            data["jpeg_quality"] = self.cfg.jpeg_quality
            data["key_path"] = self.cfg.key_path
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
