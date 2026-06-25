#!/usr/bin/env python3
"""
claw-discover.py — Dynamic discovery for the Claw ecosystem
Finds new agent frameworks and projects we haven't seen before.

Usage:
  python3 claw-discover.py              # discover + update sources
  python3 claw-discover.py --dry-run    # show findings without saving
  python3 claw-discover.py --send       # post new discoveries to Telegram
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import argparse
from datetime import datetime, timezone, timedelta

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_FILE = os.path.join(WORKSPACE, "memory", "claw-ecosystem-sources.json")
STATE_FILE = os.path.join(WORKSPACE, "memory", "claw-ecosystem-state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

MIN_STARS = 100
MIN_STARS_NEW_REPO = 50  # for repos < 7 days old

GITHUB_QUERIES = [
    "claw+agent+in:name&sort=stars",
    "openclaw+alternative+in:description&sort=stars",
    "personal+ai+agent+self-hosted&sort=stars",
    "openclaw+fork+in:description&sort=updated",
]

HEADERS = {"User-Agent": "ClawBytes-Monitor/1.0 (github.com/ClawBack1)"}


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default or {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_known_repos(sources):
    """Get normalized set of all known repo names."""
    known = set()
    for entry in sources.get("curated", []) + sources.get("dynamic", []):
        repo = entry.get("repo", "")
        if repo:
            known.add(repo.lower())
    return known


def fetch_github(url, delay=2):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        time.sleep(delay)
        return data
    except Exception as e:
        print(f"  ⚠️  GitHub error: {e}", file=sys.stderr)
        time.sleep(delay)
        return {}


def is_new_repo(created_at):
    """Check if repo was created within last 7 days."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created) < timedelta(days=7)
    except Exception:
        return False


def discover_github(known_repos):
    """Search GitHub for new agent/claw repos."""
    print("🔍 GitHub discovery...", file=sys.stderr)
    found = []

    for query in GITHUB_QUERIES:
        url = f"https://api.github.com/search/repositories?q={query}&per_page=15"
        data = fetch_github(url)
        for item in data.get("items", []):
            full_name = item.get("full_name", "")
            if not full_name or full_name.lower() in known_repos:
                continue

            stars = item.get("stargazers_count", 0)
            created_at = item.get("created_at", "")
            threshold = MIN_STARS_NEW_REPO if is_new_repo(created_at) else MIN_STARS

            if stars >= threshold:
                found.append({
                    "repo": full_name,
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "stars": stars,
                    "url": item.get("html_url", ""),
                    "topics": item.get("topics", []),
                    "language": item.get("language", ""),
                    "createdAt": created_at,
                    "updatedAt": item.get("updated_at", ""),
                    "source": "github-search",
                    "discoveredAt": datetime.now(timezone.utc).isoformat(),
                })
                print(f"  📦 {full_name} (⭐ {stars:,})", file=sys.stderr)

    # Deduplicate
    seen = set()
    deduped = []
    for r in found:
        key = r["repo"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def discover_hn(known_repos):
    """Find GitHub repos mentioned in HN stories."""
    print("🔶 HN discovery...", file=sys.stderr)
    found = []
    queries = [
        "openclaw+alternative",
        "self-hosted+ai+agent",
        "personal+ai+agent+open+source",
    ]
    import re
    github_pattern = re.compile(r'https?://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)')

    for q in queries:
        url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=10"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            for hit in data.get("hits", []):
                story_url = hit.get("url", "")
                match = github_pattern.search(story_url)
                if match:
                    repo = match.group(1).rstrip("/")
                    if repo.lower() not in known_repos:
                        # Fetch metadata to check stars
                        meta = fetch_github(f"https://api.github.com/repos/{repo}")
                        if meta.get("stargazers_count", 0) >= MIN_STARS:
                            found.append({
                                "repo": repo,
                                "name": meta.get("name", repo.split("/")[1]),
                                "description": meta.get("description", ""),
                                "stars": meta.get("stargazers_count", 0),
                                "url": f"https://github.com/{repo}",
                                "topics": meta.get("topics", []),
                                "language": meta.get("language", ""),
                                "source": "hackernews",
                                "hnTitle": hit.get("title", ""),
                                "discoveredAt": datetime.now(timezone.utc).isoformat(),
                            })
                            print(f"  🔶 {repo} via HN", file=sys.stderr)
            time.sleep(1)
        except Exception as e:
            print(f"  ⚠️  HN error: {e}", file=sys.stderr)

    return found


def format_telegram_post(discoveries):
    """Format new discoveries as a Telegram message."""
    if not discoveries:
        return None

    lines = ["🔍 <b>New in the Claw Ecosystem</b>\n"]
    for d in discoveries[:8]:  # cap at 8 per post
        name = d.get("name") or d["repo"].split("/")[1]
        desc = d.get("description", "")[:80]
        stars = d.get("stars", 0)
        url = d.get("url", f"https://github.com/{d['repo']}")
        lines.append(f"📦 <b>{name}</b> — {desc}")
        lines.append(f"⭐ {stars:,} | <a href=\"{url}\">{d['repo']}</a>\n")

    lines.append("#ClawEcosystem #NewProject")
    return "\n".join(lines)


def send_telegram(text):
    """Send message to Telegram channel."""
    if not BOT_TOKEN or not CHANNEL_ID:
        print("⚠️  No Telegram credentials set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return resp.get("ok", False)
    except Exception as e:
        print(f"⚠️  Telegram error: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    sources = load_json(SOURCES_FILE, {"curated": [], "dynamic": [], "_meta": {}})
    known_repos = get_known_repos(sources)

    print(f"📋 Known repos: {len(known_repos)}", file=sys.stderr)

    # Run all discovery methods
    all_new = []
    all_new.extend(discover_github(known_repos))

    # Update known after github discovery to avoid re-fetching in HN
    for r in all_new:
        known_repos.add(r["repo"].lower())

    all_new.extend(discover_hn(known_repos))
    for r in all_new:
        known_repos.add(r["repo"].lower())

    # Brave discovery removed 2026-06-25 (Brave deprecated).

    # Deduplicate final list
    seen = set()
    deduped = []
    for r in all_new:
        key = r["repo"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    print(f"\n✅ Discovered {len(deduped)} new projects", file=sys.stderr)

    if not deduped:
        print("Nothing new found.")
        return

    # Print findings
    for d in deduped:
        print(f"  📦 {d['repo']} — ⭐{d.get('stars',0):,} [{d.get('source','')}]")
        if d.get("description"):
            print(f"     {d['description'][:100]}")

    if args.dry_run:
        print("\n[dry-run] Not saving or sending.", file=sys.stderr)
        return

    # Save to sources.json
    sources["dynamic"].extend(deduped)
    sources["_meta"]["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    sources["_meta"]["totalDiscovered"] = len(sources["dynamic"])
    save_json(SOURCES_FILE, sources)
    print(f"\n💾 Saved to sources.json ({len(sources['dynamic'])} dynamic entries)", file=sys.stderr)

    # Post to Telegram if --send
    if args.send:
        post = format_telegram_post(deduped)
        if post:
            ok = send_telegram(post)
            print(f"📨 Telegram: {'✅ sent' if ok else '❌ failed'}", file=sys.stderr)

    # Update state
    state = load_json(STATE_FILE, {})
    state["lastDiscovery"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
