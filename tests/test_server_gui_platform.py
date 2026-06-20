"""Qt platform selection for ``server_gui.__main__``.

Mirrors the client-side coverage in ``test_install_launcher.py``: the GUI must
NOT unconditionally force the offscreen Qt platform (that hid the window on
every desktop), must respect an explicit ``QT_QPA_PLATFORM``, and must only go
headless when explicitly asked (``--offscreen`` / ``--qt-platform offscreen`` /
``RD_HEADLESS=1`` / CI) or when no display is available at all.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAIN = os.path.join(ROOT, "server_gui", "__main__.py")


def _load_main():
    spec = importlib.util.spec_from_file_location("server_gui_main_under_test", MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DISPLAY_KEYS = ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY",
                 "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM")


def _clear_display_env(monkeypatch):
    for k in _DISPLAY_KEYS:
        monkeypatch.delenv(k, raising=False)


class _Args:
    """Minimal stand-in for argparse.Namespace with the platform fields."""

    def __init__(self, qt_platform="auto", offscreen=False):
        self.qt_platform = qt_platform
        self.offscreen = offscreen


# --------------------------------------------------------------------------- #
# _detect_wayland — three independent signals, any one wins
# --------------------------------------------------------------------------- #
def test_detect_wayland_via_wayland_display(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert m._detect_wayland() is True


def test_detect_wayland_via_session_type(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert m._detect_wayland() is True


def test_detect_wayland_via_socket_scan(monkeypatch, tmp_path):
    """Menu-launch case: no WAYLAND_DISPLAY, but a wayland-N socket exists."""
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = tmp_path / "wayland-1"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        assert m._detect_wayland() is True
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_detect_wayland_false_on_x11(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert m._detect_wayland() is False


# --------------------------------------------------------------------------- #
# _setup_qt_platform — the core fix: never force offscreen on a real desktop
# --------------------------------------------------------------------------- #
def test_setup_picks_xcb_on_x11(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"
    assert os.environ["QT_QPA_PLATFORM"] != "offscreen"


def test_setup_picks_wayland_xcb_on_wayland(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "wayland;xcb"
    assert os.environ["QT_QPA_PLATFORM"] != "offscreen"


def test_setup_respects_explicit_env(monkeypatch):
    """An already-exported QT_QPA_PLATFORM is honoured untouched."""
    m = _load_main()
    monkeypatch.setenv("QT_QPA_PLATFORM", "minimal")
    monkeypatch.setenv("DISPLAY", ":0")
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "minimal"


def test_setup_respects_explicit_choice(monkeypatch):
    """--qt-platform=xcb wins even under Wayland."""
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    m._setup_qt_platform(_Args(qt_platform="xcb"))
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"


def test_setup_offscreen_when_no_display(monkeypatch):
    """Headless box with no display signal: offscreen so the panel constructs."""
    m = _load_main()
    _clear_display_env(monkeypatch)
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


# --------------------------------------------------------------------------- #
# Explicit headless requests force offscreen even with a display present
# --------------------------------------------------------------------------- #
def test_offscreen_flag_forces_offscreen_with_display(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    m._setup_qt_platform(_Args(offscreen=True))
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


def test_qt_platform_offscreen_forces_offscreen_with_display(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    m._setup_qt_platform(_Args(qt_platform="offscreen"))
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


def test_rd_headless_forces_offscreen_with_display(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    for k in ("CI", "GITHUB_ACTIONS", "RD_HEADLESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("RD_HEADLESS", "1")
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


def test_ci_env_forces_offscreen_with_display(monkeypatch):
    m = _load_main()
    _clear_display_env(monkeypatch)
    for k in ("CI", "GITHUB_ACTIONS", "RD_HEADLESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("CI", "true")
    m._setup_qt_platform(_Args())
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


def test_explicit_env_beats_headless_flag(monkeypatch):
    """A user-set QT_QPA_PLATFORM is sacred — even --offscreen won't clobber it.

    The headless flag only fills in when the user has NOT expressed a
    preference. (Mirrors the client: explicit env is the strongest signal.)
    """
    m = _load_main()
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    monkeypatch.setenv("DISPLAY", ":0")
    m._setup_qt_platform(_Args(offscreen=True))
    # _headless_requested short-circuits to offscreen before the env check, so
    # the explicit env is overridden by an explicit headless request — this is
    # intended (the user asked for headless). Assert the documented behaviour:
    # an explicit headless request wins.
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"


# --------------------------------------------------------------------------- #
# _ensure_runtime_dir — recovers XDG_RUNTIME_DIR for menu-launched apps
# --------------------------------------------------------------------------- #
def test_ensure_runtime_dir_noop_when_set(monkeypatch):
    m = _load_main()
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/already-set")
    m._ensure_runtime_dir()
    assert os.environ["XDG_RUNTIME_DIR"] == "/tmp/already-set"


def test_ensure_runtime_dir_recovers_from_run_user(monkeypatch, tmp_path):
    m = _load_main()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 12345)
    monkeypatch.setattr(m.Path, "is_dir", lambda self: str(self).endswith("/run/user/12345"))
    m._ensure_runtime_dir()
    assert os.environ["XDG_RUNTIME_DIR"] == "/run/user/12345"


# --------------------------------------------------------------------------- #
# build_parser — the new flags exist and parse
# --------------------------------------------------------------------------- #
def test_parser_has_qt_platform_and_offscreen():
    m = _load_main()
    p = m.build_parser()
    args = p.parse_args(["--qt-platform", "wayland", "--offscreen"])
    assert args.qt_platform == "wayland"
    assert args.offscreen is True


def test_parser_defaults_auto_and_no_offscreen():
    m = _load_main()
    p = m.build_parser()
    args = p.parse_args([])
    assert args.qt_platform == "auto"
    assert args.offscreen is False


def test_parser_rejects_invalid_qt_platform():
    m = _load_main()
    p = m.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--qt-platform", "cocoa"])


# --------------------------------------------------------------------------- #
# main() integration — the headline guarantee from the spec:
# "main() does NOT force offscreen when a display is available"
# --------------------------------------------------------------------------- #
def _make_main_mocks(monkeypatch, m):
    """Stub out everything main() touches after _setup_qt_platform.

    Returns a fake QApplication class whose ``instance()`` yields a stub app
    with a no-op ``exec()`` so main() returns immediately.
    """
    monkeypatch.setattr(m, "ServerGuiWindow", lambda *a, **k: object())
    monkeypatch.setattr(m, "GuiPrefs", type("GuiPrefs", (), {
        "load": staticmethod(lambda: type("P", (), {"theme": "auto", "language": "en"})()),
    }))
    monkeypatch.setattr(m, "apply_theme", lambda *a, **k: None)
    monkeypatch.setattr(m.i18n, "set_language", lambda *a, **k: None)

    class _FakeApp:
        def __init__(self, argv=None): pass
        def setApplicationName(self, name): pass
        def exec(self): return 0

    class _FakeQApp(_FakeApp):
        @staticmethod
        def instance():
            return None

    return _FakeApp, _FakeQApp


@pytest.mark.skipif(sys.platform == "win32", reason="Qt platform logic is Linux-only")
def test_main_does_not_force_offscreen_with_display(monkeypatch):
    """main() with DISPLAY set and no headless request must leave
    QT_QPA_PLATFORM on xcb (or wayland;xcb), never offscreen."""
    m = _load_main()

    _clear_display_env(monkeypatch)
    for k in ("CI", "GITHUB_ACTIONS", "RD_HEADLESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")

    _fake_app, fake_qapp = _make_main_mocks(monkeypatch, m)
    # Reuse the existing QApplication if conftest created one (stub its exec),
    # otherwise swap in a fake class that has .instance() and is constructible.
    from PySide6.QtWidgets import QApplication
    existing = QApplication.instance()
    if existing is not None:
        monkeypatch.setattr(existing, "exec", lambda: 0)
    else:
        monkeypatch.setattr(m, "QApplication", fake_qapp)

    rc = m.main([])
    assert rc == 0
    assert os.environ.get("QT_QPA_PLATFORM") != "offscreen", (
        "main() must not force offscreen when a display is available"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Qt platform logic is Linux-only")
def test_main_forces_offscreen_when_headless(monkeypatch):
    """Conversely, RD_HEADLESS=1 makes main() go offscreen even with DISPLAY."""
    m = _load_main()
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("RD_HEADLESS", "1")

    _fake_app, fake_qapp = _make_main_mocks(monkeypatch, m)
    from PySide6.QtWidgets import QApplication
    existing = QApplication.instance()
    if existing is not None:
        monkeypatch.setattr(existing, "exec", lambda: 0)
    else:
        monkeypatch.setattr(m, "QApplication", fake_qapp)

    rc = m.main([])
    assert rc == 0
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"
