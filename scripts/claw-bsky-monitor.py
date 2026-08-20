#!/usr/bin/env python3
"""
ClawBytes Bluesky Monitor
Polls the unauthenticated Bluesky search API for harness-ecosystem phrases
and keeps only posts with real engagement. Verified 2026-06-12: no auth
needed, results minutes-fresh; api.bsky.app worked where public.api.bsky.app
403'd, so both hosts are tried in order.

State file: memory/claw-bsky-state.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-bsky-state.json"

HOSTS = ["https://api.bsky.app", "https://public.api.bsky.app"]
QUERIES = [
    '"claude code"', '"codex cli"', '"openclaw"', '"mcp server"', '"agent harness"',
    '"cursor"', '"devin desktop"', '"antigravity"', '"agent client protocol"',
]

MIN_LIKES = 20
MIN_REPOSTS = 10
MAX_ITEMS_PER_RUN = 5


def search_posts(query):
    for host in HOSTS:
        url = f"{host}/xrpc/app.bsky.feed.searchPosts?q={quote(query)}&sort=latest&limit=25&lang=en"
        try:
            req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode()).get("posts", [])
        except Exception:
            continue
    return []


def passes_engagement(post):
    return (post.get("likeCount", 0) or 0) >= MIN_LIKES or (post.get("repostCount", 0) or 0) >= MIN_REPOSTS


def post_web_url(post):
    handle = (post.get("author") or {}).get("handle", "")
    rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seenUris": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_bsky(verbose=True):
    state = load_state()
    seen = set(state.get("seenUris", []))
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []

    for query in QUERIES:
        posts = search_posts(query)
        kept = 0
        for post in posts:
            uri = post.get("uri", "")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            if not passes_engagement(post):
                continue
            url = post_web_url(post)
            if not url:
                continue
            text = " ".join(((post.get("record") or {}).get("text") or "").split())
            handle = (post.get("author") or {}).get("handle", "bsky")
            new_items.append({
                "id": uri,
                "query": query,
                "handle": handle,
                "title": f"@{handle}: {text[:110]}",
                "url": url,
                "likes": post.get("likeCount", 0) or 0,
                "reposts": post.get("repostCount", 0) or 0,
                "found_at": now_iso,
            })
            kept += 1
        if verbose:
            print(f"  = bsky {query}: {len(posts)} fetched, {kept} kept")

    new_items = new_items[:MAX_ITEMS_PER_RUN]
    state["seenUris"] = sorted(seen)[-5000:]
    if new_items:
        state["foundItems"] = (state.get("foundItems", []) + new_items)[-200:]
    save_state(state)
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor Bluesky for harness signal")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    items = check_bsky(verbose=not args.quiet)
    print(f"Bluesky: {len(items)} new item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
