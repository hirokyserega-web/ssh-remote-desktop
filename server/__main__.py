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
from pathlib import Path

from common.config import load_server_config

from .broker import Broker, PortInUseError
from .daemon import (
    daemonize,
    default_pidfile,
    install_signal_handlers,
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

    if args.daemon:
        existing = live_pid_from_pidfile(pidfile)
        if existing is not None:
            print(f"rd-server already running (pid {existing}); use --stop first.",
                  file=sys.stderr)
            return 1
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
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
