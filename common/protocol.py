"""Wire-level protocol constants and the fixed-length frame header.

Frame layout (matches the spec)::

    +--------+--------+------------------+------------------+
    | type   | flags  | length (uint32)  | payload (length) |
    | 1 byte | 1 byte | 4 bytes, BE      | ...              |
    +--------+--------+------------------+------------------+

``type``    -- one of :class:`Channel`.
``flags``   -- bit field, see :class:`Flags`.
``length``  -- big-endian unsigned 32-bit payload length.
``payload`` -- for ``control`` / ``input`` / ``clipboard`` / ``files`` it is a
               serialized message (JSON, or MessagePack when available); for
               ``video`` it is the raw encoded bitstream of one frame.
"""

from __future__ import annotations

import struct
from enum import IntEnum, IntFlag

#: Bumped whenever the wire format changes in an incompatible way. The value is
#: exchanged in the ``hello`` handshake and both peers refuse to continue when
#: the major versions disagree.
PROTO_VERSION = 1

#: ``!BBI`` -> type (u8), flags (u8), length (u32 big-endian).
FRAME_HEADER = struct.Struct("!BBI")
FRAME_HEADER_SIZE = FRAME_HEADER.size  # == 6

#: Hard cap on a single frame payload (16 MiB). Protects both peers from a
#: malformed/hostile length prefix triggering an unbounded allocation.
MAX_FRAME_PAYLOAD = 16 * 1024 * 1024


class Channel(IntEnum):
    """Logical channel multiplexed over the single SSH byte stream."""

    CONTROL = 0x01
    VIDEO = 0x02
    INPUT = 0x03
    CLIPBOARD = 0x04
    FILES = 0x05

    @classmethod
    def _missing_(cls, value):  # pragma: no cover - defensive
        return None


class Flags(IntFlag):
    """Per-frame flag bits stored in the header's ``flags`` byte."""

    NONE = 0x00
    #: Video frame is a keyframe / IDR (decoder can start here).
    KEYFRAME = 0x01
    #: Payload is MessagePack rather than JSON (control/input/clipboard/files).
    MSGPACK = 0x02
    #: Payload is a delta-encoded region rather than a full frame (JPEG mode).
    DELTA = 0x04
    #: Video payload carries an embedded cursor.
    CURSOR_EMBEDDED = 0x08


# ---------------------------------------------------------------------------
# Codec identifiers used in the handshake / session messages.
# ---------------------------------------------------------------------------
CODEC_H264 = "h264"
CODEC_H265 = "h265"
CODEC_JPEG = "jpeg"
CODEC_WEBP = "webp"

VALID_CODECS = frozenset({CODEC_H264, CODEC_H265, CODEC_JPEG, CODEC_WEBP})

# Backend identifiers reported by the server.
BACKEND_X11 = "x11"
BACKEND_WAYLAND = "wayland"


def encode_header(channel: int, flags: int, length: int) -> bytes:
    """Pack a frame header. ``length`` must already be validated by the caller."""
    return FRAME_HEADER.pack(int(channel), int(flags), int(length))


def decode_header(buf: bytes) -> tuple[int, int, int]:
    """Unpack a 6-byte header into ``(channel, flags, length)``."""
    return FRAME_HEADER.unpack(buf)
