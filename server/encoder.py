"""Frame encoders shared by both backends.

Two encoders implement a common interface (:class:`Encoder`):

* :class:`H264Encoder` -- uses **PyAV** (ffmpeg) for H.264/H.265. Best
  quality/bandwidth; supports dynamic bitrate. Produces Annex-B packets, each
  flagged as keyframe or not.
* :class:`JpegEncoder` -- per-frame JPEG with **delta by dirty rectangles**
  (from XDamage / PipeWire damage). Pure fallback that needs only Pillow; each
  message is a compact binary blob of changed tiles.

Both expose ``encode(frame, cursor) -> list[(bytes, is_keyframe)]``. When the
server runs in ``cursor_mode == "embedded"`` the hardware cursor image is
composited onto the frame here before encoding; otherwise cursor metadata is
sent separately on the control channel.

The :func:`create_encoder` factory picks H.264 when PyAV is importable and the
codec requests it, otherwise JPEG. Both honour :meth:`set_bitrate` /
:meth:`set_fps` calls coming from the adaptation logic.
"""

from __future__ import annotations

import io
import logging
import struct
from typing import Iterable

from .backend.base import CursorImage, Frame

log = logging.getLogger("rd.encoder")

try:
    import numpy as np

    _HAVE_NUMPY = True
except Exception:  # pragma: no cover
    _HAVE_NUMPY = False

try:
    import av  # PyAV

    _HAVE_AV = True
except Exception:  # pragma: no cover
    _HAVE_AV = False

try:
    from PIL import Image

    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


def _frame_to_bgr(frame: Frame):
    """Reshape a BGRA byte buffer into an (h, w, 3) BGR ndarray view."""
    arr = np.frombuffer(frame.buffer, dtype=np.uint8)
    arr = arr[: frame.height * frame.stride].reshape(frame.height, frame.stride // 4, 4)
    return arr[:, : frame.width, :3]


def _composite_cursor(bgr, frame: Frame, cursor: CursorImage | None):
    """Alpha-blend the hardware cursor onto a BGR frame at the pointer pos."""
    if cursor is None or not _HAVE_NUMPY:
        return bgr
    cw, ch = cursor.width, cursor.height
    if cw <= 0 or ch <= 0:
        return bgr
    px = frame.cursor_x - cursor.xhot
    py = frame.cursor_y - cursor.yhot
    h, w = bgr.shape[:2]
    x0, y0 = max(0, px), max(0, py)
    x1, y1 = min(w, px + cw), min(h, py + ch)
    if x0 >= x1 or y0 >= y1:
        return bgr
    cur = np.frombuffer(cursor.rgba, dtype=np.uint8).reshape(ch, cw, 4)
    sub_cur = cur[y0 - py:y1 - py, x0 - px:x1 - px, :]
    bgr = bgr.copy()  # don't mutate the read-only capture buffer
    region = bgr[y0:y1, x0:x1, :].astype(np.float32)
    alpha = (sub_cur[:, :, 3:4].astype(np.float32)) / 255.0
    cur_bgr = sub_cur[:, :, 2::-1].astype(np.float32)  # RGBA -> BGR
    blended = region * (1 - alpha) + cur_bgr * alpha
    bgr[y0:y1, x0:x1, :] = blended.astype(np.uint8)
    return bgr


class Encoder:
    codec = "none"
    is_image_codec = False

    def __init__(self, width: int, height: int, fps: int, bitrate_kbps: int):
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps

    def encode(self, frame: Frame, cursor: CursorImage | None = None):
        """Return a list of ``(payload_bytes, is_keyframe)`` tuples."""
        raise NotImplementedError

    def set_bitrate(self, kbps: int) -> None:
        self.bitrate_kbps = max(200, int(kbps))

    def set_fps(self, fps: int) -> None:
        self.fps = max(1, int(fps))

    def force_keyframe(self) -> None:
        pass

    def close(self) -> None:
        pass


class H264Encoder(Encoder):
    """H.264 / H.265 via PyAV. Input frames are BGRA -> converted to yuv420p."""

    def __init__(self, width, height, fps, bitrate_kbps, codec="h264"):
        super().__init__(width, height, fps, bitrate_kbps)
        self.codec = codec
        codec_name = "libx264" if codec == "h264" else "libx265"
        self._cc = av.CodecContext.create(codec_name, "w")
        self._cc.width = width
        self._cc.height = height
        self._cc.pix_fmt = "yuv420p"
        self._cc.framerate = fps
        self._cc.bit_rate = bitrate_kbps * 1000
        self._cc.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "g": str(fps * 2),
        }
        self._force_key = False

    def force_keyframe(self) -> None:
        self._force_key = True

    def set_bitrate(self, kbps: int) -> None:
        super().set_bitrate(kbps)
        try:
            self._cc.bit_rate = self.bitrate_kbps * 1000
        except Exception:
            pass

    def encode(self, frame: Frame, cursor: CursorImage | None = None):
        if not _HAVE_NUMPY:
            return []
        bgr = _frame_to_bgr(frame)
        bgr = _composite_cursor(bgr, frame, cursor)
        vframe = av.VideoFrame.from_ndarray(bgr[:, :, ::-1].copy(), format="rgb24")
        vframe.pict_type = "I" if self._force_key else "NONE"
        self._force_key = False
        out = []
        for packet in self._cc.encode(vframe):
            out.append((bytes(packet), bool(packet.is_keyframe)))
        return out

    def close(self):
        try:
            for _ in self._cc.encode(None):
                pass
        except Exception:
            pass


class JpegEncoder(Encoder):
    """Per-frame JPEG with delta-by-dirty-rectangles.

    Wire format of one packet payload::

        magic 'JD' | u8 ndelta | full-flag u8 |
        if full: u16 w, u16 h, u32 len, jpeg-bytes
        else: ndelta * ( u16 x,y,w,h ; u32 len ; jpeg-bytes )

    The client reconstructs the framebuffer by blitting each tile.
    """

    codec = "jpeg"
    is_image_codec = True

    def __init__(self, width, height, fps, bitrate_kbps, quality=80):
        super().__init__(width, height, fps, bitrate_kbps)
        self.quality = quality
        self._sent_full = False

    def force_keyframe(self) -> None:
        self._sent_full = False

    def _encode_tile(self, arr) -> bytes:
        img = Image.fromarray(arr[:, :, ::-1])  # BGR -> RGB
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality)
        return buf.getvalue()

    def encode(self, frame: Frame, cursor: CursorImage | None = None):
        if not (_HAVE_NUMPY and _HAVE_PIL):
            return []
        bgr = _frame_to_bgr(frame)
        bgr = _composite_cursor(bgr, frame, cursor)

        full = (not self._sent_full) or not frame.damage
        if full:
            jpeg = self._encode_tile(bgr)
            payload = b"JD" + struct.pack("!BB", 0, 1)
            payload += struct.pack("!HHI", frame.width, frame.height, len(jpeg)) + jpeg
            self._sent_full = True
            return [(payload, True)]

        rects = _merge_rects(frame.damage, frame.width, frame.height)
        parts = [b"JD", struct.pack("!BB", len(rects), 0)]
        for (x, y, w, h) in rects:
            tile = bgr[y:y + h, x:x + w, :]
            jpeg = self._encode_tile(tile)
            parts.append(struct.pack("!HHHHI", x, y, w, h, len(jpeg)))
            parts.append(jpeg)
        return [(b"".join(parts), False)]


def _merge_rects(rects, maxw, maxh, limit=12):
    """Clamp + cap the number of dirty rects; merge into bounding box if many."""
    clamped = []
    for (x, y, w, h) in rects:
        x = max(0, min(x, maxw - 1))
        y = max(0, min(y, maxh - 1))
        w = max(1, min(w, maxw - x))
        h = max(1, min(h, maxh - y))
        clamped.append((x, y, w, h))
    if len(clamped) > limit:
        xs = [r[0] for r in clamped]
        ys = [r[1] for r in clamped]
        xe = [r[0] + r[2] for r in clamped]
        ye = [r[1] + r[3] for r in clamped]
        bx, by = min(xs), min(ys)
        return [(bx, by, max(xe) - bx, max(ye) - by)]
    return clamped


def create_encoder(codec: str, width: int, height: int, *, fps: int,
                   bitrate_kbps: int, quality: int = 80) -> Encoder:
    if codec in ("h264", "h265") and _HAVE_AV and _HAVE_NUMPY:
        try:
            return H264Encoder(width, height, fps, bitrate_kbps, codec)
        except Exception as exc:
            log.warning("H.264 encoder init failed (%s); falling back to JPEG", exc)
    if _HAVE_PIL and _HAVE_NUMPY:
        return JpegEncoder(width, height, fps, bitrate_kbps, quality)
    raise RuntimeError("no usable encoder: install pyav or pillow + numpy")
