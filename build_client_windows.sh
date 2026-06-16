#!/usr/bin/env bash
set -euo pipefail
python -m nuitka \
  --standalone \
  --onefile \
  --enable-plugin=pyside6 \
  --windows-disable-console \
  --output-filename=rd-client.exe \
  client/__main__.py
