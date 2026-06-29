"""Client entry point.

Run with::

    python -m client [--config client.toml] [--host H] [--user U] ...

or, after building, the standalone ``rd-client`` executable.

Handles Qt platform-plugin selection (``QT_QPA_PLATFORM``) with robust
Wayland detection and an automatic fallback to ``xcb`` (XWayland) when the
native ``wayland`` plugin is missing, and enables HiDPI / fractional-scaling-
aware rendering.

Wayland detection uses several independent signals because the environment
a ``.desktop`` launcher inherits (D-Bus / ``systemd --user`` activation) is
often *not* the interactive shell environment — compositors like Hyprland,
Sway, river and non-systemd GNOME frequently omit ``WAYLAND_DISPLAY`` for
menu-launched apps. We also fall back to scanning ``XDG_RUNTIME_DIR`` for a
``wayland-N`` socket and honour ``XDG_SESSION_TYPE``.

Any failure to construct ``QApplication`` (the classic "click the menu entry
and nothing happens" case) is written to
``~/.config/ssh-remote-desktop/client-launch.log`` so the failure is
diagnosable instead of silent.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

try:
    from common.updater import run_update
except ImportError:
    run_update = None


def _config_dir() -> Path:
    """Where per-user config + the launch log live."""
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ssh-remote-desktop"


def _ensure_runtime_dir() -> None:
    """Make sure ``XDG_RUNTIME_DIR`` is set (Qt needs it for wayland AND xcb).

    Menu-launched processes sometimes miss it; recover from the well-known
    ``/run/user/<uid>`` location when it exists.
    """
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    if sys.platform != "linux":
        return
    try:
        uid = os.getuid()
    except AttributeError:
        return
    candidate = f"/run/user/{uid}"
    if Path(candidate).is_dir():
        os.environ["XDG_RUNTIME_DIR"] = candidate


def _detect_wayland() -> bool:
    """True when a Wayland compositor appears to be running.

    Multiple independent signals — any one wins — because the env a
    ``.desktop`` launcher inherits is unreliable on Wayland.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        # A wayland-N socket in the runtime dir means a compositor is
        # listening there even if WAYLAND_DISPLAY wasn't exported into this
        # process's environment (common for menu-launched apps).
        try:
            for entry in os.listdir(runtime):
                if entry.startswith("wayland-") and "lock" not in entry:
                    return True
        except OSError:
            pass
    return False


def _setup_qt_platform(cfg):
    """Set QT_QPA_PLATFORM honouring config + robust Wayland->xcb fallback."""
    if sys.platform != "linux":
        return
    if os.environ.get("QT_QPA_PLATFORM"):
        return  # respect an explicit user choice
    choice = (getattr(cfg, "qt_platform", None) or "auto").lower()
    if choice == "auto":
        _ensure_runtime_dir()
        if _detect_wayland():
            # 'wayland;xcb' lets Qt fall back to XWayland if the wayland plugin
            # is unavailable in the build (e.g. a Nuitka onefile built without
            # qt6-wayland present at build time).
            os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"
        elif os.environ.get("DISPLAY"):
            os.environ["QT_QPA_PLATFORM"] = "xcb"
        else:
            # No display signal at all — try both and let Qt's own error tell
            # us what's missing; the crash log will capture it.
            os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"
    elif choice in ("wayland", "xcb"):
        _ensure_runtime_dir()
        os.environ["QT_QPA_PLATFORM"] = choice
    # Unknown choice: leave QT_QPA_PLATFORM unset so Qt uses its own default.


def _write_launch_log(exc: BaseException) -> Path:
    """Append a crash record to the per-user launch log; return its path."""
    log_path = _config_dir() / "client-launch.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("=" * 72 + "\n")
            from datetime import datetime
            fh.write(f"client launch failed: {datetime.now().isoformat()}\n")
            fh.write(f"argv: {sys.argv}\n")
            fh.write(
                "env: "
                + ", ".join(
                    f"{k}={os.environ.get(k, '<unset>')}"
                    for k in ("QT_QPA_PLATFORM", "WAYLAND_DISPLAY",
                              "XDG_RUNTIME_DIR", "DISPLAY", "XDG_SESSION_TYPE")
                )
                + "\n"
            )
            fh.write(traceback.format_exc())
    except OSError:
        pass
    return log_path


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
    p.add_argument(
        "--update", action="store_true",
        help="check for updates and install the latest version",
    )
    p.add_argument("--log-level", dest="log_level")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, 'update', False):
        if run_update:
            return run_update()
        else:
            print("Updater not available.", file=sys.stderr)
            return 1
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

    # Construct QApplication inside a guard so a missing display / missing
    # platform plugin (the "menu click does nothing" case) is logged to
    # ~/.config/ssh-remote-desktop/client-launch.log instead of vanishing.
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt

        try:
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except Exception:
            pass

        app = QApplication(sys.argv)
    except Exception as exc:
        log_path = _write_launch_log(exc)
        sys.stderr.write(
            f"rd-client: failed to start Qt GUI. See {log_path}\n"
        )
        # Try to surface the failure interactively too: a second QApplication
        # attempt with the 'offscreen' platform can still show a dialog when
        # the auto-chosen platform plugin was the only thing missing.
        try:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            from PySide6.QtWidgets import QApplication as _QA, QMessageBox as _MB
            _app = _QA(sys.argv)
            _MB.critical(
                None, "SSH Remote Desktop — launch failed",
                f"The client could not open a window.\n\nDetails were written to:\n{log_path}",
            )
            return _app.exec()
        except Exception:
            pass
        return 2

    app.setApplicationName("SSH Remote Desktop")

    # Apply saved theme + language before any window is shown so all dialogs
    # open with the right look/strings.
    try:
        from .theme import apply_theme
        apply_theme(app, cfg.theme)
    except Exception:
        pass
    try:
        from .i18n import set_language
        set_language(app, cfg.language)
    except Exception:
        pass

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
