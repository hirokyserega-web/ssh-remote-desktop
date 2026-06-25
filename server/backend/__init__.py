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
import shutil

from .base import CursorImage, DisplayBackend, Frame, InputEvent

log = logging.getLogger("rd.backend")


def detect_backend_kind(env: dict | None = None, forced: str = "auto") -> str:
    """Return ``"x11"`` or ``"wayland"`` for the given session environment.

    Detection order:

    1. an explicit ``forced`` value other than ``"auto"`` wins;
    2. ``XDG_SESSION_TYPE`` if it says ``x11`` / ``wayland``;
    3. presence of ``WAYLAND_DISPLAY`` -> wayland;
    4. presence of ``DISPLAY`` -> x11;
    5. default to ``x11`` (Xvfb is the simplest headless option).

    The Wayland backend is **experimental** — without a working
    xdg-desktop-portal/PipeWire it only produces placeholder frames. So when
    ``auto`` lands on ``wayland`` we prefer ``x11`` (Xvfb) if Xvfb is
    installed (a fully working headless session), and log a prominent warning
    either way. This is what stops an Arch/Wayland host from silently getting
    a blank placeholder stream under ``backend = "auto"``. Force wayland
    explicitly with ``backend = "wayland"`` to override.
    """
    if forced and forced != "auto":
        return forced
    env = env if env is not None else dict(os.environ)
    stype = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if stype in ("x11", "wayland"):
        kind = stype
    elif env.get("WAYLAND_DISPLAY"):
        kind = "wayland"
    elif env.get("DISPLAY"):
        kind = "x11"
    else:
        kind = "x11"
    if kind == "wayland" and forced == "auto":
        if shutil.which("Xvfb"):
            log.warning(
                "auto backend resolved to 'wayland' (XDG_SESSION_TYPE / "
                "WAYLAND_DISPLAY), but the Wayland backend is EXPERIMENTAL and "
                "may only produce placeholder frames. Xvfb is installed — using "
                "the x11 backend for a working session. To force wayland, set "
                "backend = \"wayland\"."
            )
            return "x11"
        log.warning(
            "auto backend resolved to 'wayland', which is EXPERIMENTAL and may "
            "only produce placeholder frames. For a working session install Xvfb "
            "and use backend = \"x11\" (Debian/Ubuntu: sudo apt install xvfb; "
            "Arch: sudo pacman -S xorg-server-xvfb; Fedora: sudo dnf install "
            "xorg-x11-server-Xvfb)."
        )
    return kind


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
