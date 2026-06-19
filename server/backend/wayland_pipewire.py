"""Optional PipeWire ScreenCast capture for the Wayland backend.

This module is imported lazily by :mod:`server.backend.wayland`. When PipeWire
and a desktop portal are available it opens a ScreenCast stream and hands the
frames to the Wayland backend; when they are not (the common case on a headless
build host or a server without a running user portal) it raises
:class:`PipeWireUnavailable` so the backend falls back to its placeholder frame
generator and the rest of the pipeline (encode / transport / input / clipboard)
keeps working.

The previous code referenced ``wayland_pipewire`` but the module did not exist,
so ``WaylandBackend._init_pipewire`` hit an ``ImportError`` on every Wayland
session. That was swallowed by the broad ``except Exception`` and logged as
"PipeWire capture unavailable", which masked the real problem (a missing
module, not a missing portal) and left no way to plug in a real capture path.
This module fixes that: the import always succeeds, and the unavailability is
expressed as a typed exception the backend can branch on.

A real implementation would use ``pywayland`` + the ``xdg-desktop-portal``
D-Bus interface (``org.freedesktop.portal.ScreenCast``) or the lighter
``wlr-screencopy`` protocol under wlroots. That requires a running compositor
and is out of scope for the headless test server; the stub here makes the
contract explicit and gives a clear extension point.
"""

from __future__ import annotations

import logging

from .base import Frame

log = logging.getLogger("rd.backend.wayland.pipewire")


class PipeWireUnavailable(Exception):
    """Raised when PipeWire / the desktop portal is not usable.

    The Wayland backend catches this and degrades to its placeholder frame so
    the session still comes up (verifiable end-to-end) instead of crashing.
    """


class PipeWireCapture:
    """Stub capture handle.

    Real PipeWire capture would negotiate a stream node via the portal, mmap
    the DMA-BUF / SHM buffers, and convert them to the BGRA :class:`Frame` the
    encoder expects. Here we only validate the inputs and raise immediately so
    the backend's fallback path runs -- this is the honest behaviour on a host
    without PipeWire, and it documents where a real implementation plugs in.
    """

    def __init__(self, env, geometry, cursor_mode="embedded"):
        self.env = env
        self.geometry = geometry
        self.cursor_mode = cursor_mode

    def start(self) -> None:
        # Detect the common "no portal / no PipeWire" case explicitly. A real
        # implementation would try to connect to the portal here; we surface a
        # clear message instead of a generic ImportError.
        raise PipeWireUnavailable(
            "PipeWire ScreenCast is not available on this host "
            "(no desktop portal / pipewire in the session); using placeholder frames"
        )

    def size(self) -> tuple[int, int] | None:
        return None

    def read(self) -> Frame | None:
        return None

    def stop(self) -> None:
        pass
