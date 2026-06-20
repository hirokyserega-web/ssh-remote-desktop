#!/usr/bin/env bash
# Build the standalone client binary for macOS with Nuitka.
# Run on a macOS GitHub Actions runner (or a macOS host with bash + python).
# Output: dist/rd-client
#
# Builds via rd_client_entry.py (not client/__main__.py) so the ``client``
# package keeps its package context and the lazy relative imports inside
# client/__main__.py:main() resolve. See build_client_linux.sh for details.
#
# macOS notes:
#   * Nuitka standalone on macOS needs a local C compiler (clang from Xcode/
#     Command Line Tools) and the onefile bootloaders; --assume-yes-for-downloads
#     lets Nuitka fetch any extra tooling (e.g. its dependency walker) without
#     prompting in the non-interactive CI runner.
#   * The resulting bundle is a single onefile binary named ``rd-client``;
#     the release workflow packages it as
#     ``ssh-remote-desktop-client-macos-$(uname -m).tar.gz``.
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --output-dir=dist \
  --output-filename=rd-client \
  rd_client_entry.py
