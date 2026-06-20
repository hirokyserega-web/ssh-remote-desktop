#!/usr/bin/env bash
# Build the standalone client binary for Windows with Nuitka.
# Run on a Windows host (or a Windows GitHub Actions runner via bash).
# Output: dist/rd-client.exe
#
# Builds via rd_client_entry.py (not client/__main__.py) so the ``client``
# package keeps its package context and the lazy relative imports inside
# client/__main__.py:main() resolve. See build_client_linux.sh for details.
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --windows-console-mode=disable \
  --output-dir=dist \
  --output-filename=rd-client.exe \
  rd_client_entry.py
