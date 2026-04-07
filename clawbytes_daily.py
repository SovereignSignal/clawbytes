#!/usr/bin/env python3
"""Deterministic daily ClawBytes generator.

Runs the underlying monitors, ranks the strongest ecosystem items, formats a concise
link-rich Telegram post, and optionally sends it directly to the ClawBytes channel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", "/home/ubuntu-openclaw/.openclaw/workspace"))
MEMORY = WORKSPACE / "memory"
CREDS = WORKSPACE / "CREDS.md"

CHANNEL_ID = "-1003850321704"

STATE_FILES = {
    "rss": MEMORY / "claw-rss-state.json",
    "reddit": MEMORY / "claw-reddit-state.json",
    "moltbook": MEMORY / "claw-moltbook-state.json",
    "security": MEMORY / "claw-security-state.json",
    "ecosystem_items": MEMORY / "claw-ecosystem-new-items.json",
}

REPO_PRIORITY = {
    "openclaw": 100,
    "hermes": 90,
    "ironclaw": 85,
    "moltis": 82,
    "nanoclaw": 80,
    "openfang": 78,
    "picoclaw": 74,
    "codex": 68,
}

REPO_WHY = {
    "openclaw": "operator-facing runtime/infrastructure change, not filler patch churn",
    "hermes": "relevant agent runtime movement in the Claws-adjacent stack",
    "ironclaw": "meaningful ecosystem competition / architecture signal",
    "moltis": "ecosystem tooling movement worth tracking",
    "nanoclaw": "strong ecosystem project velocity",
    "openfang": "security/self-hosting adjacent project movement",
    "picoclaw": "local-first toolchain movement around Claw workflows",
    "codex": "adjacent coding-agent pressure on Claw UX and speed",
}

CATEGORY_NAMES = {
    'release': 'Ship',
    'security': 'Watch',
    'read': 'Read',
    'community': 'Community',
    'clawhub': 'ClawHub',
    'momentum': 'Momentum',
    'moltbook': 'Side Signal',
}

CATEGORY_EMOJIS = {
    'release': ['⚙️', '🛠️', '🚀', '📦', '🧰', '🦾'],
    'security': ['🚨', '🛡️', '⚠️', '🚫', '🔥', '☣️'],
    'read': ['📚', '📎', '🔎', '🧠', '🧪', '🛰️'],
    'community': ['💬', '🗣️', '📣', '👀', '🌶️', '🌀'],
    'clawhub': ['🛍️', '🧩', '🪄', '🎒', '🦀', '🪲'],
    'momentum': ['📈', '🌊', '🧲', '🛸', '📡', '✨'],
    'moltbook': ['🌊', '🫧', '🎭', '🧃', '🪩', '🦑'],
}


def read_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def cred(section: str, key: str) -> str:
    # First check environment variables
    env_key = f"{section.upper().replace(' ', '_')}_{key.upper().replace(' ', '_')}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    # Also check common env var names
    common_keys = {
        ('ClawBytes Channel', 'Bot Token'): 'TELEGRAM_BOT_TOKEN',
        ('Telegram Bots', 'Bot Token'): 'TELEGRAM_BOT_TOKEN',
        ('GitHub API', 'Token'): 'GITHUB_TOKEN',
    }
    if (section, key) in common_keys:
        env_val = os.environ.get(common_keys[(section, key)])
        if env_val:
            return env_val
    # Then check CREDS.md
    text = read_text(CREDS)
    pattern = rf"## {re.escape(section)}\n(?:.*\n)*?-\s*(?:\*\*)?{re.escape(key)}(?:\*\*)?:\s*([^\n]+)"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def run(cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def recent(items: List[dict], hours: int = 48, keys: List[str] | None = None) -> List[dict]:
    cutoff = now_utc() - timedelta(hours=hours)
    out = []
    for item in items:
        dt = None
        for key in (keys or ["found_at", "published"]):
            dt = parse_dt(item.get(key, ""))
            if dt:
                break
        if dt and dt >= cutoff:
            out.append(item)
    return out


def github_json(url: str) -> dict | list:
    token = cred("GitHub API", "Token")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ClawBytes/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def brave_search(query: str, count: int = 5) -> List[dict]:
    key = cred("Brave Search API", "API Key") or cred("Brave Search", "API Key")
    if not key:
        return []
    qs = urlencode({"q": query, "count": count, "freshness": "pd"})
    req = Request(
        f"https://api.search.brave.com/res/v1/web/search?{qs}",
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    try:
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("web", {}).get("results", [])
    except Exception:
        return []


@dataclass
class Candidate:
    title: str
    url: str
    why: str
    score: float
    category: str


def trim_text(text: str, length: int = 110) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def emoji_for(category: str, seed: str = '') -> str:
    pool = CATEGORY_EMOJIS.get(category, ['📌'])
    day_key = datetime.now().strftime('%Y-%m-%d')
    digest = hashlib.sha256(f'{category}|{seed}|{day_key}'.encode('utf-8')).hexdigest()
    idx = int(digest[:8], 16) % len(pool)
    return pool[idx]


def topic_from_title(title: str) -> str:
    low = (title or '').lower()
    if any(x in low for x in ['security', 'sandbox', 'unsafe', 'advisory', 'vulnerab', 'permission']):
        return 'security and safety risk'
    if any(x in low for x in ['expensive', 'cost', 'token', 'spend', 'cheap']):
        return 'cost pressure and pricing pain'
    if any(x in low for x in ['use case', 'use cases', 'usefulness', 'workflow', 'real workflows']):
        return 'real-world use cases and product fit'
    if any(x in low for x in ['claude code', 'codex', 'gemma', 'model', 'open router', 'openrouter']):
        return 'model choice and competitive pressure'
    if any(x in low for x in ['memory', 'drift', 'focus', 'autonomous']):
        return 'agent reliability and control'
    return 'user sentiment and operator pain points'


def read_summary(feed: str, title: str) -> str:
    low = f'{feed} {title}'.lower()
    if 'simon willison' in low:
        return 'Simon Willison on agent engineering and tooling tradeoffs; good context beyond release notes.'
    if 'security' in low or 'supply chain' in low:
        return 'Security-focused read on risks showing up around agent tooling and deployment.'
    return 'Worthwhile read for how current agent tooling maps to actual operator workflows.'


def release_summary(feed: str, title: str) -> str:
    repo = repo_name_from_feed(feed)
    if repo == 'openclaw':
        return 'Latest official OpenClaw release; scan the changelog for operator-facing runtime and workflow changes.'
    if repo == 'hermes':
        return 'Fresh Hermes Agent release; useful for tracking adjacent runtime movement in the ecosystem.'
    if repo == 'codex':
        return 'New Codex release; relevant as competitive pressure on Claw UX, speed, and developer expectations.'
    return f'New {repo.title()} release; worth a quick scan for ecosystem movement and implementation ideas.'


def security_summary(severity: str) -> str:
    sev = (severity or 'WATCHING').upper()
    if sev == 'HIGH':
        return 'High-severity advisory in the watched ecosystem; read impact and remediation before copying the pattern.'
    if sev == 'CRITICAL':
        return 'Critical advisory in the watched ecosystem; this is immediate-stop-and-check material.'
    return 'Tracked security advisory in the ecosystem; worth logging even if it is not today’s lead risk.'


def community_summary(subreddit: str, title: str, score: int, comments: int) -> str:
    topic = topic_from_title(title)
    return f'High-engagement r/{subreddit} thread on {topic} ({score} upvotes / {comments} comments).'


def clawhub_summary(title: str, description: str) -> str:
    if description:
        return trim_text(description, 110)
    return f'ClawHub skill listing worth a quick scan: {trim_text(title, 80)}.'


def moltbook_summary(title: str, karma: int, comments: int) -> str:
    topic = topic_from_title(title)
    return f'Secondary community signal on {topic} from Moltbook ({karma} karma / {comments} comments).'


def repo_name_from_feed(feed: str) -> str:
    low = feed.lower()
    for name in REPO_PRIORITY:
        if name in low:
            return name
    return low.split()[0] if low else "misc"


def select_releases(rss_state: dict) -> List[Candidate]:
    items = recent(rss_state.get("foundItems", []), 96, keys=["published", "found_at"])
    releases = []
    for item in items:
        feed = item.get("feed", "")
        if "releases" not in feed.lower():
            continue
        title = item.get("title", "")
        url = item.get("link", "")
        low_title = title.lower()
        if any(x in low_title for x in ["beta", "nightly", "staging"]):
            continue
        repo = repo_name_from_feed(feed)
        score = REPO_PRIORITY.get(repo, 50)
        dt = parse_dt(item.get("published", "") or item.get("found_at", ""))
        if dt:
            age_hours = max(0.0, (now_utc() - dt).total_seconds() / 3600)
            score += max(0.0, 96 - age_hours) / 8
        why = release_summary(feed, title)
        releases.append(Candidate(title=title, url=url, why=why, score=score, category="release"))
    seen, out = set(), []
    for c in sorted(releases, key=lambda x: x.score, reverse=True):
        if c.url and c.url not in seen:
            seen.add(c.url)
            out.append(c)
    return out[:3]


def select_reddit(reddit_state: dict) -> List[Candidate]:
    items = recent(reddit_state.get("foundItems", []), 72)
    scored = []
    for item in items:
        title = item.get("title", "")
        url = item.get("url", "")
        score = int(item.get("score", 0)) + min(int(item.get("comments", 0)), 150) * 0.6
        if item.get("subreddit") == "openclaw":
            score += 15
        if score < 25:
            continue
        dt = parse_dt(item.get('found_at',''))
        if dt:
            age_hours = max(0.0, (now_utc() - dt).total_seconds() / 3600)
            score += max(0.0, 72 - age_hours) / 10
        why = community_summary(item.get('subreddit', 'openclaw'), title, int(item.get('score', 0)), int(item.get('comments', 0)))
        scored.append(Candidate(title=f"r/{item.get('subreddit','openclaw')}: {title}", url=url, why=why, score=score, category="community"))
    seen, out = set(), []
    for c in sorted(scored, key=lambda x: x.score, reverse=True):
        if c.url not in seen:
            seen.add(c.url)
            out.append(c)
    return out[:3]


def select_moltbook(moltbook_state: dict) -> List[Candidate]:
    items = recent(moltbook_state.get("foundItems", []), 72)
    out = []
    for item in items:
        title = item.get("title", "")
        url = item.get("url", "")
        karma = int(item.get("karma", 0) or 0)
        comments = int(item.get("comments", 0) or 0)
        engagement = karma + comments
        if engagement < 120:
            continue
        # Moltbook is secondary/community signal only.
        # It should appear on very strong posts or slower news days, not drive the daily.
        score = 8 + min(engagement / 20, 18)
        why = moltbook_summary(title, karma, comments)
        out.append(Candidate(title=title, url=url, why=why, score=score, category="moltbook"))
    return sorted(out, key=lambda x: x.score, reverse=True)[:1]


def select_reads(rss_state: dict) -> List[Candidate]:
    items = recent(rss_state.get("foundItems", []), 72)
    out = []
    for item in items:
        feed = item.get("feed", "")
        if "releases" in feed.lower():
            continue
        title = item.get("title", "")
        url = item.get("link", "")
        if not url:
            continue
        low = f"{feed} {title}".lower()
        keep = False
        why = read_summary(feed, title)
        score = 18
        if "simon willison" in low and any(k in low for k in ["agent", "security", "mcp", "supply chain"]):
            keep = True
            score = 35
        elif any(k in low for k in ["openclaw", "claude code", "codex", "agent", "security"]):
            keep = True
            score = 25
        if keep:
            dt = parse_dt(item.get('published','') or item.get('found_at',''))
            if dt:
                age_hours = max(0.0, (now_utc() - dt).total_seconds() / 3600)
                score += max(0.0, 72 - age_hours) / 12
            out.append(Candidate(title=title, url=url, why=why, score=score, category="read"))
    seen, dedup = set(), []
    for c in sorted(out, key=lambda x: x.score, reverse=True):
        if c.url not in seen:
            seen.add(c.url)
            dedup.append(c)
    return dedup[:2]


def select_security(security_state: dict) -> List[Candidate]:
    alerts = recent(security_state.get("alerts", []), 24)
    if not alerts:
        return [Candidate(
            title="OpenClaw advisories",
            url="https://github.com/openclaw/openclaw/security/advisories",
            why="No fresh advisory beat the bar, so this is the standing place to check current OpenClaw security notices.",
            score=22,
            category="security",
        )]
    top = []
    sev_weight = {"CRITICAL": 100, "HIGH": 80, "WATCHING": 55, "MEDIUM": 40}
    for a in alerts:
        top.append(Candidate(
            title=a.get("title", "Security advisory"),
            url=a.get("url", "https://github.com/openclaw/openclaw/security/advisories"),
            why=security_summary(a.get('severity', 'WATCHING')),
            score=sev_weight.get(a.get("severity", "MEDIUM"), 40),
            category="security",
        ))
    return sorted(top, key=lambda x: x.score, reverse=True)[:2]


def select_clawhub() -> List[Candidate]:
    results = brave_search('site:clawhub.ai (skill OR skills) openclaw', 5)
    out = []
    for r in results:
        url = r.get('url','')
        title = r.get('title','')
        desc = r.get('description','')
        if 'clawhub.ai' not in url:
            continue
        why = clawhub_summary(title, desc)
        out.append(Candidate(title=title, url=url, why=why, score=28, category='clawhub'))
    seen, dedup = set(), []
    for c in out:
        if c.url not in seen:
            seen.add(c.url)
            dedup.append(c)
    return dedup[:1]


def select_momentum() -> List[Candidate]:
    repos = [
        ('openclaw/openclaw', 'OpenClaw repo'),
        ('qwibitai/nanoclaw', 'NanoClaw repo'),
        ('nearai/ironclaw', 'IronClaw repo'),
    ]
    out = []
    for repo, title in repos:
        try:
            data = github_json(f'https://api.github.com/repos/{repo}')
            stars = int(data.get('stargazers_count', 0))
            url = data.get('html_url', f'https://github.com/{repo}')
            why = f'{stars:,} GitHub stars of ecosystem gravity; useful context on where attention is concentrating.'
            score = 18 + min(stars / 1000, 20)
            out.append(Candidate(title=title, url=url, why=why, score=score, category='momentum'))
        except Exception:
            continue
    return sorted(out, key=lambda x: x.score, reverse=True)[:1]


def choose_items() -> List[Candidate]:
    rss = load_json(STATE_FILES['rss'])
    reddit = load_json(STATE_FILES['reddit'])
    moltbook = load_json(STATE_FILES['moltbook'])
    security = load_json(STATE_FILES['security'])

    buckets = {
        'release': select_releases(rss),
        'community': select_reddit(reddit),
        'read': select_reads(rss),
        'security': select_security(security),
        'clawhub': select_clawhub(),
        'momentum': select_momentum(),
        'moltbook': select_moltbook(moltbook),
    }

    picked: List[Candidate] = []
    picked_counts: Dict[str, int] = {}

    def take(candidate: Candidate) -> None:
        picked.append(candidate)
        picked_counts[candidate.category] = picked_counts.get(candidate.category, 0) + 1

    def can_take(candidate: Candidate) -> bool:
        caps = {
            'release': 2,
            'community': 2,
            'security': 1,
            'read': 1,
            'clawhub': 1,
            'momentum': 1,
            'moltbook': 1,
        }
        return picked_counts.get(candidate.category, 0) < caps.get(candidate.category, 1)

    # deterministic breadth first, with less tolerance for filler
    order = ['release', 'security', 'read', 'community', 'clawhub']
    for category in order:
        if buckets[category]:
            top = sorted(buckets[category], key=lambda x: x.score, reverse=True)[0]
            if can_take(top):
                take(top)

    # Momentum is context, not a must-have. Only include it on thinner days.
    if buckets['momentum'] and len(picked) < 4:
        top_momentum = sorted(buckets['momentum'], key=lambda x: x.score, reverse=True)[0]
        if can_take(top_momentum):
            take(top_momentum)

    # Moltbook is opt-in only if it's very strong or the day is otherwise thin.
    if buckets['moltbook']:
        top_molt = buckets['moltbook'][0]
        if top_molt.score >= 20 or len(picked) < 4:
            if can_take(top_molt):
                take(top_molt)

    # Fill remaining slots with the best remaining items, while capping category spam.
    picked_urls = {p.url for p in picked}
    remaining = []
    for items in buckets.values():
        for c in items:
            if c.url not in picked_urls:
                remaining.append(c)
    for c in sorted(remaining, key=lambda x: x.score, reverse=True):
        if len(picked) >= 5:
            break
        if c.url not in picked_urls and can_take(c):
            take(c)
            picked_urls.add(c.url)
    return picked[:5]


def build_lead(items: List[Candidate]) -> str:
    if not items:
        return 'Quiet day — no strong ecosystem signal cleared the bar.'
    top = items[0]
    if top.category == 'release':
        return f"Lead signal: {top.title} shipped and is the clearest thing that may change real operator or builder behavior today."
    if top.category == 'security':
        return f"Lead signal: security tops the board today, with {top.title} as the main thing worth watching."
    return f"Lead signal: {top.title} is today’s strongest item once you strip out the noise."


def section_label(category: str) -> str:
    return CATEGORY_NAMES.get(category, 'Signal')


def take_line(items: List[Candidate]) -> str:
    if not items:
        return 'Quiet tape today. Better to wait than post filler.'

    top = items[0]
    category = top.category
    if category == 'release':
        return 'Release-driven day. Shipments matter more than discourse.'
    if category == 'security':
        return 'Security-driven day. Check the risk before chasing the hype.'
    if category == 'community':
        return 'Community sentiment is the real signal today. Pay attention to what users are actually fighting about.'
    if category == 'read':
        return 'Reading day. The best signal is interpretation, not shipping velocity.'
    return 'Mixed tape today — enough movement to watch, not enough to overstate.'


def format_post(items: List[Candidate]) -> str:
    date = datetime.now().strftime('%a %b %-d, %Y')
    lines = [f"🦀 <b>ClawBytes Daily — {date}</b>", "", html_escape(build_lead(items)), ""]

    grouped: Dict[str, List[Candidate]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    section_order = ['release', 'security', 'read', 'community', 'clawhub', 'momentum', 'moltbook']
    for category in section_order:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        lines.append(f"<b>{emoji_for(category, category_items[0].title)} {section_label(category)}</b>")
        for c in category_items:
            title = html_escape(c.title)
            why = html_escape(c.why)
            lines.append(f"• <a href=\"{c.url}\">{title}</a>")
            lines.append(f"  {why}")
        lines.append("")

    lines.append(f"<b>Take</b>")
    lines.append(html_escape(take_line(items)))
    return "\n".join(lines)


def topic_message(item: Candidate, index: int, total: int) -> str:
    emoji = emoji_for(item.category, item.title)
    return "\n".join([
        f"{emoji} <b>ClawBytes — {html_escape(section_label(item.category))}</b>",
        f"<b>{index}/{total}</b>",
        "",
        f"<a href=\"{item.url}\">{html_escape(item.title)}</a>",
        "",
        html_escape(item.why),
    ])


def format_topic_posts(items: List[Candidate]) -> List[str]:
    return [topic_message(item, idx, len(items)) for idx, item in enumerate(items, start=1)]


def html_escape(s: str) -> str:
    return (
        s.replace('&', '&amp;')
         .replace('<', '&lt;')
         .replace('>', '&gt;')
         .replace('"', '&quot;')
    )


def send_telegram(message: str) -> None:
    token = cred('ClawBytes Channel', 'Bot Token') or cred('Telegram Bots', 'Bot Token')
    if not token:
        raise RuntimeError('Telegram bot token not found')
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({
        'chat_id': CHANNEL_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }).encode('utf-8')
    req = Request(url, data=data)
    with urlopen(req, timeout=20) as r:
        json.loads(r.read().decode('utf-8'))


def send_telegram_messages(messages: List[str]) -> None:
    for idx, message in enumerate(messages):
        send_telegram(message)
        if idx < len(messages) - 1:
            time.sleep(1.2)


def run_monitors() -> None:
    cmds = [
        'python3 scripts/claw-rss-monitor.py',
        'python3 scripts/claw-reddit-monitor.py',
        'python3 scripts/claw-moltbook-monitor.py',
        'python3 scripts/claw-security-monitor.py --quiet',
        'bash scripts/claw-ecosystem-monitor.sh --mode check',
    ]
    for cmd in cmds:
        # In container, WORKSPACE is /app; locally it's the workspace path
        p = run(f'cd {shlex.quote(str(WORKSPACE))} && {cmd}', timeout=300)
        if p.returncode != 0:
            print(f"⚠️ Monitor returned non-zero: {cmd}")
            print(p.stdout)
            print(p.stderr, file=sys.stderr)
            # Don't raise - allow other monitors to run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--skip-monitors', action='store_true')
    ap.add_argument('--per-topic', action='store_true', help='Output/send one update per topic instead of a single digest')
    args = ap.parse_args()

    if not args.skip_monitors:
        run_monitors()
    items = choose_items()
    if args.per_topic:
        posts = format_topic_posts(items)
        print("\n\n---\n\n".join(posts))
    else:
        post = format_post(items)
        print(post)
    if args.send:
        if args.per_topic:
            send_telegram_messages(posts)
        else:
            send_telegram(post)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
