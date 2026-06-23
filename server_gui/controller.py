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
import shutil
import subprocess
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

try:
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

# Reuse the daemon helpers (pidfile format, status, stop).
from server.daemon import (
    default_pidfile,
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
CODECS = ("h264", "h265", "jpeg", "webp")
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


def _have_systemctl() -> bool:
    return shutil.which("systemctl") is not None


def unit_installed() -> bool:
    return os.path.exists(SYSTEMD_UNIT_PATH)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", *args])


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
        return _systemctl("start", UNIT_NAME).returncode == 0

    def stop(self) -> bool:
        return _systemctl("stop", UNIT_NAME).returncode == 0

    def restart(self) -> bool:
        return _systemctl("restart", UNIT_NAME).returncode == 0

    def enable_autostart(self, on: bool) -> bool:
        if on:
            return _systemctl("enable", UNIT_NAME).returncode == 0
        return _systemctl("disable", UNIT_NAME).returncode == 0


class DaemonController(ServiceController):
    """Manages the server via ``rd-server --daemon/--stop/--status``.

    Used when the systemd unit isn't installed. The pidfile + port come from
    the loaded config so status reports the right port.
    """

    name = "daemon"

    def __init__(self, cfg: ServerGuiConfig, *, binary: str = "rd-server",
                 pidfile: Optional[str] = None):
        self.cfg = cfg
        self.binary = self._resolve_binary(binary)
        self.pidfile = pidfile or default_pidfile()
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
        cmd = self._cmd(
            "--daemon", "--pidfile", self.pidfile,
            "--host", self.cfg.host, "--port", str(self.cfg.port),
            "--backend", self.cfg.backend, "--codec", self.cfg.codec,
            "--max-sessions", str(self.cfg.max_sessions),
            "--idle-timeout", str(self.cfg.idle_timeout),
            "--shared-dir", self.cfg.shared_dir,
            "--fps", str(self.cfg.fps),
        )
        if self.cfg.log_file:
            cmd += ["--log-file", self.cfg.log_file]
        if not self.cfg.allow_password:
            # The CLI only has --no-clipboard/--no-files; password toggle is
            # config-file only. Skip rather than emit a flag the parser
            # doesn't know.
            pass
        r = _run(cmd, timeout=10.0)
        if r.returncode != 0:
            self.last_error = (r.stderr or r.stdout or "").strip() or (
                f"rd-server exited with code {r.returncode} (no error output)."
            )
        else:
            self.last_error = None
        return r.returncode == 0

    def stop(self) -> bool:
        return daemon_stop(self.pidfile)

    def enable_autostart(self, on: bool) -> bool:
        # Daemon mode can't do boot autostart on its own — that's what the
        # systemd unit is for. The GUI surfaces this in the toggle tooltip.
        return False


def pick_controller(cfg: ServerGuiConfig) -> ServiceController:
    """Return the most appropriate controller for the current machine."""
    if _have_systemctl() and unit_installed():
        return SystemdController()
    return DaemonController(cfg)


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
