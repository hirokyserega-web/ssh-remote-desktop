"""Translate Qt key events into X11 keysym names sent to the server.

The protocol speaks X11 keysym *names* (``"a"``, ``"Return"``, ``"Left"``,
``"ctrl"`` ...). This module maps Qt's ``Qt.Key`` enum and the event text to
those names so the same client works regardless of the platform Qt runs on
(Windows / xcb / wayland).
"""

from __future__ import annotations

from PySide6.QtCore import Qt

# Qt.Key -> X11 keysym name for non-printable / special keys.
_SPECIAL = {
    Qt.Key_Return: "Return",
    Qt.Key_Enter: "Return",
    Qt.Key_Tab: "Tab",
    Qt.Key_Backspace: "BackSpace",
    Qt.Key_Escape: "Escape",
    Qt.Key_Delete: "Delete",
    Qt.Key_Insert: "Insert",
    Qt.Key_Home: "Home",
    Qt.Key_End: "End",
    Qt.Key_PageUp: "Prior",
    Qt.Key_PageDown: "Next",
    Qt.Key_Left: "Left",
    Qt.Key_Right: "Right",
    Qt.Key_Up: "Up",
    Qt.Key_Down: "Down",
    Qt.Key_Space: "space",
    Qt.Key_Control: "Control_L",
    Qt.Key_Shift: "Shift_L",
    Qt.Key_Alt: "Alt_L",
    Qt.Key_AltGr: "ISO_Level3_Shift",
    Qt.Key_Meta: "Super_L",
    Qt.Key_Super_L: "Super_L",
    Qt.Key_Super_R: "Super_R",
    Qt.Key_CapsLock: "Caps_Lock",
    Qt.Key_Menu: "Menu",
}

# Punctuation by literal character -> X keysym name.
_PUNCT = {
    "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright",
    ";": "semicolon", "'": "apostrophe", "`": "grave", "\\": "backslash",
    ",": "comma", ".": "period", "/": "slash",
}

for _i in range(1, 13):
    _SPECIAL[getattr(Qt, f"Key_F{_i}")] = f"F{_i}"


def qt_event_to_keysym(ev) -> str | None:
    key = ev.key()
    if key in _SPECIAL:
        return _SPECIAL[key]
    text = ev.text()
    if text and text.isprintable() and len(text) == 1:
        if text in _PUNCT:
            return _PUNCT[text]
        return text
    # Letters/digits without usable text (e.g. with Ctrl held).
    if Qt.Key_A <= key <= Qt.Key_Z:
        return chr(key).lower()
    if Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key)
    return None


def qt_modifiers(mods) -> list[str]:
    out = []
    if mods & Qt.ControlModifier:
        out.append("ctrl")
    if mods & Qt.ShiftModifier:
        out.append("shift")
    if mods & Qt.AltModifier:
        out.append("alt")
    if mods & Qt.MetaModifier:
        out.append("meta")
    return out
