#!/usr/bin/env bash
# rd-launch — session-env wrapper for rd-client / rd-server-gui.
#
# Problem: when a .desktop entry is launched from the application menu, the
# process is spawned via D-Bus / `systemd --user` activation and frequently
# does NOT inherit the interactive shell environment. On Wayland compositors
# (Hyprland, Sway, river, non-systemd GNOME) this means WAYLAND_DISPLAY (and
# sometimes XDG_RUNTIME_DIR) is missing, so Qt can't find a display and the
# GUI dies silently — "I click the app and nothing opens".
#
# This wrapper reconstructs the missing session variables before exec'ing the
# real binary, so menu launches work the same as terminal launches. It is the
# canonical copy; scripts/install.sh embeds an identical heredoc for curl|bash
# binary installs (tests/test_install_launcher.py asserts they stay in sync).
#
# Usage: rd-launch <real-binary> [args...]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "rd-launch: missing binary argument" >&2
  exit 64
fi
BIN="$1"; shift

# XDG_RUNTIME_DIR — Qt needs this for both wayland and xcb (XWayland) sockets.
# Recover from the well-known /run/user/<uid> when the launcher env lacks it.
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
  uid="$(id -u 2>/dev/null || echo 0)"
  if [[ -d "/run/user/${uid}" ]]; then
    export XDG_RUNTIME_DIR="/run/user/${uid}"
  fi
fi

# WAYLAND_DISPLAY — usually 'wayland-0'. If unset, scan the runtime dir for a
# wayland-N socket (compositor present even though the var wasn't exported).
if [[ -z "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" && -d "${XDG_RUNTIME_DIR}" ]]; then
  for cand in "${XDG_RUNTIME_DIR}"/wayland-*; do
    [[ -S "$cand" ]] || continue
    WAYLAND_DISPLAY="${cand##*/}"
    export WAYLAND_DISPLAY
    break
  done
fi

# DISPLAY — only forward if actually set; never invent one. XWayland usually
# exports it, native-Wayland sessions may not, and that's fine (Qt's
# 'wayland;xcb' falls back gracefully).
# (no action — inherit whatever is present)

# Don't clobber an explicit QT_QPA_PLATFORM; the binary's own detection
# (client/__main__.py::_setup_qt_platform) picks wayland;xcb / xcb from the
# now-populated env.

exec "$BIN" "$@"
