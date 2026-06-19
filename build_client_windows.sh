#!/usr/bin/env bash
# Build the standalone client binary for Windows with Nuitka.
# Run on a Windows host (or a Windows GitHub Actions runner via bash).
# Output: dist/rd-client.exe
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --windows-console-mode=disable \
  --output-dir=dist \
  --output-filename=rd-client.exe \
  client/__main__.py
