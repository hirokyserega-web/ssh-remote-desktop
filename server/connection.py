"""Per-connection handler: wires the multiplexer to a session's backend.

Once a client opens the multiplexing SSH channel, a :class:`ConnectionHandler`:

* completes the ``hello``/``session`` handshake,
* creates (or reuses, when persistent) a :class:`Session`,
* runs the capture/encode loop pushing video frames,
* applies inbound input events to the backend,
* keeps clipboard synchronised in both directions (with loop protection),
* answers file-navigation commands (bytes themselves go via SFTP),
* maintains heartbeat and adapts FPS/bitrate from client stats.
"""

from __future__ import annotations

import asyncio
import logging

from common import messages
from common.framing import AsyncByteStream, Frame, Multiplexer
from common.protocol import Channel, Flags, ACCEPTED_PROTOS

from .encoder import create_encoder
from .session import Session, UserInfo

log = logging.getLogger("rd.connection")


class ConnectionHandler:
    def __init__(self, cfg, broker, username: str, reader, writer):
        self.cfg = cfg
        self.broker = broker
        self.username = username
        self.mux = Multiplexer(AsyncByteStream(reader, writer), name=f"srv-{username}")
        self.session: Session | None = None
        self.encoder = None
        self._video_task: asyncio.Task | None = None
        self._clip_task: asyncio.Task | None = None
        self._running = False
        self._last_clip_text = None     # loop protection
        self._fps = cfg.fps
        self._bitrate = cfg.bitrate_kbps
        self._stop_evt = asyncio.Event()

    # -- entry point -------------------------------------------------------
    async def run(self):
        self.mux.on(Channel.CONTROL, self._on_control)
        self.mux.on(Channel.INPUT, self._on_input)
        self.mux.on(Channel.CLIPBOARD, self._on_clipboard)
        self.mux.on(Channel.FILES, self._on_files)
        self.mux.start()
        try:
            await self._stop_evt.wait()
        finally:
            await self._teardown()

    # -- control plane -----------------------------------------------------
    async def _on_control(self, frame: Frame):
        msg = messages.loads(frame.payload, frame.flags)
        t = msg.get("t")
        if t == "hello":
            await self._handle_hello(msg)
        elif t == "ping":
            self._send_control(messages.pong(msg.get("ts", 0)))
        elif t == "stats":
            self._adapt(msg)
        elif t == "resize":
            # client view changed; record for coordinate scaling
            self._client_view = (int(msg["view"][0]), int(msg["view"][1]))

    async def _handle_hello(self, msg: dict):
        proto = int(msg.get("proto", 1))
        if proto not in ACCEPTED_PROTOS:
            self._send_control({"t": "error", "msg": f"unsupported proto {proto}"})
            self._stop_evt.set()
            return
        geometry = tuple(msg.get("geometry") or self.cfg.session_geometry)
        self._client_view = tuple(msg.get("view") or geometry)
        persistent = bool(msg.get("persistent", self.cfg.persistent_default))
        codec = msg.get("codec", self.cfg.codec)
        # If the client asked for a codec we cannot serve (e.g. h265 without
        # PyAV, or webp which we do not encode yet), fall back to the server
        # default and tell the client via the session reply so its decoder
        # auto-dects the right format.
        codec = self._negotiate_codec(codec)

        try:
            user = UserInfo(self.username)
        except KeyError:
            self._send_control({"t": "error", "msg": "unknown user"})
            self._stop_evt.set()
            return

        self.session = self.broker.acquire_session(
            user, geometry=geometry, persistent=persistent
        )
        backend = self.session.backend
        sw, sh = backend.screen_size()
        self.encoder = create_encoder(codec, sw, sh, fps=self._fps,
                                      bitrate_kbps=self._bitrate,
                                      quality=self.cfg.jpeg_quality)
        self._send_control(messages.session(
            session_id=self.session.session_id,
            backend=backend.kind,
            display=self.session.display,
            wayland_display=self.session.wayland_display,
            screen=(sw, sh), fps=self._fps,
            cursor=self.cfg.cursor_mode,
            codec=codec,
        ))
        self._running = True
        self._video_task = asyncio.create_task(self._video_loop())
        if self.cfg.clipboard_enabled:
            self._clip_task = asyncio.create_task(self._clipboard_loop())

    def _negotiate_codec(self, requested: str) -> str:
        """Return a codec we can actually encode, falling back to jpeg."""
        from .encoder import create_encoder as _ce  # noqa: F401  (capability probe)
        # We accept whatever the client asked for if the encoder factory can
        # build it; otherwise we drop to jpeg (always available via Pillow).
        if requested in ("h264", "h265"):
            try:
                import av  # noqa: F401
                import numpy  # noqa: F401
                return requested
            except Exception:
                return "jpeg"
        if requested == "jpeg":
            return "jpeg"
        # Anything else (e.g. a stale "webp" from an older config, or a typo)
        # is NOT a real wire codec the client can decode. Log it loudly and fall
        # back to jpeg instead of silently sending a stream the client can't
        # identify — the old code swallowed "webp" with no warning, so the
        # operator got JPEG while believing they had configured WebP.
        log.warning(
            "requested codec %r is not a supported wire codec; falling back to "
            "jpeg. Supported codecs: h264, h265, jpeg.", requested,
        )
        return "jpeg"

    def _adapt(self, msg: dict):
        """Lower or raise bitrate/FPS based on client-reported loss/latency."""
        loss = float(msg.get("loss", 0.0))
        rtt = float(msg.get("rtt_ms", 0.0))
        old_fps = self._fps
        if loss > 0.05 or rtt > 250:
            self._bitrate = max(800, int(self._bitrate * 0.8))
            self._fps = max(10, self._fps - 5)
        elif loss < 0.01 and rtt < 120:
            self._bitrate = min(self.cfg.bitrate_kbps, int(self._bitrate * 1.1) + 100)
            self._fps = min(self.cfg.fps, self._fps + 2)
        if self.encoder is not None:
            self.encoder.set_bitrate(self._bitrate)
            # Push the new FPS to the encoder too, not just the capture loop
            # period. Without this the encoder keeps its old frame-rate (and
            # thus its GOP / timestamp pacing) even after _video_loop picks up
            # the new 1/fps sleep, so adaptation only half-works.
            if self._fps != old_fps:
                self.encoder.set_fps(self._fps)

    # -- video loop --------------------------------------------------------
    async def _video_loop(self):
        backend = self.session.backend
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                frame_period = 1.0 / max(1, self._fps)
                t0 = loop.time()
                frame = await loop.run_in_executor(None, backend.capture)
                if frame is not None:
                    cursor = None
                    if self.cfg.cursor_mode == "embedded":
                        cursor = await loop.run_in_executor(None, backend.cursor)
                    packets = await loop.run_in_executor(
                        None, self.encoder.encode, frame, cursor
                    )
                    for data, is_key in packets:
                        flags = Flags.KEYFRAME if is_key else Flags.NONE
                        if self.encoder.is_image_codec:
                            flags |= Flags.DELTA
                        self.mux.send_video(data, flags)
                elapsed = loop.time() - t0
                await asyncio.sleep(max(0, frame_period - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("video loop error")
            self._stop_evt.set()

    # -- input -------------------------------------------------------------
    async def _on_input(self, frame: Frame):
        if not self.session:
            return
        msg = messages.loads(frame.payload, frame.flags)
        self.session.touch()
        backend = self.session.backend
        t = msg.get("t")
        # Scale client view coordinates to server screen.
        if t == "mouse_move":
            x, y = self._scale_coords(msg["x"], msg["y"])
            backend.inject_mouse_move(x, y)
        elif t == "mouse_btn":
            backend.inject_mouse_button(int(msg["button"]), bool(msg["down"]))
        elif t == "scroll":
            backend.inject_scroll(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif t == "key":
            backend.inject_key(msg["keysym"], bool(msg["down"]),
                               tuple(msg.get("mods", [])))

    def _scale_coords(self, x, y):
        """Map a client coordinate to server-screen pixels.

        Since PROTO_VERSION 2 the client sends NORMALIZED floats in ``[0, 1]``
        (the client already did the widget->draw-rect mapping). We multiply by
        the server screen size here -- the single scaling point. Legacy integer
        pixel coordinates (proto 1) are handled by the integer branch below.
        """
        sw, sh = self.session.backend.screen_size()
        if isinstance(x, float) or isinstance(y, float):
            fx = float(x)
            fy = float(y)
            fx = min(max(fx, 0.0), 1.0)
            fy = min(max(fy, 0.0), 1.0)
            return int(fx * max(1, sw - 1)), int(fy * max(1, sh - 1))
        # Legacy integer coordinates: scale from the reported client view size.
        vw, vh = getattr(self, "_client_view", (sw, sh))
        if vw and vh:
            return int(x * sw / vw), int(y * sh / vh)
        return int(x), int(y)

    # -- clipboard ---------------------------------------------------------
    async def _on_clipboard(self, frame: Frame):
        if not self.cfg.clipboard_enabled or not self.session:
            return
        msg = messages.loads(frame.payload, frame.flags)
        if msg.get("format") != "text":
            return
        data = msg.get("data", "")
        if len(data.encode("utf-8")) > self.cfg.clipboard_max_bytes:
            return
        # Loop protection: remember what we just received so the poll loop does
        # not echo it back to the client.
        self._last_clip_text = data
        await asyncio.get_running_loop().run_in_executor(
            None, self.session.backend.set_clipboard_text, data
        )

    async def _clipboard_loop(self):
        """Poll the server clipboard and push changes to the client."""
        backend = self.session.backend
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                await asyncio.sleep(0.7)
                text = await loop.run_in_executor(None, backend.get_clipboard_text)
                if text is None or text == self._last_clip_text:
                    continue
                if len(text.encode("utf-8")) > self.cfg.clipboard_max_bytes:
                    continue
                self._last_clip_text = text
                payload, flag = messages.dumps(
                    messages.clipboard("text", text, origin="server")
                )
                self.mux.send(Channel.CLIPBOARD, payload, flag)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("clipboard loop error")

    # -- files (navigation only; bytes go over SFTP) -----------------------
    async def _on_files(self, frame: Frame):
        if not self.cfg.files_enabled or not self.session:
            return
        msg = messages.loads(frame.payload, frame.flags)
        jail = self.broker.jail_for(self.session.user)
        t = msg.get("t")
        try:
            if t == "file_list":
                entries = jail.listdir(msg.get("path", ""))
                self._send_files({"t": "file_list_result",
                                  "path": msg.get("path", ""), "entries": entries})
            elif t == "file_mkdir":
                jail.mkdir(msg["path"])
                self._send_files({"t": "file_ok", "op": "mkdir", "path": msg["path"]})
            elif t == "file_remove":
                jail.remove(msg["path"])
                self._send_files({"t": "file_ok", "op": "remove", "path": msg["path"]})
            elif t == "file_stat":
                self._send_files({"t": "file_stat_result", "stat": jail.stat(msg["path"])})
        except Exception as exc:
            self._send_files({"t": "file_error", "msg": str(exc)})

    # -- senders -----------------------------------------------------------
    def _send_control(self, obj: dict):
        payload, flag = messages.dumps(obj)
        self.mux.send(Channel.CONTROL, payload, flag)

    def _send_files(self, obj: dict):
        payload, flag = messages.dumps(obj)
        self.mux.send(Channel.FILES, payload, flag)

    # -- teardown ----------------------------------------------------------
    async def _teardown(self):
        self._running = False
        for task in (self._video_task, self._clip_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            if self.encoder:
                self.encoder.close()
        except Exception:
            pass
        if self.session is not None:
            self.broker.release_session(self.session)
        await self.mux.aclose()
