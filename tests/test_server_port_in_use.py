"""Unit tests for the port-in-use error path in ``server``.

Covers the regression where ``rd-server`` launched on an already-bound port
(usually a leftover foreground process from a previous install) dumped a
multi-page asyncio/asyncssh traceback instead of an actionable message.

Tests:
- ``PortInUseError`` carries host/port/cause and stringifies usefully.
- ``Broker.start`` raises ``PortInUseError`` (not raw ``OSError``) when
  ``asyncssh.create_server`` fails to bind.
- ``server.__main__._port_in_use_hint`` prints the port and the commands to
  find/stop the holder.
- ``server.__main__.main`` returns 1 with the hint (no traceback) when the
  bind fails, both in foreground and daemon mode.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.broker import Broker, PortInUseError


# --------------------------------------------------------------------------- #
# PortInUseError
# --------------------------------------------------------------------------- #
def test_port_in_use_error_fields():
    cause = OSError(98, "Address already in use")
    err = PortInUseError("0.0.0.0", 2222, cause)
    assert err.host == "0.0.0.0"
    assert err.port == 2222
    assert err.original is cause
    # It's an OSError subclass so callers catching OSError still catch it.
    assert isinstance(err, OSError)
    assert "2222" in str(err)


def test_port_in_use_error_strmentions_port_and_errno():
    cause = OSError(98, "Address already in use")
    err = PortInUseError("0.0.0.0", 2222, cause)
    s = str(err)
    assert "0.0.0.0:2222" in s
    assert "errno 98" in s


# --------------------------------------------------------------------------- #
# Broker.start raises PortInUseError
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_broker_start_wraps_bind_failure(tmp_path, monkeypatch):
    """A bind failure from asyncssh must surface as PortInUseError."""
    import asyncssh

    from common.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=1,  # privileged port; create_server is mocked anyway
        host_key=str(tmp_path / "hk_ed25519"),
        files_enabled=False,
    )
    broker = Broker(cfg)

    async def _fake_create_server(*args, **kwargs):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(asyncssh, "create_server", _fake_create_server)

    with pytest.raises(PortInUseError) as ei:
        await broker.start()
    assert ei.value.port == cfg.port
    assert isinstance(ei.value.original, OSError)
    assert ei.value.original.errno == 98


# --------------------------------------------------------------------------- #
# _port_in_use_hint
# --------------------------------------------------------------------------- #
def test_port_in_use_hint_is_actionable():
    from server.__main__ import _port_in_use_hint

    cause = OSError(98, "Address already in use")
    err = PortInUseError("0.0.0.0", 2222, cause)
    msg = _port_in_use_hint(err)
    # Mentions the port, the likely cause, and the commands to fix it.
    assert "2222" in msg
    assert "already in use" in msg
    assert "pkill -f rd-server" in msg
    assert "ss -tlnp" in msg
    assert "rd-server --stop" in msg


# --------------------------------------------------------------------------- #
# main() returns 1 + hint (no traceback) when the bind fails
# --------------------------------------------------------------------------- #
def test_main_foreground_returns_1_with_hint_on_bind_failure(tmp_path, monkeypatch, capsys):
    """Foreground launch on a busy port must print the hint and exit 1 —
    no asyncio/asyncssh traceback dumped to stderr."""
    import asyncssh
    from server import __main__ as srv

    # Force create_server to always fail to bind.
    async def _fake_create_server(*args, **kwargs):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(asyncssh, "create_server", _fake_create_server)

    # No pidfile present, so the foreground "already running" guard is skipped
    # and we actually reach broker.start(), which raises PortInUseError.
    monkeypatch.setattr(srv, "default_pidfile", lambda: str(tmp_path / "none.pid"))

    # Keep logging quiet.
    monkeypatch.setattr(srv, "_configure_logging", lambda *a, **k: None)

    rc = srv.main(["--host", "127.0.0.1", "--port", "2222",
                   "--config", str(tmp_path / "empty.toml")])
    assert rc == 1
    out = capsys.readouterr()
    assert "already in use" in out.err
    assert "pkill -f rd-server" in out.err
    # No raw traceback leaked to stderr.
    assert "Traceback" not in out.err


def test_main_daemon_returns_1_with_hint_on_bind_failure(tmp_path, monkeypatch, capsys):
    """Daemon launch on a busy port must likewise exit 1 with the hint."""
    import asyncssh
    from server import __main__ as srv

    async def _fake_create_server(*args, **kwargs):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(asyncssh, "create_server", _fake_create_server)
    # Pretend no live daemon so the daemon path proceeds to bind.
    monkeypatch.setattr(srv, "live_pid_from_pidfile", lambda p: None)
    # Skip the real double-fork; just run the broker coroutine inline.
    monkeypatch.setattr(srv, "daemonize", lambda **k: None)
    monkeypatch.setattr(srv, "remove_pidfile", lambda p: None)
    monkeypatch.setattr(srv, "_configure_logging", lambda *a, **k: None)

    rc = srv.main(["--daemon", "--host", "127.0.0.1", "--port", "2222",
                   "--config", str(tmp_path / "empty.toml"),
                   "--pidfile", str(tmp_path / "d.pid")])
    assert rc == 1
    out = capsys.readouterr()
    assert "already in use" in out.err
    assert "rd-server --stop" in out.err
    assert "Traceback" not in out.err


# --------------------------------------------------------------------------- #
# Foreground "already running" guard
# --------------------------------------------------------------------------- #
def test_main_foreground_bails_when_pidfile_points_at_live_process(tmp_path, monkeypatch, capsys):
    """Foreground launch must refuse to start when a daemon pidfile points at a
    live process, instead of racing it for the port."""
    from server import __main__ as srv

    monkeypatch.setattr(srv, "default_pidfile", lambda: str(tmp_path / "alive.pid"))
    # Pretend a live daemon is running (current pid is always alive).
    monkeypatch.setattr(srv, "live_pid_from_pidfile", lambda p: os.getpid())
    monkeypatch.setattr(srv, "_configure_logging", lambda *a, **k: None)

    rc = srv.main(["--config", str(tmp_path / "empty.toml")])
    assert rc == 1
    out = capsys.readouterr()
    assert "already running" in out.err
    assert "--stop" in out.err
