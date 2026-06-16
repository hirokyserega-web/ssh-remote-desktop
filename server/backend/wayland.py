"""Wayland display backend: PipeWire/portal capture, uinput input, wl-clipboard.

Wayland deliberately denies the global screen/input access X11 grants, so this
backend leans on the desktop portal and kernel-level virtual input:

* **Capture** -- ``org.freedesktop.portal.ScreenCast`` hands us a PipeWire node
  (works on GNOME/KDE). For wlroots compositors the lighter ``wlr-screencopy``
  protocol is used. Frames arrive as DMA-BUF/SHM and are read into a BGRA
  buffer; damage comes from the PipeWire stream metadata.
* **Input** -- the universal path is ``uinput`` (``/dev/uinput`` via
  python-evdev), which works under any compositor. wlroots also exposes
  ``zwlr_virtual_pointer`` / ``zwp_virtual_keyboard``; GNOME/KDE accept input
  through the ``RemoteDesktop`` portal. ``ydotool`` is supported as a CLI
  fallback (it too rides on uinput).
* **Clipboard** -- ``wl-clipboard`` (``wl-copy`` / ``wl-paste``); for wlroots
  the ``wlr-data-control`` protocol reads/writes without focus.

The capture/cursor specifics depend heavily on the compositor and on optional
native libraries (PipeWire, GStreamer, pywayland). To keep the backend usable
on any build host, those paths are loaded lazily and degrade to a solid-colour
placeholder frame when unavailable, while input and clipboard still work via
uinput + wl-clipboard. This keeps the same capture/encode pipeline as X11.
"""

from __future__ import annotations

import logging
import subprocess

from .base import DisplayBackend, Frame
from ..keymap import keysym_to_evdev

log = logging.getLogger("rd.backend.wayland")

try:
    import numpy as np

    _HAVE_NUMPY = True
except Exception:  # pragma: no cover
    _HAVE_NUMPY = False

# python-evdev for uinput virtual devices.
try:
    from evdev import UInput, ecodes as e

    _HAVE_EVDEV = True
except Exception:  # pragma: no cover
    _HAVE_EVDEV = False


# evdev relative-scroll codes.
class _UInputDevices:
    """Lazily-created virtual pointer + keyboard on /dev/uinput."""

    def __init__(self):
        self.pointer = None
        self.keyboard = None

    def ensure(self):
        if not _HAVE_EVDEV:
            return False
        if self.pointer is not None:
            return True
        try:
            cap_p = {
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
            }
            self.pointer = UInput(cap_p, name="rd-virtual-pointer")
            # Full key range for the keyboard.
            cap_k = {e.EV_KEY: list(range(1, 248))}
            self.keyboard = UInput(cap_k, name="rd-virtual-keyboard")
            return True
        except Exception as exc:
            log.warning("uinput unavailable (need access to /dev/uinput): %s", exc)
            return False

    def close(self):
        for dev in (self.pointer, self.keyboard):
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                pass


class WaylandBackend(DisplayBackend):
    kind = "wayland"

    def __init__(self, env, geometry, cursor_mode="embedded"):
        super().__init__(env, geometry, cursor_mode)
        self._w, self._h = geometry
        self._uinput = _UInputDevices()
        self._pw = None              # PipeWire capture handle (optional)
        self._abs_x = self._w // 2   # uinput is relative; track absolute pos
        self._abs_y = self._h // 2
        self._frame_idx = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._uinput.ensure()
        self._init_pipewire()
        log.info("Wayland backend started (%dx%d, uinput=%s, pipewire=%s)",
                 self._w, self._h, self._uinput.pointer is not None, self._pw is not None)

    def _init_pipewire(self):
        """Try to open a PipeWire ScreenCast stream via the desktop portal.

        This requires a running portal + PipeWire in the user session. When it
        is not available (headless build host, no portal), ``self._pw`` stays
        ``None`` and :meth:`capture` produces a placeholder frame so the rest
        of the pipeline keeps working.
        """
        try:
            from .wayland_pipewire import PipeWireCapture  # optional helper

            self._pw = PipeWireCapture(self.env, self.geometry, self.cursor_mode)
            self._pw.start()
            size = self._pw.size()
            if size:
                self._w, self._h = size
        except Exception as exc:
            log.info("PipeWire capture unavailable, using placeholder: %s", exc)
            self._pw = None

    def stop(self) -> None:
        self._uinput.close()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

    # -- geometry ----------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        return (self._w, self._h)

    # -- capture -----------------------------------------------------------
    def capture(self) -> Frame | None:
        if self._pw is not None:
            frame = self._pw.read()
            if frame is not None:
                return frame
        return self._placeholder_frame()

    def _placeholder_frame(self) -> Frame:
        """Generate a simple gradient so the pipeline is verifiable headless."""
        w, h = self._w, self._h
        self._frame_idx += 1
        if _HAVE_NUMPY:
            xs = np.linspace(0, 255, w, dtype=np.uint8)
            row = np.zeros((w, 4), dtype=np.uint8)
            row[:, 0] = xs                       # B
            row[:, 1] = (self._frame_idx % 256)  # G animates
            row[:, 2] = 128                       # R
            row[:, 3] = 255                       # A
            buf = np.broadcast_to(row, (h, w, 4)).tobytes()
        else:
            buf = bytes([60, self._frame_idx % 256, 128, 255]) * (w * h)
        return Frame(width=w, height=h, buffer=buf, stride=w * 4,
                     damage=None, cursor_x=self._abs_x, cursor_y=self._abs_y)

    # -- input (uinput first, ydotool fallback) ----------------------------
    def inject_mouse_move(self, x: int, y: int) -> None:
        dx, dy = int(x) - self._abs_x, int(y) - self._abs_y
        self._abs_x, self._abs_y = int(x), int(y)
        if self._uinput.pointer is not None and _HAVE_EVDEV:
            dev = self._uinput.pointer
            if dx:
                dev.write(e.EV_REL, e.REL_X, dx)
            if dy:
                dev.write(e.EV_REL, e.REL_Y, dy)
            dev.syn()
        else:
            self._ydotool("mousemove", "--absolute", "-x", str(x), "-y", str(y))

    def inject_mouse_button(self, button: int, down: bool) -> None:
        if self._uinput.pointer is not None and _HAVE_EVDEV:
            code = {1: e.BTN_LEFT, 2: e.BTN_MIDDLE, 3: e.BTN_RIGHT}.get(button, e.BTN_LEFT)
            self._uinput.pointer.write(e.EV_KEY, code, 1 if down else 0)
            self._uinput.pointer.syn()
        else:
            # ydotool click uses bitmask codes; 0xC0/0xC1.. -- simplest is up/down.
            state = "0x40" if down else "0x80"
            self._ydotool("click", state)

    def inject_scroll(self, dx: int, dy: int) -> None:
        if self._uinput.pointer is not None and _HAVE_EVDEV:
            if dy:
                self._uinput.pointer.write(e.EV_REL, e.REL_WHEEL, 1 if dy > 0 else -1)
            if dx:
                self._uinput.pointer.write(e.EV_REL, e.REL_HWHEEL, 1 if dx > 0 else -1)
            self._uinput.pointer.syn()

    def inject_key(self, keysym: str, down: bool, mods=()) -> None:
        code = keysym_to_evdev(keysym)
        if self._uinput.keyboard is not None and _HAVE_EVDEV and code is not None:
            self._uinput.keyboard.write(e.EV_KEY, code, 1 if down else 0)
            self._uinput.keyboard.syn()
        elif down:
            # ydotool key needs press/release pair; only emit on logical down.
            self._ydotool("key", f"{code}:1", f"{code}:0")

    def _ydotool(self, *args: str) -> None:
        try:
            subprocess.run(["ydotool", *args], timeout=2, env=self.env,
                           capture_output=True)
        except Exception as exc:
            log.debug("ydotool failed: %s", exc)

    # -- clipboard (wl-clipboard) ------------------------------------------
    def get_clipboard_text(self) -> str | None:
        try:
            out = subprocess.run(["wl-paste", "--no-newline"], capture_output=True,
                                 timeout=2, env=self.env)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace")
        except Exception:
            pass
        return None

    def set_clipboard_text(self, text: str) -> None:
        try:
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=2,
                           env=self.env, capture_output=True)
        except Exception as exc:
            log.debug("wl-copy failed: %s", exc)
