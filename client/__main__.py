"""Client entry point.

Run with::

    python -m client [--config client.toml] [--host H] [--user U] ...

or, after building, the standalone ``rd-client`` executable.

Handles Qt platform-plugin selection (``QT_QPA_PLATFORM``) with an automatic
fallback to ``xcb`` (XWayland) when the native ``wayland`` plugin is missing,
and enables HiDPI / fractional-scaling-aware rendering.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _setup_qt_platform(cfg):
    """Set QT_QPA_PLATFORM honouring config + Wayland->xcb fallback (Linux)."""
    if sys.platform != "linux":
        return
    if os.environ.get("QT_QPA_PLATFORM"):
        return  # respect an explicit user choice
    choice = (cfg.qt_platform or "auto").lower()
    if choice == "auto":
        # Prefer native Wayland when a compositor is present, else xcb.
        if os.environ.get("WAYLAND_DISPLAY"):
            # 'wayland;xcb' lets Qt fall back to XWayland if the wayland plugin
            # is unavailable in the build.
            os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"
        else:
            os.environ["QT_QPA_PLATFORM"] = "xcb"
    else:
        os.environ["QT_QPA_PLATFORM"] = choice


def build_parser():
    p = argparse.ArgumentParser(prog="rd-client", description="SSH remote desktop client")
    p.add_argument("--config", help="path to client config (TOML/JSON)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--user")
    p.add_argument("--auth", choices=["key", "password", "agent"])
    p.add_argument("--key-path", dest="key_path")
    p.add_argument("--codec", choices=["h264", "h265", "jpeg"])
    p.add_argument("--qt-platform", dest="qt_platform",
                   choices=["auto", "xcb", "wayland"])
    p.add_argument("--fullscreen", dest="start_fullscreen", action="store_true", default=None)
    p.add_argument("--no-clipboard", dest="clipboard_enabled", action="store_false", default=None)
    p.add_argument("--keygen", action="store_true",
                   help="open only the SSH key generator and exit")
    p.add_argument("--log-level", dest="log_level")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    from common.config import load_client_config
    overrides = {k: v for k, v in vars(args).items()
                 if k not in ("config", "keygen") and v is not None}
    cfg = load_client_config(args.config, overrides)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _setup_qt_platform(cfg)

    # HiDPI: Qt6 enables high-DPI scaling by default; make rounding nicer.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("SSH Remote Desktop")

    if args.keygen:
        from .keys_dialog import KeysDialog
        dlg = KeysDialog(cfg, None)
        dlg.exec()
        return 0

    from .main_window import MainWindow
    win = MainWindow(cfg)
    if cfg.start_fullscreen:
        win.act_full.setChecked(True)
        win.showFullScreen()
    else:
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
