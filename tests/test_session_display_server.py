"""Tests for C2/C3: display-server readiness checks + display-number retry.

C2: a session must NOT connect a backend to a display server that never came
up — ``_start_x11``/``_start_wayland`` raise ``DisplayServerError`` with an
actionable message (missing binary / readiness timeout) instead of the old
fixed ``time.sleep(1.0)`` that produced the opaque "неизвестная ошибка".

C3: the X11 path retries the next free display number when Xvfb loses the
TOCTOU race for a number, instead of connecting to a dead display.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

if sys.platform != "linux":
    pytest.skip("server.session needs Unix pwd/getpwnam", allow_module_level=True)

from common.config import ServerConfig
from server import session
from server.session import DisplayServerError, Session, UserInfo


def _make_session(backend_kind="x11"):
    cfg = ServerConfig(backend=backend_kind, session_geometry=(320, 240))
    user = UserInfo("root")
    return Session(cfg, user, backend_kind=backend_kind,
                   geometry=(320, 240), persistent=False)


def _fake_proc(*, alive=True):
    """A stand-in subprocess.Popen for the display server."""
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    return proc


# --------------------------------------------------------------------------- #
# C2 — missing binary / readiness failures raise a typed, actionable error
# --------------------------------------------------------------------------- #
def test_start_x11_missing_xvfb_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: None)
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    s = _make_session("x11")
    with pytest.raises(DisplayServerError) as exc:
        s._start_x11()
    msg = str(exc.value)
    assert "Xvfb" in msg
    assert "install" in msg.lower()


def test_start_x11_not_ready_raises_displayserver_error(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/Xvfb")
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: False)
    monkeypatch.setattr(session, "_terminate", lambda *a, **k: None)
    s = _make_session("x11")
    s._spawn = lambda cmd, env: _fake_proc()  # spawn "succeeds" but never ready
    with pytest.raises(DisplayServerError) as exc:
        s._start_x11()
    assert "ready" in str(exc.value).lower() or "Xvfb" in str(exc.value)


def test_start_wayland_missing_compositor_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: None)
    s = _make_session("wayland")
    with pytest.raises(DisplayServerError) as exc:
        s._start_wayland()
    msg = str(exc.value)
    assert "sway" in msg
    assert "x11" in msg  # points the operator at the working x11 backend


def test_start_wayland_not_ready_raises_displayserver_error(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/sway")
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: False)
    monkeypatch.setattr(session, "_terminate", lambda *a, **k: None)
    s = _make_session("wayland")
    s._spawn = lambda cmd, env: _fake_proc()
    with pytest.raises(DisplayServerError) as exc:
        s._start_wayland()
    msg = str(exc.value).lower()
    assert "socket" in msg or "ready" in msg


# --------------------------------------------------------------------------- #
# C2 — success path: readiness confirmed before returning
# --------------------------------------------------------------------------- #
def test_start_x11_ready_succeeds(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/Xvfb")
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: True)
    s = _make_session("x11")
    s._spawn = lambda cmd, env: _fake_proc()
    env = s._start_x11()
    assert env["DISPLAY"] == s.display
    assert s.display.startswith(":")


def test_start_wayland_ready_succeeds(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/sway")
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: True)
    s = _make_session("wayland")
    s._spawn = lambda cmd, env: _fake_proc()
    env = s._start_wayland()
    assert env["WAYLAND_DISPLAY"] == s.wayland_display


# --------------------------------------------------------------------------- #
# C3 — TOCTOU: retry the next display number when Xvfb loses the race
# --------------------------------------------------------------------------- #
def test_start_x11_retries_on_race_then_succeeds(monkeypatch):
    """The first candidate's socket never appears (another session grabbed it);
    the second comes up. _spawn must be called twice and the first proc torn
    down via _terminate, then the second env returned."""
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/Xvfb")
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    # First candidate not ready, second ready.
    results = iter([False, True])
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: next(results))
    terminated = []
    monkeypatch.setattr(session, "_terminate",
                        lambda proc, **k: terminated.append(proc))
    spawns = []
    s = _make_session("x11")

    def _spawn(cmd, env):
        p = _fake_proc()
        spawns.append(p)
        return p

    s._spawn = _spawn
    env = s._start_x11()
    assert spawns, "Xvfb was never spawned"
    assert len(spawns) == 2, f"expected exactly 2 spawn attempts, got {len(spawns)}"
    assert len(terminated) == 1, "the losing first proc must be _terminate()'d"
    assert env["DISPLAY"] == s.display


def test_start_x11_all_candidates_lose_raises(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda _b: "/usr/bin/Xvfb")
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(session, "_wait_for_file", lambda *a, **k: False)
    monkeypatch.setattr(session, "_terminate", lambda *a, **k: None)
    # Force the candidate range small so the test doesn't loop 190 times.
    monkeypatch.setattr(session, "_free_display_number_candidates",
                        lambda start=10, end=200: iter([10, 11, 12]))
    s = _make_session("x11")
    s._spawn = lambda cmd, env: _fake_proc()
    with pytest.raises(DisplayServerError) as exc:
        s._start_x11()
    assert "free" in str(exc.value).lower() or "Xvfb" in str(exc.value)


# --------------------------------------------------------------------------- #
# Custom tests for Task 1, 3, 5, 6
# --------------------------------------------------------------------------- #
import os

def test_xdg_runtime_dir_fallback(monkeypatch, tmp_path):
    # If /run/user/<uid> is not writable, we should fall back to /tmp/rd-runtime-<uid>
    # To test this, let's mock os.access to return False for /run/user/
    original_access = os.access
    def mock_access(path, mode):
        if "/run/user" in str(path):
            return False
        return original_access(path, mode)
    
    monkeypatch.setattr(os, "access", mock_access)
    
    user = UserInfo("root")
    assert user.runtime_dir == f"/tmp/rd-runtime-{user.uid}"
    assert os.path.exists(user.runtime_dir)


def test_maybe_start_wm_autodetect_candidates(monkeypatch):
    s = _make_session("x11")
    spawned = []
    s._spawn = lambda cmd, env: spawned.append(cmd)
    s._wait_for_window_or_timeout = lambda: None
    
    # Mock shutil.which to say 'openbox' is present, but others are not.
    def mock_which(cmd):
        if cmd == "openbox":
            return "/usr/bin/openbox"
        if cmd in ("tint2", "xterm", "xsetroot"):
            return f"/usr/bin/{cmd}"
        return None
        
    monkeypatch.setattr(session.shutil, "which", mock_which)
    
    env = {"DISPLAY": ":1"}
    s._maybe_start_wm(env)
    
    # Since openbox is a bare WM, it should spawn openbox, tint2, xterm, and xsetroot
    assert ["openbox"] in spawned
    assert ["tint2"] in spawned
    assert ["xterm"] in spawned
    assert ["xsetroot", "-solid", "#2e3440"] in spawned


def test_maybe_start_wm_no_wm_warning(monkeypatch, caplog):
    import logging
    s = _make_session("x11")
    s._wait_for_window_or_timeout = lambda: None
    
    monkeypatch.setattr(session.shutil, "which", lambda _b: None)
    
    with caplog.at_level(logging.WARNING):
        s._maybe_start_wm({})
        
    assert "сессия пуста, будет чёрный экран, задайте window_manager в server.toml" in caplog.text


def test_backend_x11_raises_if_mss_not_available(monkeypatch):
    from server.backend import x11
    monkeypatch.setattr(x11, "_HAVE_MSS", False)
    
    # Creating an X11 backend or calling start should raise DisplayServerError
    # Let's mock create_backend or instantiate X11Backend directly
    from server.backend.x11 import X11Backend
    backend = X11Backend(env={}, geometry=(320, 240), cursor_mode="normal")
    
    with pytest.raises(DisplayServerError) as exc:
        backend._init_mss()
    assert "не установлен пакет python-xlib/mss — захват экрана невозможен" in str(exc.value)
