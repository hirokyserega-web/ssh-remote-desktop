"""Tests for scripts/install.sh: parse/help/version/uninstall handling."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = os.path.join(ROOT, "scripts", "install.sh")

pytestmark = pytest.mark.skipif(
    not os.path.exists(INSTALL) or sys.platform == "win32",
    reason="install.sh is a Linux/macOS shell script (no bash on Windows CI)",
)


def _run(args, **kw):
    """Run install.sh with bash, capture output. Never inherit env vars
    that would make the script actually install anything."""
    env = dict(os.environ)
    env.pop("SSH_REMOTE_DESKTOP_DIR", None)
    env.pop("SSH_REMOTE_DESKTOP_COMPONENT", None)
    cmd = ["bash", INSTALL, *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def test_syntax_ok():
    """bash -n must pass (no syntax errors)."""
    r = subprocess.run(["bash", "-n", INSTALL], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_help_lists_flags():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--version" in r.stdout
    assert "--from-source" in r.stdout
    assert "--uninstall" in r.stdout
    assert "--component" in r.stdout


def test_help_shows_usage():
    r = _run(["--help"])
    assert "install.sh" in r.stdout
    assert "Usage" in r.stdout or "usage" in r.stdout.lower()


def test_version_flag_prints_version():
    r = _run(["--version"])
    assert r.returncode == 0
    # The version file should exist and the output should contain it.
    vfile = os.path.join(ROOT, "VERSION")
    if os.path.exists(vfile):
        expected = open(vfile).read().strip()
        assert expected in r.stdout


def test_unknown_flag_errors():
    r = _run(["--no-such-flag"])
    assert r.returncode != 0


def test_verify_sha256_matches_prefixed_paths(tmp_path):
    """verify_sha256 must match a SHA256SUMS line whose filename carries a
    directory prefix (e.g. "staging/<asset>"), not only bare names.

    The Release pipeline emitted "staging/<archive>" entries; the old grep
    (` ${base}$`) required a space before the basename and so missed the
    slash-prefixed form, silently skipping checksum verification.
    """
    import hashlib

    # Fake archive + its real sha256.
    archive = tmp_path / "ssh-remote-desktop-client-linux-x86_64.tar.gz"
    archive.write_bytes(b"hello world")
    digest = hashlib.sha256(b"hello world").hexdigest()
    base = archive.name

    # SHA256SUMS with the historical "staging/" prefix (two-space separator).
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  staging/{base}\n")

    snippet = (
        "set -euo pipefail\n"
        f"source {INSTALL!r}\n"
        "OS=linux\n"
        # Override download_to so verify_sha256 reads our local SHA256SUMS
        # instead of curling the real one from GitHub.
        f"download_to() {{ cp {str(sums)!r} \"$2\"; }}\n"
        f"verify_sha256 {str(archive)!r} '' {base!r}\n"
        'echo "VERIFY_RC=$?"\n'
    )
    r = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True,
        env={**os.environ, "SRD_NO_RUN_MAIN": "1"},
    )
    # verify_sha256 returns 0 when the checksum matches; a skip prints a WARN
    # and still returns 0, so assert the matching path was taken (no skip warn).
    assert "VERIFY_RC=0" in r.stdout
    assert "No SHA256 entry" not in r.stdout
    assert "skipping" not in r.stdout.lower()


def test_uninstall_requires_target_dir():
    """--uninstall without SSH_REMOTE_DESKTOP_DIR set should still be a clean
    no-op (or warn) rather than crash; we only check it doesn't blow up
    outside a configured install dir."""
    env = dict(os.environ)
    env["SSH_REMOTE_DESKTOP_DIR"] = "/tmp/rd-uninstall-test-does-not-exist"
    r = subprocess.run(
        ["bash", INSTALL, "--uninstall"],
        capture_output=True, text=True, env=env,
    )
    # Either it reports nothing to remove (0) or it errors cleanly (!=0) but
    # must never hang or produce a traceback.
    assert r.returncode in (0, 1)


# ---- install_session_defaults: --with-wm / server.toml / uninstall --------

def _run_sourced(snippet, extra_env=None):
    """Run a bash snippet that sources install.sh (no main) with stubs.

    SRD_NO_RUN_MAIN=1 keeps main() from running; we exercise individual
    functions. RD_WITH_WM is stripped so env leakage doesn't flip the flag.
    """
    env = dict(os.environ)
    env.pop("SSH_REMOTE_DESKTOP_DIR", None)
    env.pop("RD_WITH_WM", None)
    env["SRD_NO_RUN_MAIN"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)


def _snippet(body, args=""):
    head = "set -euo pipefail\nsource __INSTALL__ " + args.strip() + "\n"
    return (head + body).replace("__INSTALL__", repr(INSTALL))


_SESSION_BODY = r'''OS=linux
DISTRO=ubuntu
COMPONENT=server
TOML="$RD_TEST_TOML"
LOG="$RD_TEST_LOG"
exists_root() { [[ -e "$TOML" ]]; }
mkdir_root() { mkdir -p "$(dirname "$TOML")"; }
write_root() { printf '%s' "$2" > "$TOML"; }
pkg_install() { echo "PKGINST:$@" >> "$LOG"; return 0; }
wm_present() { return "${RD_TEST_WM_PRESENT:-1}"; }
install_session_defaults
echo "WARN=$SERVER_WM_WARN"
echo "---TOML---"
cat "$TOML" 2>/dev/null || echo "<no toml>"
echo "---LOG---"
cat "$LOG" 2>/dev/null || echo "<no pkg calls>"
'''


def test_help_lists_with_wm_flag():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--with-wm" in r.stdout
    assert "RD_WITH_WM" in r.stdout


def test_with_wm_eq_arg_sets_var():
    body = 'echo "WITH_WM=$WITH_WM"\n' 'echo "REQ=$WITH_WM_REQUESTED"\n'
    r = _run_sourced(_snippet(body, args="--with-wm=xfce"))
    assert r.returncode == 0, r.stderr
    assert "WITH_WM=xfce" in r.stdout
    assert "REQ=yes" in r.stdout


def test_with_wm_bare_defaults_openbox():
    body = 'echo "WITH_WM=$WITH_WM"\n' 'echo "REQ=$WITH_WM_REQUESTED"\n'
    r = _run_sourced(_snippet(body, args="--with-wm"))
    assert r.returncode == 0, r.stderr
    assert "WITH_WM=openbox" in r.stdout
    assert "REQ=yes" in r.stdout


def test_with_wm_empty_eq_defaults_openbox():
    body = 'echo "WITH_WM=$WITH_WM"\n' 'echo "REQ=$WITH_WM_REQUESTED"\n'
    r = _run_sourced(_snippet(body, args="--with-wm="))
    assert r.returncode == 0, r.stderr
    assert "WITH_WM=openbox" in r.stdout
    assert "REQ=yes" in r.stdout


def test_rd_with_wm_env_sets_var():
    body = 'echo "WITH_WM=$WITH_WM"\n' 'echo "REQ=$WITH_WM_REQUESTED"\n'
    r = _run_sourced(_snippet(body), {"RD_WITH_WM": "xterm"})
    assert r.returncode == 0, r.stderr
    assert "WITH_WM=xterm" in r.stdout
    assert "REQ=yes" in r.stdout


def test_with_wm_does_not_swallow_next_flag():
    # `--with-wm --component server` must treat --with-wm as bare (openbox)
    # and leave --component for the parser.
    body = 'echo "WITH_WM=$WITH_WM"\n' 'echo "COMP=$COMPONENT"\n'
    r = _run_sourced(_snippet(body, args="--with-wm --component server"))
    assert r.returncode == 0, r.stderr
    assert "WITH_WM=openbox" in r.stdout
    assert "COMP=server" in r.stdout


def test_wm_package_mapping():
    body = (
        'DISTRO=ubuntu; echo "u-openbox=$(wm_package openbox)"\n'
        'DISTRO=ubuntu; echo "u-plasma=$(wm_package plasma)"\n'
        'DISTRO=ubuntu; echo "u-xfce=$(wm_package xfce)"\n'
        'DISTRO=ubuntu; echo "u-xterm=$(wm_package xterm)"\n'
        'DISTRO=fedora; echo "f-xfce=$(wm_package xfce)"\n'
        'DISTRO=fedora; echo "f-openbox=$(wm_package openbox)"\n'
        'DISTRO=ubuntu; echo "raw=$(wm_package i3-wm)"\n'
    )
    r = _run_sourced(_snippet(body))
    assert r.returncode == 0, r.stderr
    assert "u-openbox=openbox" in r.stdout
    assert "u-plasma=plasma-desktop" in r.stdout
    assert "u-xfce=xfce4" in r.stdout
    assert "u-xterm=xterm" in r.stdout
    assert "f-xfce=xfce4-session" in r.stdout
    assert "f-openbox=openbox" in r.stdout
    assert "raw=i3-wm" in r.stdout


def test_wm_binary_mapping():
    body = (
        'echo "b-openbox=$(wm_binary openbox)"\n'
        'echo "b-plasma=$(wm_binary plasma)"\n'
        'echo "b-xfce=$(wm_binary xfce)"\n'
        'echo "b-xterm=$(wm_binary xterm)"\n'
        'echo "b-raw=$(wm_binary i3)"\n'
    )
    r = _run_sourced(_snippet(body))
    assert r.returncode == 0, r.stderr
    assert "b-openbox=openbox" in r.stdout
    assert "b-plasma=plasmashell" in r.stdout
    assert "b-xfce=xfce4-session" in r.stdout
    assert "b-xterm=xterm" in r.stdout
    assert "b-raw=i3" in r.stdout


def test_install_session_defaults_creates_toml(tmp_path):
    toml = tmp_path / "server.toml"
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(_SESSION_BODY, args="--with-wm=openbox"), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "1",  # absent -> install path exercised
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=no" in r.stdout
    assert "PKGINST:openbox" in r.stdout
    assert 'backend = "x11"' in r.stdout
    assert "session_geometry = [1920, 1080]" in r.stdout
    assert 'window_manager = "openbox"' in r.stdout


def test_install_session_defaults_idempotent_wm_present(tmp_path):
    # WM already on PATH -> pkg_install must NOT be called, but server.toml
    # is still generated on first run.
    toml = tmp_path / "server.toml"
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(_SESSION_BODY, args="--with-wm=openbox"), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "0",  # present
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=no" in r.stdout
    assert "<no pkg calls>" in r.stdout
    assert 'window_manager = "openbox"' in r.stdout


def test_install_session_defaults_skips_existing_toml(tmp_path):
    # Pre-existing server.toml must never be overwritten (idempotency).
    toml = tmp_path / "server.toml"
    toml.write_text('window_manager = "custom-wm"\nbackend = "wayland"\n')
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(_SESSION_BODY, args="--with-wm=openbox"), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "0",  # present -> no pkg install either
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=no" in r.stdout
    assert "<no pkg calls>" in r.stdout
    assert 'window_manager = "custom-wm"' in r.stdout
    assert 'backend = "wayland"' in r.stdout
    assert 'window_manager = "openbox"' not in r.stdout


def test_install_session_defaults_no_wm_sets_warn(tmp_path):
    # No --with-wm: config untouched + black-screen warning flagged.
    toml = tmp_path / "server.toml"
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(_SESSION_BODY), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "0",
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=yes" in r.stdout
    assert "<no toml>" in r.stdout
    assert "<no pkg calls>" in r.stdout


def test_install_session_defaults_no_wm_keeps_existing_toml(tmp_path):
    # No --with-wm AND a server.toml already exists -> still untouched, warn.
    toml = tmp_path / "server.toml"
    toml.write_text('window_manager = "keep-me"\n')
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(_SESSION_BODY), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "0",
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=yes" in r.stdout
    assert 'window_manager = "keep-me"' in r.stdout


def test_install_session_defaults_skipped_for_client_only(tmp_path):
    # --component client: no server -> nothing happens, no warn.
    body = _SESSION_BODY.replace("COMPONENT=server", "COMPONENT=client")
    toml = tmp_path / "server.toml"
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(body, args="--with-wm=openbox"), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "1",
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=no" in r.stdout
    assert "<no toml>" in r.stdout
    assert "<no pkg calls>" in r.stdout


def test_install_session_defaults_skipped_off_linux(tmp_path):
    # macOS/Windows: no-op even with --with-wm.
    body = _SESSION_BODY.replace("OS=linux", "OS=macos")
    toml = tmp_path / "server.toml"
    log = tmp_path / "pkg.log"
    r = _run_sourced(_snippet(body, args="--with-wm=openbox"), {
        "RD_TEST_TOML": str(toml),
        "RD_TEST_LOG": str(log),
        "RD_TEST_WM_PRESENT": "1",
    })
    assert r.returncode == 0, r.stderr
    assert "WARN=no" in r.stdout
    assert "<no toml>" in r.stdout


def test_uninstall_preserves_nonempty_user_config(tmp_path):
    """--uninstall must NOT remove a non-empty ~/.config/ssh-remote-desktop
    (user keys/hosts live there), but must remove our symlinks + install dir."""
    import shutil
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".config/ssh-remote-desktop"
    cfg.mkdir(parents=True)
    (cfg / "known_hosts").write_text("fake host key fingerprint\n")
    installdir = tmp_path / "install"
    installdir.mkdir()
    (installdir / "marker").write_text("x")
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    (bindir / "rd-server").symlink_to(installdir / "marker")

    env = dict(os.environ)
    env["SSH_REMOTE_DESKTOP_DIR"] = str(installdir)
    env["HOME"] = str(home)
    r = subprocess.run(["bash", INSTALL, "--uninstall"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

    assert (cfg / "known_hosts").exists(), "non-empty user config was wiped!"
    assert cfg.exists(), "non-empty config dir was removed!"
    assert not (bindir / "rd-server").exists(), "stale symlink not removed"
    assert not installdir.exists(), "install dir not removed"


def test_uninstall_removes_empty_etc_server_toml(tmp_path):
    """--uninstall removes an EMPTY /etc/ssh-remote-desktop/server.toml (and
    the dir when empty), mirroring the user-config policy. Skipped if the
    path already exists so we never truncate a real operator config."""
    import shutil
    syscfg = "/etc/ssh-remote-desktop"
    if os.path.exists(syscfg):
        pytest.skip("/etc/ssh-remote-desktop already present; not touching it")
    created = False
    try:
        os.makedirs(syscfg, exist_ok=False)
        toml = os.path.join(syscfg, "server.toml")
        open(toml, "w").close()  # empty file
        created = True

        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env["SSH_REMOTE_DESKTOP_DIR"] = str(tmp_path / "nope")
        env["HOME"] = str(home)
        r = subprocess.run(["bash", INSTALL, "--uninstall"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(toml), "empty server.toml should be removed"
        assert not os.path.exists(syscfg), "empty /etc/ssh-remote-desktop should be removed"
        created = False
    finally:
        if created:
            shutil.rmtree(syscfg, ignore_errors=True)


def test_uninstall_keeps_nonempty_etc_server_toml(tmp_path):
    """--uninstall must NOT remove a non-empty /etc/ssh-remote-desktop/server.toml
    (operator-edited config). Skipped if the path already exists."""
    import shutil
    syscfg = "/etc/ssh-remote-desktop"
    if os.path.exists(syscfg):
        pytest.skip("/etc/ssh-remote-desktop already present; not touching it")
    created = False
    try:
        os.makedirs(syscfg, exist_ok=False)
        toml = os.path.join(syscfg, "server.toml")
        with open(toml, "w") as f:
            f.write('backend = "x11"\nwindow_manager = "openbox"\n')
        created = True

        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env["SSH_REMOTE_DESKTOP_DIR"] = str(tmp_path / "nope")
        env["HOME"] = str(home)
        r = subprocess.run(["bash", INSTALL, "--uninstall"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert os.path.exists(toml), "non-empty server.toml must be preserved"
        assert 'window_manager = "openbox"' in open(toml).read()
        created = True  # still needs cleanup since uninstall kept it
    finally:
        if created:
            shutil.rmtree(syscfg, ignore_errors=True)
