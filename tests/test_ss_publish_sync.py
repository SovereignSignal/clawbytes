"""Sync guard for the vendored ss_publish package.

ss-publish is vendored into multiple SovereignSignal repos (modelbytes,
clawbytes) as an identical ss_publish/ directory, with the canonical source at
repos/ss-publish/. Vendoring was chosen over a separate repo (2026-06-19) for
two slow-changing consumers; the cost is drift risk. This test catches it:
if anyone edits ss_publish/ in one repo without propagating to the others,
this test fails and names the file that drifted.

The canonical copy lives at ../ss-publish/ss_publish/ when the brain repos/
tree is checked out alongside this repo. When that sibling isn't present
(e.g. Railway build, fresh clone of just this repo), the test self-skips —
the per-repo unit tests (test_ss_publish_*) still cover the vendored code in
isolation.
"""
from pathlib import Path

VENDORED = Path(__file__).resolve().parent.parent / "ss_publish"
# Sibling repos tree: this repo is at <brain>/repos/clawbytes, canonical at
# <brain>/repos/ss-publish. Walk up to find it; skip if absent.
CANONICAL = Path(__file__).resolve().parents[2] / "ss-publish" / "ss_publish"

FILES = ["__init__.py", "markup.py", "publisher.py"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_vendored_package_present():
    for name in FILES:
        assert (VENDORED / name).exists(), f"vendored ss_publish/{name} missing"


def test_vendored_matches_canonical():
    if not CANONICAL.exists():
        import pytest
        pytest.skip(f"canonical ss-publish not found at {CANONICAL} "
                    f"(not in a full repos/ checkout); per-repo unit tests still cover the code")
    drift = []
    for name in FILES:
        vendored = _read(VENDORED / name)
        canonical = _read(CANONICAL / name)
        if vendored != canonical:
            drift.append(name)
    assert not drift, (
        f"ss_publish/ has drifted from the canonical copy at {CANONICAL}: {drift}. "
        f"Edit the canonical source, then copy ss_publish/ into every vendoring repo "
        f"(modelbytes, clawbytes). Run this test in each to confirm they match."
    )
