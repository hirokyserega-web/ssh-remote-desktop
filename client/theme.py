"""Unified light/dark theme for the client via Qt stylesheet + palette.

A single :func:`apply_theme` takes ``"light"`` | ``"dark"`` | ``"system"`` and
applies a coherent palette + stylesheet to the ``QApplication``. ``"system"``
defers to Qt's default (which follows the desktop on most platforms).

Keeping the theme in one place means every dialog inherits the same look and a
future per-page override only needs to set a local accent.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

THEMES = ("light", "dark", "system")

_DARK_QSS = """
QWidget { color: #e6e6e6; }
QMainWindow, QDialog { background-color: #1e1e1e; }
QToolBar { background-color: #2a2a2a; border: none; spacing: 4px; padding: 4px; }
QToolBar QToolButton { padding: 6px 10px; border-radius: 4px; }
QToolBar QToolButton:hover { background-color: #3a3a3a; }
QToolBar QToolButton:checked { background-color: #3d5a80; }
QMenuBar { background-color: #2a2a2a; }
QStatusBar { background-color: #2a2a2a; }
QListWidget, QTableWidget, QTreeWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #2b2b2b; border: 1px solid #3a3a3a; border-radius: 4px;
}
QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {
    background-color: #3d5a80;
}
QPushButton {
    background-color: #3a3a3a; border: 1px solid #4a4a4a; border-radius: 4px;
    padding: 6px 14px;
}
QPushButton:hover { background-color: #4a4a4a; }
QPushButton:default { background-color: #3d5a80; border-color: #5a7aa0; }
QProgressBar { background-color: #2b2b2b; border: 1px solid #3a3a3a; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background-color: #3d5a80; border-radius: 3px; }
QGroupBox { border: 1px solid #3a3a3a; border-radius: 4px; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLabel { background: transparent; }
QScrollBar:vertical { background: #2a2a2a; width: 12px; }
QScrollBar::handle:vertical { background: #4a4a4a; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #5a5a5a; }
"""

_LIGHT_QSS = """
QWidget { color: #1c1c1c; }
QMainWindow, QDialog { background-color: #f5f5f5; }
QToolBar { background-color: #e8e8e8; border: none; spacing: 4px; padding: 4px; }
QToolBar QToolButton { padding: 6px 10px; border-radius: 4px; }
QToolBar QToolButton:hover { background-color: #dcdcdc; }
QToolBar QToolButton:checked { background-color: #3d5a80; color: white; }
QStatusBar { background-color: #e8e8e8; }
QListWidget, QTableWidget, QTreeWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #ffffff; border: 1px solid #cccccc; border-radius: 4px;
}
QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {
    background-color: #3d5a80; color: white;
}
QPushButton {
    background-color: #e8e8e8; border: 1px solid #bbbbbb; border-radius: 4px;
    padding: 6px 14px;
}
QPushButton:hover { background-color: #dcdcdc; }
QPushButton:default { background-color: #3d5a80; color: white; border-color: #2d4a70; }
QProgressBar { background-color: #ffffff; border: 1px solid #cccccc; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background-color: #3d5a80; border-radius: 3px; }
QGroupBox { border: 1px solid #cccccc; border-radius: 4px; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLabel { background: transparent; }
"""


def _dark_palette(app: QApplication) -> None:
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#1e1e1e"))
    pal.setColor(QPalette.WindowText, QColor("#e6e6e6"))
    pal.setColor(QPalette.Base, QColor("#2b2b2b"))
    pal.setColor(QPalette.AlternateBase, QColor("#252525"))
    pal.setColor(QPalette.Text, QColor("#e6e6e6"))
    pal.setColor(QPalette.Button, QColor("#2a2a2a"))
    pal.setColor(QPalette.ButtonText, QColor("#e6e6e6"))
    pal.setColor(QPalette.Highlight, QColor("#3d5a80"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor("#2b2b2b"))
    pal.setColor(QPalette.ToolTipText, QColor("#e6e6e6"))
    pal.setColor(QPalette.PlaceholderText, QColor("#888888"))
    app.setPalette(pal)


def _light_palette(app: QApplication) -> None:
    app.setPalette(QPalette())  # system defaults are light on most platforms


def apply_theme(app: QApplication, theme: str) -> None:
    """Apply ``theme`` to ``app`` (stylesheet + palette)."""
    theme = theme if theme in THEMES else "system"
    if theme == "system":
        app.setStyleSheet("")
        app.setPalette(QPalette())
        return
    if theme == "dark":
        app.setStyleSheet(_DARK_QSS)
        _dark_palette(app)
    else:  # light
        app.setStyleSheet(_LIGHT_QSS)
        _light_palette(app)
