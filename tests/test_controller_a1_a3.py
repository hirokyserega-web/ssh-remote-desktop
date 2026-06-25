"""Tests for ЗАДАЧА A1 (GUI daemon forwards the whole config) and ЗАДАЧА A3
(orphan processes / port change / honest start-stop-restart).

Covers ``DaemonController._build_foreground_cmd`` (every ServerGuiConfig field
that the server honours is forwarded, incl. bitrate + the three auth toggles +
--config), and the start/stop/restart lifecycle:
  * start() refuses when a live rd-server is already running and does NOT
    delete its pidfile (the orphan-on-port-change bug),
  * start() reports an actionable "port busy" error before a mute failure,
  * restart() guarantees the old process is gone before starting a new one.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server_gui.controller import (
    DaemonController, ServerGuiConfig,
)


# --------------------------------------------------------------------------- #
# A1 — _build_foreground_cmd forwards every relevant field
# --------------------------------------------------------------------------- #
def test_build_foreground_cmd_forwards_all_fields(tmp_path):
    cfg = ServerGuiConfig(
        host="127.0.0.1", port=2224, backend="x11", max_sessions=7,
        idle_timeout=120, codec="h265", fps=24, bitrate_kbps=4500,
        shared_dir="/srv/share", allow_password=False, allow_publickey=True,
        run_as_user=False, log_level="DEBUG", log_file="/tmp/rd.log",
    )
    c = DaemonController(
        cfg, binary="/usr/bin/rd-server",
        pidfile=str(tmp_path / "run.pid"),
        config_path=str(tmp_path / "server.toml"),
    )
    cmd = c._build_foreground_cmd("/tmp/rd.log")
    joined = " ".join(cmd)

    # --config is passed so file-only knobs (host_key/pam_service) come from
    # the file, while the CLI flags below override the form-controlled ones.
    assert "--config" in cmd
    assert str(tmp_path / "server.toml") in cmd
    assert "--foreground" in cmd and "--pidfile" in cmd
    # Network / limits / encoding
    for flag, val in (("--host", "127.0.0.1"), ("--port", "2224"),
                      ("--backend", "x11"), ("--codec", "h265"),
                      ("--max-sessions", "7"), ("--idle-timeout", "120"),
                      ("--shared-dir", "/srv/share"), ("--fps", "24"),
                      ("--bitrate-kbps", "4500"), ("--log-level", "DEBUG")):
        assert flag in cmd, f"missing {flag}"
        assert val in joined, f"missing value for {flag}: {val}"
    # Auth / privilege toggles — these MUST reach the daemon or it starts with
    # root-requiring defaults and silently rejects every login.
    assert "--no-allow-password" in cmd   # allow_password=False
    assert "--allow-publickey" in cmd     # allow_publickey=True
    assert "--no-run-as-user" in cmd      # run_as_user=False
    # --log-file must NOT be passed: the controller redirects stderr itself.
    assert "--log-file" not in cmd


def test_build_foreground_cmd_without_config_path_omits_config(tmp_path):
    c = DaemonController(ServerGuiConfig(), binary="/usr/bin/rd-server",
                         pidfile=str(tmp_path / "run.pid"))
    cmd = c._build_foreground_cmd("/tmp/rd.log")
    assert "--config" not in cmd
    # Auth toggles are still forwarded from the form defaults.
    assert "--allow-password" in cmd or "--no-allow-password" in cmd


def test_build_foreground_cmd_round_trips_through_saved_config(tmp_path):
    """The flags the controller emits must be accepted by rd-server's parser
    (so a Start actually launches instead of argparse rejecting an unknown
    flag). Round-trip the cfg through the real parser."""
    from server.__main__ import build_parser
    cfg = ServerGuiConfig(port=2225, bitrate_kbps=3000, allow_password=False,
                          run_as_user=False, codec="jpeg", shared_dir="shared")
    c = DaemonController(cfg, binary="rd-server",
                         pidfile=str(tmp_path / "run.pid"))
    cmd = c._build_foreground_cmd("/tmp/rd.log")
    # Drop the binary + --foreground/--pidfile (the parser knows those); keep
    # the rest and confirm argparse accepts every flag the GUI emits.
    parser = build_parser()
    # The parser rejects --config pointing at a missing file only at load time,
    # not parse time, so this is a pure CLI-shape check.
    parser.parse_args([a for a in cmd if a not in ("rd-server",)])


# --------------------------------------------------------------------------- #
# A3 — start() refuses a second instance and keeps the live pidfile
# --------------------------------------------------------------------------- #
def _write_stub_server(path: str) -> None:
    """A stub rd-server that writes the pidfile then sleeps (keeps the
    'listener' up so live_pid_from_pidfile sees a live process)."""
    code = "#!" + sys.executable + "\n" + textwrap.dedent(
        """
        import json, os, sys, time
        pidfile = None
        for i, a in enumerate(sys.argv):
            if a == "--pidfile" and i + 1 < len(sys.argv):
                pidfile = sys.argv[i + 1]
        if pidfile:
            os.makedirs(os.path.dirname(os.path.abspath(pidfile)) or ".", exist_ok=True)
            with open(pidfile, "w") as f:
                json.dump({"pid": os.getpid(), "port": 2224, "host": "0.0.0.0"}, f)
        time.sleep(60)
        """)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    os.chmod(path, 0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stub + start_new_session")
def test_start_refuses_when_already_running_and_keeps_pidfile(tmp_path):
    stub = str(tmp_path / "rd-server")
    _write_stub_server(stub)
    pidfile = str(tmp_path / "run.pid")

    # Spawn the stub directly so a live rd-server is "already running" with a
    # real pidfile pointing at a live process.
    import subprocess as sp
    proc = sp.Popen([stub, "--foreground", "--pidfile", pidfile],
                    stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    try:
        # Wait for the stub to write its pidfile.
        import time as _t
        for _ in range(50):
            if os.path.exists(pidfile):
                break
            _t.sleep(0.1)
        assert os.path.exists(pidfile)

        cfg = ServerGuiConfig(port=2224, log_file=str(tmp_path / "rd.log"))
        c = DaemonController(cfg, binary=stub, pidfile=pidfile)
        ok = c.start()
        assert ok is False
        # The error must say "already running" and name the pid — NOT a mute
        # "Остановлен", and NOT a stale-pidfile deletion that orphans the stub.
        assert c.last_error is not None
        assert "уже запущен" in c.last_error
        assert str(proc.pid) in c.last_error
        # Critical: the live pidfile is untouched, so `rd-server --stop` still
        # works (the A3 orphan-on-port-change bug deleted it).
        assert os.path.exists(pidfile)
        data = json.loads(open(pidfile).read())
        assert data["pid"] == proc.pid
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bind check")
def test_start_port_busy_reports_actionable_error(tmp_path, monkeypatch):
    """C/A3: a busy port must surface an actionable error, not a mute failure.

    Mocks _port_bindable (the actual bind semantics on 0.0.0.0 vs a holder on
    127.0.0.1 are OS-dependent) so the test exercises the error-message logic
    deterministically."""
    from server_gui import controller as c
    cfg = ServerGuiConfig()
    cfg.log_file = str(tmp_path / "rd.log")
    # Force the port-busy branch without holding a real socket.
    monkeypatch.setattr(c, "_port_bindable", lambda host, port: False)
    monkeypatch.setattr(c, "_port_listener_pid", lambda port: 4242)
    ctl = c.DaemonController(cfg, binary="/bin/true",
                             pidfile=str(tmp_path / "run.pid"))
    ok = ctl.start()
    assert ok is False
    assert ctl.last_error is not None
    assert "порт" in ctl.last_error and "занят" in ctl.last_error
    assert "4242" in ctl.last_error  # the holder PID is surfaced


def test_start_missing_binary_reports_install_hint(tmp_path):
    cfg = ServerGuiConfig(port=2224, log_file=str(tmp_path / "rd.log"))
    c = DaemonController(cfg, binary=str(tmp_path / "no-such-binary"),
                         pidfile=str(tmp_path / "run.pid"))
    assert c.start() is False
    assert c.last_error is not None
    assert "not found" in c.last_error.lower() or "не найден" in c.last_error.lower() \
        or "no such" in c.last_error.lower()


# --------------------------------------------------------------------------- #
# A3 — restart() stops the old process before starting a new one
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stub + start_new_session")
def test_restart_stops_old_then_starts_new(tmp_path):
    stub = str(tmp_path / "rd-server")
    _write_stub_server(stub)
    pidfile = str(tmp_path / "run.pid")
    cfg = ServerGuiConfig(host="127.0.0.1", port=2224, log_file=str(tmp_path / "rd.log"))

    # Start a first instance.
    c = DaemonController(cfg, binary=stub, pidfile=pidfile)
    import time as _t
    assert c.start() is True
    old_pidfile = json.loads(open(pidfile).read())
    old_pid = old_pidfile["pid"]
    assert old_pid in (p.pid for p in [c] if False) or isinstance(old_pid, int)

    try:
        # restart() must stop the old process and start a fresh one.
        ok = c.restart()
        assert ok is True
        # The old process is gone.
        _t.sleep(0.2)
        from server.daemon import is_pid_alive
        assert not is_pid_alive(old_pid), "restart() left the old process alive"
        # A new pidfile exists with a different (new) pid.
        assert os.path.exists(pidfile)
        new_pid = json.loads(open(pidfile).read())["pid"]
        assert new_pid != old_pid
        assert is_pid_alive(new_pid)
    finally:
        # Clean up the new instance.
        c.stop()
        _t.sleep(0.2)


# --------------------------------------------------------------------------- #
# A3 — _port_listener_pid parses ss/lsof output (mocked)
# --------------------------------------------------------------------------- #
def test_port_listener_pid_parses_ss(monkeypatch):
    from server_gui import controller as c
    fake = c.subprocess.CompletedProcess([], 0,
        "State ... 0.0.0.0:2224 ... users:((\"rd-server\",pid=4242,fd=3))", "")
    monkeypatch.setattr(c, "_run", lambda cmd, timeout=5.0: fake)
    assert c._port_listener_pid(2224) == 4242


def test_port_listener_pid_parses_lsof_fallback(monkeypatch):
    from server_gui import controller as c
    # ss returns nothing usable, lsof returns a pid line.
    ss_empty = c.subprocess.CompletedProcess([], 0, "", "")
    lsof = c.subprocess.CompletedProcess([], 0, "4242\n", "")
    calls = {"i": 0}
    def fake_run(cmd, timeout=5.0):
        calls["i"] += 1
        return ss_empty if "ss" in cmd else lsof
    monkeypatch.setattr(c, "_run", fake_run)
    assert c._port_listener_pid(2224) == 4242


def test_port_listener_pid_none_when_unknown(monkeypatch):
    from server_gui import controller as c
    empty = c.subprocess.CompletedProcess([], 1, "", "")
    monkeypatch.setattr(c, "_run", lambda cmd, timeout=5.0: empty)
    assert c._port_listener_pid(2224) is None
