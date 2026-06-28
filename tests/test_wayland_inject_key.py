"""Wayland inject_key: must not call ydotool with "None:1" for unmapped keysyms.

When ``keysym_to_evdev(keysym)`` returns ``None`` (the keysym has no evdev
mapping), the old code fell through to the ``elif down:`` branch and called
``ydotool key None:1 None:0`` — a nonsense command that either errors out or
(no worse) does nothing. The fix: only invoke the ydotool fallback when
``code is not None``, and log the unmapped keysym at debug level instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _import_wayland():
    from server.backend import wayland as wl
    return wl


@pytest.fixture
def wl():
    mod = _import_wayland()
    return mod


def _make_backend(wl):
    """Build a WaylandBackend with uinput disabled so we exercise the
    ydotool fallback path."""
    backend = wl.WaylandBackend(env={}, geometry=(640, 480))
    # Force the uinput path off so inject_key uses the ydotool fallback.
    backend._uinput = wl._UInputDevices()
    backend._uinput.pointer = None
    backend._uinput.keyboard = None
    return backend


def test_inject_key_unmapped_keysym_does_not_call_ydotool(wl, monkeypatch):
    """An unmapped keysym (code is None) must not reach ydotool at all."""
    calls = []
    monkeypatch.setattr(wl.WaylandBackend, "_ydotool",
                        lambda self, *args: calls.append(args))
    # Poison keysym_to_evdev to always return None (unmapped).
    monkeypatch.setattr(wl, "keysym_to_evdev", lambda _ks: None)

    backend = _make_backend(wl)
    # Both down=True and down=False must be safe.
    backend.inject_key("nonexistent_keysym", True)
    backend.inject_key("nonexistent_keysym", False)
    assert calls == [], (
        f"ydotool should not be called for unmapped keysym, got: {calls}"
    )


def test_inject_key_mapped_keysym_calls_ydotool_on_down(wl, monkeypatch):
    """A mapped keysym with no uinput falls back to ydotool on logical down."""
    calls = []
    monkeypatch.setattr(wl.WaylandBackend, "_ydotool",
                        lambda self, *args: calls.append(args))
    monkeypatch.setattr(wl, "keysym_to_evdev", lambda _ks: 30)  # evdev KEY_A

    backend = _make_backend(wl)
    backend.inject_key("a", True)
    # ydotool key "30:1" "30:0" — press + release pair on logical down.
    assert len(calls) == 1
    assert "30:1" in calls[0] and "30:0" in calls[0]

    # On key-up (down=False) the ydotool fallback should NOT fire again
    # (the press+release pair was already sent on logical down).
    calls.clear()
    backend.inject_key("a", False)
    assert calls == [], "ydotool should only fire on logical down"


def test_inject_key_no_none_in_ydotool_args(wl, monkeypatch):
    """Regression: the f-string must never produce 'None:1'."""
    calls = []
    monkeypatch.setattr(wl.WaylandBackend, "_ydotool",
                        lambda self, *args: calls.append(args))
    # Return a real code so we can check the f-string formatting.
    monkeypatch.setattr(wl, "keysym_to_evdev", lambda _ks: 42)

    backend = _make_backend(wl)
    backend.inject_key("x", True)
    assert calls, "expected ydotool to be called for mapped keysym"
    for arg in calls[0]:
        assert "None" not in str(arg), f"found 'None' in ydotool arg: {arg}"
