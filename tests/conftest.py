"""Pytest bootstrap: make the repo root importable so `app.*` resolves when
pytest is invoked from anywhere."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
