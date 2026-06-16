#!/usr/bin/env bash
set -euo pipefail
python -m nuitka \
  --standalone \
  --onefile \
  --enable-plugin=pyside6 \
  --output-filename=rd-client \
  client/__main__.py
