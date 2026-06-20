"""Server GUI package: a PySide6 control panel for ``rd-server``.

* :mod:`server_gui.controller` — Qt-free logic (config, systemd/daemon
  control, log tail, prefs). Testable without a display.
* :mod:`server_gui.__main__` — the Qt window + tray. Run as
  ``python -m server_gui`` or via the ``rd-server-gui`` console script.

Reuses the client's :mod:`client.theme` (light/dark/system) and
:mod:`client.i18n` (RU/EN) so the server GUI looks and speaks like the rest
of the app.
"""
