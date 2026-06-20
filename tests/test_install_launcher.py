"""Tests for the rd-launch session-env wrapper and its install.sh heredoc sync.

Covers the "click the menu entry and nothing happens on Wayland" fix:
  - rd-launch reconstructs WAYLAND_DISPLAY / XDG_RUNTIME_DIR for menu-launched
    processes (which often lack them under D-Bus / systemd --user activation).
  - the inline heredoc embedded in scripts/install.sh stays in sync (in
    executable logic) with the canonical scripts/rd-launch.sh, so curl|bash
    binary installs get the same wrapper as source installs.
  - client/__main__._detect_wayland / _setup_qt_platform pick the right Qt
    platform from multiple independent signals, not just WAYLAND_DISPLAY.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "rd-launch.sh"
INSTALL = REPO / "scripts" / "install.sh"


def _executable_logic(text: str) -> str:
    """Strip comments + blank lines so prose drift doesn't trip the sync check."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(line.rstrip())
    return "\n".join(out)


def test_install_sh_heredoc_matches_rd_launch():
    """The inline heredoc in install.sh must match scripts/rd-launch.sh in logic."""
    install = INSTALL.read_text()
    m = re.search(r"<<'RDLAUNCH'\n(.*?)\nRDLAUNCH\b", install, re.S)
    assert m, "rd-launch heredoc not found in scripts/install.sh"
    heredoc = m.group(1)
    ref = LAUNCHER.read_text()
    assert _executable_logic(heredoc) == _executable_logic(ref), (
        "rd-launch heredoc in install.sh drifted from scripts/rd-launch.sh — "
        "update both so curl|bash installs ship the same wrapper."
    )


def test_rd_launch_recovers_wayland_display_from_socket(tmp_path):
    """Menu-launched env lacks WAYLAND_DISPLAY; rd-launch must rebuild it."""
    if not LAUNCHER.exists():
        import pytest
        pytest.skip("scripts/rd-launch.sh not present")
    # Create a real AF_UNIX socket named wayland-0 in the runtime dir so the
    # wrapper's `[[ -S "$cand" ]]` test passes.
    sock_path = tmp_path / "wayland-0"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        # A tiny binary that prints the env var we care about, then exits 0.
        probe = tmp_path / "probe.sh"
        probe.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'WAYLAND_DISPLAY=%s\\n' \"${WAYLAND_DISPLAY:-<unset>}\"\n"
            "printf 'XDG_RUNTIME_DIR=%s\\n' \"${XDG_RUNTIME_DIR:-<unset>}\"\n"
        )
        probe.chmod(0o755)
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        env["XDG_RUNTIME_DIR"] = str(tmp_path)
        result = subprocess.run(
            ["bash", str(LAUNCHER), str(probe)],
            env=env, capture_output=True, text=True, check=True,
        )
        assert "WAYLAND_DISPLAY=wayland-0" in result.stdout
        assert f"XDG_RUNTIME_DIR={tmp_path}" in result.stdout
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_rd_launch_missing_binary_arg_fails():
    """rd-launch with no binary argument must exit non-zero, not hang."""
    result = subprocess.run(
        ["bash", str(LAUNCHER)],
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "missing binary" in result.stderr.lower()


# ---- client/__main__ Wayland detection -------------------------------------

def _load_main():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rd_main_under_test", REPO / "client" / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_detect_wayland_via_wayland_display(monkeypatch):
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert m._detect_wayland() is True


def test_detect_wayland_via_session_type(monkeypatch):
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert m._detect_wayland() is True


def test_detect_wayland_via_socket_scan(monkeypatch, tmp_path):
    """The menu-launch case: no WAYLAND_DISPLAY, but a wayland-N socket exists."""
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
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
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert m._detect_wayland() is False


def test_setup_qt_platform_picks_xcb_on_x11(monkeypatch):
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    class Cfg:
        qt_platform = "auto"
    m._setup_qt_platform(Cfg())
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"


def test_setup_qt_platform_picks_wayland_xcb_on_wayland(monkeypatch):
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    class Cfg:
        qt_platform = "auto"
    m._setup_qt_platform(Cfg())
    assert os.environ["QT_QPA_PLATFORM"] == "wayland;xcb"


def test_setup_qt_platform_respects_explicit_env(monkeypatch):
    m = _load_main()
    monkeypatch.setenv("QT_QPA_PLATFORM", "minimal")
    class Cfg:
        qt_platform = "auto"
    m._setup_qt_platform(Cfg())
    assert os.environ["QT_QPA_PLATFORM"] == "minimal"


def test_setup_qt_platform_respects_explicit_choice(monkeypatch):
    m = _load_main()
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    class Cfg:
        qt_platform = "xcb"
    m._setup_qt_platform(Cfg())
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"
