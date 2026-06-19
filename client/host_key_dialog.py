"""Modal TOFU dialog shown on first connect / changed host key.

Asks the user to confirm a server's SSH host-key fingerprint (SHA256) before
the SSH handshake proceeds. The fingerprint is presented in OpenSSH format
(``SHA256:base64``) exactly as ``ssh-keygen -lf`` prints it, so the user can
compare against an out-of-band source.

If ``changed`` is True (the key differs from the one previously trusted and
saved in ``known_hosts``), the dialog warns prominently and refuses to default
to "accept" -- the user must explicitly opt in.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QCheckBox, QDialogButtonBox,
    QPushButton,
)


class HostKeyDialog(QDialog):
    def __init__(self, host: str, port: int, fingerprint: str, changed: bool,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подтверждение ключа сервера")
        self.setMinimumWidth(520)
        self._changed = changed

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 18, 20, 18)

        title = QLabel("Неизвестный ключ сервера" if not changed
                       else "Ключ сервера ИЗМЕНИЛСЯ")
        title.setStyleSheet(
            "font-size: 15pt; font-weight: 600;"
            if not changed else
            "font-size: 15pt; font-weight: 600; color: #c0392b;"
        )
        root.addWidget(title)

        info = QLabel(
            f"Хост <b>{host}:{port}</b> предъявил следующий отпечаток "
            f"ключа (SHA256):"
        )
        info.setWordWrap(True)
        root.addWidget(info)

        fp = QLabel(fingerprint)
        fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fp.setStyleSheet(
            "font-family: monospace; font-size: 12pt;"
            "background: #1f1f1f; color: #e8e8e8;"
            "padding: 10px 12px; border-radius: 6px;"
        )
        root.addWidget(fp)

        if changed:
            warn = QLabel(
                "⚠️ ВНИМАНИЕ: ключ отличается от ранее сохранённого.\n"
                "Это может означать атаку «человек посередине» либо "
                "переустановку сервера. Подтверждайте только если вы "
                "уверены в причине."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #c0392b;")
            root.addWidget(warn)

        self._remember = QCheckBox("Запомнить ключ и больше не спрашивать")
        self._remember.setChecked(True)
        root.addWidget(self._remember)

        buttons = QDialogButtonBox(QDialogButtonBox.NoButton)
        b_accept = QPushButton("Доверять")
        b_reject = QPushButton("Отклонить")
        b_accept.setDefault(False)
        b_reject.setDefault(True)
        buttons.addButton(b_accept, QDialogButtonBox.AcceptRole)
        buttons.addButton(b_reject, QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def remember(self) -> bool:
        return self._remember.isChecked()
