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


def test_start_exports_session_display_into_os_environ(x11, monkeypatch):
    """DISPLAY must be exported too, not just XAUTHORITY.

    mss reads DISPLAY from os.environ; if only XAUTHORITY is exported the
    capture connects to the host :0 (not the session Xvfb :N) even from a
    source install where python-xlib is present.
    """
    _wire_backend(monkeypatch, x11)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XAUTHORITY", raising=False)

    env = {"DISPLAY": ":10", "XAUTHORITY": "/tmp/rd-xauth-abc"}
    backend = x11.X11Backend(env=env, geometry=(800, 600))
    backend.start()

    # The session display is now visible in os.environ for mss / xclip / xauth.
    assert os.environ["DISPLAY"] == ":10"
    # python-xlib received the session display name, not the host :0.
    assert backend._dpy.name == ":10"

    backend.stop()
    # Broker's original :0 is restored.
    assert os.environ["DISPLAY"] == ":0"


def test_start_exports_env_without_xlib(monkeypatch):
    """The env export must happen even when python-xlib is absent (Nuitka onefile).

    This is the primary regression: in the prebuilt binary _HAVE_XLIB is False,
    so the old code skipped the export and mss connected to the host display.
    We force _HAVE_XLIB=False and verify DISPLAY/XAUTHORITY still land in
    os.environ before _init_mss runs.
    """
    mod = _import_x11()
    monkeypatch.setattr(mod, "_HAVE_XLIB", False)
    monkeypatch.setattr(mod.X11Backend, "_init_mss", lambda self: None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XAUTHORITY", raising=False)

    env = {"DISPLAY": ":15", "XAUTHORITY": "/tmp/cookie-onefile"}
    backend = mod.X11Backend(env=env, geometry=(800, 600))
    backend.start()

    assert os.environ["DISPLAY"] == ":15"
    assert os.environ["XAUTHORITY"] == "/tmp/cookie-onefile"

    backend.stop()
    assert os.environ["DISPLAY"] == ":0"
    assert "XAUTHORITY" not in os.environ


# --------------------------------------------------------------------------- #
# _init_mss must target the session display, not os.environ
# --------------------------------------------------------------------------- #
class _FakeMss:
    """Records the kwargs mss.mss() was called with; no live X server."""

    def __init__(self, **kwargs):
        self.captured_kwargs = dict(kwargs)
        self.monitors = [{"width": 800, "height": 600, "left": 0, "top": 0}]

    def close(self):
        pass


def test_init_mss_uses_session_display_not_os_environ(monkeypatch):
    """_init_mss must pass self.env["DISPLAY"] to mss, not read os.environ.

    Acceptance criterion: capture goes from the session display (:N from
    self.env), not the host :0. We poison os.environ["DISPLAY"] with :0 and
    confirm mss.mss() is still called with the session's :42.
    """
    mod = _import_x11()
    # Make mss available even if the real package is missing on the test host.
    fake_mss_module = MagicMock()
    fake_mss_module.mss = _FakeMss
    monkeypatch.setattr(mod, "mss", fake_mss_module)
    monkeypatch.setattr(mod, "_HAVE_MSS", True)
    monkeypatch.setenv("DISPLAY", ":0")  # host display — must NOT be used

    env = {"DISPLAY": ":42", "XAUTHORITY": "/tmp/cookie"}
    backend = mod.X11Backend(env=env, geometry=(800, 600))
    backend._init_mss()

    assert backend._mss is not None
    assert backend._mss.captured_kwargs.get("display") == ":42"
    assert backend._mss.captured_kwargs.get("display") != ":0"


def test_init_mss_falls_back_to_os_environ_when_env_empty(monkeypatch):
    """When self.env has no DISPLAY, _init_mss falls back to os.environ."""
    mod = _import_x11()
    fake_mss_module = MagicMock()
    fake_mss_module.mss = _FakeMss
    monkeypatch.setattr(mod, "mss", fake_mss_module)
    monkeypatch.setattr(mod, "_HAVE_MSS", True)
    monkeypatch.setenv("DISPLAY", ":7")

    backend = mod.X11Backend(env={}, geometry=(800, 600))
    backend._init_mss()

    assert backend._mss.captured_kwargs.get("display") == ":7"
