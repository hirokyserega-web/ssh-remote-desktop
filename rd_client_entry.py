"""Nuitka entry point for the ``rd-client`` binary.

Building ``client/__main__.py`` directly with Nuitka runs it as ``__main__``
without a parent package, so the lazy relative imports inside ``main()``
(``from .theme import ...``, ``from .main_window import ...``) fail with
``ImportError: attempted relative import with no known parent package``.

Importing ``client.__main__`` here keeps the ``client`` package context, so the
relative imports resolve. Mirrors ``rd_server_entry.py``.
"""
from client.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
