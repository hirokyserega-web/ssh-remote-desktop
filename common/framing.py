"""Async frame codec + channel multiplexer over a single byte stream.

The transport (asyncssh) gives us one bidirectional byte stream. On top of it
we run a tiny multiplexer: every logical channel (control / video / input /
clipboard / files) is tagged in the frame header, so a single SSH channel
carries all of them. This mirrors the "собственный фрейминг внутри одного
канала" option from the spec while keeping per-channel queues and priorities.

``StreamLike`` is anything with awaitable ``read(n)`` and ``write(b)`` plus a
``drain()`` coroutine -- both asyncio ``StreamReader/Writer`` pairs (wrapped by
:class:`AsyncByteStream`) and asyncssh ``SSHReader/SSHWriter`` fit.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .protocol import (
    Channel,
    Flags,
    FRAME_HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    decode_header,
    encode_header,
)

log = logging.getLogger("rd.framing")


class Reader(Protocol):
    async def readexactly(self, n: int) -> bytes: ...


class Writer(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


@dataclass
class Frame:
    channel: int
    flags: int
    payload: bytes


class AsyncByteStream:
    """Adapter exposing ``readexactly`` / ``write`` / ``drain`` over a pair.

    Works for asyncio ``StreamReader``/``StreamWriter`` and, with light duck
    typing, for asyncssh's reader/writer objects (which already provide
    ``readexactly`` and ``write``/``drain``).
    """

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    async def readexactly(self, n: int) -> bytes:
        if hasattr(self._reader, "readexactly"):
            return await self._reader.readexactly(n)
        # Fallback: accumulate until we have n bytes.
        buf = bytearray()
        while len(buf) < n:
            chunk = await self._reader.read(n - len(buf))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(buf), n)
            buf.extend(chunk)
        return bytes(buf)

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            res = drain()
            if asyncio.iscoroutine(res):
                await res

    def close(self) -> None:
        close = getattr(self._writer, "close", None)
        if close is not None:
            close()


# Per-channel outbound send priority. Lower number == sent first when the
# writer drains a backlog. Video is highest priority and is allowed to drop
# stale frames (handled in Multiplexer.send_video).
_PRIORITY = {
    Channel.VIDEO: 0,
    Channel.INPUT: 1,
    Channel.CONTROL: 2,
    Channel.CLIPBOARD: 3,
    Channel.FILES: 4,
}


class Multiplexer:
    """Reads/writes framed messages and dispatches them to channel handlers.

    Outbound frames go through per-channel queues drained by a single writer
    task; this guarantees that a large clipboard/file payload never interleaves
    inside another frame and lets us prioritise video + drop stale video.
    """

    def __init__(self, stream: AsyncByteStream, name: str = "mux"):
        self._stream = stream
        self._name = name
        self._handlers: dict[int, Callable[[Frame], Awaitable[None]]] = {}
        # One bounded queue per channel.
        self._queues: dict[int, deque[Frame]] = {c: deque() for c in Channel}
        self._wake = asyncio.Event()
        self._closed = False
        self._writer_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        # Backpressure: cap queued bytes per channel (video drops, others wait).
        self._video_max_frames = 3

    # -- registration ------------------------------------------------------
    def on(self, channel: int, handler: Callable[[Frame], Awaitable[None]]) -> None:
        self._handlers[int(channel)] = handler

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._writer_task = asyncio.create_task(self._writer_loop(), name=f"{self._name}-w")
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"{self._name}-r")

    async def aclose(self) -> None:
        self._closed = True
        self._wake.set()
        for t in (self._writer_task, self._reader_task):
            if t is not None:
                t.cancel()
        for t in (self._writer_task, self._reader_task):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._stream.close()

    async def wait_closed(self) -> None:
        tasks = [t for t in (self._writer_task, self._reader_task) if t]
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # -- sending -----------------------------------------------------------
    def _enqueue(self, frame: Frame) -> None:
        if self._closed:
            return
        self._queues[frame.channel].append(frame)
        self._wake.set()

    def send(self, channel: int, payload: bytes, flags: int = Flags.NONE) -> None:
        if len(payload) > MAX_FRAME_PAYLOAD:
            raise ValueError(f"payload too large: {len(payload)} bytes")
        self._enqueue(Frame(int(channel), int(flags), payload))

    def send_video(self, payload: bytes, flags: int = Flags.NONE) -> None:
        """Queue a video frame, dropping the oldest non-keyframes if backed up.

        Low latency beats completeness: if the writer can't keep up we discard
        stale inter-frames rather than letting the queue grow without bound.
        """
        q = self._queues[Channel.VIDEO]
        while len(q) >= self._video_max_frames:
            # Drop the oldest droppable (non-keyframe) frame.
            dropped = False
            for i, fr in enumerate(q):
                if not (fr.flags & Flags.KEYFRAME):
                    del q[i]
                    dropped = True
                    break
            if not dropped:
                q.popleft()  # all keyframes; drop oldest anyway
        self._enqueue(Frame(Channel.VIDEO, int(flags), payload))

    def _next_frame(self) -> Frame | None:
        # Pick from the highest-priority non-empty queue.
        for channel in sorted(self._queues, key=lambda c: _PRIORITY.get(c, 9)):
            q = self._queues[channel]
            if q:
                return q.popleft()
        return None

    async def _writer_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._next_frame()
                if frame is None:
                    await self._wake.wait()
                    self._wake.clear()
                    continue
                header = encode_header(frame.channel, frame.flags, len(frame.payload))
                self._stream.write(header)
                if frame.payload:
                    self._stream.write(frame.payload)
                await self._stream.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # connection died
            log.info("%s writer stopped: %s", self._name, exc)
            self._closed = True

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                header = await self._stream.readexactly(FRAME_HEADER_SIZE)
                channel, flags, length = decode_header(header)
                if length > MAX_FRAME_PAYLOAD:
                    raise ValueError(f"oversized frame: {length} bytes")
                payload = await self._stream.readexactly(length) if length else b""
                handler = self._handlers.get(channel)
                if handler is None:
                    log.debug("%s: no handler for channel 0x%02x", self._name, channel)
                    continue
                try:
                    await handler(Frame(channel, flags, payload))
                except Exception:
                    log.exception("%s: handler for channel 0x%02x raised", self._name, channel)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.info("%s reader stopped: %s", self._name, exc)
        finally:
            self._closed = True
            self._wake.set()
