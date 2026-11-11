"""Pytest bootstrap: make the ``facesmith`` package importable when the suite is
run from the repo root as ``pytest tools/facesmith``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
