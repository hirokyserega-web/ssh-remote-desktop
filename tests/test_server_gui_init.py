"""Regression: ServerGuiWindow must pick a service controller at construction.

The panel used to call ``_refresh_svc()`` (→ ``pick_controller(self.cfg)``)
BEFORE ``_load_form_from_config()`` set ``self.cfg``, so the AttributeError
was swallowed by the try/except in ``_refresh_svc`` and ``_svc`` stayed
``None``. The status then read "Не установлен" and every Start click failed
with "Не удалось запустить сервер" — regardless of port or config. This test
constructs the real window (headless) and asserts a controller is picked.
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="server_gui is Linux-only (Qt platform / daemon logic)",
)


def test_window_picks_service_controller_on_init(qapp, tmp_path):
    from server_gui.__main__ import ServerGuiWindow
    from server_gui.controller import (
        DaemonController, SystemdController, GuiPrefs,
    )

    prefs = GuiPrefs.load(str(tmp_path / "prefs.json"))
    win = ServerGuiWindow(
        str(tmp_path / "server.toml"), prefs, use_tray=False,
    )
    # The controller must be picked (not None) so the status shows the real
    # daemon/systemd state instead of "Не установлен".
    assert isinstance(win._svc, (DaemonController, SystemdController)), (
        "_svc is None — _refresh_svc ran before self.cfg was set; "
        "the panel is stuck on 'Не установлен' and Start can't work."
    )
    # And self.cfg must exist (the dependency _refresh_svc reads).
    assert win.cfg is not None


def test_window_status_not_not_installed_on_first_launch(qapp, tmp_path):
    """The status label must NOT read 'Не установлен' on a fresh launch."""
    from server_gui.__main__ import ServerGuiWindow
    from server_gui.controller import GuiPrefs

    prefs = GuiPrefs.load(str(tmp_path / "prefs.json"))
    win = ServerGuiWindow(
        str(tmp_path / "server.toml"), prefs, use_tray=False,
    )
    # With a real controller picked, the label is the daemon/systemd state
    # ("Остановлен"/"Запущен"), never the dead-controller "Не установлен".
    assert win.lbl_state.text() != "Не установлен"


# ---- tray icon PNG must be a valid image (regression: libpng error) ------- #
def test_tray_icon_png_is_valid():
    """The embedded TRAY_ICON_B64 must decode to a real PNG that libpng
    accepts. A malformed IDAT chunk caused `libpng error: IDAT: invalid bit
    length repeat` when the tray was initialized under sudo."""
    import base64
    import struct
    import zlib

    from server_gui.__main__ import TRAY_ICON_B64

    raw = base64.b64decode(TRAY_ICON_B64)
    # PNG signature
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG signature"
    # Walk chunks: IHDR must be first, IDAT must decompress cleanly.
    pos = 8
    ihdr_seen = False
    idat_data = b""
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        chunk_data = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            ihdr_seen = True
            w, h = struct.unpack(">II", chunk_data[:8])
            assert w > 0 and h > 0, f"invalid dimensions {w}x{h}"
        elif ctype == b"IDAT":
            idat_data += chunk_data
        pos += 12 + length  # length + type + data + CRC
    assert ihdr_seen, "no IHDR chunk"
    assert idat_data, "no IDAT chunk"
    # The actual libpng check: zlib must decompress without error.
    zlib.decompress(idat_data)  # raises on malformed bit-length repeats
