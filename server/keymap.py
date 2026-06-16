"""Key translation tables.

The client sends X11 *keysym* names (e.g. ``"a"``, ``"Return"``, ``"ctrl"``).
The X11 backend turns those into keycodes through the live keyboard mapping;
the Wayland/uinput backend needs Linux *evdev* codes instead. This module
provides:

* :func:`keysym_to_evdev` -- keysym name -> ``evdev`` key code (uinput).
* :data:`MOD_KEYSYMS`     -- canonical modifier keysym names.

For X11 the backend resolves keysyms dynamically via Xlib's
``keysym_to_keycode``; only the evdev table needs to be explicit here.
"""

from __future__ import annotations

try:
    from evdev import ecodes as e

    _E = e
except Exception:  # pragma: no cover - evdev optional on build host
    _E = None


MOD_KEYSYMS = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "meta": "Super_L",
    "super": "Super_L",
    "win": "Super_L",
    "altgr": "ISO_Level3_Shift",
}


# Map of keysym name -> evdev ecodes attribute name. Resolved lazily so the
# module imports even when python-evdev is not installed.
_KEYSYM_TO_EVDEV_NAME: dict[str, str] = {
    # letters
    **{c: f"KEY_{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz"},
    # digits
    **{d: f"KEY_{d}" for d in "0123456789"},
    # whitespace / editing
    "space": "KEY_SPACE",
    "Return": "KEY_ENTER",
    "Enter": "KEY_ENTER",
    "Tab": "KEY_TAB",
    "BackSpace": "KEY_BACKSPACE",
    "Escape": "KEY_ESC",
    "Delete": "KEY_DELETE",
    "Insert": "KEY_INSERT",
    "Home": "KEY_HOME",
    "End": "KEY_END",
    "Prior": "KEY_PAGEUP",
    "Page_Up": "KEY_PAGEUP",
    "Next": "KEY_PAGEDOWN",
    "Page_Down": "KEY_PAGEDOWN",
    # arrows
    "Left": "KEY_LEFT",
    "Right": "KEY_RIGHT",
    "Up": "KEY_UP",
    "Down": "KEY_DOWN",
    # modifiers
    "Control_L": "KEY_LEFTCTRL",
    "Control_R": "KEY_RIGHTCTRL",
    "Shift_L": "KEY_LEFTSHIFT",
    "Shift_R": "KEY_RIGHTSHIFT",
    "Alt_L": "KEY_LEFTALT",
    "Alt_R": "KEY_RIGHTALT",
    "Super_L": "KEY_LEFTMETA",
    "Super_R": "KEY_RIGHTMETA",
    "ISO_Level3_Shift": "KEY_RIGHTALT",
    "Caps_Lock": "KEY_CAPSLOCK",
    # punctuation
    "minus": "KEY_MINUS",
    "equal": "KEY_EQUAL",
    "bracketleft": "KEY_LEFTBRACE",
    "bracketright": "KEY_RIGHTBRACE",
    "semicolon": "KEY_SEMICOLON",
    "apostrophe": "KEY_APOSTROPHE",
    "grave": "KEY_GRAVE",
    "backslash": "KEY_BACKSLASH",
    "comma": "KEY_COMMA",
    "period": "KEY_DOT",
    "slash": "KEY_SLASH",
    # function keys
    **{f"F{i}": f"KEY_F{i}" for i in range(1, 13)},
}


def keysym_to_evdev(keysym: str):
    """Return the evdev key code for ``keysym`` or ``None`` if unmapped."""
    if _E is None:
        return None
    # Normalise common aliases (modifiers passed by name).
    keysym = MOD_KEYSYMS.get(keysym.lower(), keysym) if keysym else keysym
    name = _KEYSYM_TO_EVDEV_NAME.get(keysym)
    if name is None and len(keysym) == 1:
        name = _KEYSYM_TO_EVDEV_NAME.get(keysym.lower())
    if name is None:
        return None
    return getattr(_E, name, None)


try:
    from Xlib import XK as _XK

    _HAVE_XK = True
except Exception:  # pragma: no cover - Xlib optional on build host
    _XK = None
    _HAVE_XK = False


def keysym_to_x11(keysym: str):
    """Return the X11 keysym integer for ``keysym`` name, or ``None``.

    Accepts modifier aliases (``ctrl`` -> ``Control_L``), single printable
    characters (resolved via ``XK.string_to_keysym``) and explicit X keysym
    names such as ``Return`` / ``Left`` / ``F5``.
    """
    if not _HAVE_XK or not keysym:
        return None
    # Resolve modifier aliases to canonical X keysym names.
    name = MOD_KEYSYMS.get(keysym.lower(), keysym)
    # Direct name lookup (XK_<name>).
    sym = _XK.string_to_keysym(name)
    if sym:
        return sym
    # Single character: map via its literal string.
    if len(keysym) == 1:
        sym = _XK.string_to_keysym(keysym)
        if sym:
            return sym
        return ord(keysym)
    return None
