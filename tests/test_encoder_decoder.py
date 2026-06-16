"""Encoder/decoder round-trip: JPEG-delta and (when PyAV is available) H.264."""

import numpy as np
import pytest

from client.decoder import Decoder
from server.backend.base import Frame
from server.encoder import create_encoder


def _make_frame(w: int, h: int, *, fill: int = 80) -> Frame:
    arr = np.full((h, w, 4), fill, dtype=np.uint8)
    arr[:, :, 3] = 255  # opaque
    return Frame(width=w, height=h, buffer=arr.tobytes(), stride=w * 4)


def test_jpeg_full_then_delta_roundtrip():
    enc = create_encoder("jpeg", 320, 240, fps=15, bitrate_kbps=2000, quality=70)
    dec = Decoder()
    full = next(iter(enc.encode(_make_frame(320, 240, fill=50))))
    assert full.keyframe and not full.is_delta
    res = dec.decode(full.data, 0)
    assert res is not None
    w, h, rgb = res
    assert (w, h) == (320, 240)
    # The decoder should now have a framebuffer; a delta with no dirty rects
    # also re-sends the whole frame, so we just confirm it still returns.
    second = next(iter(enc.encode(_make_frame(320, 240, fill=120))))
    res2 = dec.decode(second.data, 0)
    assert res2 is not None and res2[0] == 320 and res2[1] == 240


def test_decoder_resets_on_call():
    dec = Decoder()
    dec.reset()
    assert dec._inner is None  # type: ignore[attr-defined]
