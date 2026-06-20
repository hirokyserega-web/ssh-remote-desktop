"""Daemon control helpers for ``rd-server`` (stdlib only).

This module contains everything that does *not* need the asyncio runtime:

* a small JSON pidfile format ``{"pid": int, "port": int, "host": str}`` with
  atomic write (``tmp + os.replace``) and stale-PID detection,
* a correct double-fork + ``setsid`` daemonization that rebinds stdio to a
  log file (or ``/dev/null``),
* ``stop``/``status`` helpers used by the ``--stop``/``--status`` CLI flags.

No third-party imports, no Qt, no asyncssh — so the unit tests can exercise
the pidfile/stop/status logic without spinning up a real SSH broker.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional


def default_pidfile() -> str:
    """Default pidfile path: ``/run/...`` under root, XDG config otherwise.

    ``os.geteuid`` is Unix-only; on Windows there is no ``/run`` root path, so
    we always fall through to the XDG config location there.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "/run/ssh-remote-desktop.pid"
    return os.path.expanduser("~/.config/ssh-remote-desktop/rd-server.pid")


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists.

    Uses ``os.kill(pid, 0)`` and treats EPERM as "alive but not ours".
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not killable by us
    return True


def read_pidfile(path: str) -> Optional[dict]:
    """Read the pidfile and return its parsed dict, or ``None`` if missing /
    unreadable / malformed. Never raises. The caller decides what "stale"
    means (e.g. by checking ``is_pid_alive`` on the returned ``pid``).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def write_pidfile(path: str, pid: int, *, port: int = 0, host: str = "") -> None:
    """Atomically write ``{pid, port, host}`` to ``path``.

    Writes to ``path + ".tmp"`` then ``os.replace``s it, so a concurrent
    reader never sees a half-written file. Parent dirs are created.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    payload = {"pid": int(pid), "port": int(port), "host": str(host)}
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def remove_pidfile(path: str) -> None:
    """Remove the pidfile if present; never raise on missing file."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def live_pid_from_pidfile(path: str) -> Optional[int]:
    """Return the pid recorded in ``path`` if that pid is alive, else ``None``.

    If the file exists but its pid is dead, the pidfile is considered stale and
    is removed so a fresh daemon can start.
    """
    data = read_pidfile(path)
    if not data:
        return None
    pid = data["pid"]
    if is_pid_alive(pid):
        return pid
    remove_pidfile(path)
    return None


def daemonize(log_file: Optional[str] = None) -> None:
    """Detach from the controlling terminal via the classic double-fork.

    * first fork → child is not a process-group leader,
    * ``setsid`` → new session + new process group, no controlling tty,
    * second fork → child can never reacquire a tty,
    * reopen stdin from ``/dev/null``, stdout/stderr to ``log_file`` (or
      ``/dev/null``).

    Stdlib only (``os`` / ``signal``). The pid of the final daemon is the
    current process after the call returns — the parent that called
    ``daemonize`` has already ``_exit``ed in the first fork.
    """
    if os.getenv("RD_SERVER_DAEMON_TEST_NOFORK") == "1":
        # Test escape hatch: skip the real forks so we can exercise the
        # rest of main() inside a single pytest process.
        _reset_stdio(log_file)
        return

    # First fork.
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.setsid()

    # Second fork.
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # We are now the daemon: new session, no controlling tty.
    os.chdir("/")
    os.umask(0o027)
    _reset_stdio(log_file)


def _reset_stdio(log_file: Optional[str]) -> None:
    """Reopen stdio: stdin from /dev/null, stdout+stderr to log_file or null."""
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull_fd, 0)
    except OSError:
        pass

    if log_file:
        # Append so a restarted daemon keeps history; create if missing.
        try:
            log_fd = os.open(
                log_file,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
        except OSError:
            log_fd = devnull_fd
    else:
        log_fd = devnull_fd

    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
    except OSError:
        pass

    # Close the original fds we opened (the dups are independent).
    if log_fd != devnull_fd:
        try:
            os.close(log_fd)
        except OSError:
            pass
    try:
        os.close(devnull_fd)
    except OSError:
        pass


@dataclass
class DaemonStatus:
    """Result of :func:`status`."""

    state: str  # "running" | "stopped"
    pid: Optional[int] = None
    port: Optional[int] = None
    host: Optional[str] = None

    def as_lines(self) -> list[str]:
        lines = [f"state: {self.state}"]
        if self.pid is not None:
            lines.append(f"pid: {self.pid}")
        if self.port is not None:
            lines.append(f"port: {self.port}")
        if self.host:
            lines.append(f"host: {self.host}")
        return lines


def status(pidfile: str) -> DaemonStatus:
    """Inspect the daemon: returns ``running`` with pid/port, or ``stopped``."""
    data = read_pidfile(pidfile)
    if not data:
        return DaemonStatus(state="stopped")
    pid = data["pid"]
    if not is_pid_alive(pid):
        remove_pidfile(pidfile)
        return DaemonStatus(state="stopped")
    return DaemonStatus(
        state="running",
        pid=pid,
        port=data.get("port"),
        host=data.get("host"),
    )


def stop(pidfile: str, *, timeout: float = 10.0, poll: float = 0.1) -> bool:
    """Send SIGTERM to the daemon recorded in ``pidfile`` and wait for exit.

    Returns ``True`` if the process exited within ``timeout`` (and the pidfile
    is cleaned up), ``False`` if there was nothing to stop or it didn't exit.
    """
    pid = live_pid_from_pidfile(pidfile)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pidfile(pidfile)
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            remove_pidfile(pidfile)
            return True
        time.sleep(poll)
    # Still alive — leave the pidfile so the user can investigate / SIGKILL.
    return False


def install_signal_handlers(loop, on_signal) -> None:
    """Wire SIGTERM/SIGINT to ``on_signal`` on the given asyncio loop.

    Falls back to ``signal.signal`` if ``add_signal_handler`` isn't supported
    (e.g. on Windows), so the foreground path still shuts down cleanly there.
    """
    import asyncio  # local: keep the module importable without asyncio

    def _handler(*_):
        if asyncio.iscoroutinefunction(on_signal):
            asyncio.ensure_future(on_signal())
        else:
            on_signal()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handler)
    except (NotImplementedError, RuntimeError):
        # Windows / event loops without add_signal_handler.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: _handler())


def _print_status_line(s: DaemonStatus, stream=None) -> None:
    """Human-readable status, used by ``rd-server --status``."""
    stream = stream or sys.stdout
    for line in s.as_lines():
        print(line, file=stream)
