"""Tests for server.auth: pwd-guard and authorized_keys lookup."""
from __future__ import annotations
from server import auth

def test_user_exists_known_uid():
    # root (uid 0) exists on every Unix.
    assert auth.user_exists("root") is True

def test_user_exists_missing_returns_false():
    assert auth.user_exists("this_user_should_not_exist_zo_test") is False

def test_authorized_keys_for_missing_user_empty():
    assert auth.authorized_keys_for("this_user_should_not_exist_zo_test") == []

def test_authorized_keys_for_root_skips_missing_file():
    # /root/.ssh/authorized_keys may or may not exist; must never raise.
    assert isinstance(auth.authorized_keys_for("root"), list)
