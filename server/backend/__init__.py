"""Display-backend abstraction with X11 and Wayland implementations.

The server is built around a single interface -- :class:`base.DisplayBackend`
-- that exposes capture / input / cursor / clipboard. Two concrete backends
implement it (:mod:`x11` and :mod:`wayland`). Selection is automatic based on
the session environment (``XDG_SESSION_TYPE`` / ``WAYLAND_DISPLAY`` /
``DISPLAY``) and can be forced via config.
"""

from __future__ import annotations

import logging
import os

from .base import CursorImage, DisplayBackend, Frame, InputEvent

log = logging.getLogger("rd.backend")


def detect_backend_kind(env: dict | None = None, forced: str = "auto") -> str:
    """Return ``"x11"`` or ``"wayland"`` for the given session environment.

    Detection order mirrors the spec:

    1. an explicit ``forced`` value other than ``"auto"`` wins;
    2. ``XDG_SESSION_TYPE`` if it says ``x11`` / ``wayland``;
    3. presence of ``WAYLAND_DISPLAY`` -> wayland;
    4. presence of ``DISPLAY`` -> x11;
    5. default to ``x11`` (Xvfb is the simplest headless option).
    """
    if forced and forced != "auto":
        return forced
    env = env if env is not None else dict(os.environ)
    stype = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if stype in ("x11", "wayland"):
        return stype
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "x11"


def create_backend(kind: str, env: dict, geometry: tuple[int, int], *,
                   cursor_mode: str = "embedded"):
    """Instantiate the concrete backend for ``kind`` (``x11``/``wayland``)."""
    if kind == "wayland":
        from .wayland import WaylandBackend

        return WaylandBackend(env=env, geometry=geometry, cursor_mode=cursor_mode)
    from .x11 import X11Backend

    return X11Backend(env=env, geometry=geometry, cursor_mode=cursor_mode)


__all__ = [
    "DisplayBackend",
    "Frame",
    "CursorImage",
    "InputEvent",
    "detect_backend_kind",
    "create_backend",
]
