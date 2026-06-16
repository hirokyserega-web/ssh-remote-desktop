"""Client SSH transport + multiplexed session, with auto-reconnect.

Runs an asyncio loop on a background thread and exposes a thread-safe-ish API
the Qt GUI can call. The transport:

* opens one SSH connection (key or password auth, host-key TOFU),
* opens a single SSH session channel and runs the frame multiplexer on it,
* performs the ``hello``/``session`` handshake,
* exposes callbacks for video packets, clipboard updates, file results and
  session info,
* reconnects automatically on drop.

File *bytes* use the same connection's SFTP subsystem (see
:mod:`client.files`); this module owns the asyncssh connection both share.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

import asyncssh

from common import messages
from common.framing import AsyncByteStream, Frame, Multiplexer
from common.protocol import Channel, Flags, PROTO_VERSION

log = logging.getLogger("rd.client.transport")


class Transport:
    def __init__(self, cfg, *, password: str | None = None):
        self.cfg = cfg
        self._password = password
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn = None
        self._chan_writer = None
        self.mux: Multiplexer | None = None
        self._stop = False
        self._connected = threading.Event()

        # Callbacks (set by the GUI). All are invoked from the asyncio thread.
        self.on_video: Callable[[bytes, int], None] = lambda data, flags: None
        self.on_clipboard: Callable[[dict], None] = lambda msg: None
        self.on_files: Callable[[dict], None] = lambda msg: None
        self.on_session: Callable[[dict], None] = lambda msg: None
        self.on_state: Callable[[str, str], None] = lambda state, detail: None

        self.session_info: dict | None = None

    # -- public API (thread-safe) -----------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="rd-transport", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def wait_connected(self, timeout: float = 30) -> bool:
        return self._connected.wait(timeout)

    def send_input(self, obj: dict):
        self._send(Channel.INPUT, obj)

    def send_control(self, obj: dict):
        self._send(Channel.CONTROL, obj)

    def send_clipboard(self, obj: dict):
        self._send(Channel.CLIPBOARD, obj)

    def send_files(self, obj: dict):
        self._send(Channel.FILES, obj)

    def get_connection(self):
        """Return the live asyncssh connection (for SFTP); may be None."""
        return self._conn

    def run_coro(self, coro):
        """Schedule a coroutine on the transport loop, return a concurrent future."""
        if self._loop is None:
            raise RuntimeError("transport loop not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- internals ---------------------------------------------------------
    def _send(self, channel: int, obj: dict):
        if self.mux is None or self._loop is None:
            return
        payload, flag = messages.dumps(obj)
        self._loop.call_soon_threadsafe(self.mux.send, channel, payload, flag)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("transport loop crashed")
        finally:
            self._loop.close()

    async def _main(self):
        attempts = 0
        while not self._stop:
            try:
                await self._connect_once()
                attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.on_state("error", str(exc))
                log.warning("connection failed: %s", exc)
            self._connected.clear()
            if self._stop or not self.cfg.auto_reconnect:
                break
            attempts += 1
            if self.cfg.max_reconnect_attempts and attempts > self.cfg.max_reconnect_attempts:
                self.on_state("failed", "max reconnect attempts reached")
                break
            self.on_state("reconnecting", f"attempt {attempts}")
            await asyncio.sleep(self.cfg.reconnect_delay)

    async def _connect_once(self):
        self.on_state("connecting", f"{self.cfg.host}:{self.cfg.port}")
        opts = self._connect_options()
        async with asyncssh.connect(self.cfg.host, self.cfg.port, **opts) as conn:
            self._conn = conn
            stdin, stdout, stderr = await conn.open_session(encoding=None)
            stream = AsyncByteStream(stdout, stdin)
            self.mux = Multiplexer(stream, name="cli")
            self.mux.on(Channel.VIDEO, self._h_video)
            self.mux.on(Channel.CONTROL, self._h_control)
            self.mux.on(Channel.CLIPBOARD, self._h_clipboard)
            self.mux.on(Channel.FILES, self._h_files)
            self.mux.start()
            await self._handshake()
            self.on_state("connected", self.cfg.host)
            self._connected.set()
            await self.mux.wait_closed()
            self._connected.clear()
            self.on_state("disconnected", "")

    def _connect_options(self) -> dict:
        import os
        opts: dict = {
            "username": self.cfg.user,
            "known_hosts": None if self.cfg.accept_unknown_host else self._known_hosts(),
        }
        if self.cfg.auth == "password" and self._password is not None:
            opts["password"] = self._password
            opts["client_keys"] = None
        elif self.cfg.auth == "agent":
            pass  # asyncssh uses the agent by default
        else:  # key
            key_path = os.path.expanduser(self.cfg.key_path)
            opts["client_keys"] = [key_path]
            if self._password:  # passphrase for the key
                opts["passphrase"] = self._password
        return opts

    def _known_hosts(self):
        import os
        path = os.path.expanduser(self.cfg.known_hosts)
        return path if os.path.exists(path) else None

    async def _handshake(self):
        hello = messages.hello(
            codec=self.cfg.codec,
            view=tuple(self.cfg.geometry),
            user=self.cfg.user,
            auth=self.cfg.auth,
            new_session=self.cfg.new_session,
            geometry=tuple(self.cfg.geometry),
            persistent=self.cfg.persistent,
            proto=PROTO_VERSION,
        )
        payload, flag = messages.dumps(hello)
        self.mux.send(Channel.CONTROL, payload, flag)

    # -- inbound handlers --------------------------------------------------
    async def _h_video(self, frame: Frame):
        self.on_video(frame.payload, frame.flags)

    async def _h_control(self, frame: Frame):
        msg = messages.loads(frame.payload, frame.flags)
        t = msg.get("t")
        if t == "session":
            self.session_info = msg
            self.on_session(msg)
        elif t == "pong":
            pass
        elif t == "error":
            self.on_state("error", msg.get("msg", "server error"))

    async def _h_clipboard(self, frame: Frame):
        self.on_clipboard(messages.loads(frame.payload, frame.flags))

    async def _h_files(self, frame: Frame):
        self.on_files(messages.loads(frame.payload, frame.flags))
