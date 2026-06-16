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


def _make_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> AsyncByteStream:
    return AsyncByteStream(reader, writer)


async def _loopback_pair() -> tuple[AsyncByteStream, AsyncByteStream]:
    """Two connected asyncio StreamReader/Writer pairs over a localhost socket.

    The "server" side accepts one connection; the "client" side connects to
    it. We return (server_stream, client_stream).
    """
    loop = asyncio.get_running_loop()

    server_conn: asyncio.StreamReader | None = None
    server_writer: asyncio.StreamWriter | None = None
    server_connected = asyncio.Event()

    async def _on_client(reader, writer):
        nonlocal server_conn, server_writer
        server_conn = reader
        server_writer = writer
        server_connected.set()

    server = await asyncio.start_server(_on_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(server_connected.wait(), timeout=2)
        return (
            _make_stream(server_conn, server_writer),
            _make_stream(client_reader, client_writer),
        )
    finally:
        server.close()
        await server.wait_closed()


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

    payload_in, _ = messages.dumps(messages.mouse_move(10, 20))
    mux_a.send(Channel.INPUT, payload_in)

    mux_a.send_video(b"\x00\x00\x00\x01", Flags.KEYFRAME)
    mux_a.send_video(b"\x00\x00\x00\x02", Flags.NONE)

    payload_ctl, _ = messages.dumps(messages.pong(42))
    mux_b.send(Channel.CONTROL, payload_ctl)

    # Wait for the queues to drain, polling the writer side.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if (received_b and Channel.INPUT in [c for c, *_ in received_b]
                and any(c == Channel.CONTROL for c, *_ in received_a)):
            break

    assert received_b, "no frames received by b"
    assert any(c == Channel.INPUT for c, *_ in received_b)
    assert any(c == Channel.VIDEO for c, *_ in received_b)
    assert any(c == Channel.CONTROL for c, *_ in received_a)

    await mux_a.aclose()
    await mux_b.aclose()


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

    # Stuff more video frames than _video_max_frames (3); send 8 non-keyframes.
    for i in range(8):
        mux_a.send_video(bytes([i]), Flags.NONE)

    # Drain.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(received_videos) >= 3:
            break

    await mux_a.aclose()
    await mux_b.aclose()

    assert 1 <= len(received_videos) <= 3, f"unexpected: {len(received_videos)}"
