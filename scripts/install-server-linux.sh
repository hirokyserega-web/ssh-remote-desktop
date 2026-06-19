#!/usr/bin/env bash
# One-line install for the SERVER on a Linux host.
# Thin wrapper around the universal installer; forwards every flag including
# --version / --from-source. Run with sudo (the server needs root for PAM,
# Xvfb, and /dev/uinput).
#
# Usage:
#   curl -fsSL .../install-server-linux.sh | sudo bash
#   ... | sudo bash -s -- --version 1.1.0
#   ... | sudo bash -s -- --from-source
set -euo pipefail

# Some distros don't have curl by default.
if ! command -v curl >/dev/null 2>&1; then
  apt-get update && apt-get install -y curl || \
    dnf install -y curl || pacman -S --noconfirm curl || apk add curl || true
fi

export SSH_REMOTE_DESKTOP_DIR="${SSH_REMOTE_DESKTOP_DIR:-/opt/ssh-remote-desktop}"

curl -fsSL \
  https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh \
  | bash -s -- --component server --dir "$SSH_REMOTE_DESKTOP_DIR" "$@"
