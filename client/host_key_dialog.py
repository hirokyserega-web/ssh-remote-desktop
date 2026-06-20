"""Modal TOFU dialog shown on first connect / changed host key.

Asks the user to confirm a server's SSH host-key fingerprint (SHA256) before
the SSH handshake proceeds. The fingerprint is presented in OpenSSH format
(``SHA256:base64``) exactly as ``ssh-keygen -lf`` prints it, so the user can
compare against an out-of-band source.

If ``changed`` is True (the key differs from the one previously trusted and
saved in ``known_hosts``), the dialog warns prominently and refuses to default
to "accept" -- the user must explicitly opt in. When ``old_fingerprint`` is
provided it is shown side-by-side with the new one so the user can see exactly
what changed.

All user-facing strings are wrapped in ``self.tr()`` so the runtime i18n
layer (:mod:`client.i18n`) can translate them at request time.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QCheckBox, QDialogButtonBox,
    QPushButton,
)


def _fp_label(text: str, *, highlight: bool = False) -> QLabel:
    """Build a monospace fingerprint label with the standard card style."""
    lbl = QLabel(text)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    style = (
        "font-family: monospace; font-size: 12pt;"
        "background: #1f1f1f; color: #e8e8e8;"
        "padding: 10px 12px; border-radius: 6px;"
    )
    if highlight:
        style += " border: 1px solid #c0392b;"
    lbl.setStyleSheet(style)
    return lbl


class HostKeyDialog(QDialog):
    def __init__(self, host: str, port: int, fingerprint: str, changed: bool,
                 old_fingerprint: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Подтверждение ключа сервера"))
        self.setMinimumWidth(540)
        self._changed = changed

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 18, 20, 18)

        title = QLabel(
            self.tr("Неизвестный ключ сервера") if not changed
            else self.tr("Ключ сервера ИЗМЕНИЛСЯ")
        )
        title.setStyleSheet(
            "font-size: 15pt; font-weight: 600;"
            if not changed else
            "font-size: 15pt; font-weight: 600; color: #c0392b;"
        )
        root.addWidget(title)

        info = QLabel(
            self.tr("Хост <b>{}:{}</b> предъявил следующий отпечаток "
                    "ключа (SHA256):").format(host, port)
        )
        info.setWordWrap(True)
        root.addWidget(info)

        # When the key changed and we have the old fingerprint, show both
        # side-by-side so the user can see exactly what differs.
        if changed and old_fingerprint:
            old_box = QVBoxLayout()
            old_box.setSpacing(2)
            old_lbl = QLabel(self.tr("Ранее сохранённый отпечаток:"))
            old_lbl.setStyleSheet("color: #9aa0a6;")
            old_box.addWidget(old_lbl)
            old_box.addWidget(_fp_label(old_fingerprint))
            root.addLayout(old_box)

            new_box = QVBoxLayout()
            new_box.setSpacing(2)
            new_lbl = QLabel(self.tr("Новый отпечаток:"))
            new_lbl.setStyleSheet("color: #c0392b; font-weight: 600;")
            new_box.addWidget(new_lbl)
            new_box.addWidget(_fp_label(fingerprint, highlight=True))
            root.addLayout(new_box)
        else:
            root.addWidget(_fp_label(fingerprint, highlight=changed))

        if changed:
            warn = QLabel(
                self.tr("⚠️ ВНИМАНИЕ: ключ отличается от ранее сохранённого.\n"
                        "Это может означать атаку «человек посередине» либо "
                        "переустановку сервера. Подтверждайте только если вы "
                        "уверены в причине.")
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #c0392b;")
            root.addWidget(warn)

        self._remember = QCheckBox(self.tr("Запомнить ключ и больше не спрашивать"))
        # On a key change, default to NOT remembering — force an explicit opt-in
        # so a careless Enter doesn't silently overwrite the trusted key.
        self._remember.setChecked(not changed)
        root.addWidget(self._remember)

        buttons = QDialogButtonBox(QDialogButtonBox.NoButton)
        b_accept = QPushButton(self.tr("Доверять"))
        b_reject = QPushButton(self.tr("Отклонить"))
        b_accept.setDefault(False)
        b_reject.setDefault(True)
        buttons.addButton(b_accept, QDialogButtonBox.AcceptRole)
        buttons.addButton(b_reject, QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def remember(self) -> bool:
        return self._remember.isChecked()
