# Changelog

All notable changes to this project are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.3] - 2026-06-20

### Fixed
- Nuitka-built ``rd-server`` failed at startup with
  ``ImportError: attempted relative import with no known parent package``
  because ``build_server_linux.sh`` pointed Nuitka at ``server/__main__.py``
  directly (running it as ``__main__`` without a parent package). It now builds
  via ``rd_server_entry.py``, which imports ``server.__main__:main`` normally so
  the ``server`` package keeps its package context and relative imports resolve.

## [1.4.2] - 2026-06-20

### Fixed
- Release Linux binaries are now built on ubuntu-22.04 instead of ubuntu-latest
  so the onefile executables don't pick up a GLIBC_2.38 requirement from the
  newer runner.

## [1.4.1] - 2026-06-20

### Fixed
- Release pipeline wrote SHA256SUMS with a `staging/` path prefix
  (`staging/<asset>`); `install.sh`'s checksum matcher required a space
  before the basename and so never matched, silently skipping verification
  of every downloaded binary. The matcher now accepts a `/`-prefixed path,
  and the pipeline emits bare asset names.

## [1.4.0] - 2026-06-20

### Added
- `rd-server-gui` Qt platform flags: `--qt-platform {auto,xcb,wayland,offscreen}`
  and `--offscreen`, mirroring the client. Auto-detects Wayland (wayland;xcb)
  or X (xcb); offscreen only when no display or explicitly requested.
- `install.sh --diagnose` / `--doctor`: prints resolved rd-* commands, PATH
  membership, effective Qt platform, and Qt6 system-library presence.
- Arch Linux derivative support (Garuda, ArcoLinux, CachyOS, …) via
  `/etc/os-release` `ID_LIKE` mapping; full xcb-util-* + qt6-wayland package set.
- `tests/test_server_gui_platform.py` (21 tests) and
  `tests/test_install_path.py` (10 tests) for the new GUI/install behaviour.

### Changed
- `install-client-linux.sh` defaults to a user-writable dir
  (`~/.local/share/ssh-remote-desktop`) so the one-liner works without sudo;
  `/opt` still selectable via `--dir` under sudo.
- `install.sh ensure_path` now targets the rc matching `$SHELL` (bash/zsh/fish)
  instead of always editing `~/.bashrc`+`~/.profile`, is idempotent
  (marker-guarded), and warns with the exact line to run for the current shell.

### Fixed
- `rd-server-gui` no longer hides its window on real desktops: removed the
  unconditional `QT_QPA_PLATFORM=offscreen` default. An explicit
  `QT_QPA_PLATFORM` is now sacred; `--qt-platform=auto` only falls back to
  offscreen when no display is available.
- `install-client-linux.sh` no longer dies at the first `/opt` write when run
  without sudo (the wrapper ran unprivileged but defaulted to a root-only dir).
- Removed stray empty `te` file from the repo root.
- CI: install/PATH tests now portable across root-sandbox and non-root runners
  (no `setpriv`); CI/GitHub-Actions no longer treated as a headless trigger.

## [1.3.0] - 2026-06-20

### Added
- Server daemon mode: `rd-server --daemon/--stop/--status` with double-fork,
  setsid, atomic pidfile, and stdio rebound to `--log-file` (or `/dev/null`).
  Refuses to start if a live pidfile already points at a running process.
- Systemd integration: `packaging/systemd/ssh-remote-desktop.service` plus
  `rd-server install` / `uninstall` subcommands that write the unit to
  `/etc/systemd/system`, run `daemon-reload`, and optionally `enable --now`.
- `rd-server-gui` — a PySide6 control panel for the server (host/port/backend/
  codec/limits/auth toggles/logging), with a Qt-free testable controller, atomic
  secret-free config writes, start/stop/restart via systemd-or-daemon fallback,
  live log tail, RU/EN i18n, and light/dark theme.
- `server-gui` optional extra (`PySide6>=6.6`) and `rd-server-gui` entry point.

### Changed
- `server.broker` splits `serve_forever` so the pidfile is written between
  listener start and the run-forever wait (daemon mode).
- i18n dictionary extended with server-GUI strings (RU/EN).

## [1.2.0] - 2026-06-20

## [1.1.0] - 2026-06-20

### Added
- Release pipeline: GitHub Actions workflow auto-builds standalone
  binaries (Nuitka) for Linux (client + server), Windows (client), and
  macOS (client, optional) on every `v*` tag, packages them with
  SHA256SUMS, and publishes a GitHub Release.
- `scripts/release.sh` helper to cut a release (bump VERSION + CHANGELOG,
  commit, tag, push) with `--dry-run` and `--no-push` support.
- One-line installers (`install.sh`, `install.ps1`) now download
  prebuilt release binaries first (with SHA256 verification) and only
  fall back to building from source when no matching asset exists.
- Runtime i18n (RU/EN) with live language switching via Preferences.
- Unified light/dark theme system with live switching.
- Preferences dialog (theme, language, default codec, JPEG quality, key path).
- Connection-state overlay with animated spinner in the desktop view.
- Toolbar with reconnect, files, keys, preferences, fullscreen actions.

### Fixed
- Cross-platform dependencies: split Linux-only packages (evdev,
  pywayland, dbus-next) into `requirements-linux.txt` so Windows/macOS
  installs no longer fail.
- Guarded `import pwd` in server modules for Windows test compatibility.
- Fixed invalid `expandedBy` Qt call; missing `QSize` import.
- CI: added `pyyaml` for release-workflow tests; skip bash-script tests
  on Windows; gate POSIX-only 0600 key-permission assertion.

## [1.0.0] - 2026-06-16

- First public release.
