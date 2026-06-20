"""Tests for server.auth: pwd-guard and authorized_keys lookup."""
from __future__ import annotations

import sys
import pytest

from server import auth

skip_non_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="server-side user lookup is POSIX-only (pwd)",
)


@skip_non_posix
def test_user_exists_known_uid():
    assert auth.user_exists("root") is True


@skip_non_posix
def test_user_exists_missing_returns_false():
    assert auth.user_exists("this_user_should_not_exist_zo_test") is False


@skip_non_posix
def test_authorized_keys_for_missing_user_empty():
    assert auth.authorized_keys_for("this_user_should_not_exist_zo_test") == []


@skip_non_posix
def test_authorized_keys_for_root_skips_missing_file():
    assert isinstance(auth.authorized_keys_for("root"), list)


def test_require_pwd_raises_on_non_posix():
    """On non-POSIX hosts _require_pwd must raise RuntimeError, not AttributeError."""
    if sys.platform == "win32":
        with pytest.raises(RuntimeError, match="POSIX pwd"):
            auth._require_pwd()
    else:
        auth._require_pwd()  # must not raise on Linux
