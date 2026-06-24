"""Unit tests for ``server_gui.controller`` (Qt-free config + service logic).

The controller is deliberately split out of the Qt layer so these can run
headless. Covers: secret-dropping, config round-trip, validation, controller
selection, log tailing, and GUI prefs persistence.
"""
from __future__ import annotations

import os
import sys
import textwrap
import re

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server_gui import controller as ctrl
from server_gui.controller import (
    BACKENDS, CODECS, ConfigController, ConfigError, GuiPrefs,
    LOG_LEVELS, ServerGuiConfig, ServiceController, _has_sensitive_key,
    dumps_toml, pick_controller, tail_log,
)


# --------------------------------------------------------------------------- #
# ServerGuiConfig defaults
# --------------------------------------------------------------------------- #
def test_default_config_values():
    c = ServerGuiConfig()
    assert c.port == 2222
    assert c.backend == "auto"
    assert c.codec == "h264"
    assert c.allow_password is True
    assert c.allow_publickey is True
    assert c.log_level == "INFO"


def test_config_to_dict_has_no_secrets():
    d = ServerGuiConfig().to_dict()
    # No key in the dict may be a secret key.
    assert all(not _has_sensitive_key(k) for k in d)
    assert "password" not in d and "private_key" not in d


# --------------------------------------------------------------------------- #
# _has_sensitive_key — the exact-match filter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key,expected", [
    ("password", True),
    ("private_key", True),
    ("host_key", True),
    ("token", True),
    ("secret", True),
    # Boolean toggles that merely *mention* a secret must NOT be stripped.
    ("allow_password", False),
    ("allow_publickey", False),
    # Normal fields.
    ("port", False),
    ("host", False),
    ("codec", False),
    # Dash-normalized forms.
    ("private-key", True),
    ("host-key", True),
])
def test_sensitive_key_filter(key, expected):
    assert _has_sensitive_key(key) is expected


# --------------------------------------------------------------------------- #
# dumps_toml
# --------------------------------------------------------------------------- #
def test_dumps_toml_scalars():
    s = dumps_toml({"port": 2222, "host": "0.0.0.0", "on": True,
                    "off": False, "lst": [1, 2], "rate": 1.5})
    assert 'port = 2222' in s
    assert 'host = "0.0.0.0"' in s
    assert 'on = true' in s
    assert 'off = false' in s
    assert 'lst = [1, 2]' in s


def test_dumps_toml_escapes_quotes_and_backslashes():
    s = dumps_toml({"path": 'C:\\foo"bar'})
    assert 'path = "C:\\\\foo\\"bar"' in s


# --------------------------------------------------------------------------- #
# ConfigController.load
# --------------------------------------------------------------------------- #
def test_load_missing_file_returns_defaults(tmp_path):
    c = ConfigController(str(tmp_path / "nope.toml")).load()
    assert c == ServerGuiConfig()


def test_load_known_keys(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(textwrap.dedent("""\
        port = 9999
        backend = "wayland"
        codec = "jpeg"
        max_sessions = 5
        allow_password = false
    """), encoding="utf-8")
    c = ConfigController(str(p)).load()
    assert c.port == 9999
    assert c.backend == "wayland"
    assert c.codec == "jpeg"
    assert c.max_sessions == 5
    assert c.allow_password is False


def test_load_drops_unknown_and_sensitive_keys(tmp_path):
    p = tmp_path / "s.toml"
    # 'password' and 'unknown_field' must be silently ignored.
    p.write_text(textwrap.dedent("""\
        port = 31337
        password = "hunter2"
        unknown_field = "ignored"
    """), encoding="utf-8")
    c = ConfigController(str(p)).load()
    assert c.port == 31337
    assert not hasattr(c, "password")
    assert not hasattr(c, "unknown_field")


def test_load_normalizes_dashed_keys(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('idle-timeout = 99\nshared-dir = "/tmp"\n', encoding="utf-8")
    c = ConfigController(str(p)).load()
    assert c.idle_timeout == 99
    assert c.shared_dir == "/tmp"


def test_load_malformed_raises(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("this is = = not toml [[[", encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigController(str(p)).load()


# --------------------------------------------------------------------------- #
# ConfigController.validate
# --------------------------------------------------------------------------- #
def test_validate_valid_config():
    c = ServerGuiConfig(shared_dir="shared")  # relative → deferred (no fs check)
    assert ConfigController.validate(c) == []


def test_validate_port_out_of_range():
    c = ServerGuiConfig(port=0)
    assert any("port" in e for e in ConfigController.validate(c))
    c = ServerGuiConfig(port=70000)
    assert any("port" in e for e in ConfigController.validate(c))


def test_validate_bad_backend():
    c = ServerGuiConfig(backend="nope")
    assert any("backend" in e for e in ConfigController.validate(c))


def test_validate_bad_codec():
    c = ServerGuiConfig(codec="mp3")
    assert any("codec" in e for e in ConfigController.validate(c))


def test_validate_bad_log_level():
    c = ServerGuiConfig(log_level="VERBOSE")
    assert any("log_level" in e for e in ConfigController.validate(c))


def test_validate_negative_limits():
    for field in ("max_sessions", "idle_timeout", "fps", "bitrate_kbps"):
        c = ServerGuiConfig(**{field: -1})
        assert any(field in e for e in ConfigController.validate(c)), field


def test_validate_shared_dir_absolute_must_exist():
    c = ServerGuiConfig(shared_dir="/this/path/does/not/exist/xyz")
    errs = ConfigController.validate(c)
    assert any("shared_dir" in e for e in errs)


def test_validate_enums_match_constants():
    # Every advertised backend/codec/log_level must validate cleanly.
    for b in BACKENDS:
        assert ConfigController.validate(ServerGuiConfig(backend=b, shared_dir="shared")) == []
    for co in CODECS:
        assert ConfigController.validate(ServerGuiConfig(codec=co, shared_dir="shared")) == []
    for ll in LOG_LEVELS:
        assert ConfigController.validate(ServerGuiConfig(log_level=ll, shared_dir="shared")) == []


# --------------------------------------------------------------------------- #
# ConfigController.save — round-trip + secret guarantee
# --------------------------------------------------------------------------- #
def test_save_and_reload_round_trip(tmp_path):
    p = str(tmp_path / "rt.toml")
    orig = ServerGuiConfig(port=4242, backend="x11", codec="h265",
                           max_sessions=3, fps=15, allow_password=False,
                           log_level="DEBUG", shared_dir="shared")
    ConfigController(p).save(orig)
    loaded = ConfigController(p).load()
    assert loaded == orig


def test_save_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "deep" / "dir" / "s.toml")
    ConfigController(p).save(ServerGuiConfig(shared_dir="shared"))
    assert os.path.exists(p)


def test_save_invalid_raises(tmp_path):
    p = str(tmp_path / "bad.toml")
    with pytest.raises(ConfigError):
        ConfigController(p).save(ServerGuiConfig(port=0))


def test_save_never_persists_secrets(tmp_path):
    """Load a file containing a stray secret, save it back; the secret must
    not survive the round-trip — the GUI must never be a way to persist one."""
    p = str(tmp_path / "secret.toml")
    src = tmp_path / "src.toml"
    src.write_text(textwrap.dedent("""\
        port = 2200
        shared_dir = "shared"
        password = "hunter2"
        private_key = "/secret/key"
    """), encoding="utf-8")
    c = ConfigController(str(src)).load()
    ConfigController(p).save(c)
    saved = (tmp_path / "secret.toml").read_text(encoding="utf-8")
    assert "hunter2" not in saved          # the secret value is gone
    # The bare secret *keys* must not be TOML keys — but the boolean toggles
    # ``allow_password`` / ``allow_publickey`` are fine (they're not secrets),
    # so match only a line whose key is exactly "password" / "private_key".
    assert not re.search(r"(?m)^\s*password\s*=", saved)
    assert not re.search(r"(?m)^\s*private_key\s*=", saved)
    assert "port = 2200" in saved


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "atom.toml")
    ConfigController(p).save(ServerGuiConfig(shared_dir="shared"))
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith("atom.toml.tmp")]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# pick_controller
# --------------------------------------------------------------------------- #
def test_pick_controller_returns_daemon_when_unit_absent(tmp_path):
    # No systemd unit installed in this environment -> daemon fallback.
    c = ServerGuiConfig()
    ctl = pick_controller(c)
    assert isinstance(ctl, ServiceController)
    # Either systemd (if a unit were present) or daemon; here it must be daemon.
    assert ctl.name == "daemon"


def test_daemon_controller_state_stopped_when_no_pidfile(tmp_path):
    c = ServerGuiConfig()
    ctl = ctrl.DaemonController(c, pidfile=str(tmp_path / "none.pid"))
    st = ctl.state()
    assert st.state == "stopped"
    assert st.managed_by == "daemon"


def test_daemon_controller_stop_nothing_running(tmp_path):
    c = ServerGuiConfig()
    ctl = ctrl.DaemonController(c, pidfile=str(tmp_path / "none.pid"))
    assert ctl.stop() is False


def test_daemon_controller_enable_autostart_unsupported():
    c = ServerGuiConfig()
    ctl = ctrl.DaemonController(c, pidfile="/tmp/none.pid")
    assert ctl.enable_autostart(True) is False


# --------------------------------------------------------------------------- #
# tail_log
# --------------------------------------------------------------------------- #
def test_tail_log_returns_last_n(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text("\n".join(f"line{i}" for i in range(50)) + "\n",
                 encoding="utf-8")
    out = tail_log(str(p), n=5)
    assert out.splitlines() == ["line45", "line46", "line47", "line48", "line49"]


def test_tail_log_missing_file_returns_empty(tmp_path):
    assert tail_log(str(tmp_path / "nope.log")) == ""


# --------------------------------------------------------------------------- #
# GuiPrefs
# --------------------------------------------------------------------------- #
def test_gui_prefs_defaults():
    p = GuiPrefs()
    assert p.theme == "system"
    assert p.language == "ru"
    assert p.minimize_to_tray is False


def test_gui_prefs_round_trip(tmp_path):
    path = str(tmp_path / "prefs.json")
    p = GuiPrefs(theme="dark", language="en", minimize_to_tray=True,
                 _path=path)
    p.save()
    loaded = GuiPrefs.load(path)
    assert loaded.theme == "dark"
    assert loaded.language == "en"
    assert loaded.minimize_to_tray is True


def test_gui_prefs_load_missing_returns_defaults(tmp_path):
    p = GuiPrefs.load(str(tmp_path / "none.json"))
    assert p.theme == "system"
    assert p.language == "ru"


def test_gui_prefs_load_malformed_returns_defaults(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    p = GuiPrefs.load(path)
    assert p.theme == "system"


def test_gui_prefs_to_dict_excludes_private_path():
    p = GuiPrefs(_path="/secret")
    d = p.to_dict()
    assert "_path" not in d


# --------------------------------------------------------------------------- #
# DaemonController.start — foreground-spawn (onefile-safe) path
# --------------------------------------------------------------------------- #
def _write_stub_server(path: str, *, fail: bool = False) -> None:
    """Write a tiny stub that mimics `rd-server --foreground --pidfile P ...`.

    Success mode: writes the pidfile with its own pid then sleeps (keeps the
    listener 'up' so the controller's grace window sees the pidfile). Failure
    mode: prints a diagnostic to stderr and exits 1 immediately, so the
    controller's early-exit detection surfaces last_error.
    """
    import sys
    import textwrap
    fail_flag = "True" if fail else "False"
    code = "#!" + sys.executable + "\n" + textwrap.dedent(
        """
        import json, os, sys, time
        pidfile = None
        for i, a in enumerate(sys.argv):
            if a == "--pidfile" and i + 1 < len(sys.argv):
                pidfile = sys.argv[i + 1]
        if __FAIL__:
            sys.stderr.write("STUB: cannot bind port 2222: address already in use\\n")
            sys.stderr.flush()
            sys.exit(1)
        if pidfile:
            os.makedirs(os.path.dirname(os.path.abspath(pidfile)) or ".", exist_ok=True)
            with open(pidfile, "w") as f:
                json.dump({"pid": os.getpid(), "port": 2222, "host": "0.0.0.0"}, f)
        time.sleep(30)
        """
    ).replace("__FAIL__", fail_flag)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    os.chmod(path, 0o755)


def test_daemon_controller_start_foreground_spawn_success(tmp_path):
    stub = str(tmp_path / "rd-server")
    _write_stub_server(stub, fail=False)
    pidfile = str(tmp_path / "run.pid")
    c = ServerGuiConfig()
    # log_file must be writable so the controller can capture child stderr.
    c.log_file = str(tmp_path / "rd.log")
    ctl = ctrl.DaemonController(c, binary=stub, pidfile=pidfile)
    try:
        ok = ctl.start()
        assert ok is True
        assert ctl.last_error is None
        # The child wrote the pidfile once its (stub) listener came up.
        assert os.path.exists(pidfile)
    finally:
        # Clean up the stub child we spawned.
        import subprocess as _sp
        _sp.run(["pkill", "-f", stub], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)


def test_daemon_controller_start_foreground_spawn_failure(tmp_path):
    stub = str(tmp_path / "rd-server")
    _write_stub_server(stub, fail=True)
    pidfile = str(tmp_path / "run.pid")
    c = ServerGuiConfig()
    c.log_file = str(tmp_path / "rd.log")
    ctl = ctrl.DaemonController(c, binary=stub, pidfile=pidfile)
    ok = ctl.start()
    assert ok is False
    # The early-exit cause (from the child's stderr, captured into the log)
    # must be surfaced — not silent "Остановлен".
    assert ctl.last_error is not None
    assert "address already in use" in ctl.last_error or "exited" in ctl.last_error
    assert not os.path.exists(pidfile)  # failed start cleans the pidfile


def test_daemon_controller_start_missing_binary(tmp_path):
    c = ServerGuiConfig()
    pidfile = str(tmp_path / "run.pid")
    ctl = ctrl.DaemonController(c, binary=str(tmp_path / "no-such-binary"),
                                pidfile=pidfile)
    ok = ctl.start()
    assert ok is False
    assert ctl.last_error is not None
    assert "not found" in ctl.last_error.lower() or "no such" in ctl.last_error.lower()


# --------------------------------------------------------------------------- #
# privilege_warning — TASK 4 banner source
# --------------------------------------------------------------------------- #
def test_privilege_warning_none_when_root(monkeypatch):
    # As root, no warning regardless of auth toggles.
    monkeypatch.setattr(ctrl.os, "geteuid", lambda: 0, raising=False)
    c = ServerGuiConfig(allow_password=True, run_as_user=True)
    assert ctrl.privilege_warning(c) is None


def test_privilege_warning_when_unprivileged_and_password_on(monkeypatch):
    monkeypatch.setattr(ctrl.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(ctrl.os, "getgroups", lambda: [1000], raising=False)
    c = ServerGuiConfig(allow_password=True, run_as_user=False)
    w = ctrl.privilege_warning(c)
    assert w is not None
    assert "root" in w.lower()
    assert "shadow" in w.lower()


def test_privilege_warning_when_unprivileged_and_run_as_user(monkeypatch):
    monkeypatch.setattr(ctrl.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(ctrl.os, "getgroups", lambda: [1000], raising=False)
    c = ServerGuiConfig(allow_password=False, run_as_user=True)
    w = ctrl.privilege_warning(c)
    assert w is not None
    assert "root" in w.lower()


def test_privilege_warning_none_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(ctrl.os, "geteuid", lambda: 1000, raising=False)
    c = ServerGuiConfig(allow_password=False, run_as_user=False)
    assert ctrl.privilege_warning(c) is None
