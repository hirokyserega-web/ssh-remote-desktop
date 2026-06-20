"""File manager dialog: browse the server's shared folder, upload/download.

Navigation commands go over the ``files`` control channel; the actual bytes
move over SFTP (:class:`client.files.FileTransfer`). Progress is shown in a bar
and transfers can be cancelled.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget, QListWidgetItem,
    QLabel, QProgressBar, QFileDialog, QMessageBox, QComboBox,
)
from PySide6.QtCore import Qt, Signal, QObject

from .files import FileTransfer


def _human_size(n: int) -> str:
    """Format a byte count as a short human-readable string (KiB/MiB/...)."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# Sort modes for the listing. Dirs always sort above files within each mode.
_SORT_MODES = ["name", "size", "mtime"]


class _ProgressBridge(QObject):
    progress = Signal(int, int)
    done = Signal(str, bool, str)
    listing = Signal(list)
    error = Signal(str)


class FilesDialog(QDialog):
    def __init__(self, transport, cfg, parent=None):
        super().__init__(parent)
        self.transport = transport
        self.cfg = cfg
        self.setWindowTitle(self.tr("Файловый менеджер — общая папка"))
        self.resize(560, 480)
        self._cwd = ""
        self._last_entries: list[dict] = []
        self._transfer = FileTransfer(transport)
        self._bridge = _ProgressBridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)
        self._bridge.listing.connect(self._populate)
        self._bridge.error.connect(self._on_error)

        root = QVBoxLayout(self)

        nav = QHBoxLayout()
        up = QPushButton(self.tr("Вверх"))
        up.clicked.connect(self._go_up)
        refresh = QPushButton(self.tr("Обновить"))
        refresh.clicked.connect(self._refresh)
        nav.addWidget(QLabel(self.tr("Путь:")))
        # Breadcrumb container: one clickable button per path segment + root.
        self.crumbs = QHBoxLayout()
        self.crumbs.setSpacing(2)
        nav.addLayout(self.crumbs, 1)
        nav.addWidget(up)
        nav.addWidget(refresh)
        root.addLayout(nav)

        # Sort control: name / size / mtime.
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel(self.tr("Сортировка:")))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([self.tr("имя"), self.tr("размер"), self.tr("дата")])
        self.sort_combo.currentIndexChanged.connect(lambda _i: self._populate(self._last_entries))
        sort_row.addWidget(self.sort_combo)
        sort_row.addStretch(1)
        root.addLayout(sort_row)

        self.listing = QListWidget()
        self.listing.itemDoubleClicked.connect(self._open_item)
        root.addWidget(self.listing, 1)

        actions = QHBoxLayout()
        upload = QPushButton(self.tr("Загрузить на сервер…"))
        upload.clicked.connect(self._upload)
        download = QPushButton(self.tr("Скачать выбранное…"))
        download.clicked.connect(self._download)
        mkdir = QPushButton(self.tr("Создать папку"))
        mkdir.clicked.connect(self._mkdir)
        delete = QPushButton(self.tr("Удалить"))
        delete.clicked.connect(self._delete)
        actions.addWidget(upload)
        actions.addWidget(download)
        actions.addWidget(mkdir)
        actions.addWidget(delete)
        root.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # The transport routes file-channel results here.
        transport.on_files = self._dispatch_files_result
        self._refresh()

    # -- server path helpers ----------------------------------------------
    def _shared_root(self) -> str:
        return ""  # jail root == shared dir; paths are relative to it

    def _refresh(self):
        self.transport.send_files({"t": "file_list", "path": self._cwd})

    def _go_up(self):
        if self._cwd:
            self._cwd = os.path.dirname(self._cwd.rstrip("/"))
            self._refresh()

    def _open_item(self, item: QListWidgetItem):
        data = item.data(Qt.UserRole)
        if data and data.get("is_dir"):
            name = data["name"]
            self._cwd = f"{self._cwd.rstrip('/')}/{name}".lstrip("/")
            self._refresh()

    # -- results from server ----------------------------------------------
    def _dispatch_files_result(self, msg: dict):
        # Called from the transport thread; marshal to the GUI thread via the
        # bridge's queued signals (never touch widgets off-thread).
        t = msg.get("t")
        if t == "file_list_result":
            self._bridge.listing.emit(msg.get("entries", []))
        elif t == "file_error":
            self._bridge.error.emit(msg.get("msg", "ошибка"))

    def _on_error(self, msg: str):
        QMessageBox.warning(self, self.tr("Файлы"), msg)

    def _populate(self, entries: list[dict]):
        self._last_entries = entries or []
        self.listing.clear()
        self._render_crumbs()
        mode = self.sort_combo.currentIndex() if hasattr(self, "sort_combo") else 0
        for ent in self._sorted_entries(self._last_entries, mode):
            label = ("📁 " if ent["is_dir"] else "📄 ") + ent["name"]
            if not ent["is_dir"]:
                label += f"   ({_human_size(int(ent.get('size', 0)))})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ent)
            self.listing.addItem(item)

    @staticmethod
    def _sorted_entries(entries: list[dict], mode: int) -> list[dict]:
        """Sort entries: directories first, then by the selected mode."""
        key = {0: lambda e: e.get("name", "").lower(),
               1: lambda e: -int(e.get("size", 0)),
               2: lambda e: -int(e.get("mtime", 0))}.get(mode, lambda e: e.get("name", ""))
        return sorted(entries, key=lambda e: (not e.get("is_dir", False), key(e)))

    def _render_crumbs(self):
        """Rebuild the clickable breadcrumb buttons from self._cwd."""
        # Clear previous crumbs.
        while self.crumbs.count():
            it = self.crumbs.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        root_btn = QPushButton("/")
        root_btn.setFlat(True)
        root_btn.clicked.connect(lambda: self._cd(""))
        self.crumbs.addWidget(root_btn)
        if not self._cwd:
            return
        acc = ""
        parts = [p for p in self._cwd.split("/") if p]
        for i, part in enumerate(parts):
            acc = f"{acc}/{part}".lstrip("/")
            sep = QLabel("›")
            sep.setStyleSheet("color: gray;")
            self.crumbs.addWidget(sep)
            btn = QPushButton(part)
            btn.setFlat(True)
            # Capture the accumulated path for this segment.
            target = acc
            btn.clicked.connect(lambda _=False, t=target: self._cd(t))
            # Last segment is the current dir; highlight it.
            if i == len(parts) - 1:
                btn.setStyleSheet("font-weight: bold;")
            self.crumbs.addWidget(btn)
        self.crumbs.addStretch(1)

    def _cd(self, path: str):
        self._cwd = path
        self._refresh()

    # -- transfers ---------------------------------------------------------
    def _remote_path(self, name: str) -> str:
        return f"{self._cwd.rstrip('/')}/{name}".lstrip("/")

    def _progress_cb(self, sent, total):
        self._bridge.progress.emit(sent, total)

    def _upload(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Файл для загрузки"),
                                              os.path.expanduser("~"))
        if not path:
            return
        name = os.path.basename(path)
        remote = self._remote_path(name)
        self.progress.setVisible(True)
        fut = self.transport.run_coro(
            self._transfer.upload(path, remote, self._progress_cb)
        )
        fut.add_done_callback(lambda f: self._bridge.done.emit(remote, f.exception() is None,
                                                               str(f.exception() or "")))

    def _download(self):
        item = self.listing.currentItem()
        if not item:
            return
        data = item.data(Qt.UserRole)
        if data.get("is_dir"):
            return
        dest_dir = QFileDialog.getExistingDirectory(self, self.tr("Куда сохранить"),
                                                     os.path.expanduser("~"))
        if not dest_dir:
            return
        remote = self._remote_path(data["name"])
        local = os.path.join(dest_dir, data["name"])
        self.progress.setVisible(True)
        fut = self.transport.run_coro(
            self._transfer.download(remote, local, self._progress_cb)
        )
        fut.add_done_callback(lambda f: self._bridge.done.emit(local, f.exception() is None,
                                                               str(f.exception() or "")))

    def _mkdir(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, self.tr("Новая папка"), self.tr("Имя папки:"))
        if ok and name:
            self.transport.send_files({"t": "file_mkdir", "path": self._remote_path(name)})
            self._refresh()

    def _delete(self):
        item = self.listing.currentItem()
        if not item:
            return
        data = item.data(Qt.UserRole)
        if QMessageBox.question(self, self.tr("Удалить"), self.tr(f"Удалить {data['name']}?")) == QMessageBox.Yes:
            self.transport.send_files({"t": "file_remove", "path": self._remote_path(data["name"])})
            self._refresh()

    # -- progress / done slots (GUI thread) --------------------------------
    def _on_progress(self, sent, total):
        self.progress.setVisible(True)
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(sent)

    def _on_done(self, path, ok, err):
        self.progress.setVisible(False)
        if not ok:
            QMessageBox.warning(self, self.tr("Передача"), self.tr(f"Ошибка: {err}"))
        else:
            self._refresh()
