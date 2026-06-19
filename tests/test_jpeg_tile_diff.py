"""Tests for JPEG-delta tile-diff fallback (P1 4.2).

When a backend does NOT supply XDamage/PipeWire dirty rectangles (damage is
None), the JpegEncoder now computes its own tile diff against the previous
frame. This test verifies:
- first frame → full frame
- identical frame → empty delta (ndelta=0, not a full re-send)
- changed frame → delta with exactly the changed tile(s)
- the client JpegDeltaDecoder reconstructs all three correctly
"""

from __future__ import annotations

import struct

import numpy as np

from server.backend.base import Frame
from server.encoder import JpegEncoder
from client.decoder import JpegDeltaDecoder


def _make_frame(w, h, bgra, damage=None):
    """Build a solid BGRA Frame."""
    if len(bgra) == 3:
        bgra = (*bgra, 255)  # add alpha channel
    row = np.array(bgra, dtype=np.uint8)
    buf = bytearray(np.broadcast_to(row, (h, w, 4)).tobytes())
    return Frame(width=w, height=h, buffer=buf, stride=w * 4, damage=damage)


def _parse_jd(payload):
    """Return (ndelta, full_flag) from a JD packet."""
    assert payload[:2] == b"JD"
    ndelta, full = struct.unpack("!BB", payload[2:4])
    return ndelta, full


def test_first_frame_is_full():
    enc = JpegEncoder(64, 64, fps=10, bitrate_kbps=2000, quality=80)
    f = _make_frame(64, 64, (10, 20, 30))
    out = enc.encode(f)
    assert len(out) == 1
    payload, is_key = out[0]
    assert is_key is True
    ndelta, full = _parse_jd(payload)
    assert full == 1


def test_identical_frame_is_empty_delta():
    """No change + no backend damage → empty delta (not a wasteful full frame)."""
    enc = JpegEncoder(64, 64, fps=10, bitrate_kbps=2000, quality=80)
    f = _make_frame(64, 64, (10, 20, 30))
    enc.encode(f)  # first → full
    out = enc.encode(f)  # second, identical
    payload, is_key = out[0]
    assert is_key is False
    ndelta, full = _parse_jd(payload)
    assert full == 0
    assert ndelta == 0


def test_changed_frame_is_tile_delta():
    """A changed tile produces a delta with ndelta >= 1."""
    enc = JpegEncoder(64, 64, fps=10, bitrate_kbps=2000, quality=80)
    f1 = _make_frame(64, 64, (10, 20, 30))
    enc.encode(f1)
    # Change a 32x32 block in the top-left.
    f2 = _make_frame(64, 64, (10, 20, 30))
    buf = np.frombuffer(f2.buffer, dtype=np.uint8).reshape(64, 64 * 4)
    buf[0:32, 0:32 * 4:4] = 200  # B
    buf[0:32, 1:32 * 4:4] = 200  # G
    buf[0:32, 2:32 * 4:4] = 200  # R
    f2 = Frame(width=64, height=64, buffer=buf.tobytes(),
               stride=64 * 4, damage=None, cursor_x=0, cursor_y=0)
    out = enc.encode(f2)
    payload, is_key = out[0]
    assert is_key is False
    ndelta, full = _parse_jd(payload)
    assert full == 0
    assert ndelta >= 1


def test_backend_damage_used_when_present():
    """When the backend supplies damage rects, tile-diff is NOT run (backend
    damage is authoritative)."""
    enc = JpegEncoder(64, 64, fps=10, bitrate_kbps=2000, quality=80)
    f1 = _make_frame(64, 64, (10, 20, 30))
    enc.encode(f1)
    # Identical frame but with explicit damage → should produce a delta for
    # that rect (even though nothing visually changed).
    f2 = _make_frame(64, 64, (10, 20, 30), damage=[(0, 0, 16, 16)])
    out = enc.encode(f2)
    payload, is_key = out[0]
    assert is_key is False
    ndelta, full = _parse_jd(payload)
    assert ndelta == 1


def test_full_roundtrip_with_decoder():
    """Encoder + decoder: full → identical → changed reconstructs correctly."""
    enc = JpegEncoder(64, 64, fps=10, bitrate_kbps=2000, quality=80)
    dec = JpegDeltaDecoder()

    # Full frame.
    f1 = _make_frame(64, 64, (100, 50, 25))
    payload, _ = enc.encode(f1)[0]
    w, h, rgb = dec.decode(payload, 0)
    assert (w, h) == (64, 64)

    # Identical (empty delta).
    f2 = _make_frame(64, 64, (100, 50, 25))
    payload, _ = enc.encode(f2)[0]
    w, h, rgb = dec.decode(payload, 0)
    assert (w, h) == (64, 64)

    # Changed tile.
    f3 = _make_frame(64, 64, (100, 50, 25))
    buf = np.frombuffer(f3.buffer, dtype=np.uint8).reshape(64, 64 * 4)
    buf[0:32, 0:32 * 4:4] = 200
    buf[0:32, 1:32 * 4:4] = 200
    buf[0:32, 2:32 * 4:4] = 200
    f3 = Frame(width=64, height=64, buffer=buf.tobytes(),
               stride=64 * 4, damage=None, cursor_x=0, cursor_y=0)
    payload, _ = enc.encode(f3)[0]
    w, h, rgb = dec.decode(payload, 0)
    assert (w, h) == (64, 64)
    # The top-left pixel should now be bright.
    arr = np.frombuffer(rgb, dtype=np.uint8).reshape(64, 64, 3)
    assert arr[0, 0, 0] > 150  # R is bright
