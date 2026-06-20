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


# ---- resilience: corrupted / empty / unreadable config ------------------- #
# A config file that can't be parsed must never crash the app on launch — it
# should fall back to built-in defaults and emit a warning. This guards the
# "rd-client crashed with tomllib.TOMLDecodeError: Invalid statement at line 1,
# column 1" regression where an empty/garbage client.toml aborted startup.

def test_corrupted_toml_falls_back_to_defaults(tmp_path, capsys):
    cfg_file = tmp_path / "client.toml"
    cfg_file.write_text("this is not valid toml at all === {{{")
    cfg = load_client_config(str(cfg_file))
    # Must NOT raise — defaults returned.
    assert cfg.auth in {"key", "password", "agent"}
    captured = capsys.readouterr()
    assert "ignoring unreadable config" in captured.err
    assert str(cfg_file) in captured.err


def test_empty_config_file_falls_back_to_defaults(tmp_path, capsys):
    """An empty (whitespace-only) .toml is a valid 'no overrides' config.

    Previously tomllib.loads("") raised "Invalid statement at line 1, column 1"
    — the exact crash in the user's traceback. Empty must yield defaults with
    no warning (it's not malformed, just empty).
    """
    cfg_file = tmp_path / "client.toml"
    cfg_file.write_text("   \n\n  \t\n")
    cfg = load_client_config(str(cfg_file))
    assert cfg.auth in {"key", "password", "agent"}
    captured = capsys.readouterr()
    # Empty file is benign — no warning expected.
    assert "ignoring unreadable config" not in captured.err


def test_corrupted_json_falls_back_to_defaults(tmp_path, capsys):
    cfg_file = tmp_path / "server.json"
    cfg_file.write_text('{"port": not-a-number}')
    cfg = load_server_config(str(cfg_file))
    assert cfg.port == 2222  # default
    captured = capsys.readouterr()
    assert "ignoring unreadable config" in captured.err


def test_unreadable_config_falls_back_to_defaults(tmp_path, capsys):
    """A permission-denied file shouldn't crash startup either."""
    import os as _os
    import pytest as _pytest
    # Root bypasses file permissions, so this can't be exercised as root.
    if _os.geteuid() == 0:
        _pytest.skip("root reads any file; permission test needs non-root")
    cfg_file = tmp_path / "client.toml"
    cfg_file.write_text("port = 5555")
    cfg_file.chmod(0o000)  # unreadable
    try:
        cfg = load_client_config(str(cfg_file))
        assert cfg.auth in {"key", "password", "agent"}  # defaults
        captured = capsys.readouterr()
        assert "ignoring unreadable config" in captured.err
    finally:
        cfg_file.chmod(0o644)  # restore so cleanup works
