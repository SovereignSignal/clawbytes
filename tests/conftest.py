"""Shared test setup for clawbytes_threads tests.

CLAWBYTES_MEMORY_DIR must point at a throwaway dir BEFORE clawbytes_threads
is imported: the module resolves MEMORY and ALLOWED_SUBREDDITS at import time
and would otherwise read (and let tests write) the repo's tracked memory/.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP_MEMORY = tempfile.mkdtemp(prefix="clawbytes-test-memory-")
os.environ["CLAWBYTES_MEMORY_DIR"] = _TMP_MEMORY
os.environ.setdefault("WORKSPACE", str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# scripts/ holds scheduler.py and the monitors, which some tests import directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
