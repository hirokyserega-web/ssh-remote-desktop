#!/usr/bin/env bash
# One-line install for the SERVER on a Linux host.
# Sets up system packages, runs the dev checkout, builds the server binary.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-server-linux.sh | sudo bash
#   ... | sudo bash -s -- --no-build
set -euo pipefail

# Some distros don't have curl by default.
if ! command -v curl >/dev/null 2>&1; then
  apt-get update && apt-get install -y curl || \
    dnf install -y curl || pacman -S --noconfirm curl || apk add curl || true
fi

export SSH_REMOTE_DESKTOP_DIR="${SSH_REMOTE_DESKTOP_DIR:-/opt/ssh-remote-desktop}"

curl -fsSL \
  https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh \
  | bash -s -- --dev --build --dir "$SSH_REMOTE_DESKTOP_DIR" "$@"
