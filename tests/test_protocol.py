"""Wire-protocol tests: header encoding, max-payload guard, frame flags."""

import struct

import pytest

from common.protocol import (
    Channel,
    Flags,
    FRAME_HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    decode_header,
    encode_header,
)


def test_header_roundtrip():
    for ch, fl, ln in [
        (Channel.CONTROL, 0, 0),
        (Channel.VIDEO, Flags.KEYFRAME, 65535),
        (Channel.INPUT, Flags.NONE, 1),
        (Channel.CLIPBOARD, Flags.MSGPACK, 1234567),
        (Channel.FILES, Flags.DELTA | Flags.KEYFRAME, 42),
    ]:
        h = encode_header(ch, fl, ln)
        assert len(h) == FRAME_HEADER_SIZE == 6
        assert h[0] == int(ch)
        ch2, fl2, ln2 = decode_header(h)
        assert (ch2, fl2, ln2) == (int(ch), fl, ln)


def test_header_uses_big_endian_length():
    h = encode_header(Channel.VIDEO, 0, 0x01020304)
    assert h[2:6] == b"\x01\x02\x03\x04"


def test_max_payload_known():
    assert MAX_FRAME_PAYLOAD == 16 * 1024 * 1024


def test_encode_rejects_oversize():
    with pytest.raises(struct.error):
        encode_header(Channel.VIDEO, 0, MAX_FRAME_PAYLOAD + 1)
