"""Message serializers + builders."""

import pytest

from common import messages
from common.protocol import Flags


def test_roundtrip_json_when_msgpack_missing(monkeypatch):
    monkeypatch.setattr(messages, "_HAVE_MSGPACK", False)
    obj = {"t": "hello", "n": 1, "list": [1, 2, 3], "u": "\u041f"}
    payload, flag = messages.dumps(obj)
    assert not (flag & Flags.MSGPACK)
    assert messages.loads(payload, flag) == obj


def test_roundtrip_msgpack_when_available():
    if not messages.prefers_msgpack():
        pytest.skip("msgpack not installed")
    obj = {"t": "ping", "ts": 123, "data": "\u041f\u0440\u0438\u0432\u0435\u0442"}
    payload, flag = messages.dumps(obj)
    assert flag & Flags.MSGPACK
    assert messages.loads(payload, flag) == obj


def test_loads_rejects_unexpected_encoding(monkeypatch):
    monkeypatch.setattr(messages, "_HAVE_MSGPACK", False)
    with pytest.raises(RuntimeError):
        messages.loads(b"x", Flags.MSGPACK)


def test_builders_match_spec():
    assert messages.hello(codec="h264", view=(1920, 1080), user="alice",
                          auth="key") == {
        "t": "hello", "proto": 1, "codec": "h264", "view": [1920, 1080],
        "user": "alice", "auth": "key", "new_session": True,
        "geometry": [1920, 1080], "persistent": False,
    }
    s = messages.session(session_id="x", backend="x11", display=":42",
                         wayland_display=None, screen=(1920, 1080),
                         fps=30, cursor="embedded")
    assert s["t"] == "session" and s["backend"] == "x11" and s["screen"] == [1920, 1080]
