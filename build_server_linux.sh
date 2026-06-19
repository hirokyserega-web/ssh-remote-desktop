#!/usr/bin/env bash
# Build the standalone server binary for Linux with Nuitka.
# Output: dist/rd-server
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --output-dir=dist \
  --output-filename=rd-server \
  server/__main__.py
