"""Serialization helpers and typed builders for control-plane messages.

Payloads are serialized with MessagePack when the ``msgpack`` package is
available (more compact, faster), otherwise JSON. The chosen encoding is
signalled per frame through :class:`common.protocol.Flags.MSGPACK`, so a peer
that only has JSON still interoperates with one that prefers MessagePack.
"""

from __future__ import annotations

import json
import time
from typing import Any

try:  # optional, see prompt "сериализация сообщений (опционально)"
    import msgpack  # type: ignore

    _HAVE_MSGPACK = True
except Exception:  # pragma: no cover - msgpack simply not installed
    msgpack = None  # type: ignore
    _HAVE_MSGPACK = False

from .protocol import Flags, PROTO_VERSION


def prefers_msgpack() -> bool:
    return _HAVE_MSGPACK


def dumps(obj: Any) -> tuple[bytes, Flags]:
    """Serialize ``obj`` -> ``(payload_bytes, flag)``.

    ``flag`` is :data:`Flags.MSGPACK` when MessagePack was used so the receiver
    knows how to decode it.
    """
    if _HAVE_MSGPACK:
        return msgpack.packb(obj, use_bin_type=True), Flags.MSGPACK
    return json.dumps(obj, separators=(",", ":")).encode("utf-8"), Flags.NONE


def loads(payload: bytes, flags: int) -> Any:
    """Deserialize a payload honouring the per-frame MessagePack flag."""
    if flags & Flags.MSGPACK:
        if not _HAVE_MSGPACK:  # pragma: no cover - mismatched peers
            raise RuntimeError("peer sent MessagePack but msgpack is unavailable")
        return msgpack.unpackb(payload, raw=False)
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# Builders -- thin wrappers so call sites read like the spec's examples and we
# keep the message vocabulary in one place.
# ---------------------------------------------------------------------------
def hello(
    *,
    codec: str,
    view: tuple[int, int],
    user: str,
    auth: str,
    new_session: bool = True,
    geometry: tuple[int, int] | None = None,
    persistent: bool = False,
    proto: int = PROTO_VERSION,
) -> dict:
    return {
        "t": "hello",
        "proto": proto,
        "codec": codec,
        "view": list(view),
        "user": user,
        "auth": auth,
        "new_session": new_session,
        "geometry": list(geometry or view),
        "persistent": persistent,
    }


def session(
    *,
    session_id: str,
    backend: str,
    display: str | None,
    wayland_display: str | None,
    screen: tuple[int, int],
    fps: int,
    cursor: str,
    codec: str | None = None,
    proto: int = PROTO_VERSION,
) -> dict:
    """Server -> client reply after the handshake.

    ``codec`` is the codec the server is actually encoding with (it may differ
    from the requested one when PyAV is unavailable and the server fell back to
    JPEG); the client uses it to build the matching decoder. ``proto`` echoes
    the negotiated protocol version.
    """
    return {
        "t": "session",
        "session_id": session_id,
        "backend": backend,
        "display": display,
        "wayland_display": wayland_display,
        "screen": list(screen),
        "fps": fps,
        "cursor": cursor,
        "codec": codec,
        "proto": proto,
    }


def mouse_move(x: int, y: int) -> dict:
    return {"t": "mouse_move", "x": int(x), "y": int(y)}


def mouse_btn(button: int, down: bool) -> dict:
    return {"t": "mouse_btn", "button": int(button), "down": bool(down)}


def scroll(dx: int, dy: int) -> dict:
    return {"t": "scroll", "dx": int(dx), "dy": int(dy)}


def key(keysym: str, down: bool, mods: list[str] | None = None) -> dict:
    return {"t": "key", "keysym": keysym, "down": bool(down), "mods": mods or []}


def clipboard(fmt: str, data, origin: str) -> dict:
    return {"t": "clipboard", "format": fmt, "data": data, "origin": origin}


def ping(ts: int | None = None) -> dict:
    """Client -> server heartbeat.

    ``ts`` is a high-resolution client timestamp (``time.monotonic``) in
    milliseconds so the server can echo it back in :func:`pong` and the client
    can measure round-trip time for adaptive bitrate/FPS. Falls back to
    ``time.time`` seconds for backward compatibility when called without arg.
    """
    if ts is None:
        return {"t": "ping", "ts": int(time.time())}
    return {"t": "ping", "ts": float(ts)}


def pong(ts) -> dict:
    return {"t": "pong", "ts": ts}


def stats(*, loss: float, rtt_ms: float, queued: int) -> dict:
    """Client -> server feedback used to drive bitrate/FPS adaptation."""
    return {"t": "stats", "loss": loss, "rtt_ms": rtt_ms, "queued": queued}
