"""Tests for the release workflow (release.yml) and scripts/release.sh."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_SH = ROOT / "scripts" / "release.sh"

# Bash-script tests are Linux/macOS only: the Windows CI runner has no bash in
# PATH (the `bash` shim routes to WSL, which has no installed distribution).
skip_no_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="release.sh is a bash script (no bash on Windows CI)",
)


# ---------- release.yml ----------

def _load_release():
    return yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))


def test_release_yml_exists():
    assert (WORKFLOWS / "release.yml").exists()


def test_release_triggers_on_version_tag():
    wf = _load_release()
    on = wf.get("on") or wf.get(True)  # yaml parses `on:` as True key
    assert "push" in on
    tags = on["push"].get("tags", [])
    assert any("v*" in t for t in tags), f"no v* tag trigger: {tags}"


def test_release_uses_release_action():
    body = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "softprops/action-gh-release" in body or "ncipollo/release-action" in body


def test_release_has_sha256sums_step():
    body = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "SHA256SUMS" in body, "workflow must produce SHA256SUMS"


def test_release_matrix_has_linux_windows_macos():
    body = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    # Each target OS must appear in the matrix include.
    for os_name in ("linux", "windows", "macos"):
        assert os_name in body, f"matrix missing {os_name}"


def test_release_validates_with_actionlint():
    """actionlint must accept release.yml (skipped if actionlint absent)."""
    if subprocess.run(["which", "actionlint"], capture_output=True).returncode != 0:
        pytest.skip("actionlint not installed")
    r = subprocess.run(
        ["actionlint", str(WORKFLOWS / "release.yml")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"actionlint errors:\n{r.stderr}"


# ---------- release.sh helper ----------

@skip_no_bash
def test_release_sh_exists_and_executable():
    assert RELEASE_SH.exists()
    assert os.access(RELEASE_SH, os.X_OK), "release.sh should be chmod +x"


@skip_no_bash
def test_release_sh_syntax_ok():
    r = subprocess.run(["bash", "-n", str(RELEASE_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n: {r.stderr}"


@skip_no_bash
def test_release_sh_requires_version_arg():
    r = subprocess.run(["bash", str(RELEASE_SH)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "usage" in (r.stdout + r.stderr).lower()


@skip_no_bash
def test_release_sh_rejects_bad_version_format():
    r = subprocess.run(
        ["bash", str(RELEASE_SH), "not-a-version"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0


@skip_no_bash
def test_release_sh_accepts_valid_version(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "VERSION").write_text("1.0.0\n")
    (repo / "CHANGELOG.md").write_text("# Changelog\n## [Unreleased]\n- test\n")
    dst = repo / "scripts" / "release.sh"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(RELEASE_SH.read_bytes())
    os.chmod(dst, 0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    r = subprocess.run(
        ["bash", str(dst), "1.2.3", "--dry-run"],
        capture_output=True, text=True, cwd=repo,
    )
    assert r.returncode == 0, f"dry-run failed:\n{r.stdout}\n{r.stderr}"


@skip_no_bash
def test_release_sh_rejects_non_semver_with_v_prefix_only(tmp_path):
    # v1.2.3 should be normalized to 1.2.3 and accepted in dry-run.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "VERSION").write_text("1.0.0\n")
    (repo / "CHANGELOG.md").write_text("# Changelog\n## [Unreleased]\n- test\n")
    dst = repo / "scripts" / "release.sh"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(RELEASE_SH.read_bytes())
    os.chmod(dst, 0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    r = subprocess.run(
        ["bash", str(dst), "v1.2.3", "--dry-run"],
        capture_output=True, text=True, cwd=repo,
    )
    assert r.returncode == 0, f"v-prefix normalization failed:\n{r.stdout}\n{r.stderr}"
