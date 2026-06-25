"""Tests for A4: the GUI status must reflect actual health, not just a
live pidfile.

A ``running`` pidfile only proves the process exists — not that logins work.
Without root the listener comes up but PAM/setuid silently fail, and a
half-started broker may not even be listening. The status label must surface
both cases instead of the old misleading "Запущен".
"""
from __future__ import annotations

import socket
import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="server_gui is Linux-only (Qt platform / daemon logic)",
)


# --------------------------------------------------------------------------- #
# port_listening (controller) — the health-check primitive
# --------------------------------------------------------------------------- #
def test_port_listening_true_when_something_accepts():
    from server_gui.controller import port_listening

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert port_listening("127.0.0.1", port, timeout=1.0) is True
        # 0.0.0.0 wildcard must still reach the loopback listener.
        assert port_listening("0.0.0.0", port, timeout=1.0) is True
    finally:
        srv.close()


def test_port_listening_false_when_closed():
    from server_gui.controller import port_listening

    # Bind then immediately close: the port is almost certainly free again.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    # A closed port never accepts a connection.
    assert port_listening("127.0.0.1", port, timeout=0.3) is False


# --------------------------------------------------------------------------- #
# _refresh_status — the label stops lying when running is not healthy
# --------------------------------------------------------------------------- #
def _make_window(qapp, tmp_path):
    from server_gui.__main__ import ServerGuiWindow
    from server_gui.controller import GuiPrefs

    prefs = GuiPrefs.load(str(tmp_path / "prefs.json"))
    return ServerGuiWindow(
        str(tmp_path / "server.toml"), prefs, use_tray=False,
    )


def _running_state():
    from server_gui.controller import ServerState
    return ServerState(state="running", pid=4242, port=2222,
                       host="127.0.0.1", managed_by="daemon")


def test_status_shows_port_not_listening_when_no_listener(qapp, tmp_path, monkeypatch):
    win = _make_window(qapp, tmp_path)
    # A running daemon whose pidfile is live…
    win._svc = MagicMock()
    win._svc.state.return_value = _running_state()
    # …but nothing is actually listening on the configured port.
    monkeypatch.setattr("server_gui.__main__.port_listening",
                        lambda host, port, timeout=0.4: False)
    # privilege_warning must be None here so the port branch is the only cause.
    monkeypatch.setattr("server_gui.__main__.privilege_warning", lambda cfg: None)

    win._refresh_status()

    assert "порт" in win.lbl_state.text() and "слушается" in win.lbl_state.text()


def test_status_shows_limited_when_running_without_root(qapp, tmp_path, monkeypatch):
    win = _make_window(qapp, tmp_path)
    win._svc = MagicMock()
    win._svc.state.return_value = _running_state()
    monkeypatch.setattr("server_gui.__main__.port_listening",
                        lambda host, port, timeout=0.4: True)
    # allow_password/run_as_user need root but we are unprivileged.
    monkeypatch.setattr("server_gui.__main__.privilege_warning",
                        lambda cfg: "needs root")

    win._refresh_status()

    assert "ограниченно" in win.lbl_state.text() or "root" in win.lbl_state.text()
    # The yellow banner must be visible.
    assert win.lbl_priv_warn.isVisible()
    assert win.lbl_priv_warn.text() == "needs root"


def test_status_shows_running_when_healthy(qapp, tmp_path, monkeypatch):
    win = _make_window(qapp, tmp_path)
    win._svc = MagicMock()
    win._svc.state.return_value = _running_state()
    monkeypatch.setattr("server_gui.__main__.port_listening",
                        lambda host, port, timeout=0.4: True)
    monkeypatch.setattr("server_gui.__main__.privilege_warning", lambda cfg: None)

    win._refresh_status()

    # Plain "Запущен" with no caveat suffix.
    assert win.lbl_state.text() == "Запущен"
