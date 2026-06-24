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


def is_frozen_onefile() -> bool:
    """True when running inside a Nuitka/PyInstaller frozen binary.

    In a frozen onefile the classic in-process double-fork (:func:`daemonize`)
    is unsafe: the first fork's parent calls ``os._exit``, which lets the
    onefile bootstrap tear down the temp-extraction dir, killing the daemon
    grandchild — and because stdio has already been rebound to the log file the
    death is silent. Callers that want daemon behaviour under a frozen binary
    must instead re-launch a foreground child (see ``server.__main__``) so the
    onefile extraction stays alive for the child's lifetime.

    Detection covers both packagers we ship: Nuitka (injects ``__compiled__``
    into the main module) and PyInstaller (sets ``sys.frozen``).
    """
    if os.environ.get("RD_SERVER_FORCE_FROZEN") == "1":
        return True
    if getattr(sys, "frozen", False):
        return True
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and getattr(main_mod, "__compiled__", None) is not None:
        return True
    return False


def default_pidfile() -> str:
    """Default pidfile path: ``/run/...`` under root, XDG config otherwise.

    ``os.geteuid`` is Unix-only; on Windows there is no ``/run`` root path, so
    we always fall through to the XDG config location there.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "/run/ssh-remote-desktop.pid"
    return os.path.expanduser("~/.config/ssh-remote-desktop/rd-server.pid")


def default_log_file() -> str:
    """Default daemon log path, kept next to the pidfile in the config dir.

    Used when the operator hasn't set ``log_file`` but needs daemon output to
    go somewhere survivable (a foreground child's stderr is redirected here by
    the onefile daemonizer / the GUI controller so start failures are visible
    instead of vanishing into /dev/null).
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "/var/log/ssh-remote-desktop/rd-server.log"
    return os.path.expanduser("~/.config/ssh-remote-desktop/rd-server.log")


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists.

    On Unix, uses ``os.kill(pid, 0)`` and treats EPERM as "alive but not
    ours".  On Windows, ``os.kill(pid, 0)`` calls ``TerminateProcess`` and
    would **kill** the process, so we use the Win32 API instead
    (``OpenProcess`` + ``GetExitCodeProcess``).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_pid_alive_win32(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not killable by us
    return True


def _is_pid_alive_win32(pid: int) -> bool:
    """Windows liveness check via OpenProcess + GetExitCodeProcess."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        # NULL handle → process doesn't exist OR we lack access.
        if kernel32.GetLastError() == ERROR_ACCESS_DENIED:
            return True  # exists but not queryable
        return False
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def _proc_comm(pid: int) -> Optional[str]:
    """Best-effort read of ``/proc/<pid>/comm`` (Linux). None if unavailable.

    Used by :func:`is_likely_rd_server` to distinguish a real rd-server from a
    process that merely inherited a recycled PID. ``comm`` is truncated to 15
    chars by the kernel — enough to spot ``rd-server`` / ``rd-server.bin`` — so
    we don't need the longer ``cmdline`` for the common case.
    """
    try:
        with open(f"/proc/{pid}/comm", "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return None


def is_likely_rd_server(pid: int) -> bool:
    """Return True if ``pid`` plausibly belongs to an rd-server process.

    Stale-pidfile detection: a recorded PID can be reused by an unrelated
    process after the real rd-server dies, so "the PID is alive" is not enough
    to claim "rd-server is already running". This checks ``/proc/<pid>/comm``:

    * ``rd-server`` / ``rd-server.bin`` (Nuitka onefile extracted) → confirmed.
    * a ``python`` / ``python3.x`` interpreter → dev mode (``python -m
      server``); trust it so the dev workflow and the pytest suite (which use
      the pytest/python PID) keep working.
    * any other live process (``nginx``, ``sshd``, …) → a recycled PID; the
      pidfile is stale.
    * ``/proc`` unavailable (non-Linux / restricted) → cannot verify, trust
      liveness alone rather than risk a false "already running".

    Only the clearly-non-rd-server, non-python case returns False, which is
    exactly the PID-reuse trap we want to clear.
    """
    if pid <= 0:
        return False
    comm = _proc_comm(pid)
    if comm is None:
        return True  # /proc unavailable → can't verify → trust liveness
    if "rd-server" in comm:
        return True
    if comm.startswith("python"):
        return True
    return False


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

    If the file exists but its pid is dead — OR alive but clearly NOT an
    rd-server process (PID was recycled by an unrelated daemon) — the pidfile
    is considered stale and is removed so a fresh daemon can start. See
    :func:`is_likely_rd_server` for the reuse-detection heuristic.
    """
    data = read_pidfile(path)
    if not data:
        return None
    pid = data["pid"]
    if not is_pid_alive(pid):
        remove_pidfile(path)
        return None
    if not is_likely_rd_server(pid):
        # Alive, but not rd-server (and not a python dev process) → the PID was
        # reused after the real rd-server died. The pidfile is stale.
        remove_pidfile(path)
        return None
    return pid


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

    if not hasattr(os, "fork"):
        raise NotImplementedError(
            "daemon mode (double-fork) is not supported on Windows; "
            "run rd-server in the foreground instead."
        )

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
    if not is_pid_alive(pid) or not is_likely_rd_server(pid):
        # Dead, or alive-but-recycled (PID reuse): the pidfile no longer
        # describes a real rd-server, so clear it and report stopped.
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
