"""Config loading: defaults, file overrides, and CLI overrides."""

from pathlib import Path

from common.config import load_client_config, load_server_config


def test_server_defaults():
    cfg = load_server_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 2222
    assert cfg.backend in {"auto", "x11", "wayland"}
    assert cfg.codec in {"h264", "jpeg"}


def test_client_defaults():
    cfg = load_client_config()
    assert cfg.auth in {"key", "password", "agent"}


def test_overrides_win(tmp_path: Path):
    cfg_file = tmp_path / "server.toml"
    cfg_file.write_text("port = 5555\nbackend = 'wayland'\ncodec = 'jpeg'\n")
    cfg = load_server_config(str(cfg_file))
    assert cfg.port == 5555
    assert cfg.backend == "wayland"
    assert cfg.codec == "jpeg"


def test_env_var_points_to_file(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "srv.json"
    cfg_file.write_text('{"port": 4000, "fps": 60}')
    monkeypatch.setenv("RD_SERVER_CONFIG", str(cfg_file))
    cfg = load_server_config()
    assert cfg.port == 4000
    assert cfg.fps == 60
