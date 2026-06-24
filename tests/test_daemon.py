"""Unit tests for ``server.daemon`` (pidfile, status, stop — stdlib only).

These exercise the pure-Python pidfile/stop/status logic without spinning up
a real SSH broker, which is exactly what the module was split out for.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import daemon


# --------------------------------------------------------------------------- #
# default_pidfile
# --------------------------------------------------------------------------- #
def test_default_pidfile_root_uses_run(monkeypatch):
    # Force the root code path regardless of the real euid, so the test is
    # deterministic on CI runners (euid != 0) as well as under root. Use
    # raising=False because os.geteuid does not exist on Windows; monkeypatch
    # then injects it so default_pidfile() exercises the root branch there too.
    monkeypatch.setattr(daemon.os, "geteuid", lambda: 0, raising=False)
    assert daemon.default_pidfile() == "/run/ssh-remote-desktop.pid"


def test_default_pidfile_non_root_uses_xdg(monkeypatch):
    monkeypatch.setattr(daemon.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(daemon.os.path, "expanduser",
                        lambda p: "/home/u" + p[1:] if p.startswith("~") else p)
    assert daemon.default_pidfile() == \
        "/home/u/.config/ssh-remote-desktop/rd-server.pid"


# --------------------------------------------------------------------------- #
# is_pid_alive
# --------------------------------------------------------------------------- #
def test_is_pid_alive_current_process():
    assert daemon.is_pid_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="PID 1 (init) is Unix-only")
def test_is_pid_alive_init():
    # PID 1 (supervisor/init) is always alive in a container.
    assert daemon.is_pid_alive(1) is True


def test_is_pid_alive_dead():
    assert daemon.is_pid_alive(999_999) is False


def test_is_pid_alive_nonpositive():
    assert daemon.is_pid_alive(0) is False
    assert daemon.is_pid_alive(-1) is False


# --------------------------------------------------------------------------- #
# read_pidfile / write_pidfile
# --------------------------------------------------------------------------- #
def test_read_pidfile_missing(tmp_path):
    assert daemon.read_pidfile(str(tmp_path / "nope.pid")) is None


def test_read_pidfile_malformed(tmp_path):
    p = tmp_path / "bad.pid"
    p.write_text("not json at all", encoding="utf-8")
    assert daemon.read_pidfile(str(p)) is None


def test_read_pidfile_no_pid_field(tmp_path):
    p = tmp_path / "nopid.pid"
    p.write_text(json.dumps({"port": 2222}), encoding="utf-8")
    assert daemon.read_pidfile(str(p)) is None


def test_write_then_read_pidfile(tmp_path):
    p = str(tmp_path / "srv.pid")
    daemon.write_pidfile(p, os.getpid(), port=2222, host="0.0.0.0")
    data = daemon.read_pidfile(p)
    assert data == {"pid": os.getpid(), "port": 2222, "host": "0.0.0.0"}


def test_write_pidfile_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "srv.pid")
    daemon.write_pidfile(p, 4242, port=1, host="h")
    # The .tmp.<pid> intermediate must be gone after os.replace.
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith("srv.pid.tmp")]
    assert leftovers == []


def test_write_pidfile_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "deep" / "nest" / "srv.pid")
    daemon.write_pidfile(p, 7, port=9, host="")
    assert os.path.exists(p)


# --------------------------------------------------------------------------- #
# live_pid_from_pidfile
# --------------------------------------------------------------------------- #
def test_live_pid_returns_alive(tmp_path):
    p = str(tmp_path / "alive.pid")
    daemon.write_pidfile(p, os.getpid())
    assert daemon.live_pid_from_pidfile(p) == os.getpid()


def test_live_pid_removes_stale(tmp_path):
    p = str(tmp_path / "stale.pid")
    daemon.write_pidfile(p, 999_999)  # dead pid
    assert daemon.live_pid_from_pidfile(p) is None
    assert not os.path.exists(p)  # stale pidfile cleaned up


# --------------------------------------------------------------------------- #
# remove_pidfile
# --------------------------------------------------------------------------- #
def test_remove_pidfile_idempotent(tmp_path):
    p = str(tmp_path / "gone.pid")
    # Removing a non-existent file must not raise.
    daemon.remove_pidfile(p)
    daemon.write_pidfile(p, 1)
    daemon.remove_pidfile(p)
    assert not os.path.exists(p)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def test_status_stopped_when_no_pidfile(tmp_path):
    s = daemon.status(str(tmp_path / "none.pid"))
    assert s.state == "stopped"
    assert s.pid is None
    assert s.port is None


def test_status_running_when_alive(tmp_path):
    p = str(tmp_path / "run.pid")
    daemon.write_pidfile(p, os.getpid(), port=2222, host="0.0.0.0")
    s = daemon.status(p)
    assert s.state == "running"
    assert s.pid == os.getpid()
    assert s.port == 2222
    assert s.host == "0.0.0.0"


def test_status_stopped_when_dead_pid_removes_file(tmp_path):
    p = str(tmp_path / "dead.pid")
    daemon.write_pidfile(p, 999_999, port=5)
    s = daemon.status(p)
    assert s.state == "stopped"
    assert not os.path.exists(p)


def test_daemon_status_as_lines():
    s = daemon.DaemonStatus(state="running", pid=12, port=2222, host="0.0.0.0")
    lines = s.as_lines()
    assert "state: running" in lines
    assert "pid: 12" in lines
    assert "port: 2222" in lines
    assert "host: 0.0.0.0" in lines


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #
def test_stop_nothing_to_stop(tmp_path):
    assert daemon.stop(str(tmp_path / "none.pid")) is False


def test_stop_stale_pidfile_returns_false_and_cleans(tmp_path):
    p = str(tmp_path / "stale.pid")
    daemon.write_pidfile(p, 999_999)
    assert daemon.stop(p) is False
    assert not os.path.exists(p)


def test_stop_live_process(tmp_path):
    """Spawn a real sleep subprocess, record its pid, stop it via the helper.

    A reaper thread calls ``proc.wait()`` so the child is reaped as soon as
    SIGTERM kills it — otherwise the un-reaped zombie keeps ``is_pid_alive``
    True and ``stop()`` would wrongly time out. (In production the daemon is
    reparented to init by the double-fork, so init reaps it immediately.)
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    try:
        p = str(tmp_path / "real.pid")
        daemon.write_pidfile(p, proc.pid, port=0, host="")
        time.sleep(0.2)
        assert daemon.stop(p, timeout=5.0, poll=0.05) is True
        assert not os.path.exists(p)  # pidfile cleaned on successful stop
        reaper.join(timeout=5)
        assert daemon.is_pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------- #
# daemonize — exercised via the test escape hatch (no real fork in pytest)
# --------------------------------------------------------------------------- #
def test_daemonize_nofork_escape_hatch(tmp_path, monkeypatch):
    monkeypatch.setenv("RD_SERVER_DAEMON_TEST_NOFORK", "1")
    log = str(tmp_path / "daemon.log")
    # Save the real stdio fds so rebinding them inside daemonize() doesn't
    # corrupt pytest's capture for the rest of the session.
    saved = (os.dup(0), os.dup(1), os.dup(2))
    try:
        # Escape-hatch path: no fork, but stdio IS rebound to the log file.
        daemon.daemonize(log)
        # Write to fd 1 (now the log file) directly — Python's sys.stdout is
        # pytest's capture object, so print() wouldn't reach the file.
        os.write(1, b"daemon-test-marker\n")
        os.fsync(1)
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        os.dup2(saved[2], 2)
        for fd in saved:
            os.close(fd)
    assert "daemon-test-marker" in (tmp_path / "daemon.log").read_text(
        encoding="utf-8")


# --------------------------------------------------------------------------- #
# is_frozen_onefile / default_log_file
# --------------------------------------------------------------------------- #
def test_is_frozen_onefile_false_in_dev():
    # Running under a plain CPython interpreter (pytest) is NOT frozen.
    assert daemon.is_frozen_onefile() is False


def test_default_log_file_next_to_pidfile():
    # Non-root: log lives in the XDG config dir alongside the pidfile.
    import os as _os
    if hasattr(_os, "geteuid") and _os.geteuid() == 0:
        assert daemon.default_log_file() == "/var/log/ssh-remote-desktop/rd-server.log"
    else:
        assert daemon.default_log_file().endswith(
            ".config/ssh-remote-desktop/rd-server.log")


# --------------------------------------------------------------------------- #
# is_likely_rd_server — stale-pidfile / PID-reuse hardening
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform == "win32", reason="reused-PID check is Linux/proc")
def test_is_likely_rd_server_rejects_unrelated_process():
    """A live but unrelated process (e.g. `sleep`) must NOT be mistaken for
    rd-server — that's the PID-reuse case a stale pidfile points at."""
    import subprocess as _sp
    proc = _sp.Popen(["sleep", "30"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    try:
        # Give the kernel a moment to populate /proc/<pid>/comm.
        time.sleep(0.1)
        assert daemon.is_likely_rd_server(proc.pid) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_live_pid_removes_reused_pid(tmp_path):
    """A pidfile whose pid is alive but is NOT rd-server is stale: remove it
    and report not-running, instead of refusing to start with 'already
    running'."""
    import subprocess as _sp
    # `sleep` is neither rd-server nor a python interpreter → treated as a
    # recycled PID. (Skip on Windows: no /proc, no `sleep`.)
    if sys.platform == "win32":
        pytest.skip("Linux/proc only")
    proc = _sp.Popen(["sleep", "30"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    try:
        time.sleep(0.1)
        p = str(tmp_path / "reused.pid")
        daemon.write_pidfile(p, proc.pid, port=2222, host="0.0.0.0")
        assert daemon.live_pid_from_pidfile(p) is None
        assert not os.path.exists(p)  # stale pidfile cleaned up
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_status_stopped_when_reused_pid_removes_file(tmp_path):
    """status() must clear a pidfile pointing at a recycled (non-rd-server)
    pid and report stopped, mirroring live_pid_from_pidfile."""
    if sys.platform == "win32":
        pytest.skip("Linux/proc only")
    import subprocess as _sp
    proc = _sp.Popen(["sleep", "30"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    try:
        time.sleep(0.1)
        p = str(tmp_path / "reused2.pid")
        daemon.write_pidfile(p, proc.pid, port=5, host="h")
        s = daemon.status(p)
        assert s.state == "stopped"
        assert not os.path.exists(p)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
