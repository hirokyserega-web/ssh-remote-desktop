"""Consistency checks between pyproject.toml and requirements*.txt (ЗАДАЧА B1).

pyproject.toml and requirements.txt / requirements-linux.txt are the two
declared sources of dependency truth. When they drift (different lower bounds,
a package present in one but missing in the other) builds become
non-reproducible and `pip install` by one route can pull a version the other
route never tested — e.g. python-pam 2.x imports ``six`` but doesn't declare
it, so a pyproject-only install died with ``ModuleNotFoundError: six`` while
the requirements files pinned it.

These tests pin the two sources together so the drift can't silently return.
Platform-agnostic (pure file parsing), so they run on the Windows CI job too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_LINUX = ROOT / "requirements-linux.txt"

_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*>=\s*([0-9][0-9A-Za-z._+~-]*)")


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _parse_req_line(line: str):
    """Return (name, lower_bound) for a ``name>=X.Y`` requirement, else None.

    Strips comments and ``; sys_platform == ...`` env markers so a Linux-only
    pin in pyproject is compared against the same pin in requirements-linux.txt.
    """
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    line = line.split(";", 1)[0].strip()
    m = _REQ_RE.match(line)
    if not m:
        return None
    return _norm(m.group(1)), m.group(2)


def _pyproject_bounds() -> dict[str, str]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("tomllib unavailable")
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    bounds: dict[str, str] = {}
    deps = list(data["project"]["dependencies"])
    for group in data["project"]["optional-dependencies"].values():
        deps.extend(group)
    for dep in deps:
        parsed = _parse_req_line(dep)
        if not parsed:
            continue
        name, ver = parsed
        # A package may appear in several extras; they must all agree.
        if name in bounds and bounds[name] != ver:
            pytest.fail(f"pyproject declares {_norm(name)} at both {bounds[name]} and {ver}")
        bounds[name] = ver
    return bounds


def _requirements_bounds(path: Path) -> dict[str, str]:
    bounds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_req_line(line)
        if not parsed:
            continue
        name, ver = parsed
        bounds[name] = ver
    return bounds


# --------------------------------------------------------------------------- #
# Lower bounds agree between pyproject and the requirements files
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("req_file", [REQUIREMENTS, REQUIREMENTS_LINUX])
def test_requirements_lower_bounds_match_pyproject(req_file):
    pyproj = _pyproject_bounds()
    reqs = _requirements_bounds(req_file)
    assert reqs, f"no requirements parsed from {req_file.name}"
    mismatches = []
    for name, ver in reqs.items():
        if name not in pyproj:
            mismatches.append(f"{name}: in {req_file.name} (>= {ver}) but absent from pyproject.toml")
        elif pyproj[name] != ver:
            mismatches.append(
                f"{name}: {req_file.name} >= {ver} vs pyproject >= {pyproj[name]}"
            )
    assert not mismatches, "\n".join(mismatches)


def test_every_requirement_has_a_pyproject_home():
    """No package may live ONLY in a requirements file — pyproject is the
    source of truth, so a requirements-only pin is undetectable to
    `pip install ssh-remote-desktop[...]`."""
    pyproj = _pyproject_bounds()
    for req_file in (REQUIREMENTS, REQUIREMENTS_LINUX):
        for name in _requirements_bounds(req_file):
            assert name in pyproj, f"{name} only in {req_file.name}, not in pyproject.toml"


# --------------------------------------------------------------------------- #
# Targeted regression assertions for the specific B1 bugs
# --------------------------------------------------------------------------- #
def test_six_pinned_alongside_python_pam_in_pyproject():
    """python-pam 2.x imports `six` at runtime without declaring it; pyproject
    must pin six next to python-pam or `import pam` dies with ModuleNotFoundError."""
    bounds = _pyproject_bounds()
    assert "six" in bounds, "six is missing from pyproject (python-pam needs it)"
    assert "python-pam" in bounds
    # six must also be pulled in on Linux via requirements-linux.txt.
    assert "six" in _requirements_bounds(REQUIREMENTS_LINUX)


def test_async_timeout_not_pinned_anywhere():
    """async-timeout was pinned in requirements.txt but never used (and absent
    from pyproject). Removed to keep a single, honest source of truth."""
    for req_file in (REQUIREMENTS, REQUIREMENTS_LINUX):
        assert "async-timeout" not in _requirements_bounds(req_file), (
            f"async-timeout should not be pinned in {req_file.name} (unused)"
        )
    assert "async-timeout" not in _pyproject_bounds()


def test_core_numpy_msgpack_match_requirements():
    """numpy/msgpack lower bounds drifted between pyproject and requirements.txt."""
    pyproj = _pyproject_bounds()
    reqs = _requirements_bounds(REQUIREMENTS)
    assert pyproj["numpy"] == reqs["numpy"]
    assert pyproj["msgpack"] == reqs["msgpack"]


# --------------------------------------------------------------------------- #
# License (B3): the LICENSE file must be the complete MIT text, not truncated.
# --------------------------------------------------------------------------- #
def test_license_file_is_complete_mit():
    """GitHub reported 'Other (NOASSERTION)' because LICENSE was truncated
    mid-sentence (~238 bytes). The full MIT text must be present and
    self-consistent so licensee/SPDX tools identify it as MIT."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License"), "LICENSE must start with 'MIT License'"
    assert "Copyright (c) 2026 hirokyserega-web" in text
    # The canonical MIT permission + no-warranty paragraphs must both be present
    # (the truncation cut off the second half).
    assert "Permission is hereby granted" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text
    assert "THE SOFTWARE IS PROVIDED" in text
    # A truncated file was ~238 bytes; the full MIT text is ~1 KB.
    assert len(text) > 900, f"LICENSE looks truncated ({len(text)} bytes)"


def test_pyproject_declares_mit_license():
    """The MIT classifier makes the license explicit in package metadata."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("tomllib unavailable")
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    classifiers = data["project"].get("classifiers", [])
    assert any("MIT License" in c for c in classifiers), (
        "pyproject classifiers must declare 'License :: OSI Approved :: MIT License'"
    )
