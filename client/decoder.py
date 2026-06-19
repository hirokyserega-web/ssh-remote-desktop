"""Client-side frame decoder mirroring :mod:`server.encoder`.

* H.264/H.265 packets are decoded with PyAV into an RGB image.
* JPEG-delta packets ('JD' container) are blitted onto a persistent
  framebuffer so only changed tiles are updated.

The decoder keeps the last full RGB framebuffer (as a bytearray) so JPEG delta
tiles compose correctly; it returns ``(width, height, rgb_bytes)`` ready for a
``QImage`` in ``Format_RGB888``.
"""

from __future__ import annotations

import io
import logging
import struct

log = logging.getLogger("rd.client.decoder")

try:
    import numpy as np

    _HAVE_NUMPY = True
except Exception:  # pragma: no cover
    _HAVE_NUMPY = False

try:
    import av

    _HAVE_AV = True
except Exception:  # pragma: no cover
    _HAVE_AV = False

try:
    from PIL import Image

    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False



class BaseDecoder:
    def decode(self, payload: bytes, flags: int):
        """Return ``(width, height, rgb_bytes)`` or ``None`` if not ready."""
        raise NotImplementedError

    def close(self):
        pass


class H264Decoder(BaseDecoder):
    """H.264 / H.265 (HEVC) decoder via PyAV.

    The server emits Annex-B bitstream packets (libx264 / libx265 output).
    PyAV's ``decode(av.Packet(...))`` consumes Annex-B directly, so we do NOT
    run the container demuxer (``CodecContext.parse``); that path buffers and
    delays frames, and its result was being overwritten anyway. Decoding each
    packet directly is the correct low-latency path for our wire format.
    """

    # Map our wire codec names to PyAV decoder names.
    _CODEC_TO_AV = {"h264": "h264", "h265": "hevc", "hevc": "hevc"}

    def __init__(self, codec="h264"):
        av_name = self._CODEC_TO_AV.get(codec, "h264")
        self._cc = av.CodecContext.create(av_name, "r")
        self._w = self._h = 0

    def decode(self, payload: bytes, flags: int):
        if not payload:
            return None
        last = None
        try:
            pkt = av.Packet(payload)
        except Exception:
            return None
        try:
            for frame in self._cc.decode(pkt):
                rgb = frame.to_ndarray(format="rgb24")
                self._h, self._w = rgb.shape[0], rgb.shape[1]
                last = (self._w, self._h, rgb.tobytes())
        except Exception:
            # Partial NAL / not enough data yet: silently wait for more.
            return None
        return last

    def close(self):
        try:
            self._cc.close()
        except Exception:
            pass


class JpegDeltaDecoder(BaseDecoder):
    """Reassembles the 'JD' container produced by JpegEncoder."""

    def __init__(self):
        self._w = 0
        self._h = 0
        self._fb = None  # numpy HxWx3 RGB

    def decode(self, payload: bytes, flags: int):
        if not (_HAVE_NUMPY and _HAVE_PIL):
            return None
        if payload[:2] != b"JD":
            return None
        ndelta, full = struct.unpack("!BB", payload[2:4])
        off = 4
        if full:
            w, h, ln = struct.unpack("!HHI", payload[off:off + 8])
            off += 8
            jpeg = payload[off:off + ln]
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            self._fb = np.asarray(img, dtype=np.uint8).copy()
            self._h, self._w = self._fb.shape[0], self._fb.shape[1]
            return (self._w, self._h, self._fb.tobytes())
        if self._fb is None:
            return None  # need a full frame first
        for _ in range(ndelta):
            x, y, w, h, ln = struct.unpack("!HHHHI", payload[off:off + 12])
            off += 12
            jpeg = payload[off:off + ln]
            off += ln
            tile = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"), dtype=np.uint8)
            self._fb[y:y + h, x:x + w, :] = tile[:h, :w, :]
        return (self._w, self._h, self._fb.tobytes())


def create_decoder(codec: str) -> BaseDecoder:
    """Build the concrete decoder for a wire codec name.

    ``h264`` / ``h265`` / ``hevc`` -> :class:`H264Decoder` (when PyAV is
    importable); ``jpeg`` (or anything else, or PyAV missing) -> JPEG-delta.
    """
    if codec in ("h264", "h265", "hevc") and _HAVE_AV:
        try:
            return H264Decoder(codec)
        except Exception as exc:
            log.warning("decoder init for %s failed (%s); using JPEG", codec, exc)
    return JpegDeltaDecoder()


class Decoder:
    """Auto-detecting decoder facade used by the GUI.

    When the negotiated codec is known (from the ``session`` reply), pass it to
    :meth:`reset` so the inner decoder is pre-built and no first-packet guessing
    happens -- this matters for h265, where guessing would wrongly create an
    h264 decoder. When the codec is unknown/``None`` the facade auto-detects
    from the first packet (``JD`` magic -> JPEG, otherwise H.264).
    """

    def __init__(self, codec: str | None = None):
        self._inner: BaseDecoder | None = None
        self._codec: str | None = codec
        if codec is not None:
            self._inner = create_decoder(codec)

    def reset(self, codec: str | None = None):
        if self._inner is not None:
            try:
                self._inner.close()
            except Exception:
                pass
        self._codec = codec
        # Pre-build when the codec is known; otherwise defer to first packet.
        self._inner = create_decoder(codec) if codec else None

    def decode(self, payload: bytes, flags: int):
        if self._inner is None:
            if payload[:2] == b"JD":
                self._inner = JpegDeltaDecoder()
            elif _HAVE_AV:
                self._inner = H264Decoder("h264")
            else:
                self._inner = JpegDeltaDecoder()
        try:
            return self._inner.decode(payload, flags)
        except Exception:
            log.debug("decode failed", exc_info=True)
            return None

    def close(self):
        self.reset()
