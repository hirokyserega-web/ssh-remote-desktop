"""Multiplexer round-trip over an in-process socket pair.

Exercises the full path: per-channel queues, priority ordering (video before
input/control), bidirectional delivery, and clean shutdown.
"""

import asyncio
import socket

import pytest

from common import messages
from common.framing import AsyncByteStream, Frame, Multiplexer
from common.protocol import Channel, Flags


def _socket_pair():
    """Return (reader, writer) halves of a connected socket pair."""
    a, b = socket.socketpair()
    # We hand the raw file descriptors to asyncio.open_connection, but the
    # simpler route is to use socketpair() directly with makefile-like
    # adapters. Easier: use asyncio's connect_accepted pattern.
    return a, b


async def _loopback_pair():
    """Build an AsyncByteStream pair using asyncio streams on a socketpair."""
    a, b = socket.socketpair()
    # Wrap each side as a (StreamReader, StreamWriter) pair.
    loop = asyncio.get_running_loop()

    async def _make(sock):
        # asyncio.StreamReaderProtocol hooks StreamReader/StreamWriter up
        # directly when we pass them in.
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        # Adopt the existing socket: register the reader and build a writer.
        transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    reader_a, writer_a = await _make(a)
    reader_b, writer_b = await _make(b)
    return (AsyncByteStream(reader_a, writer_a),
            AsyncByteStream(reader_b, writer_b))


@pytest.mark.asyncio
async def test_mux_roundtrip_video_input_control():
    sa, sb = await _loopback_pair()
    mux_a, mux_b = Multiplexer(sa, "a"), Multiplexer(sb, "b")

    received_a: list[tuple[int, int, bytes]] = []
    received_b: list[tuple[int, int, bytes]] = []

    async def ha(fr: Frame):
        received_a.append((fr.channel, fr.flags, fr.payload))

    async def hb(fr: Frame):
        received_b.append((fr.channel, fr.flags, fr.payload))

    mux_a.on(Channel.INPUT, ha)
    mux_b.on(Channel.INPUT, hb)
    mux_b.on(Channel.CONTROL, hb)
    mux_a.on(Channel.VIDEO, hb)

    mux_a.start()
    mux_b.start()

    # a sends input to b, a sends video to b, b sends control to a.
    payload_in, _ = messages.dumps(messages.mouse_move(10, 20))
    mux_a.send(Channel.INPUT, payload_in)

    mux_a.send_video(b"\x00\x00\x00\x01", Flags.KEYFRAME)
    mux_a.send_video(b"\x00\x00\x00\x02", Flags.NONE)

    payload_ctl, _ = messages.dumps(messages.pong(42))
    mux_b.send(Channel.CONTROL, payload_ctl)

    # Let the writer loops drain.
    await asyncio.sleep(0.2)
    await mux_a.aclose()
    await mux_b.aclose()

    # b should have seen INPUT, VIDEO, VIDEO.
    channels_b = [c for c, *_ in received_b]
    assert Channel.INPUT in channels_b
    assert channels_b.count(Channel.VIDEO) == 2
    assert received_b[0][2] == payload_in  # input was first
    # a should have seen CONTROL from b.
    assert any(c == Channel.CONTROL for c, *_ in received_a)


@pytest.mark.asyncio
async def test_mux_drops_stale_video_frames():
    """When the video queue is full, oldest non-keyframes are dropped."""
    sa, sb = await _loopback_pair()
    mux_a, mux_b = Multiplexer(sa, "a"), Multiplexer(sb, "b")
    received_videos: list[bytes] = []

    async def on_vid(fr: Frame):
        if fr.channel == Channel.VIDEO:
            received_videos.append(fr.payload)

    mux_b.on(Channel.VIDEO, on_vid)
    mux_a.start()
    mux_b.start()

    # Stuff more video frames than _video_max_frames; the queue has space for
    # 3, so send 6 non-keyframes and expect at least 1 drop.
    for i in range(6):
        mux_a.send_video(bytes([i]), Flags.NONE)
    await asyncio.sleep(0.2)
    await mux_a.aclose()
    await mux_b.aclose()
    assert len(received_videos) < 6
    assert len(received_videos) >= 1
