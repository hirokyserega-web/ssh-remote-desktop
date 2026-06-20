"""Lightweight runtime localization (RU/EN) without .qm compilation.

The UI source strings are written in Russian and wrapped in ``self.tr()`` /
``QCoreApplication.translate()``. A custom :class:`DictTranslator` is installed
on the ``QApplication``: when the active language is English it returns the
English string from a table; for Russian (the source language) it returns the
string unchanged. This gives real runtime language switching with zero build
tooling (no ``pyside6-lupdate``/``lrelease``) and keeps the standard Qt ``tr()``
call sites, so a future migration to compiled ``.qm`` catalogs is a drop-in.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QTranslator

# Russian is the source language; this table maps each RU source string to its
# English equivalent. Add entries as new user-facing strings appear.
_EN: dict[str, str] = {
    # MainWindow / toolbar
    "SSH Remote Desktop": "SSH Remote Desktop",
    "Подключиться": "Connect",
    "Отключиться": "Disconnect",
    "Полный экран": "Fullscreen",
    "Файлы": "Files",
    "SSH-ключи": "SSH keys",
    "Спец. сочетания": "Special combos",
    "Синхр. буфер": "Clipboard sync",
    "Настройки": "Preferences",
    "Ctrl+Alt+Del": "Ctrl+Alt+Del",
    "Super (Win)": "Super (Win)",
    "Alt+Tab": "Alt+Tab",
    "не подключено": "offline",
    "отключено": "disconnected",
    "Сначала подключитесь.": "Connect first.",
    # states
    "подключение…": "connecting…",
    "переподключение…": "reconnecting…",
    "в сети": "online",
    "ошибка": "error",
    "Нет сигнала": "No signal",
    "Подключение…": "Connecting…",
    "Переподключение…": "Reconnecting…",
    # ConnectDialog
    "Подключение": "Connection",
    "Хост:": "Host:",
    "Порт:": "Port:",
    "Пользователь:": "User:",
    "Аутентификация:": "Authentication:",
    "Приватный ключ:": "Private key:",
    "Пароль / passphrase:": "Password / passphrase:",
    "Пароль:": "Password:",
    "Кодек:": "Codec:",
    "Разрешение сессии:": "Session resolution:",
    "Сохранять сессию для переподключения": "Keep session for reconnect",
    "Запустить в полноэкранном режиме": "Start in fullscreen",
    "Профиль:": "Profile:",
    "Сохранить профиль": "Save profile",
    "Удалить профиль": "Delete profile",
    "Проверить соединение": "Test connection",
    "Укажите хост.": "Enter a host.",
    "Укажите пользователя.": "Enter a user.",
    "Разрешение должно быть вида WxH (например 1920x1080), не меньше 320x240.":
        "Resolution must be WxH (e.g. 1920x1080), at least 320x240.",
    # FilesDialog
    "Файловый менеджер — общая папка": "File manager — shared folder",
    "Путь:": "Path:",
    "Вверх": "Up",
    "Обновить": "Refresh",
    "Загрузить на сервер…": "Upload to server…",
    "Скачать выбранное…": "Download selected…",
    "Создать папку": "New folder",
    "Удалить": "Delete",
    "Новая папка": "New folder",
    "Имя папки:": "Folder name:",
    "Передача": "Transfer",
    # KeysDialog
    "Менеджер SSH-ключей": "SSH key manager",
    "Тип ключа:": "Key type:",
    "Размер RSA (бит):": "RSA size (bits):",
    "Комментарий:": "Comment:",
    "Папка:": "Folder:",
    "Имя файла:": "File name:",
    "Сгенерировать ключ": "Generate key",
    "Сохранить в файлы": "Save to files",
    "Публичный ключ:": "Public key:",
    "Отпечаток (SHA256):": "Fingerprint (SHA256):",
    "Копировать публичный ключ": "Copy public key",
    "Копировать команду установки": "Copy install command",
    "Установка на сервер (выполнить на сервере под нужным пользователем):":
        "Install on the server (run on the server as the target user):",
    "Закрыть": "Close",
    "Сохранено": "Saved",
    "Нет ключа": "No key",
    "Сначала сгенерируйте ключ.": "Generate a key first.",
    "comment@host (необязательно)": "comment@host (optional)",
    "необязательно": "optional",
    "Папка для ключей": "Key folder",
    "Обзор…": "Browse…",
    # Preferences
    "Предпочтения": "Preferences",
    "Тема:": "Theme:",
    "Язык:": "Language:",
    "Кодек по умолчанию:": "Default codec:",
    "Качество JPEG:": "JPEG quality:",
    "Путь к ключам:": "Key path:",
    "Светлая": "Light",
    "Тёмная": "Dark",
    "Системная": "System",
    "Русский": "Russian",
    "English": "English",
    "Применить": "Apply",
    "ОК": "OK",
    "Отмена": "Cancel",
    'Разрешение должно быть вида WxH (например 1920x1080), от 16x16 до 7680x4320.': 'Resolution must be WxH (e.g. 1920x1080), from 16x16 up to 7680x4320.',
    'Сортировка:': 'Sort:',
    'имя': 'name',
    'размер': 'size',
    'дата': 'date',
    'Копировать отпечаток': 'Copy fingerprint',
    '— сгенерируйте ключ, чтобы увидеть отпечаток —': '— generate a key to see its fingerprint —',
    'Ошибка': 'Error',
    'Не удалось сгенерировать ключ:\\n{exc}': 'Could not generate key:\\n{exc}',
    'Не удалось сохранить:\\n{exc}': 'Could not save:\\n{exc}',
}

TRANSLATIONS: dict[str, dict[str, str]] = {"en": _EN}

_ACTIVE_LANG = "ru"
_translator: "DictTranslator | None" = None

SUPPORTED_LANGS = ("ru", "en")


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class DictTranslator(QTranslator):
    """Translate source strings via a Python dict for the active language."""

    def __init__(self, lang: str = "ru"):
        super().__init__()
        self.lang = lang
        self._table = TRANSLATIONS.get(lang, {})

    def translate(self, context, sourceText, disambiguation=None, n=-1):  # noqa: ARG002
        src = _decode(sourceText)
        if self.lang == "ru":
            return src  # source language: return unchanged
        return self._table.get(src)


def active_language() -> str:
    return _ACTIVE_LANG


def set_language(app, lang: str) -> None:
    """Install/replace the dict translator for ``lang`` on ``app``."""
    global _ACTIVE_LANG, _translator
    lang = lang if lang in SUPPORTED_LANGS else "ru"
    if _translator is not None:
        QCoreApplication.removeTranslator(_translator)
    _translator = DictTranslator(lang)
    app.installTranslator(_translator)
    _ACTIVE_LANG = lang


def tr(source: str) -> str:
    """Module-level helper: translate ``source`` for the active language.

    Useful in contexts without a ``QObject`` (e.g. module-scope constants).
    """
    if _ACTIVE_LANG == "ru":
        return source
    return TRANSLATIONS.get(_ACTIVE_LANG, {}).get(source, source)
