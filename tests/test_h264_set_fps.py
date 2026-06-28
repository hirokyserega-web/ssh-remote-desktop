"""H264Encoder.set_fps must push the new framerate into the PyAV CodecContext.

Previously set_fps (inherited from the base Encoder) only updated self.fps,
leaving self._cc.framerate at the value fixed at construction time. The
adaptation logic in connection._adapt calls set_fps when the FPS changes,
but the encoder kept stamping packets with the old framerate — so the
capture loop's sleep period changed while the encoder's PTS pacing did not.
This test verifies the override forwards the new fps to _cc.framerate.
"""

from __future__ import annotations

import pytest

from server.encoder import H264Encoder, Encoder, _HAVE_AV


pytestmark = pytest.mark.skipif(
    not _HAVE_AV,
    reason="PyAV not installed — H264Encoder requires it",
)


def _make_encoder(fps=30):
    return H264Encoder(width=320, height=240, fps=fps, bitrate_kbps=2000,
                       codec="h264")


def test_set_fps_updates_codec_context_framerate():
    enc = _make_encoder(fps=30)
    assert enc._cc.framerate == 30
    enc.set_fps(15)
    assert enc.fps == 15
    assert enc._cc.framerate == 15


def test_set_fps_clamps_and_propagates():
    enc = _make_encoder(fps=60)
    enc.set_fps(0)  # base clamps to max(1, ...)
    assert enc.fps == 1
    assert enc._cc.framerate == 1


def test_base_encoder_set_fps_still_just_sets_self_fps():
    """The base Encoder (e.g. JpegEncoder) only updates self.fps — no _cc."""
    base = Encoder(width=320, height=240, fps=30, bitrate_kbps=2000)
    base.set_fps(10)
    assert base.fps == 10
