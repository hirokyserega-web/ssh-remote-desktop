"""Tests for coordinate scaling (P0 3.1) and codec echo (P0 3.2).

After the fix:
- The client sends normalized [0,1) floats for mouse_move (not pixel ints).
- The server maps those to screen pixels in a single place (_scale_coords).
- The session reply echoes the negotiated codec.
"""

from __future__ import annotations

import asyncio

from common.protocol import PROTO_VERSION
from common import messages


def test_hello_view_uses_session_geometry_default():
    """hello.view defaults to session geometry when no explicit view is given."""
    msg = messages.hello(codec="jpeg", view=(1920, 1080), user="alice", auth="key")
    assert msg["view"] == [1920, 1080]
    assert msg["geometry"] == [1920, 1080]


def test_session_reply_carries_codec():
    """session reply must include the negotiated codec so the client builds
    the matching decoder (P0 3.2)."""
    msg = messages.session(
        session_id="abc123",
        backend="x11",
        display=":10",
        wayland_display=None,
        screen=(1920, 1080),
        fps=30,
        cursor="embedded",
        codec="jpeg",
    )
    assert msg["codec"] == "jpeg"
    assert msg["proto"] == PROTO_VERSION


def test_session_reply_codec_none_when_fallback():
    """codec=None is a valid value (server fell back but didn't echo it)."""
    msg = messages.session(
        session_id="x", backend="x11", display=":10",
        wayland_display=None, screen=(1280, 720), fps=30, cursor="embedded",
    )
    assert msg["codec"] is None


class FakeBackend:
    """Minimal stand-in for a DisplayBackend, just enough for _scale_coords."""
    kind = "x11"

    def __init__(self, w, h):
        self._w, self._h = w, h

    def screen_size(self):
        return (self._w, self._h)


class FakeSession:
    def __init__(self, w, h):
        self.backend = FakeBackend(w, h)


def _make_handler(w=1920, h=1080):
    """Build a ConnectionHandler with just enough state for _scale_coords."""
    from server.connection import ConnectionHandler

    h_obj = ConnectionHandler.__new__(ConnectionHandler)
    h_obj.session = FakeSession(w, h)
    h_obj._client_view = (800, 600)  # irrelevant with normalized coords
    return h_obj


def test_scale_coords_normalized_to_full_screen():
    """A normalized (0.5, 0.5) lands in the centre of the screen."""

    async def _run():
        h = _make_handler(2560, 1440)
        assert h._scale_coords(0.5, 0.5) == (1279, 719)

    asyncio.run(_run())


def test_scale_coords_fractional():
    """0.1 of a 240px screen is 24px."""

    async def _run():
        h = _make_handler(320, 240)
        assert h._scale_coords(0.1, 0.1) == (31, 23)

    asyncio.run(_run())


def test_scale_coords_zero_and_one():
    """0 → top-left, ~1.0 → bottom-right (clamped)."""

    async def _run():
        h = _make_handler(100, 100)
        assert h._scale_coords(0.0, 0.0) == (0, 0)
        assert h._scale_coords(1.0, 1.0) == (99, 99)

    asyncio.run(_run())