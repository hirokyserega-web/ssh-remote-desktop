"""Tests for the PATH logic in ``scripts/install.sh`` (``ensure_path``).

Covers:
- the right rc file is touched for each login shell (bash / zsh / fish)
- the fish variant uses ``set -gx`` syntax, not ``export``
- marker-guarded idempotency: re-runs never duplicate the line
- a warning is printed when ``~/.local/bin`` is NOT on PATH for the session
- no-op when the dir is already on PATH, and skipped entirely under root
- ``--diagnose`` prints the on/off-PATH status and exits 0
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = os.path.join(ROOT, "scripts", "install.sh")
MARKER = "ssh-remote-desktop installer: local bin on PATH"

pytestmark = pytest.mark.skipif(
    not os.path.exists(INSTALL) or sys.platform == "win32",
    reason="install.sh is a Linux/macOS shell script (no bash on Windows CI)",
)


def _run_ensure_path(home: str, shell: str, os_name: str = "linux",
                     root: bool = False,
                     extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Source install.sh (guarded so main() doesn't run), then call ensure_path.

    Uses SRD_NO_RUN_MAIN=1 + a synthetic HOME/SHELL/PATH so the function runs
    in isolation without performing a real install. The root/non-root branch
    of ensure_path is controlled by overriding the ``srd_is_root`` seam inside
    the snippet — this is environment-independent (works identically under the
    root sandbox and a non-root GitHub runner), unlike ``setpriv`` which fails
    with EPERM when the runner is already non-root.
    """
    env = dict(os.environ)
    env["SRD_NO_RUN_MAIN"] = "1"
    env["HOME"] = home
    env["SHELL"] = shell
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"  # deliberately lacks ~/.local/bin
    if extra_env:
        env.update(extra_env)
    # `root` selects which way the seam is overridden: true => pretend root
    # (ensure_path must skip); false => pretend non-root (ensure_path must run).
    root_seam = "srd_is_root() { return 0; }" if root else "srd_is_root() { return 1; }"
    snippet = textwrap.dedent(f"""\
        set -euo pipefail
        export HOME={home!r}
        export SHELL={shell!r}
        export PATH={env['PATH']!r}
        source {INSTALL!r}
        OS={os_name!r}
        {root_seam}
        ensure_path
        echo "SRD_ENSURE_PATH_DONE rc=$?"
    """)
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, env=env,
    )


# ---- bash ----------------------------------------------------------------- #

def test_ensure_path_writes_bashrc_for_bash(tmp_path):
    home = tmp_path
    r = _run_ensure_path(str(home), "/bin/bash")
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    bashrc = home / ".bashrc"
    assert bashrc.exists(), "ensure_path should create ~/.bashrc for bash users"
    content = bashrc.read_text()
    assert MARKER in content
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in content
    # Fish syntax must NOT leak into the bash rc.
    assert "set -gx" not in content


def test_ensure_path_warns_when_bin_not_on_path(tmp_path):
    home = tmp_path
    r = _run_ensure_path(str(home), "/bin/bash")
    # The warning goes to stderr and must contain the exact line to run.
    assert "~/.local/bin is not on PATH" in r.stderr or \
           ".local/bin is not on PATH" in r.stderr
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in r.stderr


# ---- zsh ------------------------------------------------------------------ #

def test_ensure_path_writes_zshrc_for_zsh(tmp_path):
    home = tmp_path
    r = _run_ensure_path(str(home), "/usr/bin/zsh")
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    zshrc = home / ".zshrc"
    assert zshrc.exists(), "ensure_path should create ~/.zshrc for zsh users"
    content = zshrc.read_text()
    assert MARKER in content
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in content
    # bashrc must NOT be created for a zsh user
    assert not (home / ".bashrc").exists()


# ---- fish ----------------------------------------------------------------- #

def test_ensure_path_writes_fish_config_with_set_gx(tmp_path):
    home = tmp_path
    r = _run_ensure_path(str(home), "/usr/bin/fish")
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    fish_cfg = home / ".config" / "fish" / "config.fish"
    assert fish_cfg.exists(), "ensure_path should create the fish config dir + file"
    content = fish_cfg.read_text()
    assert MARKER in content
    # Fish syntax — NOT bash export
    assert "set -gx PATH $HOME/.local/bin $PATH" in content
    assert "export PATH" not in content


# ---- idempotency ---------------------------------------------------------- #

def test_ensure_path_is_idempotent(tmp_path):
    home = tmp_path
    _run_ensure_path(str(home), "/bin/bash")
    bashrc = home / ".bashrc"
    first = bashrc.read_text()
    # Run again — the marker line must not be duplicated.
    r2 = _run_ensure_path(str(home), "/bin/bash")
    assert "SRD_ENSURE_PATH_DONE rc=0" in r2.stdout
    second = bashrc.read_text()
    assert second == first, "ensure_path must not duplicate the PATH line on re-run"
    assert second.count(MARKER) == 1


# ---- skip conditions ------------------------------------------------------ #

def test_ensure_path_noop_when_already_on_path(tmp_path):
    home = tmp_path
    # PATH already includes ~/.local/bin for this session, so ensure_path must
    # short-circuit (no rc edit, no warn).
    bindir = str(home) + "/.local/bin"
    r = _run_ensure_path(str(home), "/bin/bash",
                         extra_env={"PATH": "/usr/local/bin:/usr/bin:/bin:" + bindir})
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    # No rc file should have been created because PATH already had the dir.
    assert not (home / ".bashrc").exists()
    # And no warning printed.
    assert "not on PATH" not in r.stderr


def test_ensure_path_skipped_under_root(tmp_path):
    home = tmp_path
    r = _run_ensure_path(str(home), "/bin/bash", root=True)
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    # Root must never get ~/.bashrc edited.
    assert not (home / ".bashrc").exists()
    assert "not on PATH" not in r.stderr


def test_ensure_path_skipped_on_non_linux(tmp_path):
    home = tmp_path
    # macOS / others: ensure_path returns early (Linux-only by design).
    r = _run_ensure_path(str(home), "/bin/bash", os_name="macos")
    assert "SRD_ENSURE_PATH_DONE rc=0" in r.stdout
    assert not (home / ".bashrc").exists()


# ---- --diagnose ----------------------------------------------------------- #

def test_diagnose_reports_path_status(tmp_path):
    """--diagnose must print the on/off-PATH verdict and exit 0."""
    env = dict(os.environ)
    env.pop("SSH_REMOTE_DESKTOP_DIR", None)
    env["HOME"] = str(tmp_path)
    env["PATH"] = "/usr/bin:/bin"  # lacks ~/.local/bin
    r = subprocess.run(
        ["bash", INSTALL, "--diagnose"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "diagnostics" in r.stdout.lower()
    assert "NOT on PATH" in r.stdout or "not on PATH" in r.stdout


def test_help_lists_diagnose_flag():
    r = subprocess.run(
        ["bash", INSTALL, "--help"],
        capture_output=True, text=True,
        env={**os.environ, "SRD_NO_RUN_MAIN": "1"},
    )
    assert r.returncode == 0
    assert "--diagnose" in r.stdout or "--doctor" in r.stdout


# ---- install_user_symlinks / real_user_home (sudo stale-symlink fix) ----- #

def _run_install_snippet(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Source install.sh (guarded) and run an arbitrary snippet against it."""
    e = dict(os.environ)
    e["SRD_NO_RUN_MAIN"] = "1"
    if env:
        e.update(env)
    full = f"set -euo pipefail\nsource {INSTALL!r}\nOS=linux\n" + snippet
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, env=e)


def test_install_user_symlinks_overwrites_stale_link(tmp_path):
    """A stale ~/.local/bin/rd-server pointing at an old install dir must be
    repointed at the freshly-installed binary (the regression that left the
    user running a crashing old binary after a sudo re-install)."""
    target_dir = tmp_path / "srd"
    (target_dir / "bin").mkdir(parents=True)
    server_bin = target_dir / "bin" / "rd-server"
    server_bin.write_text("#!/bin/sh\necho new\n")
    server_bin.chmod(0o755)

    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    # Stale symlink → a path that no longer exists (old install dir removed).
    (bindir / "rd-server").symlink_to("/tmp/srd-old-gone/rd-server")

    snippet = textwrap.dedent(f"""\
        TARGET_DIR={str(target_dir)!r}
        install_user_symlinks {str(home)!r}
        echo "LINK=$(readlink {str(bindir / 'rd-server')!r})"
    """)
    r = _run_install_snippet(snippet, env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert str(server_bin) in r.stdout, f"stale link not repointed: {r.stdout}"


def test_real_user_home_empty_without_sudo_user(tmp_path):
    r = _run_install_snippet('echo "HOME_OUT=$(real_user_home)"\n',
                             env={"HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "HOME_OUT=" in r.stdout
    # Empty output after the = means no SUDO_USER → no real-user home.
    assert r.stdout.strip().endswith("HOME_OUT=")


def test_real_user_home_resolves_sudo_user(tmp_path):
    """With SUDO_USER set, real_user_home resolves the user's home via getent.
    getent is overridden in the snippet so the test doesn't depend on a real
    non-root account existing on the runner."""
    snippet = textwrap.dedent("""\
        getent() { printf 'alice:x:1000:1000::/home/alice:/bin/bash\n'; }
        echo "HOME_OUT=$(real_user_home)"
    """)
    r = _run_install_snippet(snippet, env={"HOME": str(tmp_path), "SUDO_USER": "alice"})
    assert r.returncode == 0, r.stderr
    assert "HOME_OUT=/home/alice" in r.stdout


def test_post_install_repoints_real_user_under_sudo(tmp_path):
    """post_install under sudo must symlink into the invoking user's
    ~/.local/bin (resolved via SUDO_USER), not just /root's."""
    target_dir = tmp_path / "srd"
    (target_dir / "bin").mkdir(parents=True)
    for name in ("rd-server", "rd-client", "rd-server-gui", "rd-launch"):
        b = target_dir / "bin" / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)

    root_home = tmp_path / "roothome"
    real_home = tmp_path / "alice"
    root_home.mkdir()
    real_home.mkdir()

    snippet = textwrap.dedent(f"""\
        TARGET_DIR={str(target_dir)!r}
        # Pretend we're running under sudo with SUDO_USER=alice; getent returns
        # alice's home. EUID is readonly in bash so we don't touch it —
        # real_user_home only checks SUDO_USER, and launcher_dirs is stubbed.
        getent() {{ printf 'alice:x:1000:1000::{str(real_home)!r}:/bin/bash\n'; }}
        SUDO_USER=alice
        # launcher_dirs touches system dirs as root; stub it to avoid needing
        # /usr/share write access in the sandbox.
        launcher_dirs() {{ BINDIR={str(tmp_path / "bin")}; APPSDIR={str(tmp_path / "apps")}; ICONDIR={str(tmp_path / "icons")}; mkdir -p "$BINDIR" "$APPSDIR" "$ICONDIR"; }}
        install_desktop_entries() {{ :; }}
        ensure_path() {{ :; }}
        post_install >/tmp/srd_post_$$.out 2>&1
        echo "POST_RC=$?"
        cat /tmp/srd_post_$$.out; rm -f /tmp/srd_post_$$.out
        echo "REAL_LINK=$(readlink {str(real_home)}/.local/bin/rd-server 2>/dev/null || echo NONE)"
    """)
    r = _run_install_snippet(snippet, env={"HOME": str(root_home)})
    assert r.returncode == 0, r.stderr
    assert "POST_RC=0" in r.stdout
    assert str(target_dir / "bin" / "rd-server") in r.stdout, (
        f"post_install did not repoint the real user's symlink: {r.stdout}"
    )
