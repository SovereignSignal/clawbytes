#!/usr/bin/env python3
"""
ClawBytes Reddit Monitor
Monitors Reddit for OpenClaw ecosystem discussions.

State file: memory/claw-reddit-state.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Workspace path - use env var or default
import os
WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = WORKSPACE / "memory"
STATE_FILE = MEMORY_DIR / "claw-reddit-state.json"

# Subreddits to monitor
SUBREDDITS = [
    {
        "name": "openclaw",
        "url": "https://www.reddit.com/r/openclaw/hot.json?limit=15",
        "type": "hot"
    },
    {
        "name": "selfhosted",
        "url": "https://www.reddit.com/r/selfhosted/search.json?q=openclaw&sort=new&limit=10",
        "type": "search"
    },
    {
        "name": "LocalLLaMA",
        "url": "https://www.reddit.com/r/LocalLLaMA/search.json?q=openclaw+OR+claw+agent&sort=new&limit=10",
        "type": "search"
    },
    {
        "name": "homelab",
        "url": "https://www.reddit.com/r/homelab/search.json?q=openclaw+OR+ai+agent&sort=new&limit=5",
        "type": "search"
    },
    {
        "name": "singularity",
        "url": "https://www.reddit.com/r/singularity/search.json?q=openclaw&sort=new&limit=5",
        "type": "search"
    }
]

# Quality thresholds
MIN_SCORE = 50
MIN_COMMENTS = 20

def load_state():
    """Load state from file or return default."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenPosts": [], "lastCheck": None, "foundItems": []}

def save_state(state):
    """Save state to file."""
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_reddit(url, timeout=15):
    """Fetch Reddit JSON API."""
    headers = {
        "User-Agent": "ClawBytes/1.0 (Reddit Monitor; contact: github.com/ClawBack1)"
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        print(f"  HTTP {e.code}: {url[:50]}...")
        return None
    except URLError as e:
        print(f"  URL Error: {e.reason}")
        return None
    except json.JSONDecodeError as e:
        print(f"  JSON Error: {e}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

def parse_posts(data, subreddit_name):
    """Parse Reddit posts from JSON response."""
    posts = []
    
    if not data or "data" not in data:
        return posts
    
    children = data.get("data", {}).get("children", [])
    
    for child in children:
        post = child.get("data", {})
        if not post:
            continue
        
        posts.append({
            "id": post.get("id", ""),
            "title": post.get("title", ""),
            "subreddit": post.get("subreddit", subreddit_name),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "url": f"https://www.reddit.com{post.get('permalink', '')}",
            "created_utc": post.get("created_utc", 0),
            "selftext": post.get("selftext", "")[:200],
            "link_flair_text": post.get("link_flair_text", "")
        })
    
    return posts

def passes_quality_filter(post, subreddit_type):
    """Check if post passes quality thresholds."""
    # r/openclaw posts are always interesting
    if post["subreddit"].lower() == "openclaw":
        return True
    
    # For search results, require quality thresholds
    if post["score"] >= MIN_SCORE or post["num_comments"] >= MIN_COMMENTS:
        return True
    
    return False

def check_subreddits(verbose=True):
    """Check all subreddits for new posts."""
    state = load_state()
    new_items = []
    sub_status = {}
    seen_posts = set(state.get("seenPosts", []))
    
    for sub in SUBREDDITS:
        name = sub["name"]
        url = sub["url"]
        sub_type = sub["type"]
        
        if verbose:
            print(f"\n🔴 Checking r/{name} ({sub_type})")
        
        # Rate limiting
        time.sleep(1)
        
        data = fetch_reddit(url)
        
        if data is None:
            sub_status[name] = "failed"
            continue
        
        posts = parse_posts(data, name)
        if verbose:
            print(f"  Found {len(posts)} posts")
        
        sub_status[name] = f"ok ({len(posts)} posts)"
        
        for post in posts:
            post_id = post["id"]
            
            # Skip seen posts
            if post_id in seen_posts:
                continue
            
            # Quality filter
            if not passes_quality_filter(post, sub_type):
                continue
            
            new_items.append({
                "id": post_id,
                "subreddit": post["subreddit"],
                "title": post["title"],
                "url": post["url"],
                "score": post["score"],
                "comments": post["num_comments"],
                "flair": post["link_flair_text"],
                "found_at": datetime.now(timezone.utc).isoformat()
            })
            
            seen_posts.add(post_id)
            
            if verbose:
                emoji = "🔥" if post["score"] >= 100 else "📝"
                print(f"  {emoji} [{post['score']}↑ {post['num_comments']}💬] {post['title'][:50]}")
    
    # Update state
    state["seenPosts"] = list(seen_posts)[-1000:]  # Keep last 1000 IDs
    
    if new_items:
        state["foundItems"] = state.get("foundItems", []) + new_items
        state["foundItems"] = state["foundItems"][-200:]  # Keep recent items
    
    save_state(state)
    
    return new_items, sub_status

def format_telegram_message(items):
    """Format new items for Telegram."""
    if not items:
        return None
    
    lines = ["💬 *Reddit Buzz*\n"]
    
    # Sort by score
    items_sorted = sorted(items, key=lambda x: x.get("score", 0), reverse=True)
    
    for item in items_sorted[:10]:
        score = item.get("score", 0)
        comments = item.get("comments", 0)
        title = item["title"][:70].replace("[", "\\[").replace("]", "\\]")
        subreddit = item["subreddit"]
        
        emoji = "🔥" if score >= 100 else "💬"
        lines.append(f"{emoji} *r/{subreddit}* ({score}↑ {comments}💬)")
        lines.append(f"[{title}]({item['url']})\n")
    
    lines.append("#ClawBytes #Reddit")
    return "\n".join(lines)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor Reddit for OpenClaw content")
    parser.add_argument("--all", action="store_true", help="Show all posts, skip quality filter")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--telegram", action="store_true", help="Output Telegram message format")
    parser.add_argument("--status", action="store_true", help="Show subreddit status only")
    args = parser.parse_args()
    
    new_items, status = check_subreddits(verbose=not args.quiet)
    
    print(f"\n{'='*50}")
    print(f"Found {len(new_items)} quality posts")
    
    if args.telegram and new_items:
        msg = format_telegram_message(new_items)
        print(f"\n--- Telegram Message ---\n{msg}")
    
    # Summary
    working = sum(1 for s in status.values() if "ok" in s)
    print(f"\nSubreddits: {working}/{len(SUBREDDITS)} accessible")

if __name__ == "__main__":
    main()
