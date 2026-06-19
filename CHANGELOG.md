# Changelog

All notable changes to this project are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
