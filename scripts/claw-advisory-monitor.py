#!/usr/bin/env python3
"""
ClawBytes GitHub Advisory Monitor
Polls the GitHub Advisory Database and emits Watch items only for packages
we already track. Not an ecosystem=pip firehose: the response is filtered
on vulnerabilities[].package.name against an allowlist.

First sighting records seen GHSA ids and emits nothing. Item URL is the
advisory's own html_url. At most DAILY_CAP items per UTC day.

State file: memory/claw-advisory-state.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-advisory-state.json"

# Recent advisories across ecosystems. Do not pass ecosystem=pip — that is
# the unfiltered firehose the retired monitor became.
ADVISORY_URL = "https://api.github.com/advisories?per_page=50&sort=updated&direction=desc"
DAILY_CAP = 2

# Exact package names (lowercase). Scoped names match on the full string or
# the segment after the last "/". Drawn from repos we already watch, plus
# the framework packages the coverage plan names. Not a free-text search.
PACKAGE_ALLOWLIST = frozenset({
    "langchain",
    "langgraph",
    "mcp",
    "claude-code",
    "codex",
    "openai-codex",
    "openclaw",
    "hermes",
    "ironclaw",
    "moltis",
    "nanoclaw",
    "openfang",
    "picoclaw",
    "claude-agent-sdk",
    "claude-code-action",
    "cursor",
    "copilot",
    "devin",
    "antigravity",
    "kiro",
    "gemini",
    "factory",
    "windsurf",
    "grok-build",
    "opencode",
    "pi-coding",
    "oh-my-pi",
    "herdr",
    "openai-agents",
    "openhands",
    "aider",
    "warp",
    "replit",
    "augment-code",
    "cline",
    "kilo-code",
    "kilocode",
    "kimi-code",
    "open-interpreter",
    "deepagents",
    "deep-agents",
    "mistral-vibe",
    "codewhale",
    "mimo-code",
    "agno-agi",
    "tau-coding",
    "roo-code",
    "continue",
    "goose",
    "qwen-code",
    "smolagents",
    "junie",
    "e2b",
    "crush",
    "vercel-ai",
    "agent-framework",
})


def _github_headers():
    headers = {
        "User-Agent": "ClawBytes/1.0",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_advisories(timeout=30):
    req = Request(ADVISORY_URL, headers=_github_headers())
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return data if isinstance(data, list) else []


def allowlist_hit(package_name: str):
    """Return the allowlist key that matches this package name, or None."""
    raw = (package_name or "").strip().lower()
    if not raw:
        return None
    last = raw.split("/")[-1]
    if raw in PACKAGE_ALLOWLIST:
        return raw
    if last in PACKAGE_ALLOWLIST:
        return last
    return None


def matching_package(advisory: dict):
    for vuln in advisory.get("vulnerabilities") or []:
        name = ((vuln.get("package") or {}).get("name")) or ""
        hit = allowlist_hit(name)
        if hit:
            return hit
    return None


def select_advisories(advisories, state, now_iso):
    """Choose which advisories to emit. Mutates state. First call baselines.

    Unemitted matches (over the daily cap) stay out of seenGhsaIds so a later
    run can still post them. Non-matching ids are remembered so they are not
    reconsidered.
    """
    ids = [a.get("ghsa_id") for a in advisories if a.get("ghsa_id")]
    if not state.get("baselined"):
        state["baselined"] = True
        state["seenGhsaIds"] = sorted(set(ids))[-5000:]
        state["emittedDay"] = now_iso[:10]
        state["emittedToday"] = 0
        return []

    seen = set(state.get("seenGhsaIds") or [])
    day = now_iso[:10]
    if state.get("emittedDay") != day:
        state["emittedDay"] = day
        state["emittedToday"] = 0
    remaining = max(0, DAILY_CAP - int(state.get("emittedToday") or 0))
    items = []
    for advisory in advisories:
        gid = advisory.get("ghsa_id") or ""
        if not gid or gid in seen:
            continue
        package = matching_package(advisory)
        if not package:
            seen.add(gid)
            continue
        if remaining <= 0:
            continue
        url = advisory.get("html_url") or f"https://github.com/advisories/{gid}"
        summary = (advisory.get("summary") or "").strip()
        severity = (advisory.get("severity") or "unknown").strip()
        items.append({
            "id": gid,
            "package": package,
            "title": summary or f"{gid} ({package})",
            "url": url,
            "summary": f"{severity} advisory affecting {package}",
            "severity": severity,
            "published": advisory.get("published_at") or "",
            "found_at": now_iso,
        })
        seen.add(gid)
        remaining -= 1
    state["emittedToday"] = int(state.get("emittedToday") or 0) + len(items)
    state["seenGhsaIds"] = sorted(seen)[-5000:]
    return items


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"baselined": False, "seenGhsaIds": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_advisories(verbose=True, fetch=None):
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    fetch = fetch or fetch_advisories
    try:
        advisories = fetch()
    except Exception as exc:
        if verbose:
            print(f"  ! advisories fetch failed: {exc}")
        return []
    first = not state.get("baselined")
    items = select_advisories(advisories, state, now_iso)
    if items:
        state["foundItems"] = (state.get("foundItems", []) + items)[-200:]
    save_state(state)
    if verbose:
        if first:
            print(f"  = advisories: baseline recorded ({len(state.get('seenGhsaIds') or [])} ghsa ids), 0 items")
        else:
            print(f"  = advisories: {len(items)} new item(s)")
    return items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor GitHub security advisories")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    items = check_advisories(verbose=not args.quiet)
    print(f"Advisories: {len(items)} new item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
