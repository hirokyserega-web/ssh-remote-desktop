"""Configuration loading for both client and server.

Configuration is layered: built-in defaults <- config file (TOML/JSON/INI)
<- command-line arguments. Both the client and the server share this module so
the precedence rules and file discovery behave identically.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib  # type: ignore
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore


# ---------------------------------------------------------------------------
# Server config
# ---------------------------------------------------------------------------
@dataclass
class ServerConfig:
    # Network
    host: str = "0.0.0.0"
    port: int = 2222

    # Host key for the SSH server (generated on first run if missing).
    host_key: str = "~/.config/ssh-remote-desktop/host_ed25519"

    # Backend selection: "auto" | "x11" | "wayland".
    backend: str = "auto"

    # Session model.
    max_sessions: int = 10
    idle_timeout: int = 600          # seconds; 0 disables
    persistent_default: bool = False  # keep sessions for reconnect by default
    session_geometry: tuple[int, int] = (1920, 1080)
    session_depth: int = 24

    # Encoding.
    codec: str = "h264"              # falls back to jpeg if pyav missing
    fps: int = 30
    bitrate_kbps: int = 6000
    jpeg_quality: int = 80
    cursor_mode: str = "embedded"    # "embedded" | "metadata"

    # X11 backend.
    xvfb_bin: str = "Xvfb"
    window_manager: str = ""          # optional WM/DE command (e.g. "startplasma-x11", "openbox")

    # Wayland backend.
    wayland_compositor: str = "sway"  # "sway" | "weston" | "kwin" | "gnome"
    use_uinput: bool = True           # input emulation via /dev/uinput

    # Clipboard.
    clipboard_enabled: bool = True
    clipboard_max_bytes: int = 1 * 1024 * 1024

    # File sharing / SFTP jail.
    files_enabled: bool = True
    shared_dir: str = "~/shared"      # relative to each user's HOME
    sftp_chunk_size: int = 256 * 1024

    # Auth.
    allow_password: bool = True
    allow_publickey: bool = True
    run_as_user: bool = True          # drop privileges to the target user
    # PAM service name used by server/auth.check_password (default "login").
    # Override to e.g. "sshd" / "system-auth" when your distro's PAM policy
    # for "login" is too strict or missing. Has no effect when
    # allow_password=false or python-pam is unavailable.
    pam_service: str = "login"

    # Logging.
    log_level: str = "INFO"
    log_file: str = ""

    @staticmethod
    def _coerce(name: str, value: Any) -> Any:
        if name == "session_geometry" and isinstance(value, (list, tuple)):
            return (int(value[0]), int(value[1]))
        return value


# ---------------------------------------------------------------------------
# Client config
# ---------------------------------------------------------------------------
@dataclass
class ClientConfig:
    host: str = ""
    port: int = 2222
    user: str = ""

    # Auth: "key" | "password" | "agent".
    auth: str = "key"
    key_path: str = "~/.config/ssh-remote-desktop/id_ed25519"
    known_hosts: str = "~/.config/ssh-remote-desktop/known_hosts"
    accept_unknown_host: bool = False  # TOFU prompt handled in UI when False

    # Session request.
    new_session: bool = True
    persistent: bool = False
    geometry: tuple[int, int] = (1920, 1080)
    codec: str = "h264"

    # Display / rendering.
    qt_platform: str = "auto"          # auto|xcb|wayland (sets QT_QPA_PLATFORM)
    start_fullscreen: bool = False
    scale_to_window: bool = True
    hidpi: bool = True

    # Clipboard / files toggles (privacy).
    clipboard_enabled: bool = True
    clipboard_max_bytes: int = 1 * 1024 * 1024
    files_enabled: bool = True
    local_shared_dir: str = "~/ssh-remote-desktop-shared"

    # Reconnection.
    auto_reconnect: bool = True
    reconnect_delay: float = 2.0
    max_reconnect_attempts: int = 0    # 0 == infinite

    # UI preferences.
    theme: str = "system"               # "light" | "dark" | "system"
    language: str = "ru"                # "ru" | "en"
    jpeg_quality: int = 80              # default JPEG quality for the connect dialog

    log_level: str = "INFO"

    @staticmethod
    def _coerce(name: str, value: Any) -> Any:
        if name == "geometry" and isinstance(value, (list, tuple)):
            return (int(value[0]), int(value[1]))
        return value


def _warn_parse_failure(path: Path, exc: Exception) -> None:
    """Log a parse failure and fall back to defaults instead of crashing.

    A corrupted/empty/half-written config file must never prevent the app from
    launching — the user just gets the built-in defaults and a warning telling
    them which file to fix. Printed to stderr (logger isn't set up yet at config
    load time).
    """
    import sys
    print(f"WARNING: ignoring unreadable config {path}: {exc}. "
          f"Using built-in defaults. Fix or remove the file to silence this.",
          file=sys.stderr)


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    # An empty file is a valid "no overrides" config — don't hand an empty
    # string to the parsers (tomllib rejects it as "Invalid statement at
    # line 1, column 1", which is exactly the crash users saw).
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn_parse_failure(path, exc)
        return {}
    if not text.strip():
        return {}
    suffix = path.suffix.lower()
    if suffix in (".toml", ".tml") and tomllib is not None:
        try:
            return tomllib.loads(text)
        except Exception as exc:
            _warn_parse_failure(path, exc)
            return {}
    if suffix in (".json", ".js"):
        try:
            return json.loads(text)
        except Exception as exc:
            _warn_parse_failure(path, exc)
            return {}
    # Fallback: try JSON then TOML.
    try:
        return json.loads(text)
    except Exception:
        if tomllib is not None:
            try:
                return tomllib.loads(text)
            except Exception as exc:
                _warn_parse_failure(path, exc)
        else:
            _warn_parse_failure(path, ValueError("no TOML/JSON parser available"))
    return {}


def _apply(cfg, data: dict):
    names = {f.name for f in fields(cfg)}
    for key, value in data.items():
        key = key.replace("-", "_")
        if key in names and value is not None:
            setattr(cfg, key, type(cfg)._coerce(key, value))
    return cfg


def load_server_config(path: str | None = None, overrides: dict | None = None) -> ServerConfig:
    cfg = ServerConfig()
    candidates = [path] if path else [
        os.environ.get("RD_SERVER_CONFIG"),
        "./server.toml",
        "~/.config/ssh-remote-desktop/server.toml",
        "/etc/ssh-remote-desktop/server.toml",
    ]
    for cand in candidates:
        if cand:
            p = Path(os.path.expanduser(cand))
            if p.exists():
                _apply(cfg, _read_file(p))
                break
    if overrides:
        _apply(cfg, {k: v for k, v in overrides.items() if v is not None})
    return cfg


def load_client_config(path: str | None = None, overrides: dict | None = None) -> ClientConfig:
    cfg = ClientConfig()
    candidates = [path] if path else [
        os.environ.get("RD_CLIENT_CONFIG"),
        "./client.toml",
        "~/.config/ssh-remote-desktop/client.toml",
    ]
    for cand in candidates:
        if cand:
            p = Path(os.path.expanduser(cand))
            if p.exists():
                _apply(cfg, _read_file(p))
                break
    if overrides:
        _apply(cfg, {k: v for k, v in overrides.items() if v is not None})
    return cfg


def to_dict(cfg) -> dict:
    return asdict(cfg)
