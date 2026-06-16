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
    def __init__(self, codec="h264"):
        codec_name = "h264" if codec == "h264" else "hevc"
        self._cc = av.CodecContext.create(codec_name, "r")
        self._w = self._h = 0

    def decode(self, payload: bytes, flags: int):
        try:
            packets = self._cc.parse(payload)
        except Exception:
            packets = []
            try:
                pkt = av.Packet(payload)
                packets = [pkt]
            except Exception:
                return None
        last = None
        for pkt in packets:
            try:
                for frame in self._cc.decode(pkt):
                    rgb = frame.to_ndarray(format="rgb24")
                    self._h, self._w = rgb.shape[0], rgb.shape[1]
                    last = (self._w, self._h, rgb.tobytes())
            except Exception:
                continue
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
    if codec in ("h264", "h265") and _HAVE_AV:
        try:
            return H264Decoder(codec)
        except Exception as exc:
            log.warning("H.264 decoder init failed (%s); using JPEG", exc)
    return JpegDeltaDecoder()


class Decoder:
    """Auto-detecting decoder facade used by the GUI.

    Picks the concrete decoder from the first received packet: a ``JD`` magic
    prefix means JPEG-delta, anything else is treated as an H.264/H.265
    bitstream. :meth:`reset` drops the inner decoder so the next packet
    re-detects (used when a new session starts).
    """

    def __init__(self):
        self._inner: BaseDecoder | None = None

    def reset(self):
        if self._inner is not None:
            try:
                self._inner.close()
            except Exception:
                pass
        self._inner = None

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
