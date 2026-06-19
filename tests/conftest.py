"""pytest configuration: make the project root importable."""

import os
import sys

# Tests run with `pytest` from the project root; ensure that root is on sys.path
# so `import common`, `import client`, `import server` work without packaging.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """A headless QApplication for GUI tests (offscreen Qt platform)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
