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
#
# Includes:
#   * the server's own packages (server / common / crypto) — always explicit
#     so a future lazy import inside them can't be missed by static analysis;
#   * asyncssh / cryptography / bcrypt / msgpack / numpy — the SSH + encoding
#     stack. asyncssh is imported under a try/except in broker.py, and its
#     transitive deps (cryptography, bcrypt) are pulled in lazily; forcing
#     them here guarantees the frozen binary has them instead of dying with
#     ModuleNotFoundError on a clean machine that has no venv;
#   * pam — the PAM password backend (also force-included so _HAVE_PAM=True);
#   * Xlib / mss — the X11 capture backend, imported lazily by server.backend.
#     Xlib.ext.{xtest,damage,xfixes} are force-included explicitly because
#     x11.py imports them inside try/except at module top-level — Nuitka's
#     static analysis can miss lazily-resolved extension modules, and without
#     them XTEST input, XDamage tracking and XFixes cursor are silently
#     disabled in the frozen binary (the _HAVE_XLIB=False path).
#
# --onefile-tempdir-spec places the onefile extraction in a predictable,
# per-process cache dir instead of /tmp. /tmp is often a size-limited tmpfs
# that gets cleaned aggressively; a cache-dir spec keeps the extraction
# predictable and large enough for the ~70 MB payload. {PID} keeps concurrent
# rd-server processes (the daemon spawner and its foreground child) from
# colliding on the same extraction dir.
set -euo pipefail

python -m nuitka \
  --standalone \
  --onefile \
  --onefile-tempdir-spec='{CACHE_DIR}/rd-server/{PID}' \
  --assume-yes-for-downloads \
  --output-dir=dist \
  --output-filename=rd-server \
  --include-package=server \
  --include-package=common \
  --include-package=crypto \
  --include-package=asyncssh \
  --include-package=cryptography \
  --include-module=bcrypt \
  --include-package=msgpack \
  --include-package=numpy \
  --include-package=Xlib \
  --include-package=Xlib.ext \
  --include-module=Xlib.ext.xtest \
  --include-module=Xlib.ext.damage \
  --include-module=Xlib.ext.xfixes \
  --include-package=mss \
  --include-package=pam \
  --include-module=pam \
  rd_server_entry.py

# Smoke check: verify that the compiled binary can run and imports correctly
echo "Running smoke check on compiled server binary..."
dist/rd-server --help > /dev/null
echo "Smoke check passed successfully!"
