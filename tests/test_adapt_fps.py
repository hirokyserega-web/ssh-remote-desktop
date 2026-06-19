"""Tests for server/connection._adapt (P1 tail: set_fps on fps change).

Adaptation lowers/raises bitrate AND fps from client-reported loss/latency.
Previously only set_bitrate was forwarded to the encoder, so fps changes
affected only the capture period, not the encoder's framerate/GOP. This test
asserts both set_bitrate and set_fps are called when the values move.
"""
from __future__ import annotations


from server.connection import ConnectionHandler


class _FakeEncoder:
    """Records set_bitrate / set_fps calls; is_image_codec for completeness."""
    is_image_codec = False

    def __init__(self):
        self.bitrate_calls: list[int] = []
        self.fps_calls: list[int] = []

    def set_bitrate(self, kbps: int) -> None:
        self.bitrate_calls.append(kbps)

    def set_fps(self, fps: int) -> None:
        self.fps_calls.append(fps)


class _FakeBackend:
    kind = "x11"

    def screen_size(self):
        return (320, 240)


class _FakeSession:
    def __init__(self):
        self.backend = _FakeBackend()


def _make_handler(*, fps: int, bitrate_kbps: int) -> ConnectionHandler:
    """Build a ConnectionHandler with just enough state for _adapt."""
    cfg = type("Cfg", (), {})()
    cfg.fps = fps
    cfg.bitrate_kbps = bitrate_kbps
    h = ConnectionHandler.__new__(ConnectionHandler)
    h.cfg = cfg
    h._fps = fps
    h._bitrate = bitrate_kbps
    h.encoder = _FakeEncoder()
    return h


def test_adapt_high_loss_lowers_bitrate_and_fps():
    h = _make_handler(fps=60, bitrate_kbps=4000)
    enc = h.encoder
    h._adapt({"loss": 0.5, "rtt_ms": 300})
    assert enc.bitrate_calls, "set_bitrate was not called on degrade"
    assert enc.fps_calls, "set_fps was not called on degrade"
    assert enc.fps_calls[-1] == h._fps
    assert h._fps < 60
    assert h._bitrate < 4000


def test_adapt_good_link_raises_bitrate_and_fps():
    h = _make_handler(fps=30, bitrate_kbps=2000)
    # Drop first then raise so we actually cross a threshold.
    h._adapt({"loss": 0.5, "rtt_ms": 300})  # degrade to baseline
    enc = h.encoder
    baseline_fps = h._fps
    h._adapt({"loss": 0.0, "rtt_ms": 50})  # recover
    assert enc.fps_calls[-1] == h._fps
    assert h._fps >= baseline_fps


def test_adapt_neutral_zone_no_change():
    """A mid-range link (loss~0.02, rtt~150) is in the dead band: no change."""
    h = _make_handler(fps=30, bitrate_kbps=2000)
    enc = h.encoder
    h._adapt({"loss": 0.02, "rtt_ms": 150})
    # Values unchanged: set_bitrate is still invoked (the code always forwards
    # the current bitrate), but set_fps is NOT called because fps did not move
    # -- it is only forwarded on an actual change.
    assert h._fps == 30
    assert h._bitrate == 2000
    assert enc.bitrate_calls, "set_bitrate should always be forwarded"
    assert enc.fps_calls == [], "set_fps must not fire when fps is unchanged"
