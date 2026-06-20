"""Nuitka entry point for the ``rd-server`` binary.

The real entry point lives in ``server.__main__:main`` (see pyproject.toml's
console script). ``build_server_linux.sh`` invokes Nuitka on *this* wrapper
instead of ``server/__main__.py`` directly because Nuitka's onefile mode runs
``__main__`` without a parent package, so the relative imports inside
``server/__main__.py`` (``from .broker import Broker``) fail with
``ImportError: attempted relative import with no known parent package``.

Importing ``server.__main__`` as an absolute module here makes Python set
``__package__ = "server"`` for it, so its relative imports resolve and Nuitka
pulls the whole ``server`` package into the bundle.
"""
import sys

from server.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
