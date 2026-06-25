#!/usr/bin/env python3
"""ClawBytes Source Discovery — Automatically find and validate new content sources.

Searches for new RSS feeds, subreddits, and HN topics related to AI/agents.
Validates working sources and saves them to a dynamic config that the monitors
automatically load alongside their hardcoded sources.

State file: memory/clawbytes-discovered-sources.json
Dynamic config: memory/clawbytes-dynamic-feeds.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "clawbytes-discovered-sources.json"
DYNAMIC_FEEDS_FILE = MEMORY_DIR / "clawbytes-dynamic-feeds.json"

# Subreddit discovery queries
SUBREDDIT_QUERIES = [
    "ai agent",
    "llm framework",
    "coding assistant",
    "local ai",
    "machine learning news",
]

# Subreddits that look AI-ish but aren't relevant enough
SUBREDDIT_EXCLUSIONS = {
    "framework",  # Generic programming, not AI
    "homeassistant",  # Smart home, not AI agents
    "machinelearningjobs",  # Job board, not content
    "machinelearningcollab",  # Collaboration, not news
    "mlquestions",  # Q&A, not news
    "learnmachinelearning",  # Educational, not news
}

def load_state():
    """Load discovery state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "lastDiscovery": None,
        "discoveredFeeds": [],
        "discoveredSubreddits": [],
        "failedFeeds": [],
        "discoveryLog": [],
    }


def save_state(state):
    """Save discovery state."""
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastDiscovery"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_dynamic_feeds():
    """Load dynamic feeds config that monitors will read."""
    if DYNAMIC_FEEDS_FILE.exists():
        with open(DYNAMIC_FEEDS_FILE) as f:
            return json.load(f)
    return {
        "rss_feeds": [],
        "subreddits": [],
        "hn_queries": [],
        "lastUpdated": None,
    }


def save_dynamic_feeds(feeds):
    """Save dynamic feeds config."""
    MEMORY_DIR.mkdir(exist_ok=True)
    feeds["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    tmp = DYNAMIC_FEEDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feeds, indent=2))
    tmp.replace(DYNAMIC_FEEDS_FILE)


# Brave-based RSS feed discovery removed 2026-06-25 (Brave deprecated).


def discover_subreddits(state):
    """Discover new relevant subreddits."""
    print("\n🔴 Discovering new subreddits...")
    known_subs = {
        "openclaw", "selfhosted", "localllama", "homelab", "singularity",
        "machinelearning", "artificial"
    }
    dynamic = load_dynamic_feeds()
    known_subs.update(f.get("name", "").lower() for f in dynamic.get("subreddits", []))
    
    new_subs = []
    headers = {"User-Agent": "ClawBytes/1.0 (Reddit Discovery)"}
    
    for query in SUBREDDIT_QUERIES:
        url = f"https://www.reddit.com/subreddits/search.json?q={quote(query)}&sort=subscribers&limit=10"
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for child in data.get("data", {}).get("children", []):
                    sub = child.get("data", {})
                    name = sub.get("display_name", "").lower()
                    subscribers = sub.get("subscribers", 0)
                    
                    if name in known_subs or name in SUBREDDIT_EXCLUSIONS or (subscribers or 0) < 1000:
                        continue
                    
                    # Check if description mentions AI/agent topics
                    desc = (sub.get("public_description", "") or "").lower()
                    title = (sub.get("title", "") or "").lower()
                    ai_terms = ["ai", "agent", "llm", "language model", "gpt", "claude",
                                "machine learning", "artificial intelligence", "neural", "deep learning",
                                "open source", "self-hosted"]
                    
                    if any(term in desc or term in title for term in ai_terms):
                        print(f"    ✅ Found: r/{name} ({subscribers:,} subscribers)")
                        new_subs.append({
                            "name": name,
                            "url": f"https://www.reddit.com/r/{name}/hot.json?limit=10",
                            "type": "hot",
                            "subscribers": subscribers,
                            "discovered_via": query,
                        })
                        known_subs.add(name)
                        state["discoveredSubreddits"].append({
                            "name": name,
                            "subscribers": subscribers,
                            "found_at": datetime.now(timezone.utc).isoformat(),
                        })
        except Exception as e:
            print(f"    ⚠️ Error searching for '{query}': {e}")
        
        time.sleep(2)  # Reddit rate limit
    
    return new_subs


def discover_hn_topics(state):
    """Generate new HN search queries based on trending topics."""
    print("\n🔶 Generating new HN search queries from trending topics...")
    
    # Try to find trending topics from recent HN
    new_queries = []
    headers = {"User-Agent": "ClawBytes/1.0"}
    
    # Check what's on HN front page
    url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    try:
        with urlopen(Request(url, headers=headers), timeout=10) as resp:
            top_ids = json.loads(resp.read().decode("utf-8"))[:30]
            
            for story_id in top_ids[:15]:
                story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
                try:
                    with urlopen(Request(story_url, headers=headers), timeout=5) as sresp:
                        story = json.loads(sresp.read().decode("utf-8"))
                        title = story.get("title", "").lower()
                        
                        # Extract notable AI/tech topics
                        ai_keywords = {
                            "gpt": "GPT",
                            "claude": "Claude",
                            "gemini": "Gemini",
                            "llama": "Llama",
                            "deepseek": "DeepSeek",
                            "mistral": "Mistral",
                            "diffusion": "diffusion model",
                            "transformer": "transformer architecture",
                            "reasoning": "reasoning model",
                            "agentic": "agentic AI",
                            "coding agent": "coding agent",
                            "safety": "AI safety",
                            "alignment": "AI alignment",
                            "open source ai": "open source AI",
                        }
                        
                        for keyword, label in ai_keywords.items():
                            if keyword in title:
                                new_queries.append(label)
                except Exception:
                    continue
    except Exception as e:
        print(f"    ⚠️ HN top stories error: {e}")
    
    # Also check what topic keywords appeared in recent backlog
    backlog_file = MEMORY_DIR / "clawbytes-backlog.json"
    if backlog_file.exists():
        try:
            backlog = json.loads(backlog_file.read_text())
            items = backlog.get("items", [])
            recent = [i for i in items 
                      if i.get("discoveredAt", "") > (datetime.now(timezone.utc).isoformat()[:10] - "T00:00:00")]
            for item in recent[-50:]:
                title = item.get("title", "").lower()
                for keyword, label in ai_keywords.items():
                    if keyword in title:
                        new_queries.append(label)
        except Exception:
            pass
    
    # Deduplicate and create queries
    unique_topics = list(set(new_queries))[:10]
    new_hn_queries = []
    existing_queries = {
        "openclaw OR claw agent", "AI agent framework", "coding agent autonomous",
        "AI agent", "LLM agent tool use", "MCP model context protocol",
        "AI assistant local self-hosted", "claude code OR cursor OR windsurf OR copilot",
        "AI agent security vulnerability", "LLM prompt injection exploit",
        "AI agent safety risk", "LLM agent architecture",
        "new LLM model release", "open source LLM weights",
        "GPT OR Claude OR Gemini OR Llama OR Mistral", "reasoning model AI",
    }
    
    for topic in unique_topics:
        query = f"{topic} AI"
        if query.lower() not in {q.lower() for q in existing_queries}:
            new_hn_queries.append({
                "query": query,
                "tags": "story",
                "min_points": 15,
                "discovered_via": "auto",
            })
    
    if new_hn_queries:
        print(f"    ✅ Generated {len(new_hn_queries)} new HN queries: {[q['query'] for q in new_hn_queries]}")
    else:
        print("    ℹ️ No new HN topics discovered (current coverage is good)")
    
    return new_hn_queries


def prune_dead_feeds(dynamic):
    """Remove feeds that have been consistently failing."""
    to_remove = []
    for feed in dynamic.get("rss_feeds", []):
        fail_count = feed.get("consecutive_fails", 0)
        if fail_count >= 5:
            print(f"  🗑️ Pruning dead feed: {feed['name']} ({feed['url']})")
            to_remove.append(feed)
    
    for feed in to_remove:
        dynamic["rss_feeds"].remove(feed)
    
    return len(to_remove)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Discover new ClawBytes content sources")
    parser.add_argument("--dry-run", action="store_true", help="Don't save changes")
    parser.add_argument("--feeds-only", action="store_true",
                        help="(Deprecated no-op: RSS feed discovery was Brave-based and removed)")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    print("🔍 ClawBytes Source Discovery")
    print("=" * 50)

    state = load_state()
    dynamic = load_dynamic_feeds()

    # Brave-based RSS feed discovery removed 2026-06-25 (Brave deprecated).
    new_subs = []
    new_hn = []
    if not args.feeds_only:
        # Discover subreddits
        new_subs = discover_subreddits(state)

        # Discover HN topics
        new_hn = discover_hn_topics(state)

        # Add to dynamic config
        dynamic["subreddits"].extend(new_subs)
        dynamic["hn_queries"].extend(new_hn)

    # Prune dead feeds
    pruned = prune_dead_feeds(dynamic)

    # Save
    if not args.dry_run:
        save_state(state)
        save_dynamic_feeds(dynamic)
        print(f"\n✅ Saved {len(new_subs)} new subreddits, {len(new_hn)} new HN queries")
        print(f"   Pruned {pruned} dead feeds")
    else:
        print("\n🏃 Dry run — no changes saved")
        print(f"   Would add: {len(new_subs)} subreddits, {len(new_hn)} HN queries")
    
    # Summary
    print(f"\n📊 Dynamic Sources Summary:")
    print(f"   RSS Feeds: {len(dynamic.get('rss_feeds', []))}")
    print(f"   Subreddits: {len(dynamic.get('subreddits', []))}")
    print(f"   HN Queries: {len(dynamic.get('hn_queries', []))}")


if __name__ == "__main__":
    main()