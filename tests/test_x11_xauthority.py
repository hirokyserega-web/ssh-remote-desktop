"""Regression tests for the DisplayConnectionError fix in ``X11Backend``.

python-xlib reads the X authorization cookie from ``os.environ["XAUTHORITY"]``
(global process env) inside ``xdisplay.Display()``, NOT from the per-session
``env`` dict the backend stores on ``self.env``. The backend now exports the
session ``XAUTHORITY`` into ``os.environ`` right before constructing the
``Display`` and restores the previous value in ``stop()`` so parallel sessions
don't clobber each other.

These tests stub ``xdisplay.Display`` (no live X server needed) and assert the
export/restore behaviour around ``start()``/``stop()``.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


def _import_x11():
    from server.backend import x11 as x11mod

    return x11mod


@pytest.fixture
def x11():
    mod = _import_x11()
    if not mod._HAVE_XLIB:
        pytest.skip("python-xlib unavailable")
    return mod


class _FakeDisplay:
    """Minimal stand-in for ``Xlib.display.Display``.

    Records the value of ``os.environ["XAUTHORITY"]`` observed at construction
    time -- this is exactly what python-xlib's ``connect.get_auth()`` would
    read to find the session cookie.
    """

    def __init__(self, name):
        self.name = name
        self.captured_xauthority = os.environ.get("XAUTHORITY")
        self._screen = MagicMock()
        root = MagicMock()
        root.get_geometry.return_value = MagicMock(width=800, height=600)
        self._screen.root = root

    def screen(self):
        return self._screen

    def close(self):
        pass


def _wire_backend(monkeypatch, x11):
    """Disable the post-connect init helpers so start() needs no live X server."""
    monkeypatch.setattr(x11.X11Backend, "_init_damage", lambda self: None)
    monkeypatch.setattr(x11.X11Backend, "_init_xfixes", lambda self: None)
    monkeypatch.setattr(x11.X11Backend, "_init_mss", lambda self: None)
    monkeypatch.setattr(x11.xdisplay, "Display", _FakeDisplay)


def test_start_exports_session_xauthority_into_os_environ(x11, monkeypatch):
    _wire_backend(monkeypatch, x11)
    # Broker process had no XAUTHORITY of its own.
    monkeypatch.delenv("XAUTHORITY", raising=False)

    env = {"DISPLAY": ":10", "XAUTHORITY": "/tmp/rd-xauth-abc"}
    backend = x11.X11Backend(env=env, geometry=(800, 600))
    backend.start()

    # python-xlib would have seen the session cookie at Display() time...
    assert backend._dpy.captured_xauthority == "/tmp/rd-xauth-abc"
    # ...and it stays exported for the lifetime of the session.
    assert os.environ["XAUTHORITY"] == "/tmp/rd-xauth-abc"

    backend.stop()
    # No prior value -> fully removed so parallel sessions aren't affected.
    assert "XAUTHORITY" not in os.environ


def test_stop_restores_pre_existing_xauthority(x11, monkeypatch):
    _wire_backend(monkeypatch, x11)
    # Simulate a broker that already had an XAUTHORITY in its environment.
    monkeypatch.setenv("XAUTHORITY", "/tmp/broker-preexisting")

    env = {"DISPLAY": ":11", "XAUTHORITY": "/tmp/session-cookie"}
    backend = x11.X11Backend(env=env, geometry=(800, 600))
    backend.start()
    assert os.environ["XAUTHORITY"] == "/tmp/session-cookie"

    backend.stop()
    # The broker's original value is restored, not left as the session cookie.
    assert os.environ["XAUTHORITY"] == "/tmp/broker-preexisting"
