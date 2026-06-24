"""Integration tests for the frozen-onefile daemonization path (ЗАДАЧА 1).

``server.__main__._daemonize_onefile`` replaces the unsafe in-process
double-fork under a Nuitka onefile with a "re-launch a foreground child"
strategy. These tests exercise that strategy end-to-end with a stub binary
(so we don't need a real frozen build): the spawner must start the child in a
new session, redirect its output to a log file, wait for the child to write
the pidfile (success) OR detect an early exit and surface its stderr (failure),
and always clean up the pidfile on failure.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server.__main__ import _daemonize_onefile, _foreground_argv, build_parser
from common.config import load_server_config


def _write_stub_exe(path: str, *, fail: bool) -> None:
    """A stub that mimics a frozen `rd-server --foreground --pidfile P`.

    It writes the pidfile (so the spawner's grace window sees it) then sleeps,
    OR — in fail mode — prints a diagnostic to stderr and exits 1 immediately.
    """
    fail_flag = "True" if fail else "False"
    code = "#!" + sys.executable + "\n" + textwrap.dedent(
        """
        import json, os, sys, time
        pidfile = None
        for i, a in enumerate(sys.argv):
            if a == "--pidfile" and i + 1 < len(sys.argv):
                pidfile = sys.argv[i + 1]
        if __FAIL__:
            sys.stderr.write("STUB: ModuleNotFoundError: No module named 'asyncssh'\\n")
            sys.stderr.flush()
            sys.exit(1)
        if pidfile:
            os.makedirs(os.path.dirname(os.path.abspath(pidfile)) or ".", exist_ok=True)
            with open(pidfile, "w") as f:
                json.dump({"pid": os.getpid(), "port": 22999, "host": "0.0.0.0"}, f)
        time.sleep(30)
        """
    ).replace("__FAIL__", fail_flag)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    os.chmod(path, 0o755)


def _make_args(pidfile: str, log_file: str) -> tuple[object, object]:
    args = build_parser().parse_args(
        ["--daemon", "--port", "22999", "--pidfile", pidfile,
         "--host-key", "/tmp/onefile-test-hostkey", "--log-level", "DEBUG",
         "--log-file", log_file]
    )
    overrides = {k: v for k, v in vars(args).items()
                 if k not in {"config", "daemon", "foreground", "pidfile",
                              "stop", "status", "install_service",
                              "uninstall_service", "enable_service",
                              "disable_service"} and v is not None}
    cfg = load_server_config(None, overrides)
    return args, cfg


def test_foreground_argv_rebuilds_without_daemon_flag():
    args = build_parser().parse_args(
        ["--daemon", "--port", "22999", "--pidfile", "/tmp/x.pid",
         "--host", "1.2.3.4", "--backend", "x11"]
    )
    cmd = _foreground_argv(args, pidfile="/tmp/x.pid")
    assert "--foreground" in cmd
    assert "--daemon" not in cmd
    assert "--pidfile" in cmd and "/tmp/x.pid" in cmd
    assert "--port" in cmd and "22999" in cmd
    assert "--host" in cmd and "1.2.3.4" in cmd
    # --log-file must NOT be forwarded: the spawner redirects stderr itself.
    assert "--log-file" not in cmd


def test_daemonize_onefile_success(tmp_path, monkeypatch, capsys):
    stub = str(tmp_path / "rd-server")
    _write_stub_exe(stub, fail=False)
    pidfile = str(tmp_path / "run.pid")
    log_file = str(tmp_path / "rd.log")
    args, cfg = _make_args(pidfile, log_file)
    # Point the spawner at our stub instead of sys.argv[0].
    monkeypatch.setattr("server.__main__._onefile_executable", lambda: stub)

    rc = _daemonize_onefile(args, cfg, pidfile)

    assert rc == 0
    assert os.path.exists(pidfile)
    data = json.loads(open(pidfile).read())
    assert isinstance(data["pid"], int)
    # Clean up the stub child.
    import signal
    try:
        os.kill(data["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass


def test_daemonize_onefile_early_exit_surfaces_stderr(tmp_path, monkeypatch, capsys):
    stub = str(tmp_path / "rd-server")
    _write_stub_exe(stub, fail=True)
    pidfile = str(tmp_path / "run.pid")
    log_file = str(tmp_path / "rd.log")
    args, cfg = _make_args(pidfile, log_file)
    monkeypatch.setattr("server.__main__._onefile_executable", lambda: stub)

    rc = _daemonize_onefile(args, cfg, pidfile)

    assert rc == 1
    # The early-exit cause (the child's stderr, captured into the log file)
    # must be surfaced to the spawner's stderr — NOT silent success.
    captured = capsys.readouterr()
    assert "asyncssh" in captured.err or "exited early" in captured.err
    # A failed daemonization must never leave a stale pidfile behind.
    assert not os.path.exists(pidfile)


def test_daemonize_onefile_missing_log_dir_reports_error(tmp_path, monkeypatch):
    stub = str(tmp_path / "rd-server")
    _write_stub_exe(stub, fail=False)
    pidfile = str(tmp_path / "run.pid")
    # An unwritable log dir forces the spawner to fail before spawning.
    args = build_parser().parse_args(["--daemon", "--pidfile", pidfile])
    cfg = load_server_config(None, {"port": 22999})
    cfg.log_file = str(tmp_path / "nope" / "deep" / "rd.log")
    monkeypatch.setattr("server.__main__._onefile_executable", lambda: stub)
    # Make the parent unwritable so makedirs succeeds but the leaf can't be
    # opened... simpler: point log_file at a path whose dir can't be created.
    cfg.log_file = "/proc/cannot-create/rd.log"
    rc = _daemonize_onefile(args, cfg, pidfile)
    assert rc == 1
    assert not os.path.exists(pidfile)
