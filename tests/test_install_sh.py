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
