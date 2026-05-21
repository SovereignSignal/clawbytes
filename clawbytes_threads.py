#!/usr/bin/env python3
"""ClawBytes category-thread collector/publisher.

Purpose:
- Build a shared backlog from existing source monitors
- Publish one category bundle at a time (Ship / Watch / Read / Community)
- Preserve cross-category discoveries by queueing them for later

Examples:
  python3 scripts/clawbytes_threads.py collect
  python3 scripts/clawbytes_threads.py status
  python3 scripts/clawbytes_threads.py preview --category ship
  python3 scripts/clawbytes_threads.py publish --category watch --send
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# Notion Claws signal integration (must import before WORKSPACE usage).
# claw_notion_signals lives in scripts/, so both dirs go on sys.path.
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
from claw_notion_signals import enrich_ship_with_notion, find_notion_signals, to_backlog_candidates

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY = WORKSPACE / "memory"
CREDS = WORKSPACE / "CREDS.md"

BACKLOG_FILE = MEMORY / "clawbytes-backlog.json"
THREAD_STATE_FILE = MEMORY / "clawbytes-thread-state.json"

CHANNEL_ID = "-100REDACTED"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

CATEGORY_META = {
    "ship": {
        "label": "Ship",
        "emoji": "⚙️",
        "ttl_hours": 168,  # 7 days (was 96)
        "default_limit": 4,
        "intro": "Fresh releases and product movement worth scanning.",
        "windows": [9, 18],
        "min_items": [1, 3],
        "min_top_score": [85, 95],
    },
    "watch": {
        "label": "Watch",
        "emoji": "🚨",
        "ttl_hours": 168,  # 7 days (was 120)
        "default_limit": 3,
        "intro": "Security, breakage, and risk signals worth watching closely.",
        "windows": [10, 19],
        "min_items": [1, 2],
        "min_top_score": [25, 55],
    },
    "read": {
        "label": "Read",
        "emoji": "📚",
        "ttl_hours": 168,  # 7 days (was 96)
        "default_limit": 3,
        "intro": "Context pieces worth the click, not just headline noise.",
        "windows": [12, 20],
        "min_items": [1, 2],
        "min_top_score": [15, 30],
    },
    "community": {
        "label": "Community",
        "emoji": "💬",
        "ttl_hours": 96,  # 4 days (was 72)
        "default_limit": 4,
        "intro": "What users and builders are actually talking about right now.",
        "windows": [11, 17],
        "min_items": [2, 3],
        "min_top_score": [25, 60],
    },
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

def _load_dynamic_subreddits():
    """Load dynamically discovered subreddits."""
    dynamic_path = MEMORY / "clawbytes-dynamic-feeds.json"
    extra = set()
    if dynamic_path.exists():
        try:
            dynamic = json.loads(dynamic_path.read_text())
            for sub in dynamic.get("subreddits", []):
                extra.add(sub.get("name", "").lower())
        except Exception:
            pass
    return extra


ALLOWED_SUBREDDITS = {"openclaw", "selfhosted", "localllama", "machinelearning", "artificial", "homelab", "singularity"} | _load_dynamic_subreddits()

# Reddit topics that belong in Read, not Community
READ_REDDIT_TERMS = [
    "how to", "tutorial", "guide", "workflow", "setup", "config",
    "comparison", "vs", "benchmark", "review", "deep dive",
    "architecture", "internals", "explained", "behind the",
    "what i learned", "lessons", "experience report",
]

# Reddit topics that belong in Watch
WATCH_REDDIT_TERMS = [
    "broken", "bug", "error", "crash", "vulnerability", "security",
    "exploit", "outage", "degraded", "regression", "broke",
    "unsafe", "leak", "injection",
]

# LLM enrichment settings
LLM_URL = os.environ.get("CLAWBYTES_LLM_URL", "https://llm.example.com/v1")
LLM_MODEL = os.environ.get("CLAWBYTES_LLM_MODEL", "gemma4:31b-cloud")
LLM_API_KEY = os.environ.get("CLAWBYTES_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

SECURITY_TERMS = [
    "security", "advisory", "vulnerability", "cve", "sandbox", "unsafe",
    "injection", "exploit", "supply chain", "permission", "credential",
]

READ_TERMS = [
    "agent", "agentic", "workflow", "memory", "mcp", "security",
    "claude code", "codex", "openclaw",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_day_key(dt: Optional[datetime] = None) -> str:
    return (dt or now_local()).strftime("%Y-%m-%d")


def read_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def load_json(path: Path, default):
    if path.exists():
        text = path.read_text()
        if text.strip():
            return json.loads(text)
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic rename


def cred(section: str, key: str) -> str:
    # Env vars take precedence over CREDS.md so Railway deploys work without
    # a CREDS.md file. Generic SECTION_KEY form first, then a small explicit map
    # for the well-known cases that don't match the generic form.
    env_key = f"{section.upper().replace(' ', '_')}_{key.upper().replace(' ', '_')}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    common_keys = {
        ('ClawBytes Channel', 'Bot Token'): 'TELEGRAM_BOT_TOKEN',
        ('Telegram Bots', 'Bot Token'): 'TELEGRAM_BOT_TOKEN',
        ('GitHub API', 'Token'): 'GITHUB_TOKEN',
    }
    env_name = common_keys.get((section, key))
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
    text = read_text(CREDS)
    pattern = rf"## {re.escape(section)}\n(?:.*\n)*?-\s*(?:\*\*)?{re.escape(key)}(?:\*\*)?:\s*([^\n]+)"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def trim(text: str, length: int = 120) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def ensure_files() -> None:
    # Only create files if they don't exist — avoid unnecessary round-trips
    # that risk corrupting large JSON files on concurrent writes or crashes.
    if not BACKLOG_FILE.exists() or BACKLOG_FILE.stat().st_size == 0:
        save_json(BACKLOG_FILE, {"items": []})
    if not THREAD_STATE_FILE.exists() or THREAD_STATE_FILE.stat().st_size == 0:
        save_json(
            THREAD_STATE_FILE,
            {
                "seenSourceKeys": [],
                "postedBacklogIds": [],
                "postedUrls": [],
                "lastCollectedAt": None,
                "lastPublishedAt": {},
                "publishLog": [],
            },
        )


def source_key(kind: str, raw_id: str, url: str) -> str:
    return f"{kind}:{raw_id or url}"


def backlog_id(url: str, title: str) -> str:
    return hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()[:16]


def repo_name_from_feed(feed: str) -> str:
    low = (feed or "").lower()
    for name in REPO_PRIORITY:
        if name in low:
            return name
    return low.split()[0] if low else "misc"


def display_repo_name(repo: str) -> str:
    return {
        "openclaw": "OpenClaw",
        "hermes": "Hermes Agent",
        "ironclaw": "IronClaw",
        "moltis": "Moltis",
        "nanoclaw": "NanoClaw",
        "openfang": "OpenFang",
        "picoclaw": "PicoClaw",
        "codex": "Codex",
    }.get(repo, repo.title())


def normalize_release_title(repo: str, title: str) -> str:
    clean = (title or "").strip()
    repo_label = display_repo_name(repo)

    if not clean:
        return repo_label

    low = clean.lower()

    # OpenClaw-style date versions
    datever = re.search(r"\b(20\d{2}\.\d{1,2}\.\d{1,2}(?:-\d+)?)\b", clean)
    if datever:
        return f"{repo_label} {datever.group(1)}"

    # Semantic versions with optional prerelease tails
    semver = re.search(r"\bv?(\d+\.\d+\.\d+(?:[-.]?(?:alpha|beta|rc)[-.]?\d+)?)\b", clean, re.IGNORECASE)
    if semver:
        version = semver.group(1)
        version = re.sub(r"(?i)(alpha|beta|rc)\.?", r"\1.", version)
        version = version.replace("..", ".").rstrip(".")
        return f"{repo_label} {version}"

    # Fallback for titles that already mention the repo but still need cleanup
    if repo_label.lower() in low:
        normalized = clean
        for alias in [repo.lower(), repo_label.lower()]:
            normalized = re.sub(rf"\b{re.escape(alias)}\b", "", normalized, flags=re.IGNORECASE).strip(" -–:()")
        return f"{repo_label} {normalized}".strip()

    if re.match(r"^(v?\d+[\w.\-]*(?:\s*[-–]\s*\d{4}-\d{2}-\d{2})?)$", clean):
        return f"{repo_label} {clean}"

    if clean.startswith("0.") or clean.startswith("1.") or clean.startswith("2."):
        return f"{repo_label} {clean}"

    return clean


def age_score(dt: Optional[datetime], max_hours: int) -> float:
    if not dt:
        return 0.0
    hours = max(0.0, (now_utc() - dt).total_seconds() / 3600)
    return max(0.0, max_hours - hours)


def is_minor_release(title: str) -> bool:
    """Check if a release is minor (alpha, patch, hotfix, etc)."""
    low = title.lower()
    # Alpha/preview releases
    if any(x in low for x in ["alpha", "preview", "nightly", "canary", "dev", "experimental"]):
        return True
    # Patch versions (0.x.y where x stays same, or .z increments)
    if re.search(r"v?0\.\d+\.\d+$", low) and not re.search(r"v?0\.0\.1", low):
        return True
    # Hotfix/patch keywords
    if any(x in low for x in ["hotfix", "patch", "fix", "minor"]):
        return True
    # Date-based versioning (e.g. 20260413.04, 20260409.01) — treat .XX suffix as patch
    if re.search(r"20\d{6}\.\d+", low):
        return True
    return False


def classify_rss(item: dict) -> Optional[dict]:
    feed = item.get("feed", "")
    title = item.get("title", "")
    url = item.get("link", "")
    if not url:
        return None
    dt = parse_dt(item.get("published", "") or item.get("found_at", ""))
    low = f"{feed} {title}".lower()

    if "releases" in feed.lower():
        if any(x in low for x in ["beta", "nightly", "staging", "alpha"]):
            return None
        # Skip chore/ci/internal release titles
        if any(x in low for x in ["chore:", "ci:", "build:", "internal"]):
            return None
        repo = repo_name_from_feed(feed)
        display_title = normalize_release_title(repo, title)
        base_score = REPO_PRIORITY.get(repo, 50)
        # Penalize minor releases heavily
        if is_minor_release(title):
            base_score = max(20, base_score - 40)
        score = base_score + age_score(dt, 96) / 8
        summary = "New release" if repo == "openclaw" else f"New {repo.title()} release"
        return {
            "primaryCategory": "ship",
            "categories": ["ship"],
            "score": round(score, 2),
            "summary": summary,
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["ship"]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": display_title,
        }

    if any(term in low for term in READ_TERMS):
        categories = ["read"]
        if any(term in low for term in SECURITY_TERMS):
            categories = ["watch", "read"]
        score = 38 + age_score(dt, 96) / 10 + (15 if item.get("high_signal") else 5)
        summary = richer_read_summary(title, feed)
        primary = categories[0]
        return {
            "primaryCategory": primary,
            "categories": categories,
            "score": round(score, 2),
            "summary": summary,
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[primary]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": title,
        }

    return None


def title_topic(title: str) -> str:
    """Return a short, specific topic tag for Reddit community items."""
    low = (title or "").lower()
    if any(x in low for x in ["security", "unsafe", "sandbox", "permission", "api keys"]):
        return "security concerns"
    if any(x in low for x in ["thanks", "anthropic", "gratitude"]):
        return "anthropic sentiment"
    if any(x in low for x in ["free", "cheap", "expensive", "token", "cost", "spend", "budget", "pricing"]):
        return "cost and access"
    if any(x in low for x in ["use case", "usefulness", "workflow", "real workflows"]):
        return "use case fit"
    if any(x in low for x in ["best model", "which model", "model for", "claude code", "codex", "gemma"]):
        return "model selection"
    if "model" in low:
        return "model discussion"
    return "community signal"


def richer_read_summary(title: str, feed: str) -> str:
    """Return very short summary (10 words max)."""
    low = f"{title} {feed}".lower()
    
    # Feed-specific defaults
    if "simon willison" in low:
        return "Agent engineering insights"
    if "interconnects" in feed.lower():
        return "Model analysis"
    if "latent space" in feed.lower():
        return "Industry analysis"
    if "ai snake oil" in feed.lower() or "normaltech" in feed.lower():
        return "Critical AI analysis"
    if "langchain" in feed.lower():
        return "Agent framework update"
    if "huggingface" in feed.lower():
        return "Open source research"
    if "lilian weng" in feed.lower():
        return "Research deep dive"
    if "the ai edge" in feed.lower():
        return "Agent engineering"
    
    # Topic-based
    if "security" in low or "supply chain" in low:
        return "Security risks"
    if "reliability" in low or "safety" in low:
        return "Reliability research"
    if "codex" in low or "coding" in low:
        return "Code agent analysis"
    if "openclaw" in low or "claw" in low:
        return "Ecosystem insight"
    return "Worth reading"


def classify_reddit(item: dict) -> Optional[dict]:
    url = item.get("url", "")
    title = item.get("title", "")
    if not url:
        return None
    subreddit = (item.get("subreddit", "") or "").lower()
    if subreddit not in ALLOWED_SUBREDDITS:
        return None
    raw_score = int(item.get("score", 0))
    raw_comments = int(item.get("comments", 0))
    dt = parse_dt(item.get("found_at", ""))
    if subreddit == "openclaw":
        # Allow more r/openclaw posts in, but penalize generic questions
        if raw_score < 2 and raw_comments < 2:
            return None
        # Penalize low-signal generic question titles
        title_low = title.lower()
        generic_patterns = ["can ", "should i", "does ", "how do", "what ", "anyone ", "help"]
        is_generic = any(p in title_low for p in generic_patterns)
        if is_generic and raw_score < 10:
            score = raw_score * 0.5 + min(raw_comments, 100) * 0.3 + age_score(dt, 72) / 20
        else:
            score = raw_score + min(raw_comments, 200) * 0.6 + age_score(dt, 72) / 10
    else:
        if raw_score < 10 and raw_comments < 5:
            return None
        score = raw_score + min(raw_comments, 200) * 0.6 + age_score(dt, 72) / 10
    low = title.lower()

    # Classify into the right lane based on content
    categories = ["community"]
    if any(term in low for term in SECURITY_TERMS) or any(term in low for term in WATCH_REDDIT_TERMS):
        categories = ["watch", "community"]
    elif any(term in low for term in READ_REDDIT_TERMS):
        # Substantive discussions → Read, not Community
        categories = ["read", "community"]
        # Boost Read scores for high-comment discussions
        if raw_comments >= 30:
            score += 15

    # Shorter, more specific summary
    topic = title_topic(title)
    summary = f"{topic} ({raw_score}↑ / {raw_comments}💬)"
    primary = categories[0]
    return {
        "primaryCategory": primary,
        "categories": categories,
        "score": round(score, 2),
        "summary": summary,
        "rawScore": raw_score,
        "rawComments": raw_comments,
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[primary]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "reddit",
        "sourceName": item.get("subreddit", "reddit"),
        "sourceId": item.get("id", url),
        "url": url,
        "title": f"r/{item.get('subreddit', 'openclaw')}: {title}",
    }


def richer_community_summary(item: dict) -> str:
    """Return very short summary (5 words max)."""
    title = (item.get("title") or "").lower()
    
    if "rebuilt" in title or "leaked" in title:
        return "Builder interest spike"
    if "use case" in title:
        return "Clarity questions"
    if "overrated" in title:
        return "Expectation backlash"
    if "expensive" in title or "token" in title or "cost" in title:
        return "Cost concerns"
    if "model" in title or "claude" in title or "codex" in title:
        return "Model comparisons"
    if "security" in title or "unsafe" in title:
        return "Risk discussion"
    return "User attention signal"


def classify_security(item: dict) -> Optional[dict]:
    url = item.get("url", "")
    title = item.get("title", "") or "Security Advisory"
    if not url:
        return None
    advisory_id = item.get("advisory_id") or ""
    if (not advisory_id) and "api.github.com/repos/" in url:
        return None
    dt = parse_dt(item.get("found_at", ""))
    severity = (item.get("severity", "WATCHING") or "WATCHING").upper()
    sev_bonus = {"CRITICAL": 100, "HIGH": 80, "WATCHING": 55, "MEDIUM": 40}.get(severity, 40)
    summary = {
        "CRITICAL": "Critical advisory in the watched ecosystem; this is immediate-stop-and-check material.",
        "HIGH": "High-severity advisory in the watched ecosystem; read impact and remediation before copying the pattern.",
        "WATCHING": "Tracked security issue in the ecosystem; worth watching even if it is not today’s top risk.",
    }.get(severity, "Tracked security issue in the ecosystem; worth a quick review.")
    ttl = 120 if severity in {"CRITICAL", "HIGH"} else 72
    return {
        "primaryCategory": "watch",
        "categories": ["watch"],
        "score": round(sev_bonus + age_score(dt, ttl) / 8, 2),
        "summary": summary,
        "expiresAt": (dt or now_utc()) + timedelta(hours=ttl),
        "publishedAt": dt,
        "sourceType": "security",
        "sourceName": item.get("repo", "security"),
        "sourceId": advisory_id or item.get("url"),
        "url": url,
        "title": title,
    }


def classify_hackernews(item: dict) -> Optional[dict]:
    """Classify HN stories into lanes."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    
    raw_score = int(item.get("score", 0) or 0)
    raw_comments = int(item.get("comments", 0) or 0)
    if raw_score + raw_comments < 5:
        return None
    
    dt = parse_dt(item.get("created_at", "") or item.get("found_at", ""))
    category_hint = item.get("category_hint", "community")
    
    # Override hint based on title analysis
    low = title.lower()
    if any(t in low for t in ["security", "vulnerability", "exploit", "injection", "unsafe", "attack"]):
        primary = "watch"
        categories = ["watch", "community"]
    elif any(t in low for t in ["architecture", "framework", "how ", "why ", "protocol", "deep dive"]):
        primary = "read"
        categories = ["read", "community"]
    else:
        primary = category_hint if category_hint in ("read", "watch") else "community"
        categories = [primary, "community"]
    
    # HN scoring: points + comments bonus, with decay
    score = raw_score * 0.8 + min(raw_comments, 100) * 0.5 + age_score(dt, 72) / 10
    # Boost Watch items (security stories are high-value)
    if primary == "watch":
        score += 15
    # Boost Read items from HN (often high-quality)
    if primary == "read":
        score += 10
    
    summary = f"HN discussion ({raw_score}pts / {raw_comments} comments)"
    
    return {
        "primaryCategory": primary,
        "categories": categories,
        "score": round(score, 2),
        "summary": summary,
        "rawScore": raw_score,
        "rawComments": raw_comments,
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[primary]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "hackernews",
        "sourceName": "hackernews",
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def classify_moltbook(item: dict) -> Optional[dict]:
    karma = int(item.get("karma", 0) or 0)
    comments = int(item.get("comments", 0) or 0)
    if karma + comments < 25:
        return None
    url = item.get("url", "")
    if not url:
        return None
    dt = parse_dt(item.get("found_at", ""))
    title = item.get("title", "")
    return {
        "primaryCategory": "community",
        "categories": ["community"],
        "score": round(12 + karma + comments * 1.2 + age_score(dt, 48) / 10, 2),
        "summary": "Moltbook community signal",
        "expiresAt": (dt or now_utc()) + timedelta(hours=48),
        "publishedAt": dt,
        "sourceType": "moltbook",
        "sourceName": "moltbook",
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def backlog_item(candidate: dict) -> dict:
    created = now_utc().isoformat()
    item = {
        "id": backlog_id(candidate["url"], candidate["title"]),
        "url": candidate["url"],
        "title": candidate["title"],
        "summary": trim(candidate["summary"], 140),
        "sourceType": candidate["sourceType"],
        "sourceName": candidate["sourceName"],
        "sourceId": candidate["sourceId"],
        "primaryCategory": candidate["primaryCategory"],
        "categories": candidate["categories"],
        "score": candidate["score"],
        "publishedAt": candidate["publishedAt"].isoformat() if candidate.get("publishedAt") else None,
        "discoveredAt": created,
        "expiresAt": candidate["expiresAt"].isoformat(),
        "status": "queued",
        "postedCategories": [],
    }
    if "rawScore" in candidate:
        item["rawScore"] = candidate["rawScore"]
    if "rawComments" in candidate:
        item["rawComments"] = candidate["rawComments"]
    return item


def reddit_counts(item: dict) -> tuple[int, int]:
    raw_score = item.get("rawScore")
    raw_comments = item.get("rawComments")
    if raw_score is None or raw_comments is None:
        m = re.search(r"\((\d+) upvotes / (\d+) comments\)", item.get("summary", ""))
        if m:
            return int(m.group(1)), int(m.group(2))
        return 0, 0
    return int(raw_score), int(raw_comments)


def hydrate_item(item: dict) -> dict:
    """Return item as-is without re-summarizing."""
    return dict(item)


def is_fresh(candidate: dict) -> bool:
    expires = candidate.get("expiresAt")
    return bool(expires and expires > now_utc())


def run_monitors() -> None:
    """Run source monitors to refresh state files before collecting."""
    cmds = [
        'python3 scripts/claw-rss-monitor.py',
        'python3 scripts/claw-reddit-monitor.py',
        'python3 scripts/claw-moltbook-monitor.py',
        'python3 scripts/claw-security-monitor.py --quiet',
        'bash scripts/claw-ecosystem-monitor.sh --mode check',
    ]
    for cmd in cmds:
        p = subprocess.run(
            f'cd {shlex.quote(str(WORKSPACE))} && {cmd}',
            shell=True,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if p.returncode != 0:
            print(f"Monitor returned non-zero: {cmd}", file=sys.stderr)
            print(p.stderr, file=sys.stderr)


def collect_candidates() -> Dict[str, List[dict]]:
    rss = load_json(MEMORY / "claw-rss-state.json", {}).get("foundItems", [])
    reddit = load_json(MEMORY / "claw-reddit-state.json", {}).get("foundItems", [])
    security = load_json(MEMORY / "claw-security-state.json", {}).get("alerts", [])
    moltbook = load_json(MEMORY / "claw-moltbook-state.json", {}).get("foundItems", [])
    hackernews = load_json(MEMORY / "claw-hn-state.json", {}).get("foundItems", [])
    notion = load_json(MEMORY / "clawbytes-notion-signals.json", [])
    return {
        "rss": rss,
        "reddit": reddit,
        "security": security,
        "moltbook": moltbook,
        "hackernews": hackernews,
        "notion": notion,
    }


def collect_into_backlog() -> dict:
    ensure_files()
    
    # Refresh Notion signals before collection
    try:
        notion_signals = find_notion_signals(hours_back=48)
        notion_candidates = to_backlog_candidates(notion_signals)
        if notion_candidates:
            CACHE_FILE = MEMORY / "clawbytes-notion-signals.json"
            CACHE_FILE.write_text(json.dumps(notion_candidates, indent=2, default=str))
    except Exception:
        pass  # Notion fetch is best-effort; don't block collection on failure
    
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})

    seen_source_keys = set(state.get("seenSourceKeys", []))
    existing_ids = {item["id"] for item in backlog.get("items", [])}

    added = []
    candidates = collect_candidates()

    for item in candidates["rss"]:
        candidate = classify_rss(item)
        if not candidate:
            continue
        if not is_fresh(candidate):
            continue
        key = source_key("rss", candidate["sourceId"], candidate["url"])
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        b = backlog_item(candidate)
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    for item in candidates["reddit"]:
        candidate = classify_reddit(item)
        if not candidate:
            continue
        if not is_fresh(candidate):
            continue
        key = source_key("reddit", candidate["sourceId"], candidate["url"])
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        b = backlog_item(candidate)
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    for item in candidates["security"]:
        candidate = classify_security(item)
        if not candidate:
            continue
        if not is_fresh(candidate):
            continue
        key = source_key("security", candidate["sourceId"], candidate["url"])
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        b = backlog_item(candidate)
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    for item in candidates["moltbook"]:
        candidate = classify_moltbook(item)
        if not candidate:
            continue
        if not is_fresh(candidate):
            continue
        key = source_key("moltbook", candidate["sourceId"], candidate["url"])
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        b = backlog_item(candidate)
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    for item in candidates["hackernews"]:
        candidate = classify_hackernews(item)
        if not candidate:
            continue
        if not is_fresh(candidate):
            continue
        key = source_key("hackernews", candidate["sourceId"], candidate["url"])
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        b = backlog_item(candidate)
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    # Notion signals from Claws page edits
    for item in candidates["notion"]:
        if not item.get("url"):
            continue
        dt = parse_dt(item.get("last_edit", "") or item.get("publishedAt", ""))
        if not dt:
            continue
        # Notion signals expire quickly — fresh insight only
        if (now_utc() - dt).total_seconds() > 172800:  # 48h
            continue
        key = source_key("notion", item.get("sourceId", ""), item.get("url", ""))
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        
        # Convert notion signal to backlog item
        b = {
            "id": backlog_id(item["url"], item.get("title", "Notion signal")),
            "url": item["url"],
            "title": item.get("title", ""),
            "summary": trim(item.get("summary", "")[:140], 140),
            "sourceType": "notion",
            "sourceName": item.get("sourceName", "Notion Claws"),
            "sourceId": item.get("sourceId", ""),
            "primaryCategory": item.get("primaryCategory", "ship"),
            "categories": item.get("categories", ["ship"]),
            "score": round(item.get("score", 80), 2),
            "publishedAt": dt.isoformat() if dt else None,
            "discoveredAt": now_utc().isoformat(),
            "expiresAt": (dt + timedelta(hours=72)).isoformat() if dt else (now_utc() + timedelta(hours=72)).isoformat(),
            "status": "queued",
            "postedCategories": [],
            "notion_page_id": item.get("notion_page_id"),
            "notion_insights": item.get("notion_insights", {}),
        }
        if b["id"] not in existing_ids:
            backlog["items"].append(b)
            existing_ids.add(b["id"])
            added.append(b)

    now = now_utc()
    for item in backlog["items"]:
        if item.get("status") == "queued":
            expires = parse_dt(item.get("expiresAt", ""))
            if expires and expires < now:
                item["status"] = "expired"

    backlog["items"] = sorted(
        backlog["items"],
        key=lambda x: (x.get("status") != "queued", -(x.get("score") or 0), x.get("publishedAt") or ""),
    )

    state["seenSourceKeys"] = list(seen_source_keys)[-5000:]
    state["lastCollectedAt"] = now.isoformat()

    save_json(BACKLOG_FILE, backlog)
    save_json(THREAD_STATE_FILE, state)

    counts = {c: 0 for c in CATEGORY_META}
    for item in added:
        counts[item["primaryCategory"]] += 1

    return {"added": len(added), "counts": counts, "items": added}


def queue_for_category(category: str) -> List[dict]:
    ensure_files()
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    posted_urls = set(state.get("postedUrls", []))
    now = now_utc()
    out = []
    for item in backlog.get("items", []):
        if item.get("status") != "queued":
            continue
        if category not in item.get("categories", []):
            continue
        if item.get("url") in posted_urls:
            continue
        expires = parse_dt(item.get("expiresAt", ""))
        if expires and expires < now:
            continue
        out.append(item)
    return sorted(out, key=lambda x: (-(x.get("score") or 0), x.get("publishedAt") or ""))


def source_bucket(item: dict) -> str:
    if item.get("sourceType") == "rss" and item.get("primaryCategory") == "ship":
        return repo_name_from_feed(item.get("sourceName", ""))
    if item.get("sourceType") == "security":
        return item.get("sourceName", "security")
    return item.get("sourceName", item.get("sourceType", "misc"))


def bundle_for_category(category: str, limit: Optional[int] = None) -> List[dict]:
    items = queue_for_category(category)
    target = limit or CATEGORY_META[category]["default_limit"]
    picked: List[dict] = []
    bucket_counts: Dict[str, int] = {}
    bucket_cap = 1 if category == "ship" else 2 if category == "watch" else 3

    for item in items:
        bucket = source_bucket(item)
        if bucket_counts.get(bucket, 0) >= bucket_cap:
            continue
        picked.append(item)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if len(picked) >= target:
            break

    return picked


def fetch_release_body(url: str) -> str:
    """Fetch GitHub release body text from the API."""
    # Convert GitHub release URL to API URL
    # https://github.com/owner/repo/releases/tag/v1.0 -> https://api.github.com/repos/owner/repo/releases/tags/v1.0
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/releases/tag/(.+)", url)
    if not m:
        return ""
    owner, repo, tag = m.group(1), m.group(2), m.group(3)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        req = Request(api_url, headers={"User-Agent": "ClawBytes/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        body = (data.get("body") or "").strip()
        # Truncate to 300 chars for LLM context
        if len(body) > 300:
            body = body[:297] + "..."
        return body
    except Exception:
        return ""


def fetch_article_snippet(url: str) -> str:
    """Fetch first ~500 chars of text from an article URL for LLM grounding."""
    try:
        req = Request(url, headers={"User-Agent": "ClawBytes/1.0", "Accept": "text/html"})
        with urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Strip HTML tags
        import re as _re
        text = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL)
        text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL)
        text = _re.sub(r'<[^>]+>', ' ', text)
        text = _re.sub(r'\s+', ' ', text).strip()
        # Take first 500 chars
        if len(text) > 500:
            text = text[:497] + "..."
        return text
    except Exception:
        return ""


BANNED_VERBS = [
    "explores", "explored", "exploring",
    "reveals", "revealed", "revealing",
    "highlights", "highlighted", "highlighting",
    "dives into", "dove into", "diving into",
    "breaks down", "broke down", "breaking down",
    "unpacks", "unpacked", "unpacking",
    "delves into", "delved into", "delving into",
    "examines", "examined", "examining",
    "offers", "offered", "offering",
    "showcases", "showcased", "showcasing",
    "demonstrates", "demonstrated", "demonstrating",
    "rages on", "raging on",
    "sparks debate", "sparking debate",
    "heating up", "heated up",
    "gaining traction", "gaining steam",
    "worth watching", "worth noting",
    "notable", "notably",
]


def strip_banned_verbs(text: str) -> str:
    """Replace banned soft verbs with stronger alternatives or remove."""
    low = text.lower()
    # Map banned verbs to replacements
    replacements = {
        "explores": "maps",
        "explored": "mapped",
        "exploring": "mapping",
        "reveals": "finds",
        "revealed": "found",
        "revealing": "finding",
        "highlights": "flags",
        "highlighted": "flagged",
        "highlighting": "flagging",
        "dives into": "tackles",
        "breaks down": "cuts through",
        "unpacks": "traces",
        "unpacked": "traced",
        "unpacking": "tracing",
        "delves into": "traces",
        "delving into": "tracing",
        "examines": "audits",
        "examined": "audited",
        "examining": "auditing",
        "showcases": "ships",
        "showcased": "shipped",
        "showcasing": "shipping",
        "demonstrates": "shows",
        "demonstrated": "showed",
        "demonstrating": "showing",
        "offering": "delivering",
        "rages on": "continues",
        "raging on": "continuing",
        "sparks debate": "triggers pushback",
        "sparking debate": "triggering pushback",
        "heating up": "escalating",
        "heated up": "escalated",
        "gaining traction": "spreading",
        "gaining steam": "spreading",
        "worth watching": "on the radar",
        "worth noting": "noted",
        "notable": "real",
        "notably": "clearly",
    }
    result = text
    for verb, replacement in replacements.items():
        # Case-insensitive replace preserving first char case
        import re as _re
        pattern = _re.compile(r'\b' + _re.escape(verb) + r'\b', _re.IGNORECASE)
        match = pattern.search(result)
        if match:
            matched = match.group()
            # Preserve capitalization
            if matched[0].isupper():
                fixed = replacement[0].upper() + replacement[1:]
            else:
                fixed = replacement
            result = pattern.sub(fixed, result, count=1)
    return result


def llm_summarize(items: List[dict], category: str) -> Optional[str]:
    """Use LLM to generate 'why it matters' summaries for a bundle of items."""
    if not LLM_API_KEY:
        return None
    if not items:
        return None

    # Build the prompt
    meta = CATEGORY_META[category]
    prompt = f"""You write @clawbytes on Telegram — covering the AI agent ecosystem. Short, sharp, opinionated. No corporate filler.

Write a {meta['label']} lane post with {len(items)} items.

FORMAT (strict HTML):
{meta['emoji']} <b>{meta['label']}</b> — N items

[emoji] <a href="URL">ACTUAL TITLE</a> — 1 punchy sentence on why this matters

Rules:
- Use <b> for bold, <a href="URL">title text</a> for links — use the ACTUAL model/project name as link text, never "Link" or "thread"
- Ship: what changed, why operators care. Use the release notes if provided — don't invent features.
- Watch: the risk, what to check, what breaks
- Read: describe WHAT the piece IS (paper, framework, deep-dive, critique) and its core claim — but only if the source data states it. Never invent numbers, percentages, or specific findings. "A paper proposing formal metrics for agent reliability" beats "Defines 3 key metrics for agent failure rates" if you don't actually know there are 3.
- Community: the sentiment, the discovery, the real user signal. If multiple threads cover the same topic, MERGE them into one bullet (e.g. "3 threads on cost and access (58↑ total)") instead of listing each separately. NEVER repeat the raw summary text — write fresh, specific descriptions.
- MAX 150 chars per item summary. No filler. No "notable" or "worth watching." No "offering insights" or "highlights." No soft verbs: "breaks down", "unpacks", "dives into", "rages on", "sparks debate" are all banned.
- Do NOT parrot the raw summary text provided in the item data. Write original descriptions based on the actual title and topic.
- If release notes are missing or say nothing: just state what the project IS and what version dropped. 1 sentence max. Never speculate with "might," "could," or "should." Example: "IronClaw 0.1.0 — First skills release from the NEAR AI safety team." 
- Use 📦 for ship, 🚨 for watch, 📚 for read, 💬 for community
- For HN items: reference the community signal (e.g. "171 points on HN") when it adds weight
- NEVER fabricate statistics, metrics, or specific findings. If unsure, describe the piece's ambition, not its results.

Items:"""

    for i, item in enumerate(items):
        title = display_title(item)
        url = item.get("url", "")
        raw_summary = item.get("summary", "")
        source = item.get("sourceType", "")
        # Add HN engagement data to help LLM contextualize
        if source == "hackernews":
            pts = item.get("rawScore", 0)
            comments = item.get("rawComments", 0)
            raw_summary = f"{pts}pts / {comments} comments on HN"
        # For Read items from RSS, fetch article snippet for grounding
        article_snippet = ""
        if category == "read" and source == "rss" and url:
            article_snippet = fetch_article_snippet(url)
        # For ship items, try to fetch release body
        release_body = ""
        if category == "ship" and "github.com" in url:
            release_body = fetch_release_body(url)
        item_line = f"\n{i+1}. {title} | {url} | {raw_summary} | source: {source}"
        if release_body:
            item_line += f" | RELEASE NOTES: {release_body}"
        if article_snippet:
            item_line += f" | ARTICLE SNIPPET: {article_snippet}"
        prompt += item_line

    prompt += "\n\nWrite the post now:"

    try:
        data = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1200,
            "temperature": 0.4,
        }).encode()

        req = Request(
            f"{LLM_URL}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
        )
        with urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"]["content"].strip()
        # Strip banned soft verbs
        content = strip_banned_verbs(content)
        # Basic sanity check
        if len(content) < 50 or "I cannot" in content:
            return None
        return content
    except Exception as e:
        return None


def compress_ship_bundle(items: List[dict]) -> List[dict]:
    """Bundle minor releases from the same repo into one entry."""
    from collections import Counter
    repo_counts = Counter()
    for item in items:
        repo = repo_name_from_feed(item.get("sourceName", ""))
        repo_counts[repo] += 1
    
    # If a repo has 2+ items, bundle the extras into one
    bundled = []
    repo_items = {}
    for item in items:
        repo = repo_name_from_feed(item.get("sourceName", ""))
        repo_items.setdefault(repo, []).append(item)
    
    for repo, repo_list in repo_items.items():
        if len(repo_list) >= 2:
            # Keep the highest-scored item, bundle the rest
            repo_list.sort(key=lambda x: -(x.get("score") or 0))
            bundled.append(repo_list[0])
            if len(repo_list) > 1:
                rest_count = len(repo_list) - 1
                group = dict(repo_list[0])
                group["title"] = f"{display_repo_name(repo)}: {rest_count} more releases"
                group["summary"] = f"{rest_count} additional {display_repo_name(repo)} releases this cycle"
                group["url"] = repo_list[1]["url"]  # Link to the next one
                bundled.append(group)
        else:
            bundled.extend(repo_list)
    
    return bundled[:CATEGORY_META["ship"]["default_limit"]]


def compress_community_bundle(items: List[dict]) -> List[dict]:
    """Merge Reddit threads on the same topic into a single entry."""
    from collections import defaultdict
    topic_groups = defaultdict(list)
    non_reddit = []

    for item in items:
        if item.get("sourceType") != "reddit":
            non_reddit.append(item)
            continue
        topic = title_topic(item.get("title", ""))
        topic_groups[topic].append(item)

    merged = []
    for topic, group in topic_groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            # Merge: keep highest-scoring item as lead, note others exist
            group.sort(key=lambda x: -(x.get("score") or 0))
            lead = dict(group[0])
            rest_count = len(group) - 1
            total_ups = sum(i.get("rawScore", 0) for i in group)
            total_comments = sum(i.get("rawComments", 0) for i in group)
            lead["summary"] = f"{rest_count+1} threads on {topic} ({total_ups}\u2191 / {total_comments}\U0001f4ac)"
            merged.append(lead)

    # Re-sort by score
    merged.sort(key=lambda x: -(x.get("score") or 0))
    return non_reddit + merged


def compress_watch_bundle(items: List[dict]) -> List[dict]:
    advisories = [i for i in items if i.get("sourceType") == "security"]
    non_advisories = [i for i in items if i.get("sourceType") != "security"]

    if len(advisories) >= 2:
        repo = advisories[0].get("sourceName", "watched repo")
        repo_label = repo.split("/")[-1]
        repo_label = display_repo_name(repo_label)
        top = sorted(advisories, key=lambda x: -(x.get("score") or 0))
        grouped = dict(top[0])
        grouped["title"] = f"{repo_label} high-severity advisories ({len(advisories)})"
        grouped["summary"] = f"Multiple high-severity advisories are active for {repo_label}; treat this as a review-now risk cluster instead of reading GHSA IDs one by one."
        return [grouped] + non_advisories[: max(0, len(items) - 1)]

    return items


def category_take(category: str, items: List[dict]) -> str:
    if not items:
        return "No fresh backlog for this lane right now."
    if category == "ship":
        return "Release lane today: shipping matters more than discourse."
    if category == "watch":
        return "Risk lane today: check what can break before chasing the shiny stuff."
    if category == "read":
        return "Reading lane today: context beats raw velocity."
    return "Community lane today: user pain and excitement are the signal."


def source_badge(item: dict) -> str:
    source_type = item.get("sourceType")
    if source_type == "rss":
        return "release" if item.get("primaryCategory") == "ship" else "read"
    if source_type == "security":
        return "advisory"
    if source_type == "reddit":
        return "discussion"
    if source_type == "moltbook":
        return "community"
    if source_type == "hackernews":
        return "discussion"
    return source_type or "source"


def display_title(item: dict) -> str:
    if item.get("primaryCategory") == "ship":
        # Notion items: clean up the "— Notion update" suffix
        if item.get("sourceType") == "notion":
            title = item.get("title", "")
            return title.replace(" — Notion update", "").strip()
        repo = repo_name_from_feed(item.get("sourceName", ""))
        return normalize_release_title(repo, item.get("title", ""))
    return item.get("title", "")


def top_score(items: List[dict]) -> float:
    return max((item.get("score") or 0) for item in items) if items else 0.0


def publish_count_today(state: dict, category: str, day_key: Optional[str] = None) -> int:
    day = day_key or local_day_key()
    return sum(1 for event in state.get("publishLog", []) if event.get("category") == category and event.get("day") == day)


def allowed_posts_today(category: str, state: dict, items: Optional[List[dict]] = None) -> int:
    items = items or queue_for_category(category)
    meta = CATEGORY_META[category]
    if not items:
        return 0

    allowed = 0
    best = top_score(items)
    for min_items, min_score in zip(meta["min_items"], meta["min_top_score"]):
        if len(items) >= min_items and best >= min_score:
            allowed += 1
    return allowed


def lane_ready(category: str, state: Optional[dict] = None, dt_local: Optional[datetime] = None) -> dict:
    state = state or load_json(THREAD_STATE_FILE, {})
    dt_local = dt_local or now_local()
    items = queue_for_category(category)
    bundle = bundle_for_category(category)
    posts_today = publish_count_today(state, category, local_day_key(dt_local))
    allowed = allowed_posts_today(category, state, items)
    windows = CATEGORY_META[category]["windows"]

    reason = "not enough fresh backlog"
    ready = False

    if bundle and posts_today < allowed and posts_today < len(windows):
        if dt_local.hour >= windows[posts_today]:
            ready = True
            reason = "ready"
        else:
            reason = f"waiting for local window {windows[posts_today]:02d}:00"
    elif posts_today >= allowed:
        reason = "daily quota not justified by backlog yet"
    elif posts_today >= len(windows):
        reason = "max windows reached"

    return {
        "ready": ready,
        "reason": reason,
        "posts_today": posts_today,
        "allowed_today": allowed,
        "queued": len(items),
        "bundle_size": len(bundle),
        "top_score": round(top_score(items), 2) if items else 0,
    }


def format_category_bundle(category: str, limit: Optional[int] = None, use_llm: bool = True) -> str:
    """Format category bundle with optional LLM enrichment."""
    meta = CATEGORY_META[category]
    bundle = [hydrate_item(item) for item in bundle_for_category(category, limit)]
    
    if category == "watch":
        bundle = compress_watch_bundle(bundle)
    elif category == "ship":
        # Enrich with Notion editorial content before bundling
        bundle = enrich_ship_with_notion(bundle)
        bundle = compress_ship_bundle(bundle)
    elif category == "community":
        bundle = compress_community_bundle(bundle)
    
    if not bundle:
        return f"{meta['emoji']} <b>{meta['label']}</b> — Nothing new"
    
    # Try LLM enrichment first
    if use_llm and LLM_API_KEY:
        llm_result = llm_summarize(bundle, category)
        if llm_result:
            return llm_result
    
    # Fallback: static template format
    lines = [f"{meta['emoji']} <b>{meta['label']}</b> — {len(bundle)} item{'s' if len(bundle) > 1 else ''}"]
    lines.append("")
    
    for item in bundle:
        title = display_title(item)
        url = item['url']
        
        # Short summary: first sentence, max 80 chars
        summary = (item.get('summary') or '').split('.')[0][:80].strip()
        # Strip "Notion update:" prefix for cleaner display
        summary = re.sub(r'^Notion update:\s*', '', summary, flags=re.IGNORECASE)
        if summary:
            summary = f" — {summary}"
        
        # Format: emoji Title — summary [link]
        emoji = "📦" if category == "ship" else "🚨" if category == "watch" else "📚" if category == "read" else "💬"
        lines.append(f"{emoji} <a href=\"{url}\">{html_escape(title)}</a>{html_escape(summary)}")
    
    return "\n".join(lines)


def html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_telegram(message: str) -> None:
    token = cred("ClawBytes Channel", "Bot Token") or cred("Telegram Bots", "Bot Token")
    if not token:
        raise RuntimeError("Telegram bot token not found")
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({
            "chat_id": CHANNEL_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }).encode("utf-8"),
    )
    with urlopen(req, timeout=20) as response:
        json.loads(response.read().decode("utf-8"))


def mark_posted(category: str, limit: Optional[int] = None) -> List[dict]:
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    bundle = bundle_for_category(category, limit)
    posted_ids = {item["id"] for item in bundle}
    posted_urls = set(state.get("postedUrls", []))
    posted_backlog_ids = set(state.get("postedBacklogIds", []))

    for item in backlog.get("items", []):
        if item.get("id") in posted_ids:
            item["status"] = "posted"
            item["postedCategories"] = sorted(set(item.get("postedCategories", []) + [category]))
            posted_urls.add(item["url"])
            posted_backlog_ids.add(item["id"])

    state["postedUrls"] = list(posted_urls)[-5000:]
    state["postedBacklogIds"] = list(posted_backlog_ids)[-5000:]
    published_at = now_utc().isoformat()
    state.setdefault("lastPublishedAt", {})[category] = published_at
    publish_log = state.get("publishLog", [])
    publish_log.append({
        "category": category,
        "at": published_at,
        "day": local_day_key(),
        "count": len(bundle),
    })
    state["publishLog"] = publish_log[-500:]
    save_json(BACKLOG_FILE, backlog)
    save_json(THREAD_STATE_FILE, state)
    return bundle


def print_status() -> None:
    ensure_files()
    state = load_json(THREAD_STATE_FILE, {})
    backlog = load_json(BACKLOG_FILE, {"items": []})
    print("ClawBytes thread backlog status")
    print(f"last collected: {state.get('lastCollectedAt')}")
    for category, meta in CATEGORY_META.items():
        queued = [i for i in backlog.get("items", []) if i.get("status") == "queued" and category in i.get("categories", [])]
        ready = lane_ready(category, state)
        print(
            f"- {meta['label']}: {len(queued)} queued | last published: {state.get('lastPublishedAt', {}).get(category)} "
            f"| today {ready['posts_today']}/{ready['allowed_today']} | {ready['reason']}"
        )


def autopublish(send: bool = False) -> List[dict]:
    collect_into_backlog()
    state = load_json(THREAD_STATE_FILE, {})
    results = []
    for category in CATEGORY_META:
        ready = lane_ready(category, state)
        sent = False
        count = 0
        if ready["ready"]:
            message = format_category_bundle(category)
            bundle = bundle_for_category(category)
            count = len(bundle)
            if send and bundle:
                send_telegram(message)
                mark_posted(category)
                state = load_json(THREAD_STATE_FILE, {})
                sent = True
        results.append({"category": category, **ready, "sent": sent, "count": count})
    return results


def _fetch_item_context(item: dict) -> dict:
    """Fetch real source content for a single bundle item.

    Curator output is only as good as its input. Without this, curator gets just
    titles and writes generic prose. With this, curator sees release notes /
    article excerpts / Notion signals and can extract a real operator-relevant
    signal — or drop the item if there isn't one.
    """
    url = item.get("url") or ""
    context = {"fetched": {}}

    # GitHub releases — pull the release body via API
    if "github.com" in url and "/releases/tag/" in url:
        body = fetch_release_body(url)
        if body:
            context["fetched"]["release_notes"] = body
            context["fetched"]["release_notes_source"] = "github_api"

    # Other URLs — try article snippet (gracefully skips paywalls, JS-heavy sites, etc.)
    elif url.startswith("http"):
        snippet = fetch_article_snippet(url)
        if snippet:
            context["fetched"]["page_excerpt"] = snippet[:1200]
            context["fetched"]["page_excerpt_source"] = "page_text"

    # Notion editorial enrichment, if the item came through enrich_ship_with_notion
    notion_signal = item.get("notion_signal") or item.get("notionSignal")
    if notion_signal:
        context["fetched"]["notion_editorial"] = notion_signal

    # Existing summary/blurb (the deterministic stub) so curator can compare
    existing_summary = (item.get("summary") or "").strip()
    if existing_summary:
        context["existing_blurb"] = existing_summary

    return context


def curator_input_bundle(category: str, limit: Optional[int] = None) -> dict:
    """Build the JSON object the curator (scripts/curator.py) expects on stdin.

    Pre-fetches real source content for each item so the curator has substantive
    material to work with, not just titles. Without this enrichment, the curator
    can only paraphrase headlines.
    """
    meta = CATEGORY_META[category]
    raw_items = [hydrate_item(item) for item in bundle_for_category(category, limit)]

    if category == "watch":
        raw_items = compress_watch_bundle(raw_items)
    elif category == "ship":
        raw_items = enrich_ship_with_notion(raw_items)
        raw_items = compress_ship_bundle(raw_items)
    elif category == "community":
        raw_items = compress_community_bundle(raw_items)

    items = []
    for item in raw_items:
        slim = {
            "id": item.get("id"),
            "title": display_title(item),
            "url": item.get("url"),
            "source": item.get("sourceType"),
            "source_name": item.get("sourceName"),
            "score": item.get("score"),
            "published_at": item.get("publishedAt"),
        }
        slim.update(_fetch_item_context(item))
        items.append(slim)

    return {
        "lane": category,
        "lane_label": meta["label"],
        "lane_emoji": meta["emoji"],
        "items": items,
    }


def run_curator_subprocess(bundle: dict, timeout: int = 90) -> Optional[dict]:
    """Shell out to scripts/curator.py with bundle on stdin. Return parsed JSON or None on failure."""
    curator_script = Path(__file__).parent / "scripts" / "curator.py"
    if not curator_script.exists():
        print(f"curator script missing: {curator_script}", file=sys.stderr)
        return None
    try:
        proc = subprocess.run(
            ["python3", str(curator_script), "--timeout", str(timeout - 10)],
            input=json.dumps(bundle),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"curator subprocess exceeded {timeout}s budget", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("python3 not found when invoking curator", file=sys.stderr)
        return None

    # Always propagate curator stderr so Railway logs capture diagnostics even
    # when curator gracefully falls back (exit 0).
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()

    if proc.returncode != 0:
        print(f"curator subprocess exited {proc.returncode}", file=sys.stderr)
        return None

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"curator subprocess returned non-JSON: {e}; stdout={proc.stdout[:300]!r}", file=sys.stderr)
        return None


def format_curated_html(curated: dict, category: str) -> str:
    """Format the curator's approved bundle as the Telegram HTML message."""
    meta = CATEGORY_META[category]
    lines = [f"{meta['emoji']} <b>{meta['label']}</b>"]

    lead = (curated.get("lead_signal") or "").strip()
    if lead:
        lines.append("")
        lines.append(f"<b>Lead:</b> {html_escape(lead)}")

    items = curated.get("items") or []
    if items:
        lines.append("")
        for item in items:
            title = item.get("title") or ""
            url = item.get("url") or ""
            blurb = (item.get("blurb") or "").strip()
            line = f"• <a href=\"{url}\">{html_escape(title)}</a>"
            if blurb:
                line += f"\n  {html_escape(blurb)}"
            lines.append(line)

    take = (curated.get("take") or "").strip()
    if take:
        lines.append("")
        lines.append(f"<i>{html_escape(take)}</i>")

    return "\n".join(lines)


def main() -> int:
    ensure_files()

    parser = argparse.ArgumentParser(description="ClawBytes category thread system")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--run-monitors", action="store_true", help="Run source monitors before collecting")
    sub.add_parser("status")
    p_auto = sub.add_parser("autopublish")
    p_auto.add_argument("--send", action="store_true")

    p_prev = sub.add_parser("preview")
    p_prev.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_prev.add_argument("--limit", type=int)
    p_prev.add_argument("--collect-first", action="store_true")
    p_prev.add_argument("--use-curator", action="store_true", help="Run Claude curator on the bundle and print the result")
    p_prev.add_argument("--curator-timeout", type=int, default=90, help="Curator subprocess timeout in seconds")

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_pub.add_argument("--limit", type=int)
    p_pub.add_argument("--collect-first", action="store_true")
    p_pub.add_argument("--send", action="store_true")
    p_pub.add_argument("--if-ready", action="store_true")
    p_pub.add_argument("--use-curator", action="store_true", help="Run Claude curator on the bundle before sending")
    p_pub.add_argument("--curator-timeout", type=int, default=90, help="Curator subprocess timeout in seconds")

    args = parser.parse_args()

    if args.cmd == "collect":
        if getattr(args, "run_monitors", False):
            run_monitors()
        result = collect_into_backlog()
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "status":
        print_status()
        return 0

    if args.cmd == "autopublish":
        results = autopublish(send=args.send)
        print(json.dumps(results, indent=2))
        return 0

    if getattr(args, "collect_first", False):
        collect_into_backlog()

    if args.cmd == "preview":
        if getattr(args, "use_curator", False):
            input_bundle = curator_input_bundle(args.category, args.limit)
            curated = run_curator_subprocess(input_bundle, timeout=args.curator_timeout)
            if curated is None:
                print(format_category_bundle(args.category, args.limit))
                print("\n(curator failed; printed deterministic bundle)", file=sys.stderr)
                return 0
            if not curated.get("_curator", {}).get("approved", True):
                print("(curator declined to approve — would skip publish)", file=sys.stderr)
                print(json.dumps(curated.get("_curator"), indent=2), file=sys.stderr)
                return 0
            print(format_curated_html(curated, args.category))
            print("\n--- curator metadata ---", file=sys.stderr)
            print(json.dumps(curated.get("_curator", {}), indent=2), file=sys.stderr)
            return 0
        print(format_category_bundle(args.category, args.limit))
        return 0

    if args.cmd == "publish":
        if args.if_ready:
            ready = lane_ready(args.category)
            if not ready["ready"]:
                print(json.dumps({"category": args.category, **ready}, indent=2))
                return 0

        if getattr(args, "use_curator", False):
            input_bundle = curator_input_bundle(args.category, args.limit)
            curated = run_curator_subprocess(input_bundle, timeout=args.curator_timeout)
            if curated is None:
                # Fall back to deterministic path (channel reliability > editorial purity)
                print("(curator failed; falling back to deterministic bundle)", file=sys.stderr)
                message = format_category_bundle(args.category, args.limit)
                print(message)
                bundle = bundle_for_category(args.category, args.limit)
                if args.send and bundle:
                    send_telegram(message)
                    mark_posted(args.category, args.limit)
                return 0
            meta = curated.get("_curator", {})
            if not meta.get("approved", True):
                # Curator explicitly skipped
                print("(curator declined to approve — skipping publish)", file=sys.stderr)
                print(json.dumps(meta, indent=2), file=sys.stderr)
                return 0
            message = format_curated_html(curated, args.category)
            print(message)
            print("\n--- curator metadata ---", file=sys.stderr)
            print(json.dumps(meta, indent=2), file=sys.stderr)
            kept_items = curated.get("items") or []
            if args.send and kept_items:
                send_telegram(message)
                mark_posted(args.category, args.limit)
            return 0

        message = format_category_bundle(args.category, args.limit)
        print(message)
        bundle = bundle_for_category(args.category, args.limit)
        if args.send and bundle:
            send_telegram(message)
            mark_posted(args.category, args.limit)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
