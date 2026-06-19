"""Tests for the view-size-provider regression + alignment (BLOCKER).

The previous round left a broken ``self.view_`` line in
``Transport.__init__`` that raised ``AttributeError`` at construction time,
so the client could not start. These tests pin:

* ``Transport(cfg)`` constructs without error and exposes a public
  ``view_size_provider`` (None until the GUI sets it);
* ``_handshake`` uses the provider's value when set, and falls back to
  ``cfg.geometry`` *explicitly* (not via a bare ``except`` that masks bugs)
  when no provider is wired.
"""

from __future__ import annotations

import asyncio

import pytest

from common import messages
from common.config import ClientConfig
from client.transport import Transport


class _RecordingMux:
    """Stand-in for Multiplexer that records the last control frame sent."""

    def __init__(self):
        self.last_payload = None
        self.last_flag = None

    def send(self, channel, payload, flag=0):
        self.last_payload = payload
        self.last_flag = flag


def _hello_view(transport) -> tuple[int, int]:
    """Run _handshake against a recording mux and return the hello ``view``."""
    transport.mux = _RecordingMux()
    asyncio.run(transport._handshake())
    msg = messages.loads(transport.mux.last_payload, transport.mux.last_flag)
    return tuple(msg["view"])


def test_transport_constructs_without_attribute_error(tmp_path):
    """The BLOCKER: __init__ must not raise (no broken self.view_)."""
    cfg = ClientConfig(known_hosts=str(tmp_path / "kh"))
    transport = Transport(cfg)
    # Public provider attribute exists and is None until the GUI sets it.
    assert hasattr(transport, "view_size_provider")
    assert transport.view_size_provider is None
    # No private alias should linger that the GUI does not set.
    assert not hasattr(transport, "_view_size_provider")


def test_handshake_uses_provider_when_set(tmp_path):
    cfg = ClientConfig(known_hosts=str(tmp_path / "kh"), geometry=(1920, 1080))
    transport = Transport(cfg)
    transport.view_size_provider = lambda: (800, 600)
    assert _hello_view(transport) == (800, 600)


def test_handshake_falls_back_to_geometry_explicitly(tmp_path):
    """No provider -> cfg.geometry, not a silent except-masked fallback."""
    cfg = ClientConfig(known_hosts=str(tmp_path / "kh"), geometry=(1280, 720))
    transport = Transport(cfg)
    # Provider deliberately left None (headless).
    assert _hello_view(transport) == (1280, 720)


def test_handshake_provider_error_is_not_silently_masked(tmp_path):
    """A buggy provider must surface, not be swallowed into cfg.geometry."""
    cfg = ClientConfig(known_hosts=str(tmp_path / "kh"), geometry=(640, 480))
    transport = Transport(cfg)

    def bad_provider():
        raise RuntimeError("boom")

    transport.view_size_provider = bad_provider
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(transport._handshake())
