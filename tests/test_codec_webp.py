"""Tests for C1: the 'webp' codec must NOT be a silent no-op."""
from __future__ import annotations

import logging


from server.encoder import JpegEncoder, create_encoder
from server.backend.base import Frame


def _frame(w, h, fill=128):
    import numpy as np
    arr = np.full((h, w, 4), fill, dtype=np.uint8)
    arr[:, :, 3] = 255
    return Frame(width=w, height=h, buffer=arr.tobytes(), stride=w * 4)


def test_create_encoder_unknown_codec_warns_and_falls_back_to_jpeg(caplog):
    """A stray 'webp' (or any unknown codec) must log a WARNING and fall back
    to JpegEncoder instead of silently producing an undecodable stream."""
    enc = create_encoder("webp", 64, 48, fps=5, bitrate_kbps=500)
    assert isinstance(enc, JpegEncoder)
    with caplog.at_level(logging.WARNING, logger="rd.encoder"):
        create_encoder("webp", 64, 48, fps=5, bitrate_kbps=500)
    assert any("webp" in r.message and "jpeg" in r.message.lower()
               for r in caplog.records), \
        "unsupported codec must be logged as a WARNING, not silently handled"


def test_create_encoder_known_codecs_no_warning(caplog):
    """h264/h265/jpeg must NOT emit the 'unsupported codec' warning."""
    with caplog.at_level(logging.WARNING, logger="rd.encoder"):
        for codec in ("jpeg",):
            create_encoder(codec, 64, 48, fps=5, bitrate_kbps=500)
    assert not any("unsupported codec" in r.message for r in caplog.records)


def test_webp_encoder_round_trip_when_pillow_available():
    """A JPEG fallback for 'webp' must still produce a decodable full frame."""
    enc = create_encoder("webp", 32, 24, fps=5, bitrate_kbps=500)
    out = enc.encode(_frame(32, 24, fill=200))
    assert out, "jpeg fallback produced no packets"
    payload, is_key = out[0]
    assert is_key is True
    assert payload[:2] == b"JD"  # JPEG-delta container the client understands


def test_negotiate_codec_webp_falls_back_with_warning(caplog):
    """ConnectionHandler._negotiate_codec must log webp -> jpeg and return
    'jpeg' (never 'webp'), so the session reply advertises a real codec."""
    from server.connection import ConnectionHandler

    class _Cfg:
        codec = "h264"

    h = ConnectionHandler.__new__(ConnectionHandler)
    h.cfg = _Cfg()
    with caplog.at_level(logging.WARNING, logger="rd.connection"):
        result = h._negotiate_codec("webp")
    assert result == "jpeg"
    assert any("webp" in r.message for r in caplog.records)


def test_negotiate_codec_unknown_falls_back_to_server_default(caplog):
    """An entirely unknown codec string falls back to the server's configured
    codec with a warning, never returns the bogus string."""
    from server.connection import ConnectionHandler

    class _Cfg:
        codec = "jpeg"

    h = ConnectionHandler.__new__(ConnectionHandler)
    h.cfg = _Cfg()
    with caplog.at_level(logging.WARNING, logger="rd.connection"):
        result = h._negotiate_codec("some-future-codec")
    assert result == "jpeg"
    assert any("some-future-codec" in r.message for r in caplog.records)
