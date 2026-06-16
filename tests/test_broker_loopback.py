"""End-to-end broker loopback.

Spins up a real asyncssh broker on a free port, talks to it from a real
asyncssh client using the same multiplexing protocol the GUI uses, and checks
that the handshake completes and a frame flows through.
"""

import asyncio
import socket

import asyncssh
import pytest

from common import messages
from common.framing import AsyncByteStream, Multiplexer
from common.protocol import Channel, Flags
from server.broker import Broker
from server.encoder import create_encoder
from server.backend.base import Frame
import numpy as np


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _await(predicate, timeout=5.0, interval=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_handshake_and_video_roundtrip(tmp_path):
    # Minimal server config; backend 'x11' is fine because the test session
    # uses mss for capture only when called -- we never call capture here.
    from common.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=_free_port(),
        host_key=str(tmp_path / "host_ed25519"),
        backend="x11",  # session() is built without a real X server
        allow_password=True,
        allow_publickey=False,
        run_as_user=False,  # unprivileged dev/test
    )

    # Patch the server's session start to be a no-op (we only want the channel
    # handshake here; full session bring-up is exercised in a heavier test).
    from server import session as session_mod

    def fake_acquire(self, user, *, geometry, persistent):
        # Build a session object the connection handler can use without spawning
        # any display server. We give it a tiny dummy backend via a stand-in.
        from server.session import Session
        s = object.__new__(Session)
        s.cfg = self.cfg
        s.user = user
        s.backend_kind = "x11"
        s.geometry = geometry
        s.persistent = persistent
        s.session_id = "test01"
        s.display = ":99"
        s.wayland_display = None
        s._procs = []
        s._xauth = None
        s._last_activity = 0
        s._stopped = False
        s.backend = _DummyBackend(geometry)
        return s

    def fake_release(self, s):
        s._stopped = True

    class _DummyBackend:
        def __init__(self, geom):
            self.kind = "x11"
            self._w, self._h = geom
        def screen_size(self):
            return (self._w, self._h)
        def capture(self):
            return _make_frame(self._w, self._h)
        def cursor(self):
            return None
        def start(self):
            pass
        def stop(self):
            pass
        def inject_mouse_move(self, x, y): pass
        def inject_mouse_button(self, b, d): pass
        def inject_scroll(self, dx, dy): pass
        def inject_key(self, k, d, m=()): pass
        def get_clipboard_text(self): return None
        def set_clipboard_text(self, t): pass

    def _make_frame(w, h):
        arr = np.full((h, w, 4), 60, dtype=np.uint8)
        arr[:, :, 3] = 255
        return Frame(width=w, height=h, buffer=arr.tobytes(), stride=w * 4)

    # Patch the broker so it doesn't really spawn anything.
    broker = Broker(cfg)
    broker.password_validator = {"root": "secret"}
    broker.acquire_session = fake_acquire.__get__(broker)
    broker.release_session = fake_release.__get__(broker)

    server_task = asyncio.create_task(broker.start())
    await _await(lambda: broker._server is not None and broker._server.is_serving())

    try:
        # Connect a real SSH client and run the multiplexer over the stdio
        # channel the broker serves.
        async with asyncssh.connect(
            cfg.host, cfg.port, username="root", password="secret",  # noqa: S107
            known_hosts=None, client_keys=None,
        ) as conn:
            stdin, stdout, _stderr = await conn.open_session(encoding=None)
            stream = AsyncByteStream(stdout, stdin)

            got_session = asyncio.Event()
            got_video = asyncio.Event()
            video_count = {"n": 0}
            session_info = {}

            async def on_session(fr):
                msg = messages.loads(fr.payload, fr.flags)
                session_info.update(msg)
                got_session.set()

            async def on_video(fr):
                video_count["n"] += 1
                if video_count["n"] >= 1:
                    got_video.set()

            mux = Multiplexer(stream, name="test")
            mux.on(Channel.CONTROL, on_session)
            mux.on(Channel.VIDEO, on_video)
            mux.start()
            hello = messages.hello(
                codec="jpeg", view=(320, 240), user="root", auth="password",
                new_session=True, geometry=(320, 240), persistent=False, proto=1,
            )
            payload, flag = messages.dumps(hello)
            mux.send(Channel.CONTROL, payload, flag)

            # Pump a couple of video frames through the server-side encoder to
            # confirm the channel delivers them.
            await asyncio.wait_for(got_session.wait(), timeout=5)
            assert session_info.get("backend") == "x11"

            enc = create_encoder("jpeg", 320, 240, fps=10, bitrate_kbps=2000, quality=70)
            for _ in range(2):
                for data, is_key in enc.encode(_make_frame(320, 240)):
                    flags = Flags.KEYFRAME if is_key else Flags.NONE
                    if enc.is_image_codec:
                        flags |= Flags.DELTA
                    mux.send_video(data, flags)
                await asyncio.sleep(0.05)

            await asyncio.wait_for(got_video.wait(), timeout=5)
            assert video_count["n"] >= 1

            await mux.aclose()
    finally:
        broker._server.close()
        await broker._server.wait_closed()
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
