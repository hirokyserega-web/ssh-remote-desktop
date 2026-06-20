#!/usr/bin/env bash
# Build the standalone client binary for Linux with Nuitka.
# Output: dist/rd-client
#
# Builds via rd_client_entry.py (not client/__main__.py) so the ``client``
# package is imported with its package context intact and the lazy relative
# imports inside client/__main__.py:main() (from .theme, .main_window, …)
# resolve. Pointing Nuitka at the file directly runs it as __main__ without a
# parent package, breaking those imports.
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --output-dir=dist \
  --output-filename=rd-client \
  rd_client_entry.py
