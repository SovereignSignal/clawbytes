#!/usr/bin/env python3
"""
ClawBytes People Tracker
Tracks influential people in the OpenClaw ecosystem using Brave Search.

Run weekly (not daily) due to Brave API rate limits.

State file: memory/claw-people-state.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus

# Workspace path
WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-people-state.json"

# People to track
PEOPLE = [
    {
        "name": "Simon Willison",
        "query": "simonwillison.net openclaw OR claw agent 2026",
        "handle": "@simonw",
        "tags": ["security", "technical"]
    },
    {
        "name": "Peter Steinberger",
        "query": "steipete openclaw OR claw agent 2026",
        "handle": "@steipete",
        "tags": ["creator", "mobile"]
    },
    {
        "name": "VelvetShark",
        "query": "velvetshark openclaw agent 2026",
        "handle": "@velvetshark",
        "tags": ["power-user", "automation"]
    },
    {
        "name": "Min Choi",
        "query": "minchoi openclaw agent",
        "handle": "@minchoi",
        "tags": ["community", "tutorials"]
    },
    {
        "name": "Matt Shumer",
        "query": "mattshumer openclaw OR moltbook 2026",
        "handle": "@mattshumer_",
        "tags": ["moltbook", "community"]
    },
    {
        "name": "swyx",
        "query": "swyx openclaw OR ai agent framework 2026",
        "handle": "@swyx",
        "tags": ["dx", "community"]
    }
]

# Brave Search API
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

def load_state():
    """Load state from file or return default."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenUrls": [], "lastCheck": None, "foundItems": [], "byPerson": {}}

def save_state(state):
    """Save state to file."""
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def brave_search(query, api_key, count=10):
    """Search using Brave Search API."""
    import gzip
    url = f"{BRAVE_API_URL}?q={quote_plus(query)}&count={count}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key
    }
    req = Request(url, headers=headers)
    
    try:
        with urlopen(req, timeout=15) as response:
            content = response.read()
            # Handle gzip encoding
            if response.headers.get("Content-Encoding") == "gzip":
                content = gzip.decompress(content)
            data = json.loads(content.decode("utf-8"))
            return data.get("web", {}).get("results", [])
    except HTTPError as e:
        if e.code == 429:
            print("  ⚠️ Rate limited by Brave API")
        else:
            print(f"  HTTP {e.code}")
        return []
    except URLError as e:
        print(f"  URL Error: {e.reason}")
        return []
    except Exception as e:
        print(f"  Error: {e}")
        return []

def check_people(api_key=None, verbose=True):
    """Check for new content from tracked people."""
    import time
    
    if not api_key:
        api_key = os.environ.get("BRAVE_API_KEY")
    
    if not api_key:
        print("❌ BRAVE_API_KEY not set")
        return [], "no_api_key"
    
    state = load_state()
    new_items = []
    seen_urls = set(state.get("seenUrls", []))
    
    for i, person in enumerate(PEOPLE):
        # Rate limiting - wait 2s between requests
        if i > 0:
            time.sleep(2)
        name = person["name"]
        query = person["query"]
        handle = person["handle"]
        tags = person.get("tags", [])
        
        if verbose:
            print(f"\n👤 Searching: {name} ({handle})")
        
        results = brave_search(query, api_key, count=5)
        
        if verbose:
            print(f"  Found {len(results)} results")
        
        person_items = []
        
        for result in results:
            url = result.get("url", "")
            title = result.get("title", "")
            description = result.get("description", "")
            
            if url in seen_urls:
                continue
            
            # Skip non-relevant domains
            skip_domains = ["facebook.com", "linkedin.com/in/", "twitter.com", "x.com"]
            if any(d in url for d in skip_domains):
                continue
            
            item = {
                "person": name,
                "handle": handle,
                "title": title,
                "url": url,
                "description": description[:200],
                "tags": tags,
                "found_at": datetime.now(timezone.utc).isoformat()
            }
            
            new_items.append(item)
            person_items.append(item)
            seen_urls.add(url)
            
            if verbose:
                print(f"  📄 {title[:60]}")
        
        # Track per-person
        if name not in state.get("byPerson", {}):
            state["byPerson"] = state.get("byPerson", {})
            state["byPerson"][name] = []
        
        if person_items:
            state["byPerson"][name].extend(person_items)
            state["byPerson"][name] = state["byPerson"][name][-20:]  # Keep last 20
    
    # Update state
    state["seenUrls"] = list(seen_urls)[-500:]
    if new_items:
        state["foundItems"] = state.get("foundItems", []) + new_items
        state["foundItems"] = state["foundItems"][-100:]
    
    save_state(state)
    
    return new_items, "ok"

def format_telegram_message(items):
    """Format items for Telegram."""
    if not items:
        return None
    
    lines = ["👥 *People Tracker Update*\n"]
    
    # Group by person
    by_person = {}
    for item in items:
        person = item["person"]
        if person not in by_person:
            by_person[person] = []
        by_person[person].append(item)
    
    for person, person_items in by_person.items():
        handle = person_items[0].get("handle", "")
        lines.append(f"*{person}* ({handle})")
        for item in person_items[:3]:
            title = item["title"][:60].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"• [{title}]({item['url']})")
        lines.append("")
    
    lines.append("#ClawBytes #PeopleTracker")
    return "\n".join(lines)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Track influential people in OpenClaw ecosystem")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--telegram", action="store_true", help="Output Telegram message format")
    parser.add_argument("--status", action="store_true", help="Show tracking status")
    args = parser.parse_args()
    
    if args.status:
        state = load_state()
        print(f"Last check: {state.get('lastCheck', 'never')}")
        print(f"Tracked URLs: {len(state.get('seenUrls', []))}")
        print(f"\nPeople tracked:")
        for person in PEOPLE:
            count = len(state.get("byPerson", {}).get(person["name"], []))
            print(f"  • {person['name']} ({person['handle']}): {count} items")
        return
    
    new_items, status = check_people(verbose=not args.quiet)
    
    print(f"\n{'='*50}")
    print(f"Status: {status}")
    print(f"Found {len(new_items)} new items")
    
    if args.telegram and new_items:
        msg = format_telegram_message(new_items)
        print(f"\n--- Telegram Message ---\n{msg}")

if __name__ == "__main__":
    main()
