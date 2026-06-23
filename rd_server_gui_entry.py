"""Nuitka entry point for the ``rd-server-gui`` binary.

The real entry point lives in ``server_gui.__main__:main`` (see
pyproject.toml's console script). ``build_server_gui_linux.sh`` invokes Nuitka
on *this* wrapper instead of ``server_gui/__main__.py`` directly because
Nuitka's onefile mode runs ``__main__`` without a parent package, so the
relative imports inside ``server_gui/__main__.py`` would fail with
``ImportError: attempted relative import with no known parent package``.

Importing ``server_gui.__main__`` as an absolute module here makes Python treat
it as part of the ``server_gui`` package, so its imports resolve and Nuitka
pulls the whole ``server_gui`` package (plus its ``client`` / ``server.daemon``
dependencies) into the bundle. Mirrors ``rd_server_entry.py`` /
``rd_client_entry.py``.
"""
import sys

from server_gui.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
