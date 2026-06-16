#!/usr/bin/env bash
# One-line install for the CLIENT on Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-client-linux.sh | bash
#   ... | bash -s -- --qt-platform wayland
set -euo pipefail

export SSH_REMOTE_DESKTOP_DIR="${SSH_REMOTE_DESKTOP_DIR:-/opt/ssh-remote-desktop}"

curl -fsSL \
  https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh \
  | bash -s -- --dev --build --dir "$SSH_REMOTE_DESKTOP_DIR" "$@"
