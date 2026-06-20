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

    Uses SRD_NO_RUN_MAIN=1 + a synthetic environment so the function can be
    exercised in isolation without performing a real install. The sandbox runs
    as root but EUID is readonly in bash and ensure_path skips when EUID==0,
    so non-root cases drop privileges via setpriv; the root case keeps them.
    """
    env = dict(os.environ)
    env["SRD_NO_RUN_MAIN"] = "1"
    env["HOME"] = home
    env["SHELL"] = shell
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"  # deliberately lacks ~/.local/bin
    if extra_env:
        env.update(extra_env)
    # Bash snippet: source install.sh, set OS for the function, run it. OS is a
    # plain variable (not readonly) so we can override it after sourcing.
    snippet = textwrap.dedent(f"""\
        set -euo pipefail
        export HOME={home!r}
        export SHELL={shell!r}
        export PATH=$PATH
        source {INSTALL!r}
        OS={os_name!r}
        ensure_path
        echo "SRD_ENSURE_PATH_DONE rc=$?"
    """)
    if root:
        cmd = ["bash", "-c", snippet]
    else:
        # Make the synthetic HOME writable by uid 1000 (pytest creates tmp_path
        # under root) and every ancestor up to /tmp traversable, so the
        # dropped-privilege bash can mkdir ~/.config/fish etc.
        subprocess.run(["chmod", "-R", "777", home], check=False)
        _ancestor = os.path.dirname(home.rstrip("/"))
        while _ancestor and os.path.abspath(_ancestor) not in ("/", "/tmp"):
            subprocess.run(["chmod", "o+rx", _ancestor], check=False)
            _ancestor = os.path.dirname(_ancestor)
        cmd = ["setpriv", "--reuid", "1000", "--regid", "1000", "--clear-groups",
               "bash", "-c", snippet]
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env,
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
