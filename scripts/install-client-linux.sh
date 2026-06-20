#!/usr/bin/env bash
# One-line install for the CLIENT on Linux.
#
# Thin wrapper around the universal installer. Forwards every flag, including
# the new --version / --from-source / --no-build, so the client gets the
# prebuilt release binary by default (or builds from source with --from-source).
#
# Installs into a user-writable directory (~/.local/share/ssh-remote-desktop,
# matching the universal installer's default) and symlinks the binary into
# ~/.local/bin, so it works without sudo. To install system-wide into /opt
# instead, run this under sudo and set the dir explicitly:
#   curl -fsSL .../install-client-linux.sh | sudo bash -s -- --dir /opt/ssh-remote-desktop
#
# Usage:
#   curl -fsSL .../install-client-linux.sh | bash
#   ... | bash -s -- --version 1.1.0
#   ... | bash -s -- --from-source
set -euo pipefail

# Default to a user-writable dir so the one-liner works without root. The
# previous /opt default required sudo for mkdir/extract/venv but the wrapper
# ran without it, so the install died at the first write. /opt is still
# supported via an explicit --dir override (run under sudo).
export SSH_REMOTE_DESKTOP_DIR="${SSH_REMOTE_DESKTOP_DIR:-$HOME/.local/share/ssh-remote-desktop}"

curl -fsSL \
  https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh \
  | bash -s -- --component client --dir "$SSH_REMOTE_DESKTOP_DIR" "$@"
