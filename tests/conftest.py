"""pytest configuration: make the project root importable."""

import os
import sys

# Tests run with `pytest` from the project root; ensure that root is on sys.path
# so `import common`, `import client`, `import server` work without packaging.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
