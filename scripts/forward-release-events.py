#!/usr/bin/env python3
"""Forward qualified ClawBytes GitHub releases to Release Bot.

Thin CLI. The script directory is what Python puts on ``sys.path`` when this
file is launched as ``python3 scripts/forward-release-events.py``, so the
repo-root modules are inserted before import. Exit 0 always: a forward
failure must not look like a failed collect.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_forwarding import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
