#!/usr/bin/env python3
"""
ClawBytes Hacker News Monitor
Monitors HN for AI agent ecosystem discussions and security posts.

State file: memory/claw-hn-state.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-hn-state.json"

# HN search queries via Algolia. Algolia has no OR operator: a query that
# contains the token OR returns no hits (or a handful, not the union).
# Each term is its own query. check_hn dedupes stories across queries.
HN_QUERIES = [
    # Agent ecosystem
    {"query": "openclaw", "tags": "story", "min_points": 3},
    {"query": "claw agent", "tags": "story", "min_points": 3},
    {"query": "AI agent framework", "tags": "story", "min_points": 15},
    {"query": "coding agent autonomous", "tags": "story", "min_points": 10},
    {"query": "AI agent", "tags": "story", "min_points": 50},
    # Broader agent coverage
    {"query": "LLM agent tool use", "tags": "story", "min_points": 10},
    {"query": "MCP model context protocol", "tags": "story", "min_points": 5},
    {"query": "AI assistant local self-hosted", "tags": "story", "min_points": 10},
    {"query": "claude code", "tags": "story", "min_points": 15},
    {"query": "cursor", "tags": "story", "min_points": 15},
    {"query": "windsurf", "tags": "story", "min_points": 15},
    {"query": "copilot", "tags": "story", "min_points": 15},
    {"query": "antigravity", "tags": "story", "min_points": 10},
    {"query": "\"devin desktop\"", "tags": "story", "min_points": 10},
    {"query": "\"agent client protocol\"", "tags": "story", "min_points": 10},
    {"query": "kiro", "tags": "story", "min_points": 10},
    {"query": "\"kilo code\"", "tags": "story", "min_points": 10},
    {"query": "\"kimi code\"", "tags": "story", "min_points": 10},
    {"query": "\"mistral vibe\"", "tags": "story", "min_points": 10},
    {"query": "\"grok build\"", "tags": "story", "min_points": 10},
    {"query": "\"open interpreter\"", "tags": "story", "min_points": 10},
    {"query": "\"pi coding agent\"", "tags": "story", "min_points": 10},
    {"query": "\"oh-my-pi\"", "tags": "story", "min_points": 10},
    {"query": "\"oh my pi\"", "tags": "story", "min_points": 10},
    {"query": "omp.sh", "tags": "story", "min_points": 10},
    {"query": "herdr", "tags": "story", "min_points": 10},
    # Security/watch
    {"query": "AI agent security vulnerability", "tags": "story", "min_points": 5},
    {"query": "LLM prompt injection exploit", "tags": "story", "min_points": 5},
    {"query": "AI agent safety risk", "tags": "story", "min_points": 5},
    # Read lane - deep content
    {"query": "LLM agent architecture", "tags": "story", "min_points": 10},
    # Model releases and breakthroughs
    {"query": "new LLM model release", "tags": "story", "min_points": 10},
    {"query": "open source LLM weights", "tags": "story", "min_points": 15},
    {"query": "GPT", "tags": "story", "min_points": 100},
    {"query": "Claude", "tags": "story", "min_points": 100},
    {"query": "Gemini", "tags": "story", "min_points": 100},
    {"query": "Llama", "tags": "story", "min_points": 100},
    {"query": "Mistral", "tags": "story", "min_points": 100},
    {"query": "reasoning model AI", "tags": "story", "min_points": 30},
]

# One extra pass over the current front page, not another keyword search.
# Stories under this point floor are still climbing; the next collect can
# pick them up. The audit's missed front-page threads were all above it.
FRONT_PAGE_MIN_POINTS = 150
FRONT_PAGE_TERMS = (
    "agent",
    "coding",
    "claude",
    "cursor",
    "copilot",
    "windsurf",
    "mcp",
    "llm",
    "gpt",
    "chatgpt",
    "gemini",
    "llama",
    "mistral",
    "deepseek",
    "codex",
    "openai",
    "anthropic",
    "harness",
    "opus 5",
    "opencode",
    "aider",
    "cline",
    "openclaw",
    "kiro",
    "devin",
    "antigravity",
)
# Short tokens that are substrings of unrelated words.
_BOUNDARY_FRONT_PAGE = {"coding", "mcp", "llm", "gpt"}
_OR_SPLIT = re.compile(r"\s+OR\s+")

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


def _encode_query(params):
    """URL-encode a dict or a list of ``(key, value)`` pairs.

    Pre-#37 passed a dict straight to ``urlencode``. The query string has to
    stay that encoding: interpolating the params object puts spaces in the
    path, and urllib rejects it.
    """
    if isinstance(params, dict):
        pairs = params
    elif isinstance(params, (list, tuple)):
        pairs = params
    else:
        raise TypeError("Algolia params must be a dict or a list of pairs")
    return urlencode(pairs)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenIds": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    """Save state atomically (temp file + rename). A torn write aborts collect."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def split_hn_query(query):
    """Split on the token OR. Algolia does not treat it as a disjunction."""
    text = (query or "").strip()
    if not text or not _OR_SPLIT.search(text):
        return [text] if text else []
    return [part.strip() for part in _OR_SPLIT.split(text) if part.strip()]


def expand_hn_queries(queries):
    """One Algolia call per term. Later duplicates keep the first min_points."""
    seen = set()
    expanded = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        for part in split_hn_query(query.get("query", "")):
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            item = dict(query)
            item["query"] = part
            expanded.append(item)
    return expanded


def front_page_relevant(title, url=""):
    """Agent, coding, and AI-tool stories. Bare 'pi' is not a term.

    The Pi 1.0 thread is titled "Pi 1.0" and links to earendil.com, so the
    usual compounds never match it. Raspberry Pi and Sonic Pi stay out.
    """
    text = f"{title or ''} {url or ''}".lower()
    if "raspberry pi" not in text and "sonic pi" not in text and "math.pi" not in text:
        if (
            "earendil.com" in text
            or "pi coding" in text
            or "oh-my-pi" in text
            or "oh my pi" in text
            or re.search(r"\bpi\s+\d+\.\d", text)
            or re.search(r"\bpi durable\b", text)
        ):
            return True
    for term in FRONT_PAGE_TERMS:
        if term in _BOUNDARY_FRONT_PAGE:
            if re.search(rf"\b{re.escape(term)}\b", text):
                return True
        elif term in text:
            return True
    return False


def fetch_hn(query, tags="story", page=0, timeout=15, days=14, hits_per_page=20):
    """Fetch recent HN stories via Algolia API.

    ``days`` None omits the created-at filter (the front-page tag is already
    the current page). ``hits_per_page`` is 20 for search and larger for the
    front page, which is one screen of stories.
    """
    params = [
        ("query", query),
        ("tags", tags),
        ("page", page),
        ("hitsPerPage", hits_per_page),
    ]
    if days:
        min_created = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        params.append(("numericFilters", f"created_at_i>{min_created}"))
    url = f"{ALGOLIA_URL}?{_encode_query(params)}"
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  HN fetch error: {e}")
        return None


def _category_for(query, title):
    query_lower = (query or "").lower()
    if any(token in query_lower for token in ("security", "vulnerability", "injection", "safety", "risk")):
        return "watch"
    if "architecture" in query_lower or "mcp" in query_lower:
        return "read"
    title_low = (title or "").lower()
    if any(token in title_low for token in ("security", "vulnerability", "exploit", "injection", "unsafe")):
        return "watch"
    if any(token in title_low for token in ("architecture", "framework", "protocol", "how ", "why ")):
        return "read"
    return "community"


def story_from_hit(hit, query, min_points):
    """One Algolia hit as a monitor item, or None when it fails the gates."""
    if not isinstance(hit, dict):
        return None
    object_id = hit.get("objectID", "")
    if not object_id:
        return None
    title = (hit.get("title") or "").strip()
    if title.startswith("HN: "):
        title = title[4:]
    points = int(hit.get("points") or 0)
    if points < min_points:
        return None
    created_at = hit.get("created_at", "")
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - created_dt).days > 60:
                return None
        except (ValueError, TypeError):
            pass
    story_id = hit.get("story_id") or object_id
    url = f"https://news.ycombinator.com/item?id={story_id}"
    return {
        "id": object_id,
        "title": title,
        "url": url,
        "articleUrl": (hit.get("url") or "").strip(),
        "score": points,
        "comments": int(hit.get("num_comments") or 0),
        "found_at": datetime.now(timezone.utc).isoformat(),
        "created_at": created_at,
        "category_hint": _category_for(query, title),
        "sourceType": "hackernews",
    }


def _take_hit(hit, query, min_points, seen_ids, new_items):
    item = story_from_hit(hit, query, min_points)
    if not item or item["id"] in seen_ids:
        return
    new_items.append(item)
    seen_ids.add(item["id"])


def _collect_front_page(state, seen_ids, new_items, verbose):
    """Record relevant front-page article URLs and emit unseen stories.

    A failed fetch leaves the previous ``frontPage`` list in place so one
    Algolia blip does not drop Ship items that were matching it.
    """
    if verbose:
        print("  Searching: front page")
    result = fetch_hn("", tags="front_page", hits_per_page=50, days=None)
    if not result or "hits" not in result:
        return
    hits = sorted(result["hits"], key=lambda hit: int((hit or {}).get("points") or 0), reverse=True)
    recorded = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = (hit.get("title") or "").strip()
        article = (hit.get("url") or "").strip()
        points = int(hit.get("points") or 0)
        if points < FRONT_PAGE_MIN_POINTS or not front_page_relevant(title, article):
            continue
        recorded.append({
            "objectID": hit.get("objectID") or "",
            "title": title,
            "url": article,
            "points": points,
        })
        _take_hit(hit, "", FRONT_PAGE_MIN_POINTS, seen_ids, new_items)
    state["frontPage"] = recorded
    time.sleep(0.5)


def check_hn(verbose=True):
    """Check HN for new relevant stories."""
    state = load_state()
    seen_ids = set(state.get("seenIds", []))
    new_items = []

    # Merge dynamic HN queries with hardcoded ones, then split any OR
    # a discovered query still carries.
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
    all_queries = expand_hn_queries(all_queries)

    for q in all_queries:
        query = q["query"]
        min_points = q.get("min_points", 10)

        if verbose:
            print(f"  Searching: {query}")

        result = fetch_hn(query, q.get("tags", "story"))
        if not result or "hits" not in result:
            continue

        for hit in result["hits"]:
            _take_hit(hit, query, min_points, seen_ids, new_items)

        time.sleep(0.5)  # Rate limit

    _collect_front_page(state, seen_ids, new_items, verbose)

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