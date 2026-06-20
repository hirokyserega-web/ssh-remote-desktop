"""Client SSH transport + multiplexed session, with auto-reconnect.

Runs an asyncio loop on a background thread and exposes a thread-safe-ish API
the Qt GUI can call. The transport:

* opens one SSH connection (key or password auth, host-key TOFU),
* opens a single SSH session channel and runs the frame multiplexer on it,
* performs the ``hello``/``session`` handshake,
* exposes callbacks for video packets, clipboard updates, file results and
  session info,
* measures RTT and reports loss/latency stats so the server can adapt
  bitrate/FPS (see :meth:`heartbeat` / :meth:`_h_control`),
* reconnects automatically on drop.

File *bytes* use the same connection's SFTP subsystem (see
:mod:`client.files`); this module owns the asyncssh connection both share.
"""

from __future__ import annotations

import asyncio
import collections as _c
import logging
import threading
import time
from typing import Callable

import asyncssh

from common import messages
from common.framing import AsyncByteStream, Frame, Multiplexer
from common.protocol import Channel, PROTO_VERSION

from .hostkeys import (
    KnownHostsStore,
    TofuClient,
)

log = logging.getLogger("rd.client.transport")


async def _noninteractive_ask(host: str, port: int, fingerprint: str, first_time: bool, old_fingerprint: str | None = None) -> tuple[bool, bool]:
    """Headless default TOFU policy used when no GUI callback is wired.

    Refuses unknown/changed keys so programmatic use never silently trusts a
    new host. Operators who want auto-trust can pre-seed the known-hosts file.
    Returns ``(accepted, remember)``; on reject both are False.
    """
    return False, False


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
        # Host-key TOFU notification (fire-and-forget). The GUI shows a dialog
        # and resolves the pending question via :meth:`confirm_host_key`.
        self.on_host_key: "Callable[[str, int, str, bool, str | None], None] | None" = None
        # Known-hosts store; the GUI may override the path via cfg.known_hosts.
        self._hostkeys = KnownHostsStore(self.cfg.known_hosts)
        # TOFU ask bridge: when validate_host_public_key awaits, we emit the
        # on_host_key callback and block on this Event until confirm_host_key()
        # resolves it.
        self._tofu_event: asyncio.Event | None = None
        self._tofu_result: bool = False
        self._tofu_remember: bool = True

        # RTT / stats measurement (P1 4.1): each heartbeat sends a ping with a
        # millisecond timestamp; the echoed pong yields a round-trip sample.
        # We keep a small rolling window and a pending-ping counter so we can
        # estimate loss (pings sent vs pongs received) and report both to the
        # server, whose _adapt() lowers/raises bitrate+fps from them.
        self._rtt_samples: "_c.deque[int]" = _c.deque(maxlen=16)
        self._pings_sent: int = 0
        self._pongs_recv: int = 0
        self._heartbeat_tick: int = 0
        # Window-size provider set by the GUI (returns (w, h) of the desktop
        # view widget). Public so MainWindow can assign it directly. When None
        # (headless / no GUI), _handshake falls back to cfg.geometry explicitly.
        self.view_size_provider: "Callable[[], tuple[int, int]] | None" = None

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

    def heartbeat(self) -> None:
        """Send a ping carrying a millisecond timestamp (thread-safe).

        Called by the GUI's heartbeat timer. The server echoes it back as a
        pong (see :meth:`_h_control`), which we turn into an RTT sample. Every
        few heartbeats we also push a ``stats`` message so the server's
        ``_adapt`` can adjust bitrate/FPS from real network conditions.
        """
        ts = int(time.monotonic() * 1000)
        self._pings_sent += 1
        self._heartbeat_tick += 1
        self._send(Channel.CONTROL, messages.ping(ts))
        # Report stats every 5th heartbeat (~15s at the 3s timer). Sending too
        # often would itself add latency; the server adapts in steps anyway.
        if self._heartbeat_tick % 5 == 0:
            self._send_stats()

    def _send_stats(self) -> None:
        rtt = (
            sum(self._rtt_samples) / len(self._rtt_samples)
            if self._rtt_samples else 0.0
        )
        # Loss as a fraction in [0, 1]: unanswered pings over pings sent. We
        # floor the denominator so a cold start (1-2 pings) does not report a
        # huge loss spike; once we have samples the ratio is meaningful.
        sent = max(self._pings_sent, 5)
        loss = max(0.0, (sent - self._pongs_recv) / sent)
        queued = 0
        if self.mux is not None:
            # Approximate outbound backlog across all channels.
            try:
                queued = sum(len(q) for q in self.mux._queues.values())
            except Exception:
                queued = 0
        self._send(Channel.CONTROL, messages.stats(loss=loss, rtt_ms=rtt, queued=queued))

    def get_stats(self):
        """Return a snapshot of RTT/loss stats (thread-safe)."""
        return {
            "rtt_ms": sum(self._rtt_samples) / len(self._rtt_samples) if self._rtt_samples else 0.0,
            "loss": max(0.0, (self._pings_sent - self._pongs_recv) / self._pings_sent) if self._pings_sent else 0.0,
            "pings_sent": self._pings_sent,
            "pongs_recv": self._pongs_recv,
        }

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
            stdin, stdout, _stderr = await conn.open_session(encoding=None)
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
            # We do our own TOFU validation via TofuClient below, so tell
            # asyncssh not to enforce its own known_hosts check (which would
            # reject every first-time host before our callback runs). Passing
            # known_hosts=None disables asyncssh's built-in validation entirely.
            "known_hosts": None,
        }
        # TOFU client: ask() is awaited on the asyncio loop and blocks until
        # the GUI resolves it via confirm_host_key(). The non-interactive
        # fallback is used only when no GUI callback was wired (headless use).
        ask = self._ask_host_key if self.on_host_key is not None else _noninteractive_ask
        opts["client"] = TofuClient(self._hostkeys, ask)
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

    async def _ask_host_key(self, host: str, port: int, fingerprint: str, first_time: bool, old_fingerprint: str | None = None) -> tuple[bool, bool]:
        """Notify the GUI and block on the asyncio loop until it answers.

        The GUI's on_host_key callback is fire-and-forget (it emits a Qt signal
        and returns immediately); the actual dialog runs on the Qt thread and
        calls back via :meth:`confirm_host_key`, which resolves ``_tofu_event``.
        Returns ``(accepted, remember)``.
        """
        self._tofu_event = asyncio.Event()
        self.on_host_key(host, port, fingerprint, first_time, old_fingerprint)
        await self._tofu_event.wait()
        return self._tofu_result, self._tofu_remember

    def confirm_host_key(self, accepted: bool, remember: bool = True) -> None:
        """Resolve a pending TOFU question (called from the GUI thread).

        ``remember`` is forwarded to :class:`TofuClient` so an unchecked
        "remember" checkbox trusts the key for this connection only without
        persisting it.
        """
        if self._tofu_event is not None and self._loop is not None:
            self._tofu_result = accepted
            self._tofu_remember = remember
            self._loop.call_soon_threadsafe(self._tofu_event.set)

    async def _handshake(self):
        # Ask the GUI for the current view size. With PROTO 2 the server scales
        # mouse coords from normalized floats, so this is advisory -- the
        # authoritative size arrives in the resize message on connect -- but we
        # send the real widget size so the server's initial _client_view is not
        # misleading. When no provider is set (headless), use cfg.geometry.
        # We do NOT wrap the provider call in try/except: a buggy provider
        # should surface as a real error, not be silently masked.
        if self.view_size_provider is not None:
            view = tuple(self.view_size_provider())
        else:
            view = tuple(self.cfg.geometry)
        hello = messages.hello(
            codec=self.cfg.codec,
            view=view,
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
            # RTT sample: the server echoed our ping timestamp back verbatim.
            ts = msg.get("ts")
            if ts is not None:
                rtt = max(0, int(time.monotonic() * 1000) - int(ts))
                self._rtt_samples.append(rtt)
                self._pongs_recv += 1
        elif t == "error":
            self.on_state("error", msg.get("msg", "server error"))

    async def _h_clipboard(self, frame: Frame):
        self.on_clipboard(messages.loads(frame.payload, frame.flags))

    async def _h_files(self, frame: Frame):
        self.on_files(messages.loads(frame.payload, frame.flags))
