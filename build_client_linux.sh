#!/usr/bin/env bash
# Build the standalone client binary for Linux with Nuitka.
# Output: dist/rd-client
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --output-dir=dist \
  --output-filename=rd-client \
  client/__main__.py
