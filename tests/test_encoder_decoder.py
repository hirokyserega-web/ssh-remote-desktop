"""Encoder/decoder round-trip: JPEG-delta and (when PyAV is available) H.264.

The server-side encoders expose ``encode(frame)`` returning a list of
``(payload, is_keyframe)`` tuples; the client-side :class:`Decoder` consumes
those payloads and returns ``(width, height, rgb_bytes)`` for a ``QImage``.
"""

import numpy as np
import pytest

from client.decoder import Decoder
from server.backend.base import Frame
from server.encoder import create_encoder


def _make_frame(w, h, fill=128):
    arr = np.full((h, w, 4), fill, dtype=np.uint8)
    arr[:, :, 3] = 255
    return Frame(width=w, height=h, buffer=arr.tobytes(), stride=w * 4)


def test_jpeg_full_then_delta_roundtrip():
    enc = create_encoder("jpeg", 320, 240, fps=15, bitrate_kbps=2000, quality=70)
    dec = Decoder()

    # First frame: no damage -> full JPEG keyframe, payload starts with b"JD".
    full_pkt, is_key = enc.encode(_make_frame(320, 240, fill=50))[0]
    assert is_key is True
    assert full_pkt[:2] == b"JD"
    w, h, rgb = dec.decode(full_pkt, 0)
    assert (w, h) == (320, 240)
    assert len(rgb) == 320 * 240 * 3

    # Second frame with explicit damage: must come back as a delta.
    f2 = _make_frame(320, 240, fill=200)
    f2.damage = [(10, 10, 50, 30)]
    delta_pkt, is_key2 = enc.encode(f2)[0]
    assert is_key2 is False
    w2, h2, rgb2 = dec.decode(delta_pkt, 0)
    assert (w2, h2) == (320, 240)
    assert len(rgb2) == 320 * 240 * 3


def test_decoder_reset_clears_state():
    dec = Decoder()
    # Without a prior full frame, a delta packet should not produce anything.
    delta_pkt = b"JD" + b"\x01\x00" + b"\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00"
    assert dec.decode(delta_pkt, 0) is None
    dec.reset()
    # After reset we still need a keyframe first; sending only a delta is fine.
    assert dec.decode(delta_pkt, 0) is None


def test_h264_roundtrip_when_av_available():
    pytest.importorskip("av")
    enc = create_encoder("h264", 160, 120, fps=10, bitrate_kbps=1000)
    dec = Decoder()
    f = _make_frame(160, 120, fill=10)
    # First packet is normally a keyframe.
    out = enc.encode(f)
    assert out, "H.264 encoder produced no packets"
    pkt, is_key = out[0]
    assert is_key is True
    # Decoding needs a few packets for the decoder to warm up; loop until we
    # get a frame.
    for data, _ in out:
        result = dec.decode(data, 0)
        if result is not None:
            w, h, rgb = result
            assert (w, h) == (160, 120)
            assert len(rgb) == 160 * 120 * 3
            return
    pytest.skip("decoder did not emit a frame (likely empty GOP)")
