"""Root pytest setup so a bare `python -m pytest -q` collects every suite.

content-engine/ declares `pythonpath = ["src"]` in its own pyproject, which
pytest only honours when it picks content-engine/ as rootdir. Running from the
repo root without explicit paths skipped that, so its tests failed to import.
"""
import sys
from pathlib import Path

_CE_SRC = Path(__file__).resolve().parent / "content-engine" / "src"
if _CE_SRC.is_dir() and str(_CE_SRC) not in sys.path:
    sys.path.insert(0, str(_CE_SRC))
