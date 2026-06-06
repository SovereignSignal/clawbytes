#!/usr/bin/env python3
"""
claw-digest-generator.py - Generate Telegram-ready digests for the Claw Ecosystem channel

Usage:
    ./claw-digest-generator.py --mode daily [--channel-id ID] [--bot-token TOKEN] [--send]
    ./claw-digest-generator.py --mode weekly [--channel-id ID] [--bot-token TOKEN] [--send]
    ./claw-digest-generator.py --mode discover [--channel-id ID] [--bot-token TOKEN] [--send]

Environment variables:
    BRAVE_API_KEY - Brave Search API key (required for weekly mode)
    TELEGRAM_BOT_TOKEN - Bot token (alternative to --bot-token)
    TELEGRAM_CHANNEL_ID - Channel ID (alternative to --channel-id)
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
MEMORY_DIR = WORKSPACE / "memory"
NEW_ITEMS_FILE = MEMORY_DIR / "claw-ecosystem-new-items.json"
DISCOVERIES_FILE = MEMORY_DIR / "claw-ecosystem-discoveries.json"
SOURCES_FILE = MEMORY_DIR / "claw-ecosystem-sources.json"
STATE_FILE = MEMORY_DIR / "claw-ecosystem-state.json"
SECURITY_STATE_FILE = MEMORY_DIR / "claw-security-state.json"


def load_json(path):
    """Load a JSON file safely."""
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def load_new_items():
    """Load the new items from the monitor script output."""
    data = load_json(NEW_ITEMS_FILE)
    if not data:
        return {
            "timestamp": None,
            "newReleases": [],
            "newHNStories": [],
            "newSkills": [],
            "summary": {"hasNews": False}
        }
    return data


def load_discoveries():
    """Load discoveries from the discovery output file."""
    data = load_json(DISCOVERIES_FILE)
    if not data:
        return {
            "timestamp": None,
            "newDiscoveries": [],
            "summary": {"hasDiscoveries": False}
        }
    return data


def load_sources():
    """Load the sources.json file."""
    return load_json(SOURCES_FILE)


def load_security_state():
    """Load tracked security state."""
    data = load_json(SECURITY_STATE_FILE)
    if not data:
        return {"alerts": [], "allClear": True}
    return data


def trim(text: str, length: int = 110) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def release_priority(repo: str) -> int:
    repo = (repo or "").lower()
    order = {
        "openclaw/openclaw": 0,
        "nousresearch/hermes-agent": 1,
        "nearai/ironclaw": 2,
        "moltis-org/moltis": 3,
        "rightnow-ai/openfang": 4,
        "qwibitai/nanoclaw": 5,
        "sipeed/picoclaw": 6,
        "openai/codex": 7,
    }
    return order.get(repo, 99)


def github_api(url: str) -> dict:
    """Simple GitHub API call."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ClawBack-Ecosystem/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def fetch_recent_commits(repo: str, days: int = 7) -> list:
    """Fetch commits from the last N days for a repo."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = f"https://api.github.com/repos/{repo}/commits?since={since}&per_page=10"
    data = github_api(url)
    if not isinstance(data, list):
        return []
    commits = []
    for c in data:
        commit = c.get("commit", {})
        commits.append({
            "sha": c.get("sha", "")[:7],
            "message": (commit.get("message", "") or "").split("\n")[0],
            "author": commit.get("author", {}).get("name", ""),
            "date": commit.get("author", {}).get("date", "")
        })
    return commits


def get_ecosystem_activity(sources: dict, days: int = 7, max_repos: int = 15) -> list:
    """Get repos with recent commit activity, grouped by category."""
    curated = sources.get("curated", [])
    active = []
    for project in curated:
        repo = project.get("repo")
        if not repo or not project.get("exists", True):
            continue
        commits = fetch_recent_commits(repo, days)
        if len(commits) >= 2:
            active.append({
                "repo": repo,
                "name": project.get("name", repo.split("/")[-1]),
                "description": project.get("description", ""),
                "stars": project.get("stars", 0),
                "category": project.get("notion_category", "Ecosystem"),
                "commits": commits,
                "commit_count": len(commits),
                "url": project.get("url", f"https://github.com/{repo}")
            })
    # Sort by commit count desc, then stars desc
    active.sort(key=lambda x: (-x["commit_count"], -x.get("stars", 0)))
    return active[:max_repos]


def notion_category_emoji(category: str) -> str:
    emojis = {
        "Core": "👑",
        "Tiny Claws": "🐾",
        "Self Hosted": "🏠",
        "Enterprise Big Tech": "🏢",
        "Enterprise Startups": "🚀",
        "Infrastructure": "⚙️",
        "Ecosystem": "🌿"
    }
    return emojis.get(category, "📦")


def summarize_security(security_state: dict):
    alerts = security_state.get("alerts", [])[-10:]
    if not alerts:
        return {"label": "✅ All clear", "items": []}

    severity_order = {"CRITICAL": 0, "HIGH": 1, "WATCHING": 2, "MEDIUM": 3}
    alerts = sorted(alerts, key=lambda x: severity_order.get(x.get("severity", "MEDIUM"), 3))
    top = alerts[:3]
    worst = top[0].get("severity", "MEDIUM")
    label = "🔴 Critical" if worst == "CRITICAL" else "🟠 High" if worst == "HIGH" else "🟡 Watching"
    return {"label": label, "items": top}


def brave_search(query: str, api_key: str, count: int = 5, freshness: str = "pw"):
    """Search using Brave Search API.
    
    Args:
        query: Search query
        api_key: Brave API key
        count: Number of results
        freshness: Time filter (pd=day, pw=week, pm=month)
    """
    if not api_key:
        print("⚠️  No Brave API key provided, skipping web search", file=sys.stderr)
        return []
    
    url = "https://api.search.brave.com/res/v1/web/search"
    params = urllib.parse.urlencode({
        "q": query,
        "count": count,
        "freshness": freshness,
        "text_decorations": "false"
    })
    
    req = urllib.request.Request(
        f"{url}?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            return data.get("web", {}).get("results", [])
    except Exception as e:
        print(f"⚠️  Brave search error: {e}", file=sys.stderr)
        return []


def search_ecosystem_news(api_key: str):
    """Search for recent news about the Claw ecosystem."""
    queries = [
        "openclaw AI agent",
        "hermes agent NousResearch",
        "claude code anthropic",
        "AI coding agents 2026"
    ]
    
    all_results = []
    seen_urls = set()
    
    for query in queries:
        results = brave_search(query, api_key, count=3, freshness="pw")
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "description": r.get("description", ""),
                    "age": r.get("age", "")
                })
    
    return all_results[:10]  # Limit to 10 results


def format_daily_digest(items: dict, news: list, security_state: dict) -> str:
    """Format a stronger daily digest message for Telegram."""
    sources = load_sources()
    lines = []
    lines.append(f"🦀 **ClawBytes Daily — {datetime.now().strftime('%B %d, %Y')}**")
    lines.append("")

    releases = sorted(items.get("newReleases", []), key=lambda x: release_priority(x.get("repo", "")))
    hn_items = sorted(items.get("newHNStories", []), key=lambda x: x.get("points", 0), reverse=True)
    skills = items.get("newSkills", [])
    security = summarize_security(security_state)
    
    # Recent commit activity (last 1 day for daily)
    active_repos = get_ecosystem_activity(sources, days=1, max_repos=8)

    if security["items"]:
        lead = trim(security["items"][0].get("title", "Security issue detected"), 140)
    elif releases:
        lead = trim(f"{releases[0].get('repo','').split('/')[-1]} shipped {releases[0].get('tag','a new release')}", 140)
    elif active_repos:
        lead = trim(f"{active_repos[0]['name']} ({active_repos[0]['commit_count']} commits) — active dev continues", 140)
    elif hn_items:
        lead = trim(hn_items[0].get("title", "Community chatter picked up"), 140)
    elif skills:
        lead = trim(f"New ClawHub activity: {skills[0].get('name','new skill spotted')}", 140)
    else:
        lead = "Quiet day — no major ecosystem movement detected."

    lines.append(f"**Lead Signal:** {lead}")
    lines.append(f"📅 {datetime.now().strftime('%B %d, %Y')}")
    lines.append("")
    
    # Releases
    if releases:
        lines.append("📦 **Releases**")
        for release in releases[:4]:
            repo = release.get("repo", "").split("/")[-1]
            tag = release.get("tag", "")
            body = trim(release.get("body", "") or release.get("name", ""), 120)
            url = release.get("url", "")
            lines.append(f"• **{repo} {tag}**")
            if body:
                lines.append(f"  {body}")
            if url:
                lines.append(f"  🔗 {url}")
        lines.append("")
    
    # Active development (commits in last 24h)
    if active_repos:
        lines.append("🔨 **Active Development (24h)**")
        for r in active_repos[:5]:
            name = r["name"]
            commits = r["commit_count"]
            url = r["url"]
            latest = trim(r["commits"][0]["message"], 60) if r["commits"] else ""
            lines.append(f"• **[{name}]({url})** — {commits} commits")
            if latest:
                lines.append(f"  `→ {latest}`")
        lines.append("")

    if skills:
        lines.append("🛍️ **ClawHub**")
        for skill in skills[:3]:
            lines.append(f"• **{trim(skill.get('name', ''), 90)}**")
            if skill.get("description"):
                lines.append(f"  {trim(skill.get('description', ''), 110)}")
            if skill.get("url"):
                lines.append(f"  🔗 {skill.get('url')}")
        lines.append("")

    if hn_items or news:
        lines.append("💬 **Community Pulse**")
        for story in hn_items[:2]:
            lines.append(f"• {trim(story.get('title', ''), 110)}")
            lines.append(f"  ⬆️ {story.get('points', 0)} · 💬 {story.get('comments', 0)}")
            if story.get("hn_url"):
                lines.append(f"  🔗 {story.get('hn_url')}")
        for article in news[:2]:
            lines.append(f"• {trim(article.get('title', ''), 110)}")
            if article.get("description"):
                lines.append(f"  {trim(article.get('description', ''), 110)}")
            if article.get("url"):
                lines.append(f"  🔗 {article.get('url')}")
        lines.append("")

    lines.append("⚠️ **Security**")
    lines.append(f"• {security['label']}")
    for alert in security["items"][:2]:
        lines.append(f"  • {trim(alert.get('title', ''), 95)}")
    lines.append("")

    if not any([releases, hn_items, skills, news, security["items"], active_repos]):
        lines.append("_Quiet day — no meaningful ecosystem movement detected._")
        lines.append("")

    lines.append("—")
    lines.append("_ClawBack take: focus on signal, skip the filler._")
    
    return "\n".join(lines)


def format_weekly_digest(items: dict, news: list) -> str:
    """Format a weekly digest with full ecosystem coverage."""
    sources = load_sources()
    lines = []
    lines.append("🦀 **Claw Ecosystem Weekly Digest**")
    lines.append(f"📅 Week of {datetime.now().strftime('%B %d, %Y')}")
    lines.append("")
    
    # Summary
    release_count = len(items.get("newReleases", []))
    hn_count = len(items.get("newHNStories", []))
    skill_count = len(items.get("newSkills", []))
    
    lines.append("📊 **This Week's Summary**")
    lines.append(f"• 📦 {release_count} new release(s)")
    lines.append(f"• 🔶 {hn_count} HN discussion(s)")
    lines.append(f"• 🎯 {skill_count} new skill(s)")
    lines.append("")
    
    # Releases (detailed)
    if items.get("newReleases"):
        lines.append("🚢 **What Shipped — Releases**")
        for release in items["newReleases"]:
            repo = release.get("repo", "")
            tag = release.get("tag", "")
            url = release.get("url", "")
            body = trim(release.get("body", "") or release.get("name", ""), 120)
            repo_name = repo.split("/")[-1]
            lines.append(f"📦 **[{repo_name}]({url})** {tag}")
            if body:
                lines.append(f"  _{body}_")
            lines.append("")
    
    # Recent commit activity across ecosystem
    active_repos = get_ecosystem_activity(sources, days=7, max_repos=12)
    if active_repos:
        lines.append("🔨 **What Shipped — Active Development**")
        # Group by category
        by_category = {}
        for repo in active_repos:
            cat = repo.get("category", "Ecosystem")
            by_category.setdefault(cat, []).append(repo)
        
        for cat, repos in sorted(by_category.items()):
            emoji = notion_category_emoji(cat)
            lines.append(f"\n{emoji} **{cat}**")
            for repo in repos[:3]:
                name = repo.get("name", repo["repo"].split("/")[-1])
                desc = trim(repo.get("description", ""), 90)
                commits = repo.get("commit_count", 0)
                url = repo.get("url", "")
                latest = repo.get("commits", [{}])[0].get("message", "")
                latest = trim(latest, 70)
                lines.append(f"• **[{name}]({url})** — {commits} commits")
                if desc:
                    lines.append(f"  _{desc}_")
                if latest:
                    lines.append(f"  `→ {latest}`")
            lines.append("")
    
    # HN highlights
    if items.get("newHNStories"):
        lines.append("🔶 **Top HN Discussions**")
        sorted_stories = sorted(
            items["newHNStories"], 
            key=lambda x: x.get("points", 0), 
            reverse=True
        )[:3]
        for story in sorted_stories:
            title = story.get("title", "")
            points = story.get("points", 0)
            hn_url = story.get("hn_url", "")
            lines.append(f"• {title} ({points} pts)")
            if hn_url:
                lines.append(f"  {hn_url}")
        lines.append("")
    
    # Web news
    if news:
        lines.append("📰 **Around the Web**")
        for article in news[:5]:
            title = article.get("title", "")
            url = article.get("url", "")
            age = article.get("age", "")
            lines.append(f"• {title}")
            if age:
                lines.append(f"  _{age}_")
            if url:
                lines.append(f"  🔗 {url}")
        lines.append("")
    
    # New skills
    if items.get("newSkills"):
        lines.append("🎯 **New Skills on ClawHub**")
        for skill in items["newSkills"]:
            name = skill.get("name", "")
            author = skill.get("author", "")
            lines.append(f"• **{name}**" + (f" by {author}" if author else ""))
        lines.append("")
    
    lines.append("—")
    lines.append("_Weekly digest by ClawBack 🦀_")
    lines.append("_Subscribe for daily updates!_")
    
    return "\n".join(lines)


def format_discovery_digest(discoveries: dict) -> str:
    """Format newly discovered projects as a Telegram post."""
    lines = []
    lines.append("🔍 **New in the Claw Ecosystem**")
    lines.append(f"📅 {datetime.now().strftime('%B %d, %Y')}")
    lines.append("")
    
    new_projects = discoveries.get("newDiscoveries", [])
    
    if not new_projects:
        lines.append("_No new projects discovered this run._")
        lines.append("_The ecosystem scan found no unknown projects meeting our criteria._")
        lines.append("")
    else:
        # Sort by stars descending
        sorted_projects = sorted(new_projects, key=lambda x: x.get("stars", 0), reverse=True)
        
        for project in sorted_projects[:10]:  # Limit to top 10
            name = project.get("name", project.get("repo", "").split("/")[-1])
            description = project.get("description", "")
            stars = project.get("stars", 0)
            url = project.get("url", "")
            language = project.get("language", "")
            source = project.get("source", "discovery")
            
            # Truncate long descriptions
            if len(description) > 120:
                description = description[:117] + "..."
            
            lines.append(f"**{name}**")
            if description:
                lines.append(f"_{description}_")
            
            star_str = f"⭐ {stars:,}" if stars else "⭐ new"
            lang_str = f" · {language}" if language else ""
            source_emoji = {
                "github-search": "🔍",
                "awesome-list": "📚",
                "hackernews": "🔶",
                "brave-search": "🌐"
            }.get(source, "📦")
            
            lines.append(f"{star_str}{lang_str} · {source_emoji}")
            if url:
                lines.append(f"🔗 {url}")
            lines.append("")
    
    # Add stats
    sources = load_sources()
    total_curated = len(sources.get("curated", []))
    total_dynamic = len(sources.get("dynamic", []))
    
    lines.append("—")
    lines.append(f"📊 Ecosystem: {total_curated} curated + {total_dynamic} discovered = {total_curated + total_dynamic} total")
    lines.append("")
    lines.append("#ClawEcosystem #NewProject #AIAgents")
    lines.append("_Discovery by ClawBack 🦀_")
    
    return "\n".join(lines)


def format_ecosystem_summary() -> str:
    """Format a summary of the entire ecosystem."""
    sources = load_sources()
    
    lines = []
    lines.append("🦀 **Claw Ecosystem Overview**")
    lines.append(f"📅 {datetime.now().strftime('%B %d, %Y')}")
    lines.append("")
    
    # Curated projects (sorted by stars)
    curated = sources.get("curated", [])
    if curated:
        curated_sorted = sorted(curated, key=lambda x: x.get("stars", 0), reverse=True)
        
        lines.append("🏆 **Top Projects by Stars**")
        for project in curated_sorted[:8]:
            if not project.get("exists", True):
                continue
            name = project.get("name", "")
            stars = project.get("stars", 0)
            repo = project.get("repo", "")
            proj_type = project.get("type", "")
            
            type_emoji = {
                "core": "👑",
                "ecosystem": "🌿",
                "related": "🔗",
                "competitor": "⚔️"
            }.get(proj_type, "📦")
            
            lines.append(f"{type_emoji} **{name}** — ⭐ {stars:,}")
        lines.append("")
    
    # Dynamic discoveries
    dynamic = sources.get("dynamic", [])
    if dynamic:
        lines.append(f"🔍 **Recently Discovered** ({len(dynamic)} projects)")
        recent = sorted(dynamic, key=lambda x: x.get("discoveredAt", ""), reverse=True)[:5]
        for project in recent:
            name = project.get("name", "")
            stars = project.get("stars", 0)
            lines.append(f"• {name} (⭐ {stars:,})")
        lines.append("")
    
    # Stats
    total = len(curated) + len(dynamic)
    lines.append(f"📊 **Stats**: {len(curated)} curated · {len(dynamic)} discovered · {total} total")
    lines.append("")
    lines.append("—")
    lines.append("_Ecosystem tracking by ClawBack 🦀_")
    
    return "\n".join(lines)


def create_weekly_notion_page(items: dict, news: list) -> str | None:
    """Create a rich Notion page for the weekly digest under the Claws page."""
    from datetime import timedelta
    NOTION_PAGE_ID = "337000c0-d590-805d-af74-f27d19215184"
    NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
    HEADERS = {
        "Authorization": f"Bearer {NOTION_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    
    week_start = datetime.now() - timedelta(days=datetime.now().weekday())
    week_str = week_start.strftime("%b %d")
    page_title = f"ClawBytes Weekly — Week of {week_str}"
    
    sources = load_sources()
    releases = sorted(items.get("newReleases", []), key=lambda x: release_priority(x.get("repo", "")))
    hn_items = sorted(items.get("newHNStories", []), key=lambda x: x.get("points", 0), reverse=True)
    active_repos = get_ecosystem_activity(sources, days=7, max_repos=20)
    security = summarize_security(load_security_state())
    
    children = []
    
    # Summary
    release_count = len(releases)
    hn_count = len(hn_items)
    active_count = len(active_repos)
    children.append({"object": "block", "type": "callout",
                     "callout": {"icon": {"type": "emoji", "emoji": "📊"},
                                 "rich_text": [{"type": "text", "text": {"content": f"This week: {release_count} releases, {hn_count} HN discussions, {active_count} repos with active development"}}]}})
    children.append({"object": "block", "type": "divider", "divider": {}})
    
    # Releases
    if releases:
        children.append({"object": "block", "type": "heading_2",
                         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🚢 Releases"}}]}})
        for rel in releases:
            name = rel.get("repo", "").split("/")[-1]
            tag = rel.get("tag", "")
            url = rel.get("url", "")
            body = trim(rel.get("body", "") or rel.get("name", ""), 140)
            text = f"{name} {tag}"
            if body:
                text += f"\n{body}"
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [
                                 {"type": "text", "text": {"content": text, "link": {"url": url} if url else None}}
                             ]}})
        children.append({"object": "block", "type": "divider", "divider": {}})
    
    # Active Development by category
    if active_repos:
        children.append({"object": "block", "type": "heading_2",
                         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🔨 Active Development"}}]}})
        by_category = {}
        for r in active_repos:
            cat = r.get("category", "Ecosystem")
            by_category.setdefault(cat, []).append(r)
        
        for cat, repos in sorted(by_category.items()):
            emoji = notion_category_emoji(cat)
            children.append({"object": "block", "type": "heading_3",
                             "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"{emoji} {cat}"}}]}})
            for repo in repos[:4]:
                name = repo.get("name", repo["repo"].split("/")[-1])
                url = repo.get("url", "")
                commits = repo.get("commit_count", 0)
                latest = trim(repo.get("commits", [{}])[0].get("message", ""), 80)
                text = f"{name} — {commits} commits"
                if latest:
                    text += f"\n→ {latest}"
                children.append({"object": "block", "type": "bulleted_list_item",
                                 "bulleted_list_item": {"rich_text": [
                                     {"type": "text", "text": {"content": text, "link": {"url": url} if url else None}}
                                 ]}})
        children.append({"object": "block", "type": "divider", "divider": {}})
    
    # HN Discussions
    if hn_items:
        children.append({"object": "block", "type": "heading_2",
                         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🔶 HN Discussions"}}]}})
        for story in hn_items[:5]:
            title = story.get("title", "")
            points = story.get("points", 0)
            hn_url = story.get("hn_url", "")
            text = f"{title} ({points} pts)"
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [
                                 {"type": "text", "text": {"content": text, "link": {"url": hn_url} if hn_url else None}}
                             ]}})
        children.append({"object": "block", "type": "divider", "divider": {}})
    
    # Security
    children.append({"object": "block", "type": "heading_2",
                     "heading_2": {"rich_text": [{"type": "text", "text": {"content": "⚠️ Security"}}]}})
    if security["items"]:
        for alert in security["items"][:3]:
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [
                                 {"type": "text", "text": {"content": trim(alert.get("title", ""), 120)}}
                             ]}})
    else:
        children.append({"object": "block", "type": "paragraph",
                         "paragraph": {"rich_text": [{"type": "text", "text": {"content": "✅ All clear this week."}}]}})
    children.append({"object": "block", "type": "divider", "divider": {}})
    
    # Web News
    if news:
        children.append({"object": "block", "type": "heading_2",
                         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📰 Around the Web"}}]}})
        for article in news[:5]:
            title = article.get("title", "")
            url = article.get("url", "")
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [
                                 {"type": "text", "text": {"content": title, "link": {"url": url} if url else None}}
                             ]}})
    
    # Footer
    children.append({"object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"\n—\nGenerated by ClawBack 🦀 | {datetime.now().strftime('%Y-%m-%d')}"}}]}})
    
    # Create page via Notion API
    try:
        import ssl
        url = "https://api.notion.com/v1/pages"
        data = {
            "parent": {"page_id": NOTION_PAGE_ID},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": page_title}}]}
            },
            "children": children
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=HEADERS, method="POST")
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read())
            page_id = result.get("id")
            print(f"✅ Created Notion page: {page_title} ({page_id})", file=sys.stderr)
            return page_id
    except Exception as e:
        print(f"❌ Failed to create Notion page: {e}", file=sys.stderr)
        return None


def send_to_telegram(message: str, channel_id: str, bot_token: str) -> bool:
    """Send a message to a Telegram channel."""
    if not channel_id or not bot_token:
        print("⚠️  Missing channel_id or bot_token, cannot send", file=sys.stderr)
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({
        "chat_id": channel_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }).encode()
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            if result.get("ok"):
                print("✅ Message sent to Telegram", file=sys.stderr)
                return True
            else:
                print(f"❌ Telegram error: {result}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"❌ Failed to send: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate Claw Ecosystem digests")
    parser.add_argument("--mode", choices=["daily", "weekly", "discover", "summary"], required=True,
                        help="Digest mode: daily, weekly, discover (new projects), or summary")
    parser.add_argument("--notion", action="store_true", help="Also create a Notion page for weekly digest")
    parser.add_argument("--channel-id", help="Telegram channel ID")
    parser.add_argument("--bot-token", help="Telegram bot token")
    parser.add_argument("--send", action="store_true", help="Actually send to Telegram")
    parser.add_argument("--dry-run", action="store_true", help="Just print, don't send (default)")
    
    args = parser.parse_args()
    
    # Get credentials from args or env
    channel_id = args.channel_id or os.environ.get("TELEGRAM_CHANNEL_ID")
    bot_token = args.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    brave_key = os.environ.get("BRAVE_API_KEY", "")
    security_state = load_security_state()
    
    if args.mode == "daily":
        items = load_new_items()
        news = search_ecosystem_news(brave_key)
        message = format_daily_digest(items, news, security_state)
    elif args.mode == "weekly":
        items = load_new_items()
        news = search_ecosystem_news(brave_key)
        message = format_weekly_digest(items, news)
    elif args.mode == "discover":
        discoveries = load_discoveries()
        message = format_discovery_digest(discoveries)
    elif args.mode == "summary":
        message = format_ecosystem_summary()
    else:
        print(f"Unknown mode: {args.mode}", file=sys.stderr)
        sys.exit(1)
    
    if args.mode == "weekly" and args.notion:
        items = load_new_items()
        news = search_ecosystem_news(brave_key)
        notion_page_id = create_weekly_notion_page(items, news)
        if notion_page_id:
            print(f"\n🔗 Notion page: https://sovs.notion.site/{notion_page_id.replace('-', '')}")
    
    # Output the message
    print(message)
    
    # Send if requested
    if args.send and not args.dry_run:
        if channel_id and bot_token:
            send_to_telegram(message, channel_id, bot_token)
        else:
            print("\n⚠️  --send specified but missing channel-id or bot-token", 
                  file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
