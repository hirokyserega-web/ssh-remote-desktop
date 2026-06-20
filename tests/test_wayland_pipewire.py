"""Tests for wayland_pipewire: portal-unavailable -> placeholder fallback."""
from __future__ import annotations
import pytest

from server.backend.wayland_pipewire import PipeWireCapture, PipeWireUnavailable


def test_no_dbus_next_raises_pipewire_unavailable(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "dbus_next", None)
    monkeypatch.setitem(sys.modules, "dbus_next.aio", None)
    cap = PipeWireCapture({}, (640, 480))
    with pytest.raises(PipeWireUnavailable):
        cap.start()

def test_no_portal_token_falls_back(monkeypatch):
    # dbus_next importable (stub) but portal returns no session -> fallback
    import types
    mod = types.ModuleType("dbus_next")
    aio = types.ModuleType("dbus_next.aio")
    aio.MessageBus = object
    mod.aio = aio
    monkeypatch.setitem(__import__("sys").modules, "dbus_next", mod)
    monkeypatch.setitem(__import__("sys").modules, "dbus_next.aio", aio)
    cap = PipeWireCapture({"XDG_RUNTIME_DIR": "/tmp"}, (640, 480))
    with pytest.raises(PipeWireUnavailable):
        cap.start()

def test_size_and_read_none_before_start():
    cap = PipeWireCapture({}, (640, 480))
    assert cap.size() is None
    assert cap.read() is None
    cap.stop()  # no-op, must not raise

