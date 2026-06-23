#!/usr/bin/env bash
# Build the standalone server-gui (control panel) binary for Linux with Nuitka.
# Output: dist/rd-server-gui
#
# Builds via rd_server_gui_entry.py (not server_gui/__main__.py) so the
# ``server_gui`` package is imported with its package context intact and the
# relative imports inside server_gui/__main__.py resolve. Pointing Nuitka at
# the file directly runs it as __main__ without a parent package, breaking
# those imports with "attempted relative import with no known parent package".
# Mirrors build_client_linux.sh (PySide6/Qt settings) and rd_server_entry.py
# (entry-wrapper pattern).
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --enable-plugin=pyside6 \
  --include-package=server_gui \
  --output-dir=dist \
  --output-filename=rd-server-gui \
  rd_server_gui_entry.py
