#!/usr/bin/env python3
"""ClawBytes curator — per-publish editorial review.

Reads a candidate bundle JSON from stdin, asks Claude Code to review it, returns
the (possibly modified) bundle JSON on stdout. Falls back to passing the bundle
through unchanged on any error so the publisher can keep posting.

The publisher (clawbytes_threads.py with --use-curator) invokes this and uses
the returned bundle. See docs/curator-prompt.md for the system prompt; see
the design spec at docs/superpowers/specs/2026-05-20-clawbytes-architecture-design.md
for the contract.

Exit codes:
  0 = success, valid curated bundle on stdout
  2 = unrecoverable error; caller should fall back to deterministic bundle
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from claude_common import (  # noqa: E402
    ClaudeCodeError,
    call_claude,
    load_scope,
    parse_json_from_text,
)

CURATOR_PROMPT_FILE = REPO_ROOT / "docs" / "curator-prompt.md"
DISCOVERED_REFS_FILE = REPO_ROOT / "memory" / "discovered_references.json"
DEGRADED_LOG = REPO_ROOT / "memory" / "degraded_publishes.json"


def build_user_prompt(bundle: dict) -> str:
    """The user-turn prompt: 'here is the bundle, review it, return JSON.'"""
    return (
        "Review this ClawBytes lane bundle. Apply the editorial scope and curator "
        "responsibilities defined in your system prompt. Return only the curated "
        "bundle JSON on stdout — no prose, no markdown, no commentary.\n\n"
        f"BUNDLE:\n{json.dumps(bundle, indent=2)}"
    )


def build_system_prompt() -> str:
    """Curator system prompt + scope constitution, concatenated."""
    if not CURATOR_PROMPT_FILE.exists():
        raise FileNotFoundError(f"curator prompt missing: {CURATOR_PROMPT_FILE}")
    curator_prompt = CURATOR_PROMPT_FILE.read_text()
    scope = load_scope()
    return f"{curator_prompt}\n\n---\n\n# Editorial Scope (from EDITORIAL_SCOPE.md)\n\n{scope}"


def fallback_bundle(bundle: dict, reason: str, error_kind: str = "fallback") -> dict:
    """Return the original bundle annotated with curator metadata explaining the fallback."""
    out = dict(bundle)
    out["_curator"] = {
        "approved": True,
        "dropped_item_ids": [],
        "drop_reasons": {},
        "rewrote_blurbs": [],
        "rewrote_take": False,
        "discovered_references": [],
        "anchor_check": "skipped",
        "notes": f"FALLBACK: {reason}",
        "fallback": True,
        "fallback_kind": error_kind,
    }
    return out


def log_degraded(lane: str, kind: str, message: str) -> None:
    """Append a degraded-publish event for supervisor to inspect."""
    DEGRADED_LOG.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if DEGRADED_LOG.exists():
        try:
            existing = json.loads(DEGRADED_LOG.read_text())
        except Exception:
            existing = []
    existing.append({
        "lane": lane,
        "kind": kind,
        "message": message[:500],
        "at": int(time.time()),
    })
    existing = existing[-200:]  # keep last 200 events
    DEGRADED_LOG.write_text(json.dumps(existing, indent=2))


def persist_discovered_references(refs: list) -> None:
    """Append curator-flagged references to the queue supervisor drains."""
    if not refs:
        return
    DISCOVERED_REFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if DISCOVERED_REFS_FILE.exists():
        try:
            existing = json.loads(DISCOVERED_REFS_FILE.read_text())
        except Exception:
            existing = []
    for ref in refs:
        if isinstance(ref, dict):
            ref = dict(ref)
            ref.setdefault("discovered_at", int(time.time()))
            existing.append(ref)
    existing = existing[-500:]  # bound the queue
    DISCOVERED_REFS_FILE.write_text(json.dumps(existing, indent=2))


def curate(bundle: dict, *, timeout: int = 180, dry_run: bool = False) -> dict:
    """Run the curator pass on a single bundle. Always returns a usable bundle."""
    lane = bundle.get("lane", "unknown")

    if dry_run:
        out = fallback_bundle(bundle, "dry-run mode — bundle passed through unchanged", "dry_run")
        return out

    try:
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(bundle)
    except FileNotFoundError as e:
        log_degraded(lane, "missing_prompt", str(e))
        return fallback_bundle(bundle, f"missing prompt file: {e}", "missing_prompt")

    try:
        result = call_claude(
            user_prompt,
            system_prompt=system_prompt,
            # Curator has research powers: WebSearch to find what shipped today
            # in the agent ecosystem, WebFetch to pull primary sources, Read to
            # consult EDITORIAL_SCOPE.md and the repo's own docs.
            # When the input bundle is weak, the curator is expected to actively
            # find better signal rather than skip the publish.
            allowed_tools=["WebSearch", "WebFetch", "Read"],
            timeout=timeout,
        )
    except ClaudeCodeError as e:
        log_degraded(lane, e.kind, str(e))
        # Emit stderr to our own stderr so Railway logs capture it for diagnosis
        print(f"[curator] ClaudeCodeError kind={e.kind} msg={e}", file=sys.stderr)
        if e.stderr:
            print(f"[curator] claude stderr (first 2000 chars):\n{e.stderr[:2000]}", file=sys.stderr)
        return fallback_bundle(bundle, f"claude error: {e}", e.kind)

    try:
        curated = parse_json_from_text(result.text)
    except (json.JSONDecodeError, ValueError) as e:
        log_degraded(lane, "bad_json", f"curator returned non-JSON: {result.text[:300]!r}")
        # Dump start/middle/end of the offending text so we can see what Claude actually wrote.
        # Many bad_json errors are unescaped quotes in a blurb or a trailing comma.
        print(f"[curator] BAD JSON parse error: {e}", file=sys.stderr)
        print(f"[curator] text length: {len(result.text)}", file=sys.stderr)
        print(f"[curator] text[:400]: {result.text[:400]!r}", file=sys.stderr)
        # Print 200 chars around the error position
        msg = str(e)
        import re
        m = re.search(r'char (\d+)', msg)
        if m:
            pos = int(m.group(1))
            lo = max(0, pos - 200)
            hi = min(len(result.text), pos + 200)
            print(f"[curator] text[{lo}:{hi}] (error at char {pos}): {result.text[lo:hi]!r}", file=sys.stderr)
        print(f"[curator] text[-400:]: {result.text[-400:]!r}", file=sys.stderr)
        return fallback_bundle(bundle, f"curator returned non-JSON: {e}", "bad_json")

    # Enrich curator metadata with telemetry
    meta = curated.setdefault("_curator", {})
    meta.setdefault("approved", True)
    meta["model"] = result.model
    meta["tokens_used"] = {
        "input": result.input_tokens,
        "output": result.output_tokens,
    }
    meta["duration_ms"] = result.duration_ms
    meta["fallback"] = False

    # Persist any discovered references for supervisor to drain
    refs = meta.get("discovered_references") or []
    if refs:
        persist_discovered_references(refs)

    return curated


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawBytes curator (stdin=bundle JSON, stdout=curated bundle JSON)")
    parser.add_argument("--timeout", type=int, default=180, help="Claude Code subprocess timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Pass bundle through unchanged with fallback marker (no Claude call)")
    args = parser.parse_args()

    try:
        bundle = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"stdin not valid JSON: {e}"}), file=sys.stderr)
        return 2

    curated = curate(bundle, timeout=args.timeout, dry_run=args.dry_run)
    print(json.dumps(curated, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
