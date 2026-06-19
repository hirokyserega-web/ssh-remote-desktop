#!/usr/bin/env bash
# Build the standalone client binary for Windows with Nuitka.
# Run on a Windows host (or a Windows GitHub Actions runner via bash).
# Output: dist/rd-client.exe
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --enable-plugin=pyside6 \
  --windows-disable-console \
  --output-dir=dist \
  --output-filename=rd-client.exe \
  client/__main__.py
