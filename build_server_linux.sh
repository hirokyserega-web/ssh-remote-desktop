#!/usr/bin/env bash
set -euo pipefail
python -m nuitka \
  --standalone \
  --onefile \
  --output-filename=rd-server \
  server/__main__.py
