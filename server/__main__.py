"""Server entry point.

Run with::

    python -m server [--config server.toml] [--port 2222] [--backend auto]

or, after building, the standalone ``rd-server`` executable.

Daemon and systemd integration
------------------------------
``rd-server`` can run in three ways:

* **foreground** (default, also under systemd ``Type=simple``): no forks,
  logs to stderr + ``--log-file`` if given. SIGTERM/SIGINT trigger a clean
  ``Broker.shutdown()``.
* **daemon** (``--daemon`` / ``-d``): double-fork + ``setsid`` via
  :mod:`server.daemon`, stdio rebound to ``--log-file`` or ``/dev/null``,
  PID written atomically to ``--pidfile``. Refuses to start if a live PID
  is already recorded.
* **service sub-commands** (``--install-service`` etc.): generate the
  systemd unit with the real binary path, copy it to
  ``/etc/systemd/system/``, run ``daemon-reload`` + ``enable --now``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common.config import load_server_config

from .auth import warn_if_pam_unavailable, warn_if_privileges_insufficient
from .broker import Broker, HostKeyError, PortInUseError
from .daemon import (
    daemonize,
    default_log_file,
    default_pidfile,
    install_signal_handlers,
    is_frozen_onefile,
    live_pid_from_pidfile,
    remove_pidfile,
    status,
    stop,
    write_pidfile,
)

log = logging.getLogger("rd.server")

# Path to the systemd unit template shipped in the repo. Resolved relative to
# this file so it works whether rd-server is run from a checkout, an editable
# install, or a Nuitka-frozen binary (which puts resources next to the exe).
_UNIT_TEMPLATE = "packaging/systemd/ssh-remote-desktop.service"
_SYSTEMD_DIR = "/etc/systemd/system"
_UNIT_NAME = "ssh-remote-desktop.service"


def _unit_template_path() -> Path:
    """Locate the systemd unit template next to the install tree."""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / _UNIT_TEMPLATE,          # repo / editable install
        Path(sys.prefix) / _UNIT_TEMPLATE,     # venv install
        Path("/usr/share/ssh-remote-desktop") / _UNIT_TEMPLATE,
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return here.parent / _UNIT_TEMPLATE  # default for error message


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rd-server", description="SSH remote desktop server")
    p.add_argument("--config", help="path to server config (TOML/JSON)")
    p.add_argument("--host", help="bind address")
    p.add_argument("--port", type=int, help="listen port")
    p.add_argument("--backend", choices=["auto", "x11", "wayland"], help="force display backend")
    p.add_argument("--host-key", dest="host_key", help="SSH host key path")
    p.add_argument("--max-sessions", dest="max_sessions", type=int)
    p.add_argument("--idle-timeout", dest="idle_timeout", type=int)
    p.add_argument("--codec", choices=["h264", "h265", "jpeg", "webp"])
    p.add_argument("--fps", type=int)
    p.add_argument("--shared-dir", dest="shared_dir")
    p.add_argument("--no-clipboard", dest="clipboard_enabled", action="store_false", default=None)
    p.add_argument("--no-files", dest="files_enabled", action="store_false", default=None)
    p.add_argument("--log-level", dest="log_level")
    p.add_argument("--log-file", dest="log_file")

    # --- daemon / foreground control ---------------------------------------
    run_mode = p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--foreground", dest="foreground", action="store_true", default=True,
        help="stay in the foreground (default; also used by systemd Type=simple)",
    )
    run_mode.add_argument(
        "-d", "--daemon", dest="daemon", action="store_true",
        help="detach into the background (double-fork + setsid)",
    )
    p.add_argument(
        "--pidfile", dest="pidfile", default=None,
        help=f"PID file path (default: {default_pidfile()})",
    )

    # --- control / inspection ----------------------------------------------
    p.add_argument("--stop", action="store_true", help="stop a running daemon (SIGTERM)")
    p.add_argument("--status", action="store_true", help="print daemon state and exit")

    # --- systemd unit management -------------------------------------------
    svc = p.add_mutually_exclusive_group()
    svc.add_argument(
        "--install-service", dest="install_service", action="store_true",
        help="install + enable the systemd unit for this binary",
    )
    svc.add_argument(
        "--uninstall-service", dest="uninstall_service", action="store_true",
        help="stop, disable, and remove the systemd unit",
    )
    svc.add_argument(
        "--enable-service", dest="enable_service", action="store_true",
        help="enable (start at boot) an already-installed unit",
    )
    svc.add_argument(
        "--disable-service", dest="disable_service", action="store_true",
        help="disable autostart without removing the unit",
    )
    return p


# --------------------------------------------------------------------------- #
# systemd unit management
# --------------------------------------------------------------------------- #
def _real_binary_path() -> str:
    """Return the path to use as ``ExecStart=`` in the unit.

    Prefer the ``rd-server`` console script next to this Python (so a venv
    install wires the right interpreter), fall back to ``sys.executable -m
    server`` for a checkout.
    """
    here = Path(__file__).resolve().parent
    # 1) venv/bin/rd-server next to lib/pythonX/site-packages/server/__main__.py
    for cand in (
        Path(sys.prefix) / "bin" / "rd-server",
        here.parent.parent.parent / "bin" / "rd-server",
    ):
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    # 2) Standalone frozen binary.
    if getattr(sys, "frozen", False):
        return sys.executable
    # 3) Plain ``python -m server``.
    return f"{sys.executable} -m server"


def _render_unit(binary: str, config: str | None) -> str:
    """Substitute ``ExecStart`` (and optional ``--config``) into the template."""
    tpl = _unit_template_path()
    text = tpl.read_text(encoding="utf-8")
    exec_start = binary
    if config:
        exec_start += f" --foreground --config {config}"
    else:
        exec_start += " --foreground"
    return text.replace("@EXEC_START@", exec_start)


def _have_systemctl() -> bool:
    return shutil.which("systemctl") is not None


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True, text=True, check=check,
    )


def install_service(config: str | None = None, *, enable: bool = True) -> int:
    """Write the unit to /etc/systemd/system, daemon-reload, optionally enable."""
    binary = _real_binary_path()
    unit_text = _render_unit(binary, config)
    target = Path(_SYSTEMD_DIR) / _UNIT_NAME
    print(f"Installing systemd unit → {target}")
    print(f"  ExecStart={binary}" + (f" --config {config}" if config else ""))
    if os.geteuid() != 0:
        print("  (root required; re-run with sudo)", file=sys.stderr)
        return 1
    try:
        os.makedirs(_SYSTEMD_DIR, exist_ok=True)
        target.write_text(unit_text, encoding="utf-8")
    except OSError as exc:
        print(f"  failed to write unit: {exc}", file=sys.stderr)
        return 1
    if not _have_systemctl():
        print(
            "systemctl not found on this system. The unit file was written to "
            f"{target}. To activate it manually:\n"
            "  systemctl daemon-reload\n"
            "  systemctl enable --now ssh-remote-desktop.service",
            file=sys.stderr,
        )
        return 0
    _systemctl("daemon-reload", check=False)
    if enable:
        r = _systemctl("enable", "--now", _UNIT_NAME, check=False)
        if r.returncode != 0:
            print(f"  systemctl enable --now failed: {r.stderr.strip()}", file=sys.stderr)
            return r.returncode
        print("  enabled + started.")
    else:
        print("  installed (not enabled; use --enable-service to start).")
    return 0


def uninstall_service() -> int:
    """Stop + disable + remove the unit, then daemon-reload."""
    if os.geteuid() != 0:
        print("uninstall-service requires root (re-run with sudo).", file=sys.stderr)
        return 1
    target = Path(_SYSTEMD_DIR) / _UNIT_NAME
    if _have_systemctl():
        _systemctl("stop", _UNIT_NAME, check=False)
        _systemctl("disable", _UNIT_NAME, check=False)
        _systemctl("daemon-reload", check=False)
    if target.exists():
        try:
            target.unlink()
            print(f"Removed {target}")
        except OSError as exc:
            print(f"failed to remove unit: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"{target} not present; nothing to remove.")
    return 0


def enable_service() -> int:
    if os.geteuid() != 0:
        print("enable-service requires root (re-run with sudo).", file=sys.stderr)
        return 1
    if not _have_systemctl():
        print("systemctl not found; cannot enable the unit.", file=sys.stderr)
        return 1
    r = _systemctl("enable", "--now", _UNIT_NAME, check=False)
    if r.returncode != 0:
        print(f"systemctl enable --now failed: {r.stderr.strip()}", file=sys.stderr)
    else:
        print("service enabled + started.")
    return r.returncode


def disable_service() -> int:
    if os.geteuid() != 0:
        print("disable-service requires root (re-run with sudo).", file=sys.stderr)
        return 1
    if not _have_systemctl():
        print("systemctl not found; cannot disable the unit.", file=sys.stderr)
        return 1
    r = _systemctl("disable", _UNIT_NAME, check=False)
    if r.returncode != 0:
        print(f"systemctl disable failed: {r.stderr.strip()}", file=sys.stderr)
    else:
        print("service disabled (will not start at boot).")
    return r.returncode


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _configure_logging(cfg, *, daemon: bool = False) -> None:
    # In daemon mode, daemonize() has already reopened stderr onto the log
    # file (or /dev/null), so a single StreamHandler(sys.stderr) is enough —
    # adding a FileHandler would duplicate every line.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.log_file and not daemon:
        try:
            handlers.append(logging.FileHandler(cfg.log_file))
        except OSError:
            pass
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


async def _run_broker(cfg, pidfile: str | None) -> int:
    """Start the broker, write the pidfile, install signal handlers, run.

    We do NOT use ``Broker.serve_forever()`` here because that helper calls
    ``start()`` itself; we want to start the broker, write the pidfile once
    the listener is up, and then wait on either a signal or a broker crash.
    """
    broker = Broker(cfg)
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    async def _shutdown():
        log.info("shutdown signal received")
        stop_event.set()

    install_signal_handlers(loop, _shutdown)

    # Start the broker first. If the listen fails (port in use, perms, …) we
    # exit before writing a misleading "running" pidfile.
    await broker.start()
    if pidfile:
        write_pidfile(pidfile, os.getpid(), port=cfg.port, host=cfg.host)
        log.info("wrote pidfile %s", pidfile)

    # Run the reaper + wait for either a signal or a broker failure.
    # serve_forever_after_start runs the `await asyncio.Future()` (until
    # cancelled) and runs Broker.shutdown() in its own finally — so we must
    # NOT call shutdown again here.
    serve_task = asyncio.create_task(broker.serve_forever_after_start())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    broker_error: BaseException | None = None
    if serve_task in done:
        try:
            serve_task.result()
        except BaseException as exc:  # surface listen/runtime errors visibly
            broker_error = exc
            log.exception("broker task crashed")
    elif stop_task in done:
        # Signal-driven: cancel serve_task so its finally runs shutdown().
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass

    if pidfile:
        remove_pidfile(pidfile)
    if broker_error is not None:
        return 1
    return 0


def _port_in_use_hint(exc: PortInUseError) -> str:
    """A one-line, actionable message for a port-in-use failure.

    Replaces the multi-page asyncio/asyncssh traceback the user used to see
    when a previous rd-server (often a leftover foreground process from an
    earlier install) was still holding the port.
    """
    return (
        f"rd-server: cannot listen on {exc.host}:{exc.port} — the port is "
        f"already in use.\n"
        f"  Most likely a previous rd-server is still running. Find and stop it:\n"
        f"    ss -tlnp | grep {exc.port}      # show what holds the port\n"
        f"    pkill -f rd-server              # stop all rd-server processes\n"
        f"    rd-server --stop                # stop a daemonized instance (uses its pidfile)\n"
        f"  Or pick a different port with: rd-server --port <PORT>"
    )


def _host_key_hint(exc: "HostKeyError") -> str:
    """A one-line, actionable message for a host-key creation failure.

    Replaces the bare ``OSError`` traceback the user used to see when the
    config dir (~/.config/ssh-remote-desktop) couldn't be created — usually a
    permissions issue (read-only HOME, no write rights, non-root user lacking
    access to the chosen host_key path).
    """
    return (
        f"rd-server: cannot create the SSH host key at {exc.path} "
        f"({exc.original.strerror or exc.original}).\n"
        f"  The config directory must be writable. Fix:\n"
        f"    - run as a user with write access to the host_key path, or as root;\n"
        f"    - point --host-key at a writable location, e.g.\n"
        f"        rd-server --host-key ~/.config/ssh-remote-desktop/host_ed25519\n"
        f"    - create the dir first: mkdir -p ~/.config/ssh-remote-desktop"
    )


# --------------------------------------------------------------------------- #
# Frozen-onefile daemonization
# --------------------------------------------------------------------------- #
def _onefile_executable() -> str:
    """Path to re-launch so a fresh foreground child is a standalone process.

    Under a Nuitka onefile, ``sys.executable`` is the *already-extracted*
    program; re-launching it would share the spawner's extraction dir, which the
    spawner tears down on exit — re-introducing the very bootstrap race we are
    avoiding. ``sys.argv[0]`` is the onefile binary itself, so a child launched
    from it re-extracts into its own temp dir whose lifetime is the child's
    own. That is the robust choice for a detached daemon.
    """
    if getattr(sys, "argv", None) and sys.argv and sys.argv[0]:
        return sys.argv[0]
    return sys.executable


def _foreground_argv(args, *, pidfile: str) -> list[str]:
    """Rebuild the argv needed to run the server in the foreground.

    Mirrors the override fields parsed by :func:`build_parser` so a daemon
    request (``--daemon``) becomes a foreground request (``--foreground``) with
    the same effective configuration. ``--log-file`` is deliberately omitted:
    the spawner redirects the child's stderr to the log file itself, so an
    additional ``FileHandler`` would duplicate every log line.
    """
    cmd = ["--foreground", "--pidfile", pidfile]
    if args.config:
        cmd += ["--config", args.config]
    if args.host:
        cmd += ["--host", args.host]
    if args.port is not None:
        cmd += ["--port", str(args.port)]
    if args.backend:
        cmd += ["--backend", args.backend]
    if args.host_key:
        cmd += ["--host-key", args.host_key]
    if args.max_sessions is not None:
        cmd += ["--max-sessions", str(args.max_sessions)]
    if args.idle_timeout is not None:
        cmd += ["--idle-timeout", str(args.idle_timeout)]
    if args.codec:
        cmd += ["--codec", args.codec]
    if args.fps is not None:
        cmd += ["--fps", str(args.fps)]
    if args.shared_dir:
        cmd += ["--shared-dir", args.shared_dir]
    if args.clipboard_enabled is False:
        cmd += ["--no-clipboard"]
    if args.files_enabled is False:
        cmd += ["--no-files"]
    if args.log_level:
        cmd += ["--log-level", args.log_level]
    return cmd


def _tail_file(path: str, n: int = 40) -> str:
    """Return the last ``n`` lines of ``path`` (or '' if unreadable)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""


def _daemonize_onefile(args, cfg, pidfile: str) -> int:
    """Daemonize a frozen onefile binary by launching a foreground child.

    The in-process double-fork (:func:`server.daemon.daemonize`) breaks a
    Nuitka onefile: the first fork's parent ``os._exit``s, the onefile
    bootstrap tears down the temp-extraction dir, and the daemon grandchild
    dies — with stdio already redirected to the log file, so the failure is
    invisible (the operator sees only "Остановлен").

    Instead we spawn a fresh ``--foreground`` process of ourselves in a new
    session (``start_new_session=True``), redirect its stdout/stderr to the log
    file, and let *it* write the pidfile once the listener is up. We — the
    spawner — stay just long enough (a grace window) to detect an early crash
    (missing module, port in use, perms) so the caller gets a non-zero exit and
    the stderr cause instead of silent success.
    """
    log_file = os.path.expanduser(cfg.log_file) if cfg.log_file else default_log_file()
    log_dir = os.path.dirname(os.path.abspath(log_file))
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        print(f"rd-server: cannot create log dir {log_dir}: {exc}", file=sys.stderr)
        remove_pidfile(pidfile)
        return 1
    try:
        log_fh = open(log_file, "ab", buffering=0)
    except OSError as exc:
        print(f"rd-server: cannot open log file {log_file}: {exc}", file=sys.stderr)
        remove_pidfile(pidfile)
        return 1

    exe = _onefile_executable()
    cmd = [exe, *_foreground_argv(args, pidfile=pidfile)]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log_fh.close()
        print(f"rd-server: cannot launch daemon child {exe!r}: {exc}", file=sys.stderr)
        remove_pidfile(pidfile)
        return 1

    # Grace window: an early exit (import error, port in use, perms) almost
    # always happens within the first couple of seconds. If the child is still
    # alive AND has written the pidfile, the listener is up → success. We read
    # the child's stderr back from the log file so the cause is surfaced.
    grace = 4.0
    deadline = time.monotonic() + grace
    result = 0
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            tail = _tail_file(log_file, 40).strip()
            print(
                f"rd-server: daemon child exited early (code {rc}).\n{tail}",
                file=sys.stderr,
            )
            remove_pidfile(pidfile)
            result = 1
            break
        if os.path.exists(pidfile):
            break  # listener up, pidfile written by the child
        time.sleep(0.1)
    # Closing the parent's fd object is safe: the child inherited its own dup.
    log_fh.close()
    if result == 0:
        print(f"rd-server: daemon started (pid {proc.pid}, log {log_file})",
              file=sys.stderr)
    return result


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # ----- pure side-effect commands (no config, no daemon) --------------- #
    if args.status:
        s = status(args.pidfile or default_pidfile())
        for line in s.as_lines():
            print(line)
        return 0
    if args.stop:
        ok = stop(args.pidfile or default_pidfile())
        print("stopped" if ok else "not running")
        return 0 if ok else 1
    if args.install_service:
        return install_service(args.config, enable=True)
    if args.uninstall_service:
        return uninstall_service()
    if args.enable_service:
        return enable_service()
    if args.disable_service:
        return disable_service()

    # ----- normal server start path --------------------------------------- #
    overrides = {k: v for k, v in vars(args).items()
                 if k not in {"config", "daemon", "foreground", "pidfile",
                              "stop", "status", "install_service",
                              "uninstall_service", "enable_service",
                              "disable_service"}
                 and v is not None}
    cfg = load_server_config(args.config, overrides)
    pidfile = args.pidfile or default_pidfile()

    # Surface a missing python-pam / wrong-privilege setup immediately at start
    # instead of only on the first (opaque) rejected login. PAM reads
    # /etc/shadow, so it needs root or the 'shadow' group; warn early so the
    # operator sees the real cause before any client tries to connect.
    warn_if_pam_unavailable(allow_password=cfg.allow_password,
                            pam_service=cfg.pam_service)
    warn_if_privileges_insufficient(allow_password=cfg.allow_password,
                                    run_as_user=cfg.run_as_user,
                                    pam_service=cfg.pam_service)

    if args.daemon:
        existing = live_pid_from_pidfile(pidfile)
        if existing is not None:
            print(f"rd-server already running (pid {existing}); use --stop first.",
                  file=sys.stderr)
            return 1
        # Frozen Nuitka/PyInstaller onefile: the in-process double-fork below
        # breaks the onefile bootstrap (the parent's os._exit tears down the
        # temp extraction, killing the daemon grandchild whose stdio is already
        # redirected — so the death is silent). Re-launch a foreground child in
        # a new session instead; it keeps its own extraction alive and writes
        # the pidfile itself, while we surface any early crash to the caller.
        if is_frozen_onefile():
            return _daemonize_onefile(args, cfg, pidfile)
        # Non-frozen (venv / `python -m server`): classic double-fork is safe.
        # Daemonize BEFORE configuring logging — the double-fork rebinds
        # stdio, so logging.basicConfig below will attach handlers to the
        # new stderr (which points at the log file or /dev/null).
        daemonize(log_file=cfg.log_file or None)
        # In the daemon child now; logging configured against the new stdio.
        _configure_logging(cfg, daemon=True)
        log.info("rd-server daemonized, pid=%d", os.getpid())
        try:
            return asyncio.run(_run_broker(cfg, pidfile))
        except PortInUseError as exc:
            print(_port_in_use_hint(exc), file=sys.stderr)
            return 1
        except HostKeyError as exc:
            print(_host_key_hint(exc), file=sys.stderr)
            return 1
        finally:
            remove_pidfile(pidfile)

    # Foreground (default, also under systemd Type=simple).
    # Even in foreground mode, bail out early if a daemon pidfile points at a
    # live process — otherwise we race the running instance for the port and
    # the user gets an opaque bind error instead of "already running".
    existing = live_pid_from_pidfile(pidfile)
    if existing is not None:
        print(f"rd-server already running (pid {existing}); use --stop first.",
              file=sys.stderr)
        return 1
    _configure_logging(cfg, daemon=False)
    try:
        return asyncio.run(_run_broker(cfg, pidfile if args.pidfile else None))
    except PortInUseError as exc:
        print(_port_in_use_hint(exc), file=sys.stderr)
        return 1
    except HostKeyError as exc:
        print(_host_key_hint(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
