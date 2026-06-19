"""Tests for X11 inject_key modifier-chord ordering (P1 tail).

Xlib is unavailable in CI, so we test the pure chord-ordering helper
``X11Backend._chord_events`` directly: on key-down modifiers are pressed
first (in order) then the main key; on key-up the main key is released
first then the modifiers in reverse. This is what makes Ctrl+A, Shift+1,
Alt+Tab land correctly.
"""

import pytest


def _import_x11():
    """Import X11Backend; skip the test if the module itself is unimportable."""
    try:
        from server.backend.x11 import X11Backend
        return X11Backend
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"cannot import server.backend.x11: {exc}")


def test_chord_down_presses_mods_then_main():
    X11Backend = _import_x11()
    events = X11Backend._chord_events(38, [37, 50], down=True)
    # order: ctrl(37) press, shift(50) press, main(38) press
    assert events == [("press", 37), ("press", 50), ("press", 38)]


def test_chord_up_releases_main_then_mods_reversed():
    X11Backend = _import_x11()
    events = X11Backend._chord_events(38, [37, 50], down=False)
    # order: main(38) release, shift(50) release, ctrl(37) release
    assert events == [("release", 38), ("release", 50), ("release", 37)]


def test_chord_no_mods_single_event():
    X11Backend = _import_x11()
    assert X11Backend._chord_events(24, [], down=True) == [("press", 24)]
    assert X11Backend._chord_events(24, [], down=False) == [("release", 24)]


def test_chord_preserves_mod_order_on_down():
    """Mods keep their given order on press (left-to-right as sent)."""
    X11Backend = _import_x11()
    events = X11Backend._chord_events(10, [1, 2, 3], down=True)
    assert [kc for _, kc in events] == [1, 2, 3, 10]


def test_chord_reverses_mod_order_on_up():
    """Mods release in reverse so the chord unwinds symmetrically."""
    X11Backend = _import_x11()
    events = X11Backend._chord_events(10, [1, 2, 3], down=False)
    assert [kc for _, kc in events] == [10, 3, 2, 1]
