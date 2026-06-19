#!/usr/bin/env bash
# One-line install for the CLIENT on Linux.
#
# Thin wrapper around the universal installer. Forwards every flag, including
# the new --version / --from-source / --no-build, so the client gets the
# prebuilt release binary by default (or builds from source with --from-source).
#
# Usage:
#   curl -fsSL .../install-client-linux.sh | bash
#   ... | bash -s -- --version 1.1.0
#   ... | bash -s -- --from-source
set -euo pipefail

export SSH_REMOTE_DESKTOP_DIR="${SSH_REMOTE_DESKTOP_DIR:-/opt/ssh-remote-desktop}"

curl -fsSL \
  https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh \
  | bash -s -- --component client --dir "$SSH_REMOTE_DESKTOP_DIR" "$@"
