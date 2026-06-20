# Changelog

All notable changes to this project are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- _nothing yet_

### Changed
- _nothing yet_

### Fixed
- _nothing yet_

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
