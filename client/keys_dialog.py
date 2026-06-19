"""Qt dialog for in-app SSH key generation and management.

Lets the user generate an Ed25519 or RSA keypair, optionally protect it with a
passphrase, save it to a chosen folder, view/copy the public key and the exact
``authorized_keys`` line, and see a one-liner that installs it on the server.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QPlainTextEdit, QFileDialog, QMessageBox,
    QSpinBox,
)
from PySide6.QtGui import QGuiApplication

from crypto import generate_keypair, write_keypair, authorized_keys_line, public_key_fingerprint


class KeyManagerDialog(QDialog):
    def __init__(self, cfg=None, parent=None, default_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Менеджер SSH-ключей"))
        self.resize(680, 520)
        self._keypair = None
        if default_dir is None and cfg is not None:
            default_dir = os.path.dirname(os.path.expanduser(getattr(cfg, "key_path", "")))
        self._default_dir = default_dir or os.path.expanduser(
            "~/.config/ssh-remote-desktop"
        )

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.kind = QComboBox()
        self.kind.addItems(["ed25519", "rsa"])
        form.addRow(self.tr("Тип ключа:"), self.kind)

        self.bits = QSpinBox()
        self.bits.setRange(2048, 8192)
        self.bits.setSingleStep(1024)
        self.bits.setValue(3072)
        self.bits.setEnabled(False)
        form.addRow(self.tr("Размер RSA (бит):"), self.bits)
        self.kind.currentTextChanged.connect(
            lambda t: self.bits.setEnabled(t == "rsa")
        )

        self.comment = QLineEdit()
        self.comment.setPlaceholderText(self.tr("comment@host (необязательно)"))
        form.addRow(self.tr("Комментарий:"), self.comment)

        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.Password)
        self.passphrase.setPlaceholderText(self.tr("необязательно"))
        form.addRow(self.tr("Passphrase:"), self.passphrase)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(self._default_dir)
        browse = QPushButton(self.tr("Обзор…"))
        browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.dir_edit)
        dir_row.addWidget(browse)
        form.addRow(self.tr("Папка:"), dir_row)

        self.name_edit = QLineEdit("id_ed25519")
        form.addRow(self.tr("Имя файла:"), self.name_edit)
        self.kind.currentTextChanged.connect(
            lambda t: self.name_edit.setText(f"id_{t}")
        )

        root.addLayout(form)

        btn_row = QHBoxLayout()
        gen = QPushButton(self.tr("Сгенерировать ключ"))
        gen.clicked.connect(self._generate)
        save = QPushButton(self.tr("Сохранить в файлы"))
        save.clicked.connect(self._save)
        btn_row.addWidget(gen)
        btn_row.addWidget(save)
        root.addLayout(btn_row)

        root.addWidget(QLabel(self.tr("Публичный ключ:")))
        self.pub_view = QPlainTextEdit()
        self.pub_view.setReadOnly(True)
        self.pub_view.setMaximumHeight(80)
        root.addWidget(self.pub_view)

        root.addWidget(QLabel(self.tr("Отпечаток (SHA256):")))
        self.fp_view = QLineEdit()
        self.fp_view.setReadOnly(True)
        self.fp_view.setStyleSheet("font-family: monospace;")
        root.addWidget(self.fp_view)

        copy_row = QHBoxLayout()
        copy_pub = QPushButton(self.tr("Копировать публичный ключ"))
        copy_pub.clicked.connect(self._copy_pub)
        copy_cmd = QPushButton(self.tr("Копировать команду установки"))
        copy_cmd.clicked.connect(self._copy_install_cmd)
        copy_row.addWidget(copy_pub)
        copy_row.addWidget(copy_cmd)
        root.addLayout(copy_row)

        root.addWidget(QLabel(self.tr("Установка на сервер (выполнить на сервере под нужным пользователем):")))
        self.cmd_view = QPlainTextEdit()
        self.cmd_view.setReadOnly(True)
        self.cmd_view.setMaximumHeight(90)
        root.addWidget(self.cmd_view)

        close = QPushButton(self.tr("Закрыть"))
        close.clicked.connect(self.accept)
        root.addWidget(close)

    # -- actions -----------------------------------------------------------
    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, self.tr("Папка для ключей"), self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _generate(self):
        try:
            kind = self.kind.currentText()
            kp = generate_keypair(
                key_type=kind,
                rsa_bits=self.bits.value(),
                passphrase=self.passphrase.text() or None,
                comment=self.comment.text().strip(),
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Ошибка"), self.tr(f"Не удалось сгенерировать ключ:\n{exc}"))
            return
        self._keypair = kp
        self.pub_view.setPlainText(kp.public_openssh)
        try:
            self.fp_view.setText(public_key_fingerprint(kp.public_openssh))
        except Exception:
            self.fp_view.setText("")
        line = authorized_keys_line(kp.public_openssh)
        self.cmd_view.setPlainText(
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo '{line}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )

    def _save(self):
        if self._keypair is None:
            QMessageBox.information(self, self.tr("Нет ключа"), self.tr("Сначала сгенерируйте ключ."))
            return
        directory = self.dir_edit.text().strip()
        name = self.name_edit.text().strip() or "id_ed25519"
        try:
            priv, pub = write_keypair(
                self._keypair, directory, basename=name, overwrite=True
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Ошибка"), self.tr(f"Не удалось сохранить:\n{exc}"))
            return
        QMessageBox.information(
            self, self.tr("Сохранено"),
            f"Приватный ключ: {priv}\nПубличный ключ: {pub}",
        )

    def _copy_pub(self):
        if self._keypair:
            QGuiApplication.clipboard().setText(self._keypair.public_openssh)

    def _copy_install_cmd(self):
        QGuiApplication.clipboard().setText(self.cmd_view.toPlainText())


# Alias used across the GUI.
KeysDialog = KeyManagerDialog
