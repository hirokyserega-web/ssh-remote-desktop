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
import os

from .base import CursorImage, DisplayBackend, Frame
from ..keymap import keysym_to_x11

log = logging.getLogger("rd.backend.x11")

# Sentinel for "this env var was not set in os.environ before start()" so
# stop() can remove it (rather than restoring a None that would set the key
# to the string "None").
_UNSET = object()

# Optional Xlib import -- kept soft so the module imports on non-X11 build hosts.
try:
    from Xlib import X, display as xdisplay
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
        # Saved os.environ values for DISPLAY / XAUTHORITY, restored in stop().
        # Maps key -> previous value (or _UNSET when the key was absent).
        self._saved_env: dict = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        # Export the session's DISPLAY and XAUTHORITY into os.environ BEFORE
        # any capture library is initialised. Both python-xlib's
        # xdisplay.Display() and mss.mss() read the X connection parameters
        # from os.environ (the process-global env), NOT from self.env — so
        # without this export they connect to the broker's host display
        # (typically :0 under sudo), which the session has no cookie for,
        # producing "Authorization required" / XError and a dead stream.
        #
        # This must happen UNCONDITIONALLY, not only when python-xlib is
        # available: the Nuitka onefile build ships without python-xlib, so
        # the old code skipped the export in the `if not _HAVE_XLIB` branch
        # and mss grabbed the wrong display.
        self._saved_env = {}
        for key in ("DISPLAY", "XAUTHORITY"):
            val = self.env.get(key)
            if val:
                self._saved_env[key] = os.environ.get(key, _UNSET)
                os.environ[key] = val
        try:
            self._start_capture()
        except Exception as exc:
            self._restore_env()
            display = self.env.get("DISPLAY", "?")
            log.error(
                "не удалось подключиться к дисплею сессии %s: %s. "
                "Проверьте, что Xvfb запущен и доступен этому процессу "
                "(cookie XAUTHORITY должен совпадать).",
                display, exc,
            )
            raise

    def _start_capture(self) -> None:
        """Open the X connection and capture pipeline (after env export)."""
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
        if not _HAVE_MSS:
            from server.session import DisplayServerError
            raise DisplayServerError("не установлен пакет python-xlib/mss — захват экрана невозможен")
        # mss reads DISPLAY from os.environ by default; pass the session
        # display explicitly so capture targets the Xvfb session (:N from
        # self.env), not the host :0. Fall back to os.environ (which start()
        # already exported self.env into) only when self.env has no DISPLAY.
        display = self.env.get("DISPLAY") or os.environ.get("DISPLAY")
        try:
            if display:
                self._mss = mss.mss(display=display)
            else:
                self._mss = mss.mss()
        except Exception as exc:
            raise RuntimeError(
                f"mss: не удалось подключиться к дисплею {display!r}: {exc}"
            ) from exc
        # monitor[0] is the virtual "all monitors" rect; [1] is primary.
        mons = self._mss.monitors
        self._mss_monitor = mons[1] if len(mons) > 1 else mons[0]
        self._w = self._mss_monitor["width"]
        self._h = self._mss_monitor["height"]

    def _init_damage(self):
        try:
            from Xlib.ext import damage  # noqa: F401

            if self._dpy.has_extension("DAMAGE"):
                # python-xlib's damage_create(level) creates a Damage object on
                # the drawable; the level selects how the server reports
                # changes. BoundingBox gives one rect per damaged region --
                # cheaper than RawRectangles (which emits every sub-rect) and
                # pairs well with the JPEG tile encoder's rect merge.
                self._dpy.damage.query_version(1, 1)
                self._damage = self._root.damage_create(
                    damage.DamageReportBoundingBox
                )
                self._have_damage = self._damage is not None
                if self._have_damage:
                    log.info("XDamage tracking enabled (bounding box)")
        except Exception as exc:  # pragma: no cover
            log.debug("XDamage unavailable: %s", exc)
            self._have_damage = False
            self._damage = None

    def _init_xfixes(self):
        try:
            if self._dpy.has_extension("XFIXES"):
                self._dpy.xfixes_query_version(5, 0)
                self._have_xfixes = True
        except Exception as exc:  # pragma: no cover
            log.debug("XFixes unavailable: %s", exc)
            self._have_xfixes = False

    def _restore_env(self):
        """Restore os.environ DISPLAY / XAUTHORITY saved in start()."""
        for key, saved in self._saved_env.items():
            if saved is _UNSET:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        self._saved_env = {}

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
        self._restore_env()

    # -- geometry ----------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        return (self._w, self._h)

    # -- capture -----------------------------------------------------------
    def capture(self) -> Frame | None:
        if self._mss is None:
            return None
        try:
            shot = self._mss.grab(self._mss_monitor)
        except Exception as exc:
            # mss raises XError / DisplayConnectionError when the Xvfb
            # session died or the cookie no longer matches. Log a clear,
            # actionable message instead of a raw traceback flooding the
            # video loop, and return None so the connection handler can
            # tear the session down cleanly.
            display = self.env.get("DISPLAY", "?")
            log.error(
                "захват кадра с дисплея %s не удался: %s. "
                "Возможно, Xvfb-сессия завершилась или cookie XAUTHORITY "
                "более не действителен; перезапустите сессию.",
                display, exc,
            )
            return None
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
        """Drain pending DamageNotify events and clear the damage region.

        Returns a list of ``(x, y, w, h)`` rects, or ``None`` when damage
        tracking is off (so the caller falls back to a full / tile-diff frame).
        After draining we call ``damage_subtract`` to clear the region so the
        next capture only reports newly-damaged areas.
        """
        if not self._have_damage or self._dpy is None or self._damage is None:
            return None  # whole-frame dirty
        rects: list[tuple[int, int, int, int]] = []
        try:
            from Xlib.ext import damage as _xdamage
            n = self._dpy.pending_events()
            for _ in range(n):
                ev = self._dpy.next_event()
                if getattr(ev, "type", None) != _xdamage.DamageNotify:
                    continue
                area = getattr(ev, "area", None)
                if area is not None:
                    rects.append((area.x, area.y, area.width, area.height))
            # Clear the accumulated damage so we don't re-report old rects.
            try:
                _xdamage.damage_subtract(self._damage, None, None)
            except Exception:
                pass
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
        # Map each modifier alias ("ctrl"/"shift"/"alt"/"meta"/"super") to an
        # X keycode and synthesise the full chord via XTEST. The event ordering
        # is factored into _chord_events so it can be unit-tested without a
        # live X display (see tests/test_x11_key_chord.py).
        mod_keycodes: list[int] = []
        for mod_name in mods:
            mod_sym = keysym_to_x11(mod_name)
            if mod_sym is None:
                continue
            mod_kc = self._dpy.keysym_to_keycode(mod_sym)
            if mod_kc:
                mod_keycodes.append(mod_kc)
        for evt_name, kc in self._chord_events(keycode, mod_keycodes, down):
            xevt = X.KeyPress if evt_name == "press" else X.KeyRelease
            xtest.fake_input(self._dpy, xevt, detail=kc)
        self._dpy.sync()

    @staticmethod
    def _chord_events(keycode: int, mod_keycodes, down: bool):
        """Ordered (event_name, keycode) XTEST events for a key + modifiers.

        On key-down the modifiers are pressed first (in given order), then the
        main key; on key-up the main key is released first, then the modifiers
        in reverse. ``event_name`` is ``"press"`` or ``"release"``. Pure
        function -- no X server needed -- so the ordering is unit-testable.
        """
        if down:
            return ([("press", kc) for kc in mod_keycodes]
                    + [("press", keycode)])
        return [("release", keycode)] + [("release", kc) for kc in reversed(mod_keycodes)]

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
