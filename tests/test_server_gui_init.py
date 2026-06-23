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
