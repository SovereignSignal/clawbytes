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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "clawbytes-discovered-sources.json"
DYNAMIC_FEEDS_FILE = MEMORY_DIR / "clawbytes-dynamic-feeds.json"
CREDS_FILE = WORKSPACE / "CREDS.md"

# Search queries for discovering new sources (Brave Search API)
DISCOVERY_QUERIES = [
    # AI/agent blogs and publications
    "best AI agent blogs 2026 rss feed",
    "LLM research blog rss feed",
    "AI engineering newsletter rss",
    "artificial intelligence news rss feed",
    "machine learning engineering blog rss",
    "coding agent news rss",
    "AI safety alignment blog rss",
    "open source AI projects rss feed",
    # Specific orgs that might have feeds
    "site:* blog rss artificial intelligence",
    "AI startup blog rss 2026",
    "agent framework newsletter rss",
]

# Subreddit discovery queries
SUBREDDIT_QUERIES = [
    "ai agent",
    "llm framework",
    "coding assistant",
    "local ai",
    "machine learning news",
]

# Known RSS feed domain patterns to look for in search results
RSS_PATTERNS = [
    r'href="([^"]*?/feed/?)"',
    r'href="([^"]*?/rss[^"]*?)"',
    r'href="([^"]*?/atom\.xml)"',
    r'href="([^"]*?/index\.xml)"',
    r'<link[^>]*type="application/rss\+xml"[^>]*href="([^"]*)"',
    r'<link[^>]*type="application/atom\+xml"[^>]*href="([^"]*)"',
]

# Domains we already track (hardcoded in monitors)
KNOWN_DOMAINS = {
    "simonwillison.net", "aimaker.substack.com", "ai-supremacy.com",
    "openai.com", "huggingface.co", "blog.langchain.dev",
    "lilianweng.github.io", "interconnects.ai", "aisnakeoil.substack.com",
    "latent.space", "theaiedge.substack.com",
    "github.com",  # releases
    "techcrunch.com", "theverge.com", "arstechnica.com",
    "zdnet.com", "technologyreview.com", "venturebeat.com",
    "the-decoder.com", "arxiv.org",
    "reddit.com",  # handled separately
}

# Domains that are irrelevant noise
NOISE_DOMAINS = {
    "amazon.com", "ebay.com", "walmart.com", "facebook.com", "instagram.com",
    "tiktok.com", "pinterest.com", "yelp.com", "tripadvisor.com",
}

# Subreddits that look AI-ish but aren't relevant enough
SUBREDDIT_EXCLUSIONS = {
    "framework",  # Generic programming, not AI
    "homeassistant",  # Smart home, not AI agents
    "machinelearningjobs",  # Job board, not content
    "machinelearningcollab",  # Collaboration, not news
    "mlquestions",  # Q&A, not news
    "learnmachinelearning",  # Educational, not news
}

BRAVE_API_KEY = None


def load_creds():
    """Load Brave API key from CREDS.md."""
    global BRAVE_API_KEY
    try:
        text = CREDS_FILE.read_text()
        match = re.search(r'BRAVE[_-]?API[_-]?KEY["\s:]+([^\s"\n]+)', text, re.IGNORECASE)
        if match:
            BRAVE_API_KEY = match.group(1)
            return True
    except Exception:
        pass
    
    # Try env
    import os
    BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")
    return bool(BRAVE_API_KEY)


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


def validate_rss_feed(url, timeout=10):
    """Check if a URL is a valid RSS/Atom feed."""
    headers = {
        "User-Agent": "ClawBytes/1.0 (Source Discovery; +https://github.com/ClawBack1)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            content = response.read(5000).decode("utf-8", errors="ignore")
            # Check for RSS/Atom markers
            if any(marker in content[:2000].lower() for marker in 
                   ["<rss", "<feed", "<channel>", "xmlns:atom", "atom:link"]):
                return True, "valid feed"
            return False, "no feed markers found"
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)


def search_brave(query, count=10):
    """Search Brave API for results."""
    if not BRAVE_API_KEY:
        print(f"  ⚠️ No Brave API key, skipping search: {query[:50]}")
        return []
    
    url = f"https://api.search.brave.com/res/v1/web/search?q={quote(query)}&count={count}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            results = data.get("web", {}).get("results", [])
            return results
    except Exception as e:
        print(f"  ⚠️ Brave search error: {e}")
        return []


def discover_rss_feeds(state):
    """Discover new RSS feeds via search."""
    print("\n📡 Discovering new RSS feeds...")
    new_feeds = []
    seen_urls = set()
    
    # Collect existing feed URLs
    dynamic = load_dynamic_feeds()
    existing_urls = {f.get("url", "") for f in dynamic.get("rss_feeds", [])}
    # Also include hardcoded feeds
    sys.path.insert(0, str(WORKSPACE / "scripts"))
    try:
        from claw_rss_monitor import RSS_FEEDS
        existing_urls.update(f.get("url", "") for f in RSS_FEEDS)
    except ImportError:
        pass
    
    for query in DISCOVERY_QUERIES:
        print(f"  Searching: {query[:60]}...")
        results = search_brave(query, count=10)
        
        for result in results:
            page_url = result.get("url", "")
            if not page_url:
                continue
            
            # Try common RSS paths for the domain
            parsed = urlparse(page_url)
            domain = parsed.hostname or ""
            
            if domain in NOISE_DOMAINS or domain in KNOWN_DOMAINS:
                continue
            if domain in existing_urls or page_url in existing_urls:
                continue
            
            # Try to find RSS links on the page
            potential_feeds = [
                f"https://{domain}/feed/",
                f"https://{domain}/rss/",
                f"https://{domain}/feed.xml",
                f"https://{domain}/rss.xml",
                f"https://{domain}/atom.xml",
                f"https://{domain}/index.xml",
                f"https://{domain}/blog/feed/",
                f"https://{domain}/blog/rss/",
            ]
            
            for feed_url in potential_feeds:
                if feed_url in seen_urls or feed_url in existing_urls:
                    continue
                seen_urls.add(feed_url)
                
                valid, reason = validate_rss_feed(feed_url)
                if valid:
                    name = domain.replace(".", " ").title()
                    # Check if the feed has AI-relevant content
                    print(f"    ✅ Found: {feed_url} ({reason})")
                    new_feed = {
                        "name": name,
                        "url": feed_url,
                        "tags": ["discovered", "auto"],
                        "discovered_at": datetime.now(timezone.utc).isoformat(),
                        "discovered_via": query,
                    }
                    new_feeds.append(new_feed)
                    existing_urls.add(feed_url)
                    state["discoveredFeeds"].append({
                        "url": feed_url,
                        "name": name,
                        "found_at": datetime.now(timezone.utc).isoformat(),
                        "via_query": query,
                    })
                    break  # One feed per domain is enough
            
            time.sleep(0.3)  # Rate limit
        
        time.sleep(1)  # Rate limit between queries
    
    return new_feeds


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
    parser.add_argument("--feeds-only", action="store_true", help="Only discover RSS feeds")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()
    
    print("🔍 ClawBytes Source Discovery")
    print("=" * 50)
    
    load_creds()
    state = load_state()
    dynamic = load_dynamic_feeds()
    
    # Discover RSS feeds
    new_feeds = discover_rss_feeds(state)
    
    if not args.feeds_only:
        # Discover subreddits
        new_subs = discover_subreddits(state)
        
        # Discover HN topics
        new_hn = discover_hn_topics(state)
        
        # Add to dynamic config
        dynamic["rss_feeds"].extend(new_feeds)
        dynamic["subreddits"].extend(new_subs)
        dynamic["hn_queries"].extend(new_hn)
    
    # Prune dead feeds
    pruned = prune_dead_feeds(dynamic)
    
    # Save
    if not args.dry_run:
        save_state(state)
        save_dynamic_feeds(dynamic)
        print(f"\n✅ Saved {len(new_feeds)} new feeds, {len(dynamic.get('subreddits', [])) - len(new_subs) + len(new_subs)} total dynamic subreddits")
        print(f"   Pruned {pruned} dead feeds")
    else:
        print("\n🏃 Dry run — no changes saved")
        print(f"   Would add: {len(new_feeds)} feeds")
    
    # Summary
    print(f"\n📊 Dynamic Sources Summary:")
    print(f"   RSS Feeds: {len(dynamic.get('rss_feeds', []))}")
    print(f"   Subreddits: {len(dynamic.get('subreddits', []))}")
    print(f"   HN Queries: {len(dynamic.get('hn_queries', []))}")


if __name__ == "__main__":
    main()