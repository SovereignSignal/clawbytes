#!/usr/bin/env python3
"""
ClawBytes Moltbook Monitor
Monitors Moltbook.com for AI agent posts.

Note: Moltbook has no public JSON API as of 2026-03-30.
This script scrapes the HTML frontpage for agent posts.

State file: memory/claw-moltbook-state.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Workspace path - use env var or default
WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = WORKSPACE / "memory"
STATE_FILE = MEMORY_DIR / "claw-moltbook-state.json"

# Moltbook URLs to try
MOLTBOOK_URLS = [
    "https://moltbook.com",
    "https://www.moltbook.com",
]

# Keywords to filter for
RELEVANT_KEYWORDS = [
    "openclaw", "claw", "agent", "security", "tool", "mcp",
    "skill", "automation", "terminal", "code", "hermes"
]

def load_state():
    """Load state from file or return default."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenPosts": [], "lastCheck": None, "foundItems": [], "apiStatus": "no_public_api"}

def save_state(state):
    """Save state to file."""
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_page(url, timeout=15):
    """Fetch webpage content."""
    headers = {
        "User-Agent": "ClawBytes/1.0 (Moltbook Monitor; +https://github.com/ClawBack1)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as e:
        print(f"  HTTP {e.code}: {url}")
        return None
    except URLError as e:
        print(f"  URL Error: {e.reason}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

def try_json_api():
    """Try various JSON API endpoints."""
    api_endpoints = [
        "https://moltbook.com/api/v1/posts",
        "https://moltbook.com/hot.json",
        "https://moltbook.com/api/posts",
        "https://moltbook.com/feed.json",
    ]
    
    for url in api_endpoints:
        headers = {
            "User-Agent": "ClawBytes/1.0",
            "Accept": "application/json"
        }
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("success") or data.get("posts"):
                    return data, url
        except:
            continue
    
    return None, None

def parse_api_posts(data):
    """Parse posts from Moltbook API response."""
    posts = []
    
    if not data:
        return posts
    
    api_posts = data.get("posts", [])
    if isinstance(data, list):
        api_posts = data
    
    for post in api_posts:
        post_id = post.get("id", "")
        title = post.get("title", "")
        content = post.get("content", "")[:300]
        author = post.get("author", {})
        agent_name = author.get("displayName", "") if isinstance(author, dict) else ""
        
        # Build URL - Moltbook post URLs appear to be /m/{submolt}/posts/{id}
        submolt = post.get("submolt", {}).get("name", "general") if isinstance(post.get("submolt"), dict) else "general"
        url = f"https://moltbook.com/m/{submolt}/posts/{post_id}"
        
        posts.append({
            "id": post_id,
            "title": title,
            "content": content,
            "url": url,
            "agent": agent_name,
            "karma": post.get("karma", 0),
            "commentCount": post.get("commentCount", 0)
        })
    
    return posts

def parse_html_posts(html):
    """
    Parse posts from Moltbook HTML.
    
    Note: This is fragile and depends on Moltbook's HTML structure.
    As of 2026-03-30, Moltbook shows mostly a landing page with no public posts visible.
    """
    posts = []
    
    if not html:
        return posts
    
    # Look for post patterns (this will need updating as Moltbook evolves)
    # Pattern 1: Links with post paths
    post_links = re.findall(r'href="(/m/[^"]+/posts?/[^"]+)"[^>]*>([^<]+)', html)
    for path, title in post_links:
        posts.append({
            "title": title.strip(),
            "url": f"https://moltbook.com{path}",
            "id": path
        })
    
    # Pattern 2: Card-style posts
    card_pattern = re.findall(r'class="[^"]*post[^"]*"[^>]*>.*?<h[23][^>]*>([^<]+)</h[23]>.*?href="([^"]+)"', html, re.DOTALL)
    for title, url in card_pattern:
        if not url.startswith("http"):
            url = f"https://moltbook.com{url}"
        posts.append({
            "title": title.strip(),
            "url": url,
            "id": url
        })
    
    # Pattern 3: Agent names/handles
    agent_mentions = re.findall(r'@(\w+agent\w*)', html, re.IGNORECASE)
    for agent in set(agent_mentions):
        posts.append({
            "title": f"Agent mention: @{agent}",
            "url": f"https://moltbook.com/u/{agent}",
            "id": f"agent_{agent}",
            "type": "agent_mention"
        })
    
    return posts

def is_relevant(post):
    """Check if post is relevant."""
    text = post.get("title", "").lower()
    for keyword in RELEVANT_KEYWORDS:
        if keyword in text:
            return True
    return False

def check_moltbook(verbose=True):
    """Check Moltbook for new content."""
    state = load_state()
    new_items = []
    status = "unknown"
    posts = []
    
    if verbose:
        print("🦞 Checking Moltbook...")
    
    # Try JSON API first
    api_data, api_url = try_json_api()
    if api_data:
        if verbose:
            print(f"  ✅ Found API: {api_url}")
        status = "api_active"
        state["apiStatus"] = f"api_available:{api_url}"
        
        # Parse API posts
        posts = parse_api_posts(api_data)
        if verbose:
            print(f"  Found {len(posts)} posts from API")
    else:
        status = "no_api"
        state["apiStatus"] = "no_public_api"
        if verbose:
            print("  No public API available")
        
        # Fallback to HTML scraping
        html = None
        for url in MOLTBOOK_URLS:
            html = fetch_page(url)
            if html:
                if verbose:
                    print(f"  Fetched HTML from {url}")
                break
        
        if not html:
            status = "unreachable"
            if verbose:
                print("  ❌ Moltbook unreachable")
            save_state(state)
            return [], status
        
        # Parse posts from HTML
        posts = parse_html_posts(html)
        if verbose:
            print(f"  Parsed {len(posts)} potential posts from HTML")
    
    seen_posts = set(state.get("seenPosts", []))
    
    for post in posts:
        post_id = post.get("id", post.get("url", ""))
        
        if post_id in seen_posts:
            continue
        
        # For API posts, check relevance; for HTML posts, use existing filter
        if "karma" not in post and not is_relevant(post):
            continue
        
        new_items.append({
            "title": post.get("title", ""),
            "url": post.get("url", ""),
            "id": post_id,
            "content": post.get("content", "")[:200],
            "agent": post.get("agent", ""),
            "karma": post.get("karma", 0),
            "comments": post.get("commentCount", 0),
            "source": "moltbook",
            "found_at": datetime.now(timezone.utc).isoformat()
        })
        
        seen_posts.add(post_id)
        
        if verbose:
            karma = post.get("karma", 0)
            agent = post.get("agent", "")
            agent_str = f" by {agent}" if agent else ""
            print(f"  🦞 [{karma}⬆] {post.get('title', '')[:45]}{agent_str}")
    
    # Update state
    state["seenPosts"] = list(seen_posts)[-500:]
    if new_items:
        state["foundItems"] = state.get("foundItems", []) + new_items
        state["foundItems"] = state["foundItems"][-100:]
    
    save_state(state)
    
    return new_items, status

def format_telegram_message(items):
    """Format items for Telegram."""
    if not items:
        return None
    
    lines = ["🦞 *From Moltbook*\n"]
    lines.append("_AI agents sharing their experiences_\n")
    
    for item in items[:5]:
        title = item["title"][:70].replace("[", "\\[").replace("]", "\\]")
        lines.append(f"• [{title}]({item['url']})")
    
    lines.append("\n#ClawBytes #Moltbook")
    return "\n".join(lines)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor Moltbook for AI agent posts")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--telegram", action="store_true", help="Output Telegram message format")
    parser.add_argument("--status", action="store_true", help="Show API status only")
    args = parser.parse_args()
    
    if args.status:
        state = load_state()
        print(f"Moltbook API Status: {state.get('apiStatus', 'unknown')}")
        print(f"Last check: {state.get('lastCheck', 'never')}")
        print(f"Seen posts: {len(state.get('seenPosts', []))}")
        return
    
    new_items, status = check_moltbook(verbose=not args.quiet)
    
    print(f"\n{'='*50}")
    print(f"Status: {status}")
    print(f"Found {len(new_items)} new relevant items")
    
    if status == "early_stage":
        print("\n⚠️ Note: Moltbook appears to be in early stage with minimal public content.")
        print("   Will continue monitoring for when public posts become available.")
    
    if args.telegram and new_items:
        msg = format_telegram_message(new_items)
        print(f"\n--- Telegram Message ---\n{msg}")

if __name__ == "__main__":
    main()
