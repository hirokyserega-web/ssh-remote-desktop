"""The single display-backend interface shared by X11 and Wayland.

Encoding, the SSH protocol and the session model are all written against this
interface, so adding a third backend later only means implementing these
methods.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class Frame:
    """One captured frame.

    ``buffer`` is a contiguous BGRA/BGRx byte buffer (``width * height * 4``).
    ``damage`` is the list of changed rectangles ``(x, y, w, h)`` since the
    previous frame, or ``None`` when the whole frame should be treated as dirty
    (e.g. the first frame, or backends without damage tracking).
    """

    width: int
    height: int
    buffer: bytes
    stride: int
    damage: list[tuple[int, int, int, int]] | None = None
    cursor_x: int = 0
    cursor_y: int = 0


@dataclass
class CursorImage:
    """Hardware cursor image fetched out-of-band (XFixes / portal metadata)."""

    width: int
    height: int
    xhot: int
    yhot: int
    rgba: bytes
    serial: int = 0


@dataclass
class InputEvent:
    """Normalised input event flowing client -> server before backend mapping."""

    kind: str               # "mouse_move" | "mouse_btn" | "scroll" | "key"
    x: int = 0
    y: int = 0
    button: int = 0
    down: bool = False
    dx: int = 0
    dy: int = 0
    keysym: str = ""
    mods: tuple[str, ...] = field(default_factory=tuple)


class DisplayBackend(abc.ABC):
    """Capture / input / cursor / clipboard for one user session."""

    kind: str = "base"

    def __init__(self, env: dict, geometry: tuple[int, int], cursor_mode: str = "embedded"):
        self.env = env
        self.geometry = geometry
        self.cursor_mode = cursor_mode

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """Open connections to the display server / capture pipeline."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Release all resources."""

    # -- geometry ----------------------------------------------------------
    @abc.abstractmethod
    def screen_size(self) -> tuple[int, int]:
        ...

    # -- capture -----------------------------------------------------------
    @abc.abstractmethod
    def capture(self) -> Frame | None:
        """Grab the current frame (or ``None`` if nothing is ready)."""

    # -- cursor ------------------------------------------------------------
    def cursor(self) -> CursorImage | None:
        """Return the current hardware cursor image, if exposed by the backend."""
        return None

    # -- input -------------------------------------------------------------
    @abc.abstractmethod
    def inject_mouse_move(self, x: int, y: int) -> None:
        ...

    @abc.abstractmethod
    def inject_mouse_button(self, button: int, down: bool) -> None:
        ...

    @abc.abstractmethod
    def inject_scroll(self, dx: int, dy: int) -> None:
        ...

    @abc.abstractmethod
    def inject_key(self, keysym: str, down: bool, mods: tuple[str, ...] = ()) -> None:
        ...

    def inject(self, ev: InputEvent) -> None:
        """Dispatch a normalised :class:`InputEvent` to the typed methods."""
        if ev.kind == "mouse_move":
            self.inject_mouse_move(ev.x, ev.y)
        elif ev.kind == "mouse_btn":
            self.inject_mouse_button(ev.button, ev.down)
        elif ev.kind == "scroll":
            self.inject_scroll(ev.dx, ev.dy)
        elif ev.kind == "key":
            self.inject_key(ev.keysym, ev.down, ev.mods)

    # -- clipboard ---------------------------------------------------------
    def get_clipboard_text(self) -> str | None:
        return None

    def set_clipboard_text(self, text: str) -> None:
        return None
