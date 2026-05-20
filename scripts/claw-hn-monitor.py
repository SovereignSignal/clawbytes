#!/usr/bin/env python3
"""
ClawBytes Hacker News Monitor
Monitors HN for AI agent ecosystem discussions and security posts.

State file: memory/claw-hn-state.json
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

WORKSPACE = Path(__file__).parent.parent
MEMORY_DIR = WORKSPACE / "memory"
STATE_FILE = MEMORY_DIR / "claw-hn-state.json"

# HN search queries via Algolia
HN_QUERIES = [
    # Agent ecosystem
    {"query": "openclaw OR claw agent", "tags": "story", "min_points": 3},
    {"query": "AI agent framework", "tags": "story", "min_points": 15},
    {"query": "coding agent autonomous", "tags": "story", "min_points": 10},
    {"query": "AI agent", "tags": "story", "min_points": 50},
    # New: broader agent coverage
    {"query": "LLM agent tool use", "tags": "story", "min_points": 10},
    {"query": "MCP model context protocol", "tags": "story", "min_points": 5},
    {"query": "AI assistant local self-hosted", "tags": "story", "min_points": 10},
    {"query": "claude code OR cursor OR windsurf OR copilot", "tags": "story", "min_points": 15},
    # Security/watch
    {"query": "AI agent security vulnerability", "tags": "story", "min_points": 5},
    {"query": "LLM prompt injection exploit", "tags": "story", "min_points": 5},
    {"query": "AI agent safety risk", "tags": "story", "min_points": 5},
    # Read lane - deep content
    {"query": "LLM agent architecture", "tags": "story", "min_points": 10},
    # New: model releases and breakthroughs
    {"query": "new LLM model release", "tags": "story", "min_points": 10},
    {"query": "open source LLM weights", "tags": "story", "min_points": 15},
    {"query": "GPT OR Claude OR Gemini OR Llama OR Mistral", "tags": "story", "min_points": 100},
    {"query": "reasoning model AI", "tags": "story", "min_points": 30},
]

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenIds": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_hn(query, tags="story", page=0, timeout=15):
    """Fetch HN stories via Algolia API."""
    params = f"?query={quote(query)}&tags={tags}&page={page}&hitsPerPage=15"
    url = f"{ALGOLIA_URL}{params}"
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  HN fetch error: {e}")
        return None


def check_hn(verbose=True):
    """Check HN for new relevant stories."""
    state = load_state()
    seen_ids = set(state.get("seenIds", []))
    new_items = []

    # Merge dynamic HN queries with hardcoded ones
    dynamic_path = MEMORY_DIR / "clawbytes-dynamic-feeds.json"
    all_queries = list(HN_QUERIES)
    if dynamic_path.exists():
        try:
            dynamic = json.loads(dynamic_path.read_text())
            for q in dynamic.get("hn_queries", []):
                if q.get("query", "").lower() not in {hq["query"].lower() for hq in all_queries}:
                    all_queries.append(q)
        except Exception:
            pass

    for q in all_queries:
        query = q["query"]
        min_points = q.get("min_points", 10)
        
        if verbose:
            print(f"  Searching: {query}")
        
        result = fetch_hn(query, q.get("tags", "story"))
        if not result or "hits" not in result:
            continue

        for hit in result["hits"]:
            object_id = hit.get("objectID", "")
            if not object_id or object_id in seen_ids:
                continue

            title = hit.get("title", "").strip()
            # Remove "HN: " prefix that the monitor adds
            if title.startswith("HN: "):
                title = title[4:]
            points = int(hit.get("points") or 0)
            num_comments = int(hit.get("num_comments") or 0)
            
            # Filter old posts — only items from last 60 days
            created_at = hit.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - created_dt).days > 60:
                        continue
                except (ValueError, TypeError):
                    pass
            
            if points < min_points:
                continue

            # Build HN URL
            story_id = hit.get("story_id") or object_id
            url = f"https://news.ycombinator.com/item?id={story_id}"

            # Determine category based on query
            query_lower = query.lower()
            if "security" in query_lower or "vulnerability" in query_lower or "injection" in query_lower or "safety" in query_lower or "risk" in query_lower:
                category = "watch"
            elif "architecture" in query_lower or "mcp" in query_lower:
                category = "read"
            else:
                # Default: check title for signals
                title_low = title.lower()
                if any(t in title_low for t in ["security", "vulnerability", "exploit", "injection", "unsafe"]):
                    category = "watch"
                elif any(t in title_low for t in ["architecture", "framework", "protocol", "how ", "why "]):
                    category = "read"
                else:
                    category = "community"

            item = {
                "id": object_id,
                "title": title,
                "url": url,
                "url": url,
                "score": points,
                "comments": num_comments,
                "found_at": datetime.now(timezone.utc).isoformat(),
                "created_at": created_at,
                "category_hint": category,
                "sourceType": "hackernews",
            }
            new_items.append(item)
            seen_ids.add(object_id)

        time.sleep(0.5)  # Rate limit

    # Update state
    state["seenIds"] = list(seen_ids)[-2000:]
    state["foundItems"] = (state.get("foundItems", []) + new_items)[-200:]
    save_state(state)

    if verbose:
        print(f"  HN: {len(new_items)} new items found")

    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor Hacker News for AI agent content")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        state = load_state()
        print(f"Last check: {state.get('lastCheck')}")
        print(f"Seen IDs: {len(state.get('seenIds', []))}")
        print(f"Found items: {len(state.get('foundItems', []))}")
        return

    new_items = check_hn(verbose=not args.quiet)
    print(f"Found {len(new_items)} new HN items")


if __name__ == "__main__":
    main()