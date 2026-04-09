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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("WORKSPACE", "/home/ubuntu-openclaw/.openclaw/workspace"))
MEMORY = WORKSPACE / "memory"
CREDS = WORKSPACE / "CREDS.md"

BACKLOG_FILE = MEMORY / "clawbytes-backlog.json"
THREAD_STATE_FILE = MEMORY / "clawbytes-thread-state.json"

CHANNEL_ID = "-1003850321704"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

CATEGORY_META = {
    "ship": {
        "label": "Ship",
        "emoji": "⚙️",
        "ttl_hours": 96,
        "default_limit": 4,
        "intro": "Fresh releases and product movement worth scanning.",
        "windows": [9, 18],
        "min_items": [1, 3],
        "min_top_score": [85, 95],
    },
    "watch": {
        "label": "Watch",
        "emoji": "🚨",
        "ttl_hours": 120,
        "default_limit": 3,
        "intro": "Security, breakage, and risk signals worth watching closely.",
        "windows": [10, 19],
        "min_items": [1, 2],
        "min_top_score": [55, 85],
    },
    "read": {
        "label": "Read",
        "emoji": "📚",
        "ttl_hours": 96,
        "default_limit": 3,
        "intro": "Context pieces worth the click, not just headline noise.",
        "windows": [12, 20],
        "min_items": [1, 2],
        "min_top_score": [28, 38],
    },
    "community": {
        "label": "Community",
        "emoji": "💬",
        "ttl_hours": 72,
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

ALLOWED_SUBREDDITS = {"openclaw", "selfhosted", "localllama", "homelab", "singularity"}

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
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def cred(section: str, key: str) -> str:
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
    save_json(BACKLOG_FILE, load_json(BACKLOG_FILE, {"items": []}))
    save_json(
        THREAD_STATE_FILE,
        load_json(
            THREAD_STATE_FILE,
            {
                "seenSourceKeys": [],
                "postedBacklogIds": [],
                "postedUrls": [],
                "lastCollectedAt": None,
                "lastPublishedAt": {},
                "publishLog": [],
            },
        ),
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


def classify_rss(item: dict) -> Optional[dict]:
    feed = item.get("feed", "")
    title = item.get("title", "")
    url = item.get("link", "")
    if not url:
        return None
    dt = parse_dt(item.get("published", "") or item.get("found_at", ""))
    low = f"{feed} {title}".lower()

    if "releases" in feed.lower():
        if any(x in low for x in ["beta", "nightly", "staging"]):
            return None
        repo = repo_name_from_feed(feed)
        display_title = normalize_release_title(repo, title)
        score = REPO_PRIORITY.get(repo, 50) + age_score(dt, 96) / 8
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
        score = 28 + age_score(dt, 96) / 10 + (10 if item.get("high_signal") else 0)
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
    low = (title or "").lower()
    if any(x in low for x in ["security", "unsafe", "sandbox", "permission", "api keys"]):
        return "security and safety risk"
    if any(x in low for x in ["expensive", "token", "cost", "cheap", "spend"]):
        return "cost pressure and pricing pain"
    if any(x in low for x in ["use case", "usefulness", "workflow", "real workflows"]):
        return "real-world use cases and product fit"
    if any(x in low for x in ["claude code", "codex", "gemma", "model"]):
        return "model choice and competitive pressure"
    return "user sentiment and operator pain points"


def richer_read_summary(title: str, feed: str) -> str:
    """Return very short summary (10 words max)."""
    low = f"{title} {feed}".lower()
    
    if "simon willison" in low:
        return "Agent engineering insights"
    if "grok" in low or "multi-agent" in low:
        return "Multi-agent debate pattern"
    if "research agent" in low or "what i learned" in low:
        return "Research workflow lessons"
    if "google workspace" in low:
        return "External tool integration"
    if "security" in low or "supply chain" in low:
        return "Security risks"
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
    if subreddit == "openclaw":
        if raw_score < 5 and raw_comments < 5:
            return None
    else:
        if raw_score < 20 and raw_comments < 10:
            return None
    dt = parse_dt(item.get("found_at", ""))
    score = raw_score + min(raw_comments, 200) * 0.6 + age_score(dt, 72) / 10
    categories = ["community"]
    low = title.lower()
    if any(term in low for term in SECURITY_TERMS):
        categories = ["watch", "community"]
    summary = f"High-engagement r/{item.get('subreddit', 'openclaw')} thread on {title_topic(title)} ({raw_score} upvotes / {raw_comments} comments)."
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
            timeout=300
        )
        if p.returncode != 0:
            print(f"Monitor returned non-zero: {cmd}", file=sys.stderr)
            print(p.stderr, file=sys.stderr)


def collect_candidates() -> Dict[str, List[dict]]:
    rss = load_json(MEMORY / "claw-rss-state.json", {}).get("foundItems", [])
    reddit = load_json(MEMORY / "claw-reddit-state.json", {}).get("foundItems", [])
    security = load_json(MEMORY / "claw-security-state.json", {}).get("alerts", [])
    moltbook = load_json(MEMORY / "claw-moltbook-state.json", {}).get("foundItems", [])
    return {
        "rss": rss,
        "reddit": reddit,
        "security": security,
        "moltbook": moltbook,
    }


def collect_into_backlog() -> dict:
    ensure_files()
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
    return source_type or "source"


def display_title(item: dict) -> str:
    if item.get("primaryCategory") == "ship":
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


def format_category_bundle(category: str, limit: Optional[int] = None) -> str:
    """Format category bundle in ultra-short, scannable style."""
    meta = CATEGORY_META[category]
    bundle = [hydrate_item(item) for item in bundle_for_category(category, limit)]
    if category == "watch":
        bundle = compress_watch_bundle(bundle)
    
    if not bundle:
        return f"{meta['emoji']} <b>{meta['label']}</b> — Nothing new"
    
    lines = [f"{meta['emoji']} <b>{meta['label']}</b> — {len(bundle)} item{'s' if len(bundle) > 1 else ''}"]
    lines.append("")
    
    for item in bundle:
        title = display_title(item)
        url = item['url']
        
        # Short summary: first sentence, max 80 chars
        summary = (item.get('summary') or '').split('.')[0][:80].strip()
        if summary:
            summary = f" — {summary}"
        
        # Format: 📦 Title — summary [link]
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


def main() -> int:
    ensure_files()

    parser = argparse.ArgumentParser(description="ClawBytes category thread system")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("collect").add_argument("--run-monitors", action="store_true", help="Run source monitors before collecting")
    sub.add_parser("status")
    p_auto = sub.add_parser("autopublish")
    p_auto.add_argument("--send", action="store_true")

    p_prev = sub.add_parser("preview")
    p_prev.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_prev.add_argument("--limit", type=int)
    p_prev.add_argument("--collect-first", action="store_true")

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_pub.add_argument("--limit", type=int)
    p_pub.add_argument("--collect-first", action="store_true")
    p_pub.add_argument("--send", action="store_true")
    p_pub.add_argument("--if-ready", action="store_true")

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
        print(format_category_bundle(args.category, args.limit))
        return 0

    if args.cmd == "publish":
        if args.if_ready:
            ready = lane_ready(args.category)
            if not ready["ready"]:
                print(json.dumps({"category": args.category, **ready}, indent=2))
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
