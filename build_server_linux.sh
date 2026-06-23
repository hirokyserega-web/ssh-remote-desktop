#!/usr/bin/env bash
# Build the standalone server binary for Linux with Nuitka.
# Output: dist/rd-server
#
# Builds via rd_server_entry.py (not server/__main__.py) so the ``server``
# package is imported with its package context intact and the relative
# imports inside server/__main__.py resolve. Pointing Nuitka at the file
# directly runs it as __main__ without a parent package, breaking
# ``from .broker import ...`` with "attempted relative import with no known
# parent package".
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --output-dir=dist \
  --output-filename=rd-server \
  --include-package=pam \
  --include-module=pam \
  rd_server_entry.py
