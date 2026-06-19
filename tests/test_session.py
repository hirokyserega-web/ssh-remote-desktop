"""Tests for server.session: UserInfo + _demote pwd-guard."""
from __future__ import annotations
from server import session
import pwd

def test_userinfo_root():
    u = session.UserInfo("root")
    assert u.name == "root"
    assert u.uid == 0
    assert u.home == pwd.getpwnam("root").pw_dir
    env = u.base_env()
    assert env["USER"] == "root"
    assert env["HOME"] == pwd.getpwnam("root").pw_dir

def test_userinfo_base_env_extra():
    u = session.UserInfo("root")
    env = u.base_env({"DISPLAY": ":7"})
    assert env["DISPLAY"] == ":7"

def test_free_display_number_unique():
    n1 = session._free_display_number()
    n2 = session._free_display_number()
    assert n1 == n2  # neither lock nor socket created
