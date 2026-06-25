# Changelog

All notable changes to this project are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [1.4.12] - 2026-06-25

### Fixed
- `rd-server-gui` now applies the on-screen form before Start/Restart, reports port conflicts instead of silently staying on the old port, and keeps the status honest when auth/startup is limited without root.
- `rd-server`/`rd-server-gui` now forward the auth/privilege toggles and config path correctly into daemon mode.
- Display-server startup now waits for X11/Wayland readiness instead of sleeping blindly, retries X11 display numbers on races, and falls back cleanly when the Wayland runtime dir is unwritable.
- The codec surface was cleaned up so `webp` no longer pretends to be a supported wire codec; it now falls back explicitly to JPEG with a clear warning path.
- Linux dependency pins were aligned across `pyproject.toml`, `requirements.txt`, and `requirements-linux.txt`, and the MIT license file was restored.

## [1.4.11] - 2026-06-23

### Fixed
- `rd-server-gui`

## [1.4.10] - 2026-06-23

### Fixed
- The server control panel (`rd-server-gui`) showed "Не установлен" / "Not
  installed" and refused to start the server ("Не удалось запустить сервер")
  on a fresh install, regardless of the port. Root cause: the window picked
  its service controller (`pick_controller(self.cfg)`) in `__init__` before
  `self.cfg` was created — the later `_load_form_from_config()` sets it. The
  `AttributeError` was swallowed, leaving the controller as `None`, so no
  start/stop command could ever run. Moved the controller selection to after
  the config load. Added a regression test
  (`tests/test_server_gui_init.py`).

## [1.4.9] - 2026-06-23

### Fixed
- Password authentication was always rejected: `python-pam` was not declared as
  a dependency, so it never landed in the venv or the Nuitka `rd-server` binary,
  leaving `server/auth.py` with `_HAVE_PAM=False` and `check_password()` always
  returning `False`. Added `python-pam>=2.0.2` to `requirements-linux.txt`
  (Linux-only — it ships no Windows/macOS wheels) and to the `server` /
  `linux-full` optional-dependency groups in `pyproject.toml` (guarded with a
  Linux env marker so `pip install ssh-remote-desktop[server]` still works
  cross-platform). `build_server_linux.sh` now passes
  `--include-package=pam --include-module=pam` to Nuitka so the dynamically
  imported `pam` module is actually bundled into the prebuilt binary.
- The PAM service name was hardcoded to `login`. Added a configurable
  `pam_service` field to `ServerConfig` (default `login`) and threaded it
  through `Broker.validate_password` → `check_password(service=...)`.
- Missing PAM was only diagnosed on the first (opaque) rejected login. The
  server now logs a clear ERROR at startup when `allow_password=true` and PAM
  is unavailable, also hinting that PAM reads `/etc/shadow` and needs root or
  the `shadow` group.

### Added
- `rd-server-gui` control panel is now actually built and shipped. Previously
  the console script existed in `pyproject.toml` but no binary was produced:
  added `rd_server_gui_entry.py` (Nuitka wrapper, mirroring
  `rd_server_entry.py`) and `build_server_gui_linux.sh` (mirrors
  `build_client_linux.sh` with the PySide6 plugin), wired the `server-gui`
  component into the Linux release matrix, and taught `scripts/install.sh` to
  download, verify, install and symlink `rd-server-gui` (component `server-gui`
  → binary `rd-server-gui`), build it from source, and create its menu launcher
  only when the binary is actually present (no dead menu entry).

## [1.4.8] - 2026-06-21

### Fixed
- `rd-client` could not connect to anything: every connection failed with
  ``SSHClientConnectionOptions.prepare() got an unexpected keyword argument
  'client'``. ``client/transport._connect_options`` passed the TOFU client as
  ``asyncssh.connect(..., client=...)``, but on the pinned asyncssh floor
  (``>=2.23``) that kwarg is funnelled into ``SSHClientConnectionOptions.prepare()``,
  which has no ``client`` parameter — so every connect raised ``TypeError``
  before the TCP connection was even attempted and the transport loop crashed
  after the retry budget. Switched to ``client_factory`` (a callable returning
  an ``SSHClient``), which is supported on every asyncssh release in range and
  produces one fresh ``TofuClient`` per connection (its state is self-contained).
  Added a regression test pinning the options shape against the installed
  asyncssh.

## [1.4.7] - 2026-06-21

### Fixed
- `rd-server` printed a multi-page asyncio/asyncssh traceback instead of an
  actionable message when its port was already in use (the common case: a
  previous foreground `rd-server` left over from an earlier install still held
  the port). `broker.start()` now wraps the bind in a `PortInUseError` and
  `main()` prints a one-line hint naming the port plus the exact commands to
  find and stop the old process (`ss -tlnp | grep <port>`, `pkill -f rd-server`,
  `rd-server --stop`) or pick another port. Foreground launches also now check
  the daemon pidfile for a live process first, so a stale-but-running daemon
  is reported as "already running" instead of racing the port.

## [1.4.6] - 2026-06-20

### Fixed
- `rd-client` (and `rd-server`) crashed on launch with
  `tomllib.TOMLDecodeError: Invalid statement (at line 1, column 1)` when the
  config file existed but was empty or contained garbage. `common.config._read_file`
  now treats an empty file as a valid "no overrides" config and wraps every
  TOML/JSON parse in try/except, falling back to built-in defaults with a
  warning naming the offending file instead of aborting startup.

## [1.4.5] - 2026-06-20

### Fixed
- Arch Linux: the server install tried to `pacman -S xauth`, but the package
  is named `xorg-xauth` on Arch — the install aborted with "target not found:
  xauth". Fixed to `xorg-xauth`.
- `sudo install-server-linux.sh` linked the new binary into `/root/.local/bin`
  (useless for the real user) and left a stale `~/.local/bin/rd-server` symlink
  from a previous non-sudo install in place. That stale link shadowed
  `/usr/local/bin`, so `rd-server` kept launching the old, crashing binary even
  though the fresh one was installed. The installer now resolves the invoking
  user's home via `SUDO_USER` and re-points their `~/.local/bin/rd-*` symlinks
  at the freshly-installed binary.

## [1.4.4] - 2026-06-20

### Fixed
- Nuitka-built ``rd-client`` failed at startup with
  ``ImportError: attempted relative import with no known parent package``
  because ``build_client_{linux,windows,macos}.sh`` pointed Nuitka at
  ``client/__main__.py`` directly (running it as ``__main__`` without a parent
  package). The lazy relative imports inside ``main()``
  (``from .theme import …``, ``from .main_window import …``) only fire on a real
  launch — not ``--version``/``--help`` — so the regression slipped past smoke
  tests. All three client build scripts now build via ``rd_client_entry.py``
  (which imports ``client.__main__:main`` normally), mirroring the server fix.

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
