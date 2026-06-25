"""Testable, Qt-free controller for the server GUI.

Everything that is genuinely *logic* — reading/writing ``server.toml``,
validating fields, deciding how to start/stop the server, asking systemd
about its state, tailing the log — lives here so the test suite can exercise
it without a display. The thin Qt layer in ``server_gui.__main__`` only turns
user input into controller calls and controller results into widgets.

Design notes
------------
* **No secrets in the GUI config.** :class:`ServerGuiConfig` deliberately
  omits any password / key fields. The TOML file we write only ever contains
  the fields exposed in the form. If someone loads a ``server.toml`` that
  happens to contain a stray ``password = "..."`` line, we drop it on save.
* **Atomic writes.** ``save`` writes to ``path + ".tmp"`` then
  ``os.replace``s, so a crash mid-write never leaves a half-empty config.
* **Server management is pluggable.** :class:`ServiceController` abstracts
  "start/stop/restart/status" behind a small interface with two
  implementations: :class:`SystemdController` (used when the unit is
  installed) and :class:`DaemonController` (falls back to
  ``rd-server --daemon/--stop/--status``). The GUI just picks whichever
  reports ``is_managed()``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

try:
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

# Reuse the daemon helpers (pidfile format, status, stop).
from server.daemon import (
    default_log_file,
    default_pidfile,
    is_likely_rd_server,
    is_pid_alive,
    live_pid_from_pidfile,
    remove_pidfile,
    status as daemon_status,
    stop as daemon_stop,
)

UNIT_NAME = "ssh-remote-desktop.service"
SYSTEMD_UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

# Fields exposed in the GUI form, in display order. The dataclass below lists
# the same names with sane defaults; the GUI builds its rows from this list so
# the form and the round-trip test stay in sync.
EDITABLE_FIELDS: tuple[str, ...] = (
    "host", "port", "backend", "max_sessions", "idle_timeout",
    "codec", "fps", "bitrate_kbps", "shared_dir",
    "allow_password", "allow_publickey", "run_as_user",
    "log_level", "log_file",
)

BACKENDS = ("auto", "x11", "wayland")
CODECS = ("h264", "h265", "jpeg")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Sentinel: never write these keys, even if present in a loaded file — the GUI
# must not become a way to persist secrets. Match the *whole* key (after
# dash→underscore normalization) so a boolean toggle like ``allow_password``
# is NOT stripped — only an actual ``password`` / ``private_key`` / ``token``
# value would be. ``host_key`` is sensitive (it points at a private key file)
# so it stays in the forbidden set.
SENSITIVE_KEY_HINTS = ("password", "secret", "token", "private_key", "host_key")


class ConfigError(ValueError):
    """Raised when a field value fails validation."""


@dataclass
class ServerGuiConfig:
    """Subset of :class:`common.config.ServerConfig` exposed in the GUI.

    Deliberately small and secret-free: host/port/backend/limits/codec/auth
    toggles/run_as_user/logging. Sensitive knobs (host_key path, password
    validators) are managed from the CLI / file, never from the GUI form.
    """

    host: str = "0.0.0.0"
    port: int = 2222
    backend: str = "auto"
    max_sessions: int = 10
    idle_timeout: int = 600
    codec: str = "h264"
    fps: int = 30
    bitrate_kbps: int = 6000
    shared_dir: str = "~/shared"
    allow_password: bool = True
    allow_publickey: bool = True
    run_as_user: bool = True
    log_level: str = "INFO"
    log_file: str = ""

    @classmethod
    def from_server_config(cls, cfg) -> "ServerGuiConfig":
        """Build a GUI config from a full :class:`ServerConfig`."""
        return cls(**{f.name: getattr(cfg, f.name) for f in fields(cls)})

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# TOML serialization (no extra dep — stdlib has tomllib read but no write)
# --------------------------------------------------------------------------- #
def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return f'"{_toml_escape(v)}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {type(v).__name__} to TOML")


def dumps_toml(data: dict) -> str:
    """Serialize a flat dict to a minimal TOML string (key = value lines).

    Only the subset we need: scalars + bools. No nested tables, no arrays of
    tables — :class:`ServerGuiConfig` is flat by construction.
    """
    out = ["# ssh-remote-desktop server config (written by rd-server-gui)\n"]
    for key, value in data.items():
        out.append(f"{key} = {_toml_value(value)}\n")
    return "".join(out)


def _has_sensitive_key(key: str) -> bool:
    """True only for keys that *are* secrets, not keys that merely mention
    them. ``allow_password`` → False (it's a bool toggle); ``password`` → True.

    Exact-match only — a suffix match would also catch the boolean toggle
    ``allow_password`` (it ends in ``_password``). The realistic threat (a
    stray ``password = "..."`` in a hand-edited file) is already blocked by
    :meth:`ConfigController.load`, which only copies keys that are declared
    fields of :class:`ServerGuiConfig`; this filter is belt-and-suspenders for
    the day someone adds a secret field to the dataclass by mistake.
    """
    k = key.lower().replace("-", "_")
    return k in SENSITIVE_KEY_HINTS


# --------------------------------------------------------------------------- #
# Controller: load / validate / save
# --------------------------------------------------------------------------- #
class ConfigController:
    """Reads/writes server.toml with validation; never persists secrets."""

    def __init__(self, path: str):
        self.path = str(path)

    # -- load ------------------------------------------------------------- #
    def load(self) -> ServerGuiConfig:
        """Load the config file, returning a :class:`ServerGuiConfig`.

        Missing file → defaults. Unknown / sensitive keys are silently
        dropped (the GUI never displays them and never writes them back).
        """
        cfg = ServerGuiConfig()
        p = Path(os.path.expanduser(self.path))
        if not p.exists():
            return cfg
        try:
            text = p.read_text(encoding="utf-8")
            data = tomllib.loads(text) if tomllib else {}
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {self.path}: {exc}") from exc
        names = {f.name for f in fields(ServerGuiConfig)}
        for key, value in data.items():
            norm = key.replace("-", "_")
            if norm in names and value is not None:
                setattr(cfg, norm, value)
        return cfg

    # -- validate --------------------------------------------------------- #
    @staticmethod
    def validate(cfg: ServerGuiConfig) -> list[str]:
        """Return a list of human-readable validation errors (empty = OK)."""
        errs: list[str] = []
        if not (1 <= cfg.port <= 65535):
            errs.append("port must be in 1–65535")
        if cfg.backend not in BACKENDS:
            errs.append(f"backend must be one of {BACKENDS}")
        if cfg.codec not in CODECS:
            errs.append(f"codec must be one of {CODECS}")
        if cfg.max_sessions < 0:
            errs.append("max_sessions must be ≥ 0")
        if cfg.idle_timeout < 0:
            errs.append("idle_timeout must be ≥ 0")
        if cfg.fps < 0:
            errs.append("fps must be ≥ 0")
        if cfg.bitrate_kbps < 0:
            errs.append("bitrate_kbps must be ≥ 0")
        if cfg.log_level not in LOG_LEVELS:
            errs.append(f"log_level must be one of {LOG_LEVELS}")
        # shared_dir: if absolute, must exist; if relative/`~`, defer.
        sd = os.path.expanduser(cfg.shared_dir)
        if os.path.isabs(sd) and not os.path.exists(sd):
            errs.append(f"shared_dir does not exist: {sd}")
        return errs

    # -- save ------------------------------------------------------------- #
    def save(self, cfg: ServerGuiConfig) -> None:
        """Validate, then atomically write the config. Raises on validation
        failure or write error. Guarantees no sensitive keys are persisted."""
        errs = self.validate(cfg)
        if errs:
            raise ConfigError("; ".join(errs))
        data = cfg.to_dict()
        # Belt-and-suspenders: strip anything that looks sensitive, even
        # though ServerGuiConfig has no such fields.
        data = {k: v for k, v in data.items() if not _has_sensitive_key(k)}
        text = dumps_toml(data)
        p = Path(os.path.expanduser(self.path))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# Server management: systemd first, daemon-mode fallback
# --------------------------------------------------------------------------- #
@dataclass
class ServerState:
    state: str = "stopped"          # "running" | "stopped" | "unknown"
    pid: Optional[int] = None
    port: Optional[int] = None
    host: Optional[str] = None
    autostart: bool = False         # systemd unit enabled?
    managed_by: str = "none"        # "systemd" | "daemon" | "none"
    log_path: Optional[str] = None
    error: Optional[str] = None


def _run(cmd: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Run cmd, capturing output. Never raises on non-zero exit."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


# --------------------------------------------------------------------------- #
# Port probing — used by the daemon controller (start pre-check) and the GUI
# health check. Kept Qt-free and side-effect-free so the test suite can exercise
# them directly.
# --------------------------------------------------------------------------- #
def _connect_host(host: str) -> str:
    """Normalise a bind address into one a client can connect() to.

    ``0.0.0.0`` / ``::`` are listen-only wildcards; connecting to them is
    undefined, so map them to the loopback the server is also reachable on.
    """
    if host in ("", "0.0.0.0", "::", None):
        return "127.0.0.1"
    return host


def port_listening(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True when something accepts a TCP connection on ``host:port``.

    Used by the GUI status refresh as a cheap liveness probe: a daemon with a
    live pidfile but no listener (root-requiring auth/startup that silently
    failed) is reported as "Запущен, но порт не слушается" instead of a healthy
    "Запущен".
    """
    try:
        with socket.create_connection((_connect_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_bindable(host: str, port: int) -> bool:
    """True when a fresh listener can bind ``host:port`` (i.e. it is free).

    SO_REUSEADDR mirrors what asyncssh does, so a socket in TIME_WAIT does not
    cause a false "busy" — only an *active listener* makes the bind fail. Used
    as a start() pre-check so a port collision surfaces as an actionable error
    before spawning a child that would just exit in the grace window.
    """
    h = host or "0.0.0.0"
    family = socket.AF_INET6 if ":" in h else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((h, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _port_listener_pid(port: int) -> Optional[int]:
    """Best-effort PID of the process listening on ``port`` (Linux).

    Tries ``ss`` then ``lsof``. Both need root to report *another* user's PID,
    so this often returns None for a non-rd-server holder — callers fall back to
    an actionable "port busy; run sudo ss -tlnp" message in that case. Mockable
    via :func:`_run` so the test suite can exercise the parsing without a real
    socket holder.
    """
    r = _run(["ss", "-tlnpH", f"sport = :{port}"], timeout=3.0)
    if r.stdout:
        m = re.search(r"pid=(\d+)", r.stdout)
        if m:
            return int(m.group(1))
    r = _run(["lsof", "-tci", f":{port}"], timeout=3.0)
    if r.stdout:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    return None


def _have_systemctl() -> bool:
    return shutil.which("systemctl") is not None


def unit_installed() -> bool:
    return os.path.exists(SYSTEMD_UNIT_PATH)


def _euid() -> int:
    try:
        return os.geteuid()
    except AttributeError:
        return -1


def _escalate_prefix() -> list[str]:
    """Command prefix (pkexec/sudo) for privileged actions when not root.

    Prefer ``pkexec`` — it pops a graphical PolicyKit prompt, the right UX for
    a control panel. Fall back to ``sudo`` (terminal prompt), and to an empty
    list when neither is available (the systemctl call then fails with a clear
    permission error rather than a confusing crash).
    """
    if _euid() == 0:
        return []
    if shutil.which("pkexec"):
        return ["pkexec"]
    if shutil.which("sudo"):
        return ["sudo"]
    return []


def _systemctl(*args: str, escalate: bool = False) -> subprocess.CompletedProcess:
    if escalate and _euid() != 0:
        prefix = _escalate_prefix()
        # 60s lets the operator clear the pkexec/sudo auth prompt.
        return _run([*prefix, "systemctl", *args], timeout=60.0)
    return _run(["systemctl", *args])


def privilege_warning(cfg: ServerGuiConfig) -> Optional[str]:
    """Return a warning string when the config needs root but lacks it, else None.

    ``allow_password`` routes logins through PAM (reads /etc/shadow) and
    ``run_as_user`` drops privileges via setuid; both need root. The panel
    surfaces this as a banner so the operator starts with the right privileges
    instead of seeing every login silently rejected.
    """
    if not (cfg.allow_password or cfg.run_as_user):
        return None
    if _euid() == 0:
        return None
    reasons = []
    if cfg.allow_password:
        reasons.append("парольной аутентификации (PAM читает /etc/shadow)")
    if cfg.run_as_user:
        reasons.append("запуска сессий от имени пользователя (setuid)")
    return (
        "Включены " + " и ".join(reasons) + ", для чего нужен root. "
        "Запустите сервер через systemd-юнит (User=root) либо `sudo rd-server`, "
        "добавьте пользователя в группу shadow (для паролей) или отключите эти "
        "опции — иначе входы и сессии будут молча отклоняться."
    )


class ServiceController:
    """Abstract start/stop/status. Implementations: systemd vs daemon."""

    name = "none"

    def is_managed(self) -> bool:
        return False

    def state(self) -> ServerState:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def restart(self) -> bool:
        return self.stop() and self.start()

    def enable_autostart(self, on: bool) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError


class SystemdController(ServiceController):
    """Manages the server via ``systemctl`` (when the unit is installed)."""

    name = "systemd"

    def is_managed(self) -> bool:
        return _have_systemctl() and unit_installed()

    def _active_state(self) -> str:
        r = _systemctl("show", "-p", "ActiveState", "--value", UNIT_NAME)
        return r.stdout.strip() or "unknown"

    def _main_pid(self) -> Optional[int]:
        r = _systemctl("show", "-p", "MainPID", "--value", UNIT_NAME)
        try:
            pid = int(r.stdout.strip())
        except ValueError:
            return None
        return pid or None

    def _enabled(self) -> bool:
        r = _systemctl("is-enabled", UNIT_NAME)
        return r.stdout.strip() == "enabled" or r.returncode == 0 and "enabled" in r.stdout

    def state(self) -> ServerState:
        if not self.is_managed():
            return ServerState(state="unknown", managed_by="none",
                               error="systemd unit not installed")
        active = self._active_state()
        st = ServerState(
            state="running" if active == "active" else "stopped",
            pid=self._main_pid(),
            autostart=self._enabled(),
            managed_by="systemd",
        )
        return st

    def start(self) -> bool:
        return _systemctl("start", UNIT_NAME, escalate=True).returncode == 0

    def stop(self) -> bool:
        return _systemctl("stop", UNIT_NAME, escalate=True).returncode == 0

    def restart(self) -> bool:
        return _systemctl("restart", UNIT_NAME, escalate=True).returncode == 0

    def enable_autostart(self, on: bool) -> bool:
        if on:
            return _systemctl("enable", UNIT_NAME, escalate=True).returncode == 0
        return _systemctl("disable", UNIT_NAME, escalate=True).returncode == 0


class DaemonController(ServiceController):
    """Manages the server via ``rd-server --daemon/--stop/--status``.

    Used when the systemd unit isn't installed. The pidfile + port come from
    the loaded config so status reports the right port.
    """

    name = "daemon"

    def __init__(self, cfg: ServerGuiConfig, *, binary: str = "rd-server",
                 pidfile: Optional[str] = None, config_path: Optional[str] = None):
        self.cfg = cfg
        self.binary = self._resolve_binary(binary)
        self.pidfile = pidfile or default_pidfile()
        self.config_path = config_path
        self.last_error: Optional[str] = None

    @staticmethod
    def _resolve_binary(name: str) -> str:
        """Find the rd-server executable.

        Priority:
          1. ``shutil.which(name)`` — on PATH (the common case after install).
          2. Next to *this* process's executable — when rd-server-gui is a
             Nuitka onefile binary installed alongside rd-server in the same
             bin/ dir, but that dir isn't on PATH (e.g. a sudo launch where
             only /usr/local/bin has the rd-server-gui symlink but rd-server
             was missed).
          3. The bare name as a last resort (so the error message is clear).
        """
        found = shutil.which(name)
        if found:
            return found
        import sys as _sys
        exe_dir = os.path.dirname(os.path.abspath(getattr(_sys, "executable", "")))
        if exe_dir:
            candidate = os.path.join(exe_dir, name)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return name

    def is_managed(self) -> bool:
        return shutil.which(self.binary) is not None or os.path.exists(self.binary)

    def state(self) -> ServerState:
        st = daemon_status(self.pidfile)
        return ServerState(
            state=st.state,
            pid=st.pid,
            port=st.port,
            host=st.host,
            autostart=False,           # daemon mode has no boot autostart
            managed_by="daemon",
        )

    def _cmd(self, *extra: str) -> list[str]:
        return [self.binary, *extra]

    def _resolve_log_file(self) -> str:
        """Where the spawned server's stdout/stderr go.

        Prefer the operator's ``log_file``; otherwise keep the log next to the
        pidfile (``~/.config/ssh-remote-desktop/rd-server.log`` or
        ``/var/log/...`` under root) so a failed start always leaves a trace
        instead of vanishing into /dev/null.
        """
        if self.cfg.log_file:
            return os.path.expanduser(self.cfg.log_file)
        return default_log_file()

    def _build_foreground_cmd(self, log_file: str) -> list[str]:
        """Build a ``--foreground`` command for the server.

        We deliberately do NOT use ``rd-server --daemon`` here: the in-process
        double-fork breaks a Nuitka onefile (the bootstrap tears down its temp
        extraction when the fork's parent ``os._exit``s, killing the daemon
        grandchild whose stdio is already redirected — so the failure is
        silent). Instead we run ``--foreground`` ourselves in a new session and
        redirect its output to ``log_file``; that keeps the onefile extraction
        alive for the child's whole lifetime and lets us read the child's stderr
        back to explain a failed start.

        ``--log-file`` is intentionally NOT passed: we redirect the child's
        stderr to ``log_file`` ourselves, so a second ``FileHandler`` would
        duplicate every line.

        All ServerGuiConfig fields that the server honours are forwarded as CLI
        overrides so the panel's settings actually reach the daemon. We ALSO pass
        ``--config <path>`` when a config file is known: file-only knobs the GUI
        does not expose (host_key, pam_service, clipboard/files toggles, jpeg
        quality, …) then come from the file, while the CLI flags below override
        the file per the precedence rule (defaults < file < CLI). Without these
        flags the daemon used to start with ServerConfig *defaults*
        (allow_password=True, run_as_user=True) that silently require root —
        the "запущен, но не работает" symptom.
        """
        cmd: list[str] = [self.binary, "--foreground", "--pidfile", self.pidfile]
        # --config first: file is the source of truth for non-form fields, then
        # the CLI flags below override the form-controlled ones.
        if self.config_path:
            cmd += ["--config", self.config_path]
        cmd += [
            "--host", self.cfg.host, "--port", str(self.cfg.port),
            "--backend", self.cfg.backend, "--codec", self.cfg.codec,
            "--max-sessions", str(self.cfg.max_sessions),
            "--idle-timeout", str(self.cfg.idle_timeout),
            "--shared-dir", self.cfg.shared_dir,
            "--fps", str(self.cfg.fps),
            "--bitrate-kbps", str(self.cfg.bitrate_kbps),
        ]
        # Auth / privilege toggles — these MUST reach the daemon or it starts
        # with root-requiring defaults and silently rejects every login.
        cmd += ["--allow-password" if self.cfg.allow_password else "--no-allow-password"]
        cmd += ["--allow-publickey" if self.cfg.allow_publickey else "--no-allow-publickey"]
        cmd += ["--run-as-user" if self.cfg.run_as_user else "--no-run-as-user"]
        if self.cfg.log_level:
            cmd += ["--log-level", self.cfg.log_level]
        return cmd

    def start(self) -> bool:
        # Check the binary exists before spawning — gives a clear error instead
        # of an opaque "Failed to start" from a missing rd-server.
        if not (shutil.which(self.binary) or os.path.exists(self.binary)):
            self.last_error = (
                f"rd-server binary not found: '{self.binary}' is neither on "
                f"PATH nor next to this panel. Re-run the installer "
                f"(install-server-linux.sh) or check your PATH."
            )
            return False

        # Refuse to start a second instance: a live pidfile means a server is
        # already running (often on the old port after a config change). Do NOT
        # delete a live pidfile — that would orphan the running process and make
        # `rd-server --stop` impossible. Stale pidfiles (dead / recycled PID)
        # are cleaned up by live_pid_from_pidfile itself.
        existing = live_pid_from_pidfile(self.pidfile)
        if existing is not None:
            self.last_error = (
                f"rd-server уже запущен (pid {existing}) — сначала остановите "
                f"его кнопкой «Стоп» либо используйте «Перезапуск»."
            )
            return False

        # Port pre-check: surface a collision as an actionable error BEFORE we
        # spawn a child that would just exit in the grace window. This is what
        # made "поменял порт на 2224 — не стартует" look like a mute failure:
        # the new server couldn't bind because the old one (or another process)
        # still held the port, and the panel only showed «Остановлен».
        if not _port_bindable(self.cfg.host, self.cfg.port):
            holder = _port_listener_pid(self.cfg.port)
            if holder and holder != os.getpid():
                self.last_error = (
                    f"порт {self.cfg.port} занят процессом PID {holder} "
                    f"(не наш rd-server). Освободите его: "
                    f"sudo ss -tlnp | grep :{self.cfg.port}"
                )
            else:
                self.last_error = (
                    f"порт {self.cfg.port} занят. Найдите и остановите "
                    f"держатель: sudo ss -tlnp | grep :{self.cfg.port} "
                    f"или выберите свободный порт в настройках."
                )
            return False

        log_file = self._resolve_log_file()
        log_dir = os.path.dirname(os.path.abspath(log_file))
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_fh = open(log_file, "ab", buffering=0)
        except OSError as exc:
            self.last_error = f"cannot open server log {log_file}: {exc}"
            return False

        cmd = self._build_foreground_cmd(log_file)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,   # detach: survives the GUI closing
                close_fds=True,
            )
        except OSError as exc:
            log_fh.close()
            self.last_error = f"cannot launch rd-server: {exc}"
            return False

        # Grace window: an early crash (missing module, port in use, perms)
        # almost always happens within the first few seconds. If the child is
        # still alive AND has written the pidfile, the listener is up. We read
        # the child's stderr back from the log so a failed start is explained
        # in the panel instead of shown as a bare "Остановлен".
        grace = 4.0
        deadline = time.monotonic() + grace
        ok = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = tail_log(log_file, n=40).strip()
                self.last_error = tail or (
                    f"rd-server exited with code {proc.returncode} "
                    f"(no error output)."
                )
                remove_pidfile(self.pidfile)
                log_fh.close()
                return False
            if os.path.exists(self.pidfile):
                ok = True
                break
            time.sleep(0.1)
        # Closing the parent's fd object is safe: the child inherited its own
        # dup via stdout/stderr, so it keeps writing to the log after we exit.
        log_fh.close()
        self.last_error = None if ok else (
            f"rd-server launched (pid {proc.pid}) but did not write its pidfile "
            f"within {grace:.0f}s — check {log_file}."
        )
        return ok

    def _reap_stray_on_port(self) -> None:
        """Best-effort: SIGTERM an rd-server still holding our configured port.

        A pidfile-based :meth:`stop` is the primary path, but a previous crash
        or a hand-deleted pidfile can leave an orphan listening on the port. We
        look it up with ``ss`` and, when it looks like one of our processes
        (``rd-server`` / a python dev run) and is NOT us, SIGTERM it so the
        fresh start can bind. Non-rd-server holders are left alone: the
        port-busy branch in :meth:`start` reports them to the operator instead.
        """
        pid = _port_listener_pid(self.cfg.port)
        if not pid or pid == os.getpid():
            return
        if not is_pid_alive(pid) or not is_likely_rd_server(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Give it a moment to release the port; don't block the panel long.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not is_pid_alive(pid):
                break
            time.sleep(0.1)

    def restart(self) -> bool:
        """Guarantee the old server is gone, then start a fresh one.

        Overrides the base ``stop() and start()`` so a port/config change
        reliably tears down the previous process first: the pidfile stop kills
        the recorded process, and :meth:`_reap_stray_on_port` catches an orphan
        whose pidfile was lost. Only then do we :meth:`start`.
        """
        self.stop()
        self._reap_stray_on_port()
        return self.start()

    def stop(self) -> bool:
        return daemon_stop(self.pidfile)

    def enable_autostart(self, on: bool) -> bool:
        # Daemon mode can't do boot autostart on its own — that's what the
        # systemd unit is for. The GUI surfaces this in the toggle tooltip.
        return False


def pick_controller(cfg: ServerGuiConfig, *,
                    config_path: Optional[str] = None) -> ServiceController:
    """Return the most appropriate controller for the current machine."""
    if _have_systemctl() and unit_installed():
        return SystemdController()
    return DaemonController(cfg, config_path=config_path)


# --------------------------------------------------------------------------- #
# Log tailing
# --------------------------------------------------------------------------- #
def tail_log(path: str, n: int = 200) -> str:
    """Return up to the last ``n`` lines of ``path`` (or '' if unreadable)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-n:])


def journalctl_tail(unit: str = UNIT_NAME, n: int = 200) -> str:
    """Tail the unit's journald logs (returns '' if journalctl unavailable)."""
    if not shutil.which("journalctl"):
        return ""
    r = _run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "--plain"])
    return r.stdout


# --------------------------------------------------------------------------- #
# Per-user GUI settings (window geometry, theme, language, tray prefs) — kept
# in a tiny JSON sidecar so we don't pollute server.toml with UI prefs.
# --------------------------------------------------------------------------- #
GUI_PREFS_NAME = "server-gui.json"


def gui_prefs_path() -> str:
    return os.path.expanduser(f"~/.config/ssh-remote-desktop/{GUI_PREFS_NAME}")


@dataclass
class GuiPrefs:
    theme: str = "system"            # "light" | "dark" | "system"
    language: str = "ru"             # "ru" | "en"
    minimize_to_tray: bool = False
    last_geometry: Optional[dict] = None
    _path: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_path", None)
        return {k: v for k, v in d.items() if not k.startswith("_")}

    @classmethod
    def load(cls, path: Optional[str] = None) -> "GuiPrefs":
        path = path or gui_prefs_path()
        prefs = cls(_path=path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return prefs
        for f in fields(cls):
            if f.name.startswith("_"):
                continue
            if f.name in data and data[f.name] is not None:
                setattr(prefs, f.name, data[f.name])
        return prefs

    def save(self) -> None:
        path = self._path or gui_prefs_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
