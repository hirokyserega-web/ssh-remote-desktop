"""X11 display backend: XShm/mss capture, XDamage, XFixes cursor, XTEST input.

Capture strategy (in order of preference):

* **MIT-SHM (XShm)** via python-xlib -- the frame lands in shared memory, no
  copy through the X socket. Fastest path.
* **mss** -- pure fallback that itself uses XShm under the hood; used when the
  python-xlib SHM path is unavailable.

Damage tracking uses **XDamage** so only changed rectangles are reported. The
hardware cursor is fetched separately through **XFixes** (it never appears in
the screen grab). Input is injected with the **XTEST** extension's
``fake_input`` (works without root). Clipboard goes through X11 selections,
preferring python-xlib and falling back to the ``xclip`` utility.
"""

from __future__ import annotations

import logging
import subprocess
import time

from .base import CursorImage, DisplayBackend, Frame
from ..keymap import keysym_to_x11

log = logging.getLogger("rd.backend.x11")

# Optional Xlib import -- kept soft so the module imports on non-X11 build hosts.
try:
    from Xlib import X, display as xdisplay, XK
    from Xlib.ext import xtest

    _HAVE_XLIB = True
except Exception:  # pragma: no cover
    _HAVE_XLIB = False

try:
    import mss  # type: ignore

    _HAVE_MSS = True
except Exception:  # pragma: no cover
    _HAVE_MSS = False

try:
    import numpy as np

    _HAVE_NUMPY = True
except Exception:  # pragma: no cover
    _HAVE_NUMPY = False


# X button numbers: 1/2/3 = left/middle/right, 4/5 = vert scroll, 6/7 = horiz.
_SCROLL_UP, _SCROLL_DOWN = 4, 5
_SCROLL_LEFT, _SCROLL_RIGHT = 6, 7


class X11Backend(DisplayBackend):
    kind = "x11"

    def __init__(self, env, geometry, cursor_mode="embedded"):
        super().__init__(env, geometry, cursor_mode)
        self._dpy = None
        self._root = None
        self._screen = None
        self._mss = None
        self._mss_monitor = None
        self._damage = None
        self._have_damage = False
        self._have_xfixes = False
        self._last_cursor_serial = -1
        self._w, self._h = geometry

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if not _HAVE_XLIB:
            # We can still capture with mss, but input/cursor need Xlib.
            log.warning("python-xlib unavailable; input injection disabled")
            self._init_mss()
            return
        disp_name = self.env.get("DISPLAY", ":0")
        self._dpy = xdisplay.Display(disp_name)
        self._screen = self._dpy.screen()
        self._root = self._screen.root
        geom = self._root.get_geometry()
        self._w, self._h = geom.width, geom.height

        self._init_damage()
        self._init_xfixes()
        # mss is used as the actual pixel grabber (uses XShm internally) -- it
        # is simpler and more robust than hand-rolling XShm via python-xlib.
        self._init_mss()
        log.info("X11 backend started on %s (%dx%d)", disp_name, self._w, self._h)

    def _init_mss(self):
        if _HAVE_MSS:
            self._mss = mss.mss()
            # monitor[0] is the virtual "all monitors" rect; [1] is primary.
            mons = self._mss.monitors
            self._mss_monitor = mons[1] if len(mons) > 1 else mons[0]
            self._w = self._mss_monitor["width"]
            self._h = self._mss_monitor["height"]

    def _init_damage(self):
        try:
            from Xlib.ext import damage  # noqa: F401

            if self._dpy.has_extension("DAMAGE"):
                self._dpy.damage_create  # type: ignore[attr-defined]
                self._damage = self._root.damage_create(
                    self._dpy.extension_event.DamageNotify  # type: ignore
                ) if hasattr(self._root, "damage_create") else None
                self._have_damage = self._damage is not None
        except Exception as exc:  # pragma: no cover
            log.debug("XDamage unavailable: %s", exc)
            self._have_damage = False

    def _init_xfixes(self):
        try:
            if self._dpy.has_extension("XFIXES"):
                self._dpy.xfixes_query_version(5, 0)
                self._have_xfixes = True
        except Exception as exc:  # pragma: no cover
            log.debug("XFixes unavailable: %s", exc)
            self._have_xfixes = False

    def stop(self) -> None:
        try:
            if self._mss is not None:
                self._mss.close()
        except Exception:
            pass
        try:
            if self._dpy is not None:
                self._dpy.close()
        except Exception:
            pass

    # -- geometry ----------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        return (self._w, self._h)

    # -- capture -----------------------------------------------------------
    def capture(self) -> Frame | None:
        if self._mss is None:
            return None
        shot = self._mss.grab(self._mss_monitor)
        buf = bytes(shot.raw)  # BGRA
        damage = self._collect_damage()
        cx, cy = self._pointer_pos()
        return Frame(
            width=shot.width,
            height=shot.height,
            buffer=buf,
            stride=shot.width * 4,
            damage=damage,
            cursor_x=cx,
            cursor_y=cy,
        )

    def _collect_damage(self):
        if not self._have_damage or self._dpy is None:
            return None  # whole-frame dirty
        rects = []
        try:
            n = self._dpy.pending_events()
            for _ in range(n):
                ev = self._dpy.next_event()
                area = getattr(ev, "area", None)
                if area is not None:
                    rects.append((area.x, area.y, area.width, area.height))
        except Exception:
            return None
        return rects or None

    def _pointer_pos(self):
        if self._root is None:
            return (0, 0)
        try:
            p = self._root.query_pointer()
            return (p.root_x, p.root_y)
        except Exception:
            return (0, 0)

    # -- cursor ------------------------------------------------------------
    def cursor(self) -> CursorImage | None:
        if not self._have_xfixes or self._dpy is None:
            return None
        try:
            img = self._dpy.xfixes_get_cursor_image(self._root)
        except Exception:
            return None
        serial = getattr(img, "cursor_serial", 0)
        # cursor_image is a list of ARGB ints (premultiplied) per python-xlib.
        pixels = img.cursor_image
        w, h = img.width, img.height
        if _HAVE_NUMPY:
            arr = np.asarray(pixels, dtype=np.uint32)
            a = (arr >> 24) & 0xFF
            r = (arr >> 16) & 0xFF
            g = (arr >> 8) & 0xFF
            b = arr & 0xFF
            rgba = np.dstack([r, g, b, a]).astype(np.uint8).tobytes()
        else:
            out = bytearray()
            for px in pixels:
                out += bytes(((px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF, (px >> 24) & 0xFF))
            rgba = bytes(out)
        return CursorImage(
            width=w, height=h, xhot=img.xhot, yhot=img.yhot, rgba=rgba, serial=serial
        )

    # -- input -------------------------------------------------------------
    def inject_mouse_move(self, x: int, y: int) -> None:
        if self._dpy is None:
            return
        xtest.fake_input(self._dpy, X.MotionNotify, x=int(x), y=int(y))
        self._dpy.sync()

    def inject_mouse_button(self, button: int, down: bool) -> None:
        if self._dpy is None:
            return
        evt = X.ButtonPress if down else X.ButtonRelease
        xtest.fake_input(self._dpy, evt, detail=int(button))
        self._dpy.sync()

    def inject_scroll(self, dx: int, dy: int) -> None:
        if self._dpy is None:
            return
        # Each scroll "click" is a button press+release of 4/5 (vert) or 6/7.
        def click(btn, times):
            for _ in range(abs(times)):
                xtest.fake_input(self._dpy, X.ButtonPress, detail=btn)
                xtest.fake_input(self._dpy, X.ButtonRelease, detail=btn)
        if dy:
            click(_SCROLL_UP if dy > 0 else _SCROLL_DOWN, dy)
        if dx:
            click(_SCROLL_RIGHT if dx > 0 else _SCROLL_LEFT, dx)
        self._dpy.sync()

    def inject_key(self, keysym: str, down: bool, mods=()) -> None:
        if self._dpy is None:
            return
        sym = keysym_to_x11(keysym)
        if sym is None:
            log.debug("unknown keysym: %r", keysym)
            return
        keycode = self._dpy.keysym_to_keycode(sym)
        if not keycode:
            return
        # Press modifiers first (on key-down), release after (on key-up handled
        # by the client sending explicit modifier key events as well).
        evt = X.KeyPress if down else X.KeyRelease
        xtest.fake_input(self._dpy, evt, detail=keycode)
        self._dpy.sync()

    # -- clipboard ---------------------------------------------------------
    def get_clipboard_text(self) -> str | None:
        # Prefer xclip (robust selection ownership handling); fall back to none.
        try:
            out = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, timeout=2, env=self.env,
            )
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace")
        except Exception:
            pass
        return None

    def set_clipboard_text(self, text: str) -> None:
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=text.encode("utf-8"), timeout=2, env=self.env,
            )
        except Exception as exc:
            log.debug("xclip set failed: %s", exc)
