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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ss_publish import Publisher as _SsPublisher

# Vendored shared publish core (ss_publish/). _publisher is constructed lazily
# in _ensure_publisher() (creds come from cred() / env, resolved at call time,
# not import time). send_telegram / mirror_to_slack / the scheduler's ops-alert
# routing delegate to it. Config-driven so the core stays testable and this
# channel keeps its own cred() resolution, ops banner, and disable_preview=False
# (clawbytes wants link cards in the channel; modelbytes disables them).
_publisher = None


WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).resolve().parent)))
MEMORY = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
CREDS = WORKSPACE / "CREDS.md"

BACKLOG_FILE = MEMORY / "clawbytes-backlog.json"
THREAD_STATE_FILE = MEMORY / "clawbytes-thread-state.json"

CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
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
        "min_top_score": [58, 85],
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

# How many candidates to feed the curator per lane (wider than a deterministic
# post). The curator keeps the best 3-5 in-scope ones; the surplus is headroom
# so dropping off-scope/noise items doesn't starve the lane to a single survivor.
CURATOR_INPUT_LIMIT = 8

# NOTE: repo_name_from_feed() matches these keys as substrings in dict order —
# more-specific keys ("claude agent sdk") must precede more-general ("claude").
REPO_PRIORITY = {
    "openclaw": 100,
    "hermes": 90,
    "ironclaw": 85,
    "moltis": 82,
    "nanoclaw": 80,
    "openfang": 78,
    "picoclaw": 74,
    "codex": 68,
    "claude agent sdk": 67,
    "claude code action": 67,
    "claude": 66,
    "cursor": 64,
    "copilot": 64,
    "devin desktop": 64,  # before "devin"
    "devin": 64,
    "antigravity": 64,
    "amp news": 64,  # compound — bare "amp" is a substring trap
    "gemini": 62,
    "factory": 62,
    "windsurf": 62,
    "opencode": 60,
    "openai-agents": 60,
    "agent client protocol": 60,  # compound — not bare "acp"
    "openhands": 58,
    "aider": 58,
    "mcp": 58,
    "warp blog": 58,
    "replit": 58,
    "augment code": 58,
    "cline": 56,
    "vercel-ai": 56,
    "roo code": 55,
    "continue": 54,
    "goose": 54,
    "qwen code": 54,
    "smolagents": 54,
    "junie": 56,  # before "jetbrains"
    "jetbrains": 56,
    "e2b": 52,
    "crush": 50,
    "anthropic-sdk": 58,
    "openai-python": 56,
    "python-genai": 54,
    "agent framework": 54,
}

# Vendor changelogs/blogs whose feed names are not "releases"/"release notes".
# Exact feed-name match covers backlog items that landed without tags;
# the coding-agent tag path covers new feeds without editing this tuple.
CHANGELOG_SHIP_FEED_NAMES = (
    "cursor changelog",
    "github copilot changelog",
    "amp news",
    "windsurf blog",
    "warp blog",
    "replit blog",
    "augment code blog",
    "jetbrains ai blog",
    "jetbrains junie blog",
    "zed blog",
)

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


ALLOWED_SUBREDDITS = {
    "openclaw", "selfhosted", "localllama",
    # Harness-space subs (2026-06 widening)
    "claudeai", "claudecode", "cursor", "chatgptcoding", "ai_agents", "mcp",
    # Round 2 (2026-06-12, hunter-verified operator signal)
    "codex", "anthropic", "githubcopilot", "windsurf",
} | _load_dynamic_subreddits()

# Reddit topics that belong in Read, not Community.
# Do NOT include "how to"/"tutorial"/"guide" — EDITORIAL_SCOPE excludes pedagogy.
READ_REDDIT_TERMS = [
    "workflow", "setup", "config",
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
LLM_URL = os.environ.get("CLAWBYTES_LLM_URL", "")
LLM_MODEL = os.environ.get("CLAWBYTES_LLM_MODEL", "gemma4:31b-cloud")
LLM_API_KEY = os.environ.get("CLAWBYTES_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

SECURITY_TERMS = [
    "security", "advisory", "vulnerability", "cve", "sandbox", "unsafe",
    "injection", "exploit", "supply chain", "permission", "credential",
]

READ_TERMS = [
    "agent", "agentic", "workflow", "memory", "mcp", "security",
    "claude code", "codex", "openclaw",
    # Harness-era vocabulary (2026-06 widening)
    "subagent", "harness", "skills", "hooks", "computer use",
    "context engineering", "coding agent", "copilot", "cursor",
    "gemini cli", "windsurf", "aider", "agent sdk",
    # Round 2 (2026-06-12): vendor-blog vocabulary. Substring-matched — use
    # anchored compounds for trap tokens ("opus" is in "corpus", "droid" in
    # "android", "augment" in "augmentation").
    "opus 4", "opus 5", "devin desktop", "devin", "junie", "codestral", "mistral",
    "replit", "augment code", "amp news", "warp blog", "jetbrains",
    "sourcegraph", "antigravity", "agent client protocol",
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


def _ensure_publisher():
    """Lazily build the shared-core Publisher from this channel's creds.

    creds come from cred() (env-first, CREDS.md fallback) which resolves at
    call time, so the Publisher is built on first use rather than at import —
    unlike modelbytes (which reads env at module load). Returns the cached
    instance; reconstructs if env creds have changed under it (tests do this).
    """
    global _publisher
    token = cred("ClawBytes Channel", "Bot Token") or cred("Telegram Bots", "Bot Token")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_channel = os.environ.get("CLAWBYTES_SLACK_CHANNEL_ID", "").strip()
    ops_tg = os.environ.get("CLAWBYTES_ADMIN_CHAT_ID", "").strip()
    ops_slack = os.environ.get("CLAWBYTES_OPS_SLACK_CHANNEL_ID", "").strip()
    secret_values = tuple(s for s in (token, slack_token) if s)
    cached = _publisher
    if (cached is not None
            and cached.telegram_token == token
            and cached.telegram_channel_id == CHANNEL_ID
            and cached.slack_token == slack_token
            and cached.slack_channel_id == slack_channel
            and cached.ops_telegram_chat_id == ops_tg
            and cached.ops_slack_channel_id == ops_slack
            and cached.secret_values == secret_values):
        return cached
    _publisher = _SsPublisher(
        telegram_token=token,
        telegram_channel_id=CHANNEL_ID,
        slack_token=slack_token,
        slack_channel_id=slack_channel,
        ops_telegram_chat_id=ops_tg,
        ops_slack_channel_id=ops_slack,
        disable_preview=False,  # clawbytes: link cards are the point
        ops_banner="🔧 OPS REPORT — visible only to you, never posted to the channel.",
        secret_values=secret_values,
    )
    return _publisher


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
        "claude code action": "Claude Code Action",
        "claude": "Claude Code",
        "cursor": "Cursor",
        "gemini": "Gemini CLI",
        "opencode": "OpenCode",
        "openai-agents": "OpenAI Agents SDK",
        "mcp": "MCP",
        "vercel-ai": "Vercel AI SDK",
        "continue": "Continue",
        "e2b": "E2B",
        "claude agent sdk": "Claude Agent SDK",
        "openhands": "OpenHands",
        "aider": "Aider",
        "cline": "Cline",
        "roo code": "Roo Code",
        "goose": "Goose",
        "qwen code": "Qwen Code",
        "smolagents": "Smolagents",
        "crush": "Crush",
        "anthropic-sdk": "Anthropic SDK",
        "openai-python": "OpenAI SDK",
        "python-genai": "Google GenAI SDK",
        "agent framework": "MS Agent Framework",
        "copilot": "Copilot",
        "devin desktop": "Devin Desktop",
        "devin": "Devin",
        "antigravity": "Antigravity",
        "amp news": "Amp",
        "factory": "Factory",
        "windsurf": "Windsurf",
        "warp blog": "Warp",
        "replit": "Replit",
        "augment code": "Augment Code",
        "junie": "Junie",
        "jetbrains": "JetBrains",
        "agent client protocol": "ACP",
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
    # Patch releases: any X.Y.Z with Z>0 (e.g. 2.1.160, 1.2.4, 0.4.2) — demote so
    # routine patches don't headline. Keep .0 minor/major releases (1.2.0, 2.0.0)
    # headline-worthy, and preserve the first-cut 0.0.1 exception.
    if re.search(r"v?\d+\.\d+\.[1-9]\d*\b", low) and not re.search(r"v?0\.0\.1\b", low):
        return True
    # Compact pre-release tags (PEP 440 style: 1.14.6a2, 2.0rc1, 1.0b3)
    if re.search(r"\d+\.\d+(?:\.\d+)?(?:a|b|rc)\d+", low):
        return True
    # Repository-specific prerelease tags can show up as python-v0.1.0b3 or rust-v0.1.0b3.
    if re.search(r"(?:python|rust|node|js)-v?\d+\.\d+\.\d+(?:a|b|rc)\d+", low):
        return True
    # Hotfix/patch keywords
    if any(x in low for x in ["hotfix", "patch", "fix", "minor"]):
        return True
    # Date-based versioning (e.g. 20260413.04, 20260409.01) — treat .XX suffix as patch
    if re.search(r"20\d{6}\.\d+", low):
        return True
    return False


def is_coding_agent_changelog(item: dict) -> bool:
    """True when this RSS item is a closed-source harness changelog/news/blog.

    `classify_rss` only special-cased feed names containing "releases" /
    "release notes", so Cursor Changelog / Amp News / Copilot Changelog were
    falling through to Read. Tags are the real signal — a bare "blog" must not
    Ship LangChain/Mistral. The name tuple covers backlog items that landed
    without tags.
    """
    feed = (item.get("feed") or "").lower().strip()
    if feed in CHANGELOG_SHIP_FEED_NAMES:
        return True
    tags = {str(t).lower() for t in (item.get("tags") or [])}
    if "coding-agent" not in tags:
        return False
    return "changelog" in feed or "blog" in feed or feed.endswith(" news") or " news" in f" {feed}"


def classify_rss(item: dict) -> Optional[dict]:
    feed = item.get("feed", "")
    title = item.get("title", "")
    url = item.get("link", "")
    if not url:
        return None
    dt = parse_dt(item.get("published", "") or item.get("found_at", ""))
    low = f"{feed} {title}".lower()
    feed_low = feed.lower()

    if "status" in feed_low:
        # Provider status incidents (e.g. "Anthropic: Elevated errors on Opus
        # 4.8 Fast") are operational weather, not editorial signal for a
        # harness-builder audience: they have no EDITORIAL_SCOPE mandate, ship a
        # generic blurb, and — even after the #10 same-incident dedup and #11
        # per-vendor/day cap — still read as the same alert repeating (a vendor
        # files distinctly-titled incidents daily, each taking the one slot).
        # Dropped entirely (2026-06-24). The explicit return is load-bearing: a
        # status title like "...Opus 4.8..." contains the READ_TERM "opus 4", so
        # falling through (instead of returning here) would misroute it to Read.
        return None

    if "release notes" in feed_low or is_coding_agent_changelog(item):
        # Mintlify-style changelogs title entries by date ("June 10, 2026")
        # and vendor blogs title by feature — prefix the vendor when it's not
        # already in the title. Score from REPO_PRIORITY so Cursor/Devin/Amp
        # clear Ship window 1 (58) without leaning on the age bonus.
        repo = repo_name_from_feed(feed)
        vendor = display_repo_name(repo)
        if repo not in REPO_PRIORITY:
            stripped = re.sub(
                r"\s*(release notes|changelog|blog|news)\s*$",
                "",
                feed,
                flags=re.IGNORECASE,
            ).strip()
            vendor = stripped or feed
        base_score = REPO_PRIORITY.get(repo, 55)
        score = base_score + age_score(dt, 96) / 8
        display_title = title if vendor.lower() in (title or "").lower() else f"{vendor}: {title}"
        kind = "release notes update" if "release notes" in feed_low else "changelog update"
        return {
            "primaryCategory": "ship",
            "categories": ["ship"],
            "score": round(score, 2),
            "summary": f"{vendor} {kind}",
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["ship"]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": display_title,
        }

    if "releases" in feed_low:
        if any(x in low for x in ["beta", "nightly", "staging", "alpha"]):
            return None
        if re.search(r"(?:^|[-_\s])v?\d+\.\d+\.\d+(?:a|b|rc)\d+\b", low):
            return None
        # Skip release candidates (v0.22.0rc2, 1.0-rc1) — pre-releases, same
        # class as beta/alpha. Lookbehind avoids matching words like "search".
        if re.search(r"(?<![a-z])rc[.\-]?\d", low):
            return None
        # Skip chore/ci/internal/dependency release titles
        if any(x in low for x in ["chore:", "ci:", "build:", "internal", "rusty-v8", "dependency"]):
            return None
        repo = repo_name_from_feed(feed)
        display_title = normalize_release_title(repo, title)
        base_score = REPO_PRIORITY.get(repo, 50)
        # Penalize minor releases heavily
        if is_minor_release(title):
            base_score = max(20, base_score - 30)
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
        score = 38 + age_score(dt, 168) / 10 + (15 if item.get("high_signal") else 5)
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


def classify_hf_paper(item: dict) -> Optional[dict]:
    """Classify HuggingFace Daily Papers output into Read/Watch/Community."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    dt = parse_dt(item.get("publishedAt", "") or item.get("found_at", ""))
    upvotes = int(item.get("upvotes", 0) or 0)
    relevance = int(item.get("score", 0) or 0)
    summary_text = item.get("ai_summary") or "HF Daily Papers signal"
    low = f"{title} {summary_text}".lower()

    hint = item.get("category_hint") if item.get("category_hint") in CATEGORY_META else "read"
    # HF papers are context/research signals, not Ship releases. GitHub/project
    # links add confidence, but should not route the item into Ship.
    if hint in {"ship", "watch"}:
        hint = "read"
    # Papers never route to Watch — that lane is for actionable incidents and
    # advisories (security monitor, status feeds), not academic research. Even
    # security-flavored papers belong in Read. Keeps Watch tight and Read full.
    categories = [hint]
    if hint == "community":
        categories = ["community", "read"]
    elif "agent" in low or "benchmark" in low or "tool" in low or "harness" in low:
        categories = ["read", "community"]

    primary = categories[0]
    base = {"watch": 42, "read": 30, "community": 22}.get(primary, 30)
    score = base + min(upvotes, 80) * 0.25 + relevance * 1.6 + age_score(dt, 168) / 16
    if item.get("githubRepo"):
        score += 3
    if item.get("projectPage"):
        score += 1

    return {
        "primaryCategory": primary,
        "categories": categories,
        "score": round(score, 2),
        "summary": trim(summary_text, 180),
        "rawScore": upvotes,
        "rawComments": 0,
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[primary]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "hf_papers",
        "sourceName": "HF Daily Papers",
        "sourceId": item.get("id") or item.get("hf_id") or url,
        "url": url,
        "title": title,
    }


def classify_leaderboard(item: dict) -> Optional[dict]:
    """Benchmark-board movement (SWE-bench, Aider polyglot) — capability news.

    The monitor only emits on real top-N change, so everything arriving here
    is already news; route to Ship as product/capability movement with Read
    as the secondary lane.
    """
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    dt = parse_dt(item.get("found_at", ""))
    score = 64 + age_score(dt, 168) / 10
    if item.get("change") == "new_leader":
        score += 8
    return {
        "primaryCategory": "ship",
        "categories": ["ship", "read"],
        "score": round(score, 2),
        "summary": item.get("summary") or "Leaderboard movement",
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["ship"]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "leaderboard",
        "sourceName": item.get("board", "leaderboard"),
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def classify_registry(item: dict) -> Optional[dict]:
    """Model-availability movement from machine registries (OpenRouter,
    LiteLLM pricing, HF trending). The monitor baselines silently and only
    emits post-baseline diffs, so arrivals here are already news → Ship."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    dt = parse_dt(item.get("found_at", ""))
    score = 58 + age_score(dt, 96) / 10
    return {
        "primaryCategory": "ship",
        "categories": ["ship"],
        "score": round(score, 2),
        "summary": item.get("summary") or "Registry movement",
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["ship"]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "registry",
        "sourceName": item.get("registry", "registry"),
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def classify_pagewatch(item: dict) -> Optional[dict]:
    """Feedless vendor pages (Anthropic news/engineering, Claude release
    notes, Devin CLI, xAI, DeepSeek). Lane comes from the watcher; Anthropic
    announcements score high — this was the channel's biggest blind spot."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    lane = item.get("lane") if item.get("lane") in CATEGORY_META else "ship"
    dt = parse_dt(item.get("found_at", ""))
    score = (63 if lane == "ship" else 48) + age_score(dt, 96) / 10
    return {
        "primaryCategory": lane,
        "categories": [lane],
        "score": round(score, 2),
        "summary": item.get("summary") or "Vendor page update",
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[lane]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "pagewatch",
        "sourceName": item.get("watch", "pagewatch"),
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def classify_bsky(item: dict) -> Optional[dict]:
    """High-engagement Bluesky posts on harness phrases → Community."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    likes = int(item.get("likes", 0) or 0)
    reposts = int(item.get("reposts", 0) or 0)
    dt = parse_dt(item.get("found_at", ""))
    score = likes * 0.5 + reposts * 1.0 + age_score(dt, 72) / 10
    return {
        "primaryCategory": "community",
        "categories": ["community"],
        "score": round(score, 2),
        "summary": f"Bluesky signal ({likes}♥ / {reposts}🔁)",
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["community"]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "bsky",
        "sourceName": item.get("handle", "bluesky"),
        "sourceId": item.get("id", url),
        "url": url,
        "title": title,
    }


def classify_ecosystem_hn(item: dict) -> Optional[dict]:
    """Classify HN stories produced by the ecosystem shell monitor."""
    normalized = {
        "id": item.get("id"),
        "title": item.get("title"),
        "url": item.get("hn_url") or item.get("url"),
        "score": item.get("points", 0),
        "comments": item.get("comments", 0),
        "created_at": item.get("created"),
        "found_at": item.get("found_at"),
        "sourceType": "hackernews",
    }
    return classify_hackernews(normalized)


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
    """Run source monitors to refresh state files before collecting.

    Each monitor runs isolated: a timeout, a nonzero exit, or an unexpected
    crash in ONE monitor must not starve the rest of the batch. (Previously a
    single subprocess.TimeoutExpired raised out and skipped every later
    monitor for that collect cycle.) Uses cwd= + arg list rather than
    shell=True — same pattern scheduler.py already uses, and avoids
    interpolated-shell command-injection risk.
    """
    cmds = [
        ["python3", "scripts/claw-rss-monitor.py"],
        ["python3", "scripts/claw-reddit-monitor.py"],
        ["python3", "scripts/claw-hn-monitor.py", "--quiet"],
        ["python3", "scripts/claw-moltbook-monitor.py"],
        ["python3", "scripts/claw-leaderboard-monitor.py", "--quiet"],
        ["python3", "scripts/claw-registry-monitor.py", "--quiet"],
        ["python3", "scripts/claw-pagewatch-monitor.py", "--quiet"],
        ["python3", "scripts/claw-bsky-monitor.py", "--quiet"],
        ["bash", "scripts/claw-ecosystem-monitor.sh", "--mode", "check"],
    ]
    for cmd in cmds:
        try:
            p = subprocess.run(
                cmd,
                cwd=str(WORKSPACE),
                text=True,
                capture_output=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            print(f"Monitor timed out after 300s: {' '.join(cmd)}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad monitor must not starve the rest
            print(f"Monitor crashed unexpectedly ({exc!r}): {' '.join(cmd)}", file=sys.stderr)
            continue
        if p.returncode != 0:
            print(f"Monitor returned non-zero: {' '.join(cmd)}", file=sys.stderr)
            print(p.stderr, file=sys.stderr)


def _unique_items(items: List[dict], key_fields: tuple[str, ...] = ("id", "url", "link")) -> List[dict]:
    seen = set()
    out = []
    for item in items:
        key = next((str(item.get(field)) for field in key_fields if item.get(field)), json.dumps(item, sort_keys=True)[:200])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def collect_candidates() -> Dict[str, List[dict]]:
    rss = load_json(MEMORY / "claw-rss-state.json", {}).get("foundItems", [])
    reddit = load_json(MEMORY / "claw-reddit-state.json", {}).get("foundItems", [])
    moltbook = load_json(MEMORY / "claw-moltbook-state.json", {}).get("foundItems", [])
    hackernews = load_json(MEMORY / "claw-hn-state.json", {}).get("foundItems", [])
    hf_papers = load_json(MEMORY / "claw-hf-state.json", {}).get("foundItems", [])
    leaderboard = load_json(MEMORY / "claw-leaderboard-state.json", {}).get("foundItems", [])
    registry = load_json(MEMORY / "claw-registry-state.json", {}).get("foundItems", [])
    pagewatch = load_json(MEMORY / "claw-pagewatch-state.json", {}).get("foundItems", [])
    bsky = load_json(MEMORY / "claw-bsky-state.json", {}).get("foundItems", [])
    ecosystem = load_json(MEMORY / "claw-ecosystem-new-items.json", {})
    if isinstance(ecosystem, dict):
        hf_papers = _unique_items(hf_papers + ecosystem.get("newHFPapers", []), ("id", "hf_id", "url"))
        ecosystem_hn = ecosystem.get("newHNStories", [])
    else:
        ecosystem_hn = []
    return {
        "rss": rss,
        "reddit": reddit,
        "moltbook": moltbook,
        "hackernews": hackernews,
        "hf_papers": hf_papers,
        "leaderboard": leaderboard,
        "registry": registry,
        "pagewatch": pagewatch,
        "bsky": bsky,
        "ecosystem_hn": ecosystem_hn,
    }


def classify_source_candidate(kind: str, item: dict) -> Optional[dict]:
    if kind == "rss":
        return classify_rss(item)
    if kind == "reddit":
        return classify_reddit(item)
    if kind == "moltbook":
        return classify_moltbook(item)
    if kind == "hackernews":
        return classify_hackernews(item)
    if kind == "hf_papers":
        return classify_hf_paper(item)
    if kind == "leaderboard":
        return classify_leaderboard(item)
    if kind == "registry":
        return classify_registry(item)
    if kind == "pagewatch":
        return classify_pagewatch(item)
    if kind == "bsky":
        return classify_bsky(item)
    if kind == "ecosystem_hn":
        return classify_ecosystem_hn(item)
    return None


def raw_source_label(kind: str, item: dict) -> str:
    if kind == "rss":
        return item.get("feed", "rss")
    if kind == "reddit":
        return item.get("subreddit", "reddit")
    if kind == "hf_papers":
        return "HF Daily Papers"
    if kind == "leaderboard":
        return item.get("board", "leaderboard")
    if kind == "registry":
        return item.get("registry", "registry")
    if kind == "pagewatch":
        return item.get("watch", "pagewatch")
    if kind == "bsky":
        return item.get("handle", "bluesky")
    if kind == "ecosystem_hn":
        return "hackernews/ecosystem"
    return item.get("sourceName") or item.get("sourceType") or kind


def raw_item_title(kind: str, item: dict) -> str:
    return item.get("title") or item.get("name") or item.get("advisory_id") or "(untitled)"


def audit_candidate(kind: str, item: dict, state: dict, backlog: dict) -> dict:
    candidate = classify_source_candidate(kind, item)
    row = {
        "sourceType": kind,
        "sourceName": raw_source_label(kind, item),
        "rawTitle": trim(raw_item_title(kind, item), 160),
        "rawId": item.get("id") or item.get("advisory_id") or item.get("url") or item.get("link"),
        "status": "rejected",
        "reason": "classifier_rejected",
    }
    if not candidate:
        return row

    row.update(
        {
            "title": trim(candidate.get("title", ""), 160),
            "url": candidate.get("url"),
            "primaryCategory": candidate.get("primaryCategory"),
            "categories": candidate.get("categories", []),
            "score": candidate.get("score"),
            "summary": candidate.get("summary"),
            "expiresAt": candidate.get("expiresAt").isoformat() if candidate.get("expiresAt") else None,
        }
    )

    if not is_fresh(candidate):
        row.update({"status": "rejected", "reason": "expired"})
        return row

    seen_source_keys = set(state.get("seenSourceKeys", []))
    posted_urls = set(state.get("postedUrls", []))
    existing = {existing_item.get("id"): existing_item for existing_item in backlog.get("items", [])}
    key = source_key(kind, candidate["sourceId"], candidate["url"])
    bid = backlog_id(candidate["url"], candidate["title"])
    row["sourceKey"] = key
    row["backlogId"] = bid

    if candidate["url"] in posted_urls:
        row.update({"status": "skipped", "reason": "posted_url"})
    elif bid in existing:
        row.update({"status": "skipped", "reason": "already_in_backlog", "backlogStatus": existing[bid].get("status")})
    elif key in seen_source_keys:
        row.update({"status": "skipped", "reason": "seen_source_key"})
    else:
        row.update({"status": "would_add", "reason": "passes_classifier"})
    return row


def state_item_count(path: Path) -> int:
    data = load_json(path, {})
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, dict):
        return 0
    if isinstance(data.get("foundItems"), list):
        return len(data["foundItems"])
    return sum(len(data.get(key, [])) for key in ["newReleases", "newHNStories", "newSkills", "newHFPapers", "alerts"] if isinstance(data.get(key), list))


def unconsumed_state_report() -> list:
    consumed = {
        "claw-rss-state.json",
        "claw-reddit-state.json",
        "claw-security-state.json",
        "claw-moltbook-state.json",
        "claw-hn-state.json",
        "claw-hf-state.json",
        "claw-leaderboard-state.json",
        "claw-registry-state.json",
        "claw-pagewatch-state.json",
        "claw-bsky-state.json",
        "claw-ecosystem-new-items.json",
        "clawbytes-backlog.json",
        "clawbytes-thread-state.json",
    }
    rows = []
    for path in sorted(MEMORY.glob("*.json")):
        if path.name in consumed:
            continue
        count = state_item_count(path)
        if count:
            rows.append({"file": path.name, "items": count, "consumedByBacklog": False})
    return rows


def audit_sources(category: Optional[str] = None, limit: int = 40) -> dict:
    ensure_files()
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    rows = []
    for kind, items in collect_candidates().items():
        for item in items:
            row = audit_candidate(kind, item, state, backlog)
            if category and category not in row.get("categories", []):
                continue
            rows.append(row)

    reason_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    lane_counts: Dict[str, int] = {c: 0 for c in CATEGORY_META}
    status_counts: Dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        reason_counts[row["reason"]] = reason_counts.get(row["reason"], 0) + 1
        source_counts[row["sourceType"]] = source_counts.get(row["sourceType"], 0) + 1
        if row.get("status") in {"would_add", "skipped"}:
            lane = row.get("primaryCategory")
            if lane in lane_counts:
                lane_counts[lane] += 1

    priority = {"would_add": 0, "skipped": 1, "rejected": 2}
    rows = sorted(rows, key=lambda r: (priority.get(r["status"], 9), -(r.get("score") or 0), r.get("sourceName", "")))
    return {
        "memoryDir": str(MEMORY),
        "lastCollectedAt": state.get("lastCollectedAt"),
        "rawItems": len(rows),
        "statusCounts": status_counts,
        "reasonCounts": reason_counts,
        "sourceCounts": source_counts,
        "laneCounts": lane_counts,
        "currentBundles": {cat: [{"title": item.get("title"), "score": item.get("score"), "source": item.get("sourceName")} for item in bundle_for_category(cat)] for cat in CATEGORY_META},
        "unconsumedStateFiles": unconsumed_state_report(),
        "items": rows[:limit],
    }


def print_audit(report: dict) -> None:
    print("ClawBytes ingestion audit")
    print(f"memory: {report['memoryDir']}")
    print(f"lastCollectedAt: {report.get('lastCollectedAt')}")
    print(f"raw items inspected: {report['rawItems']}")
    print(f"status: {json.dumps(report['statusCounts'], sort_keys=True)}")
    print(f"reasons: {json.dumps(report['reasonCounts'], sort_keys=True)}")
    print(f"sources: {json.dumps(report['sourceCounts'], sort_keys=True)}")
    print(f"lanes: {json.dumps(report['laneCounts'], sort_keys=True)}")

    if report["unconsumedStateFiles"]:
        print("\nunconsumed state files with items:")
        for row in report["unconsumedStateFiles"]:
            print(f"- {row['file']}: {row['items']} item(s)")

    print("\ncurrent deterministic bundles:")
    for category, items in report["currentBundles"].items():
        titles = "; ".join(f"{item['title']} ({item['score']})" for item in items) or "nothing ready"
        print(f"- {category}: {titles}")

    print("\nsource-item decisions:")
    for row in report["items"]:
        lane = row.get("primaryCategory") or "-"
        score = row.get("score") if row.get("score") is not None else "-"
        title = row.get("title") or row.get("rawTitle")
        print(f"- {row['status']}/{row['reason']} [{row['sourceType']}:{row['sourceName']}] {lane} score={score} — {title}")


def collect_into_backlog() -> dict:
    ensure_files()

    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})

    seen_source_keys = set(state.get("seenSourceKeys", []))
    existing_ids = {item["id"] for item in backlog.get("items", [])}

    added = []
    candidates = collect_candidates()

    for kind, items in candidates.items():
        for item in items:
            candidate = classify_source_candidate(kind, item)
            if not candidate:
                continue
            if not is_fresh(candidate):
                continue
            key = source_key(kind, candidate["sourceId"], candidate["url"])
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
        if item.get("status") == "queued" and item.get("sourceType") == "notion":
            item["status"] = "retired"
            item["retiredReason"] = "notion_removed_from_production_runtime"
            continue
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


def _flag_on(name: str) -> bool:
    """Truthy env flag check, matching the scheduler's CLAWBYTES_PUBLISH gate."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_scores_enabled() -> bool:
    """Opt-in (CLAWBYTES_NORMALIZE_SCORES) within-source score normalization.

    Off by default so the live channel's ranking is unchanged until explicitly
    enabled for an A/B."""
    return _flag_on("CLAWBYTES_NORMALIZE_SCORES")


def _norm_source_group(item: dict) -> str:
    """Group items for normalization by source *type* — that's where the raw
    score scales diverge (HN uses points ~0-600, ship uses REPO_PRIORITY bases
    ~50-110, registries a flat ~58). Within a type the formula is shared, so
    scores are already comparable."""
    return item.get("sourceType") or "misc"


def _percentile_ranks(scores: List[float]) -> List[float]:
    """Map raw scores to within-group percentile in [0.0, 1.0].

    Top score -> 1.0, bottom -> 0.0, ties share the mean of their positions.
    A lone item -> 1.0: it is the top of its source, and the lane's per-source
    bucket caps in bundle_for_category still stop one source from dominating —
    this is the "bias toward a real move from a smaller harness" rule.
    """
    n = len(scores)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        pr = ((i + j) / 2.0) / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = pr
        i = j + 1
    return ranks


def apply_normalized_scores(items: List[dict]) -> List[dict]:
    """Annotate each item with normScore = within-source percentile of its raw
    score, so 'top of its source' is comparable across sources. Non-destructive:
    the raw 'score' (used by min_top_score gates) is left untouched.
    """
    from collections import defaultdict

    groups: Dict[str, List[dict]] = defaultdict(list)
    for it in items:
        groups[_norm_source_group(it)].append(it)
    for grp in groups.values():
        prs = _percentile_ranks([float(it.get("score") or 0) for it in grp])
        for it, pr in zip(grp, prs):
            it["normScore"] = round(pr, 4)
    return items


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
        if item.get("sourceType") == "notion":
            continue
        # Provider-status incidents are retired (classify_rss drops them); also
        # drop any leftover backlog residue (their synthetic status.local url) so
        # the cutover is immediate instead of waiting out the 48h TTL.
        if (item.get("url") or "").startswith("https://status.local/"):
            continue
        if category not in item.get("categories", []):
            continue
        if item.get("url") in posted_urls:
            continue
        expires = parse_dt(item.get("expiresAt", ""))
        if expires and expires < now:
            continue
        out.append(item)
    # Score desc; break ties by recency. publishedAt is often missing from feeds,
    # so fall back to discoveredAt and order newest-first (stable two-pass sort).
    out.sort(key=lambda x: x.get("publishedAt") or x.get("discoveredAt") or "", reverse=True)
    out.sort(key=lambda x: -(x.get("score") or 0))
    # Optional within-source normalization: promote each source's best item so a
    # top-decile release ranks alongside a top-decile HN thread instead of losing
    # to its raw point count. Applied as the primary key (raw score stays the
    # tie-break) only when CLAWBYTES_NORMALIZE_SCORES is set.
    if _normalize_scores_enabled():
        apply_normalized_scores(out)
        out.sort(key=lambda x: -(x.get("normScore") or 0))
    return out


def source_bucket(item: dict) -> str:
    if item.get("sourceType") == "rss" and item.get("primaryCategory") == "ship":
        return repo_name_from_feed(item.get("sourceName", ""))
    return item.get("sourceName", item.get("sourceType", "misc"))


_TOPIC_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "your", "this", "that", "new",
    "now", "how", "why", "what", "using", "use", "guide", "update", "updates",
    "release", "releases", "plugin", "plugins", "tool", "tools", "framework",
    "frameworks", "role", "roles", "workflow", "workflows", "every", "almost",
    "everything", "everyone", "model", "models", "agent", "agents", "open",
    "source", "local", "fast", "support", "adds", "add", "via", "are", "you",
}


def _topic_tokens(title: str) -> set:
    """Distinctive (non-generic) lowercase tokens used to detect same-topic items."""
    toks = re.findall(r"[a-z0-9.]{4,}", (title or "").lower())
    return {t for t in toks if t not in _TOPIC_STOPWORDS}


def _same_topic(item: dict, picked: List[dict]) -> bool:
    """True if item shares a distinctive token with something already picked
    (e.g. 5 'Codex ...' headlines collapse to one)."""
    sig = _topic_tokens(item.get("title", ""))
    if not sig:
        return False
    return any(sig & _topic_tokens(p.get("title", "")) for p in picked)


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
        if category != "ship" and _same_topic(item, picked):
            continue
        picked.append(item)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if len(picked) >= target:
            break

    return picked


RELEASE_NOTE_NOISE = re.compile(
    r"dependabot|full changelog|new contributors|first contribution"
    r"|^#+\s*(what'?s changed|changelog)\s*$|^\*\*full changelog",
    re.IGNORECASE,
)


def clean_release_notes(body: str) -> str:
    """Distill a GitHub release body into LLM-worthy grounding.

    Release bodies open with changelog boilerplate (dependabot bumps,
    contributor shoutouts, compare links) that crowds real features out of a
    truncated context window. Keep substantive lines, strip link noise.
    """
    lines = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or RELEASE_NOTE_NOISE.search(line):
            continue
        # "feat: x by @dev in https://github.com/o/r/pull/1" -> "feat: x"
        line = re.sub(r"\s+by @[\w\[\]-]+( in \S+)?", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # md links -> text
        line = re.sub(r"https?://\S+", "", line).strip(" *-:")
        if line:
            lines.append(line)
    return "\n".join(lines)


def _release_diff_enabled() -> bool:
    """Opt-in (CLAWBYTES_RELEASE_DIFF) changelog diffing. Off by default: it
    costs 1-2 extra GitHub API calls per ship item at publish time, so it wants
    GITHUB_TOKEN and an explicit A/B before it touches the live channel."""
    return _flag_on("CLAWBYTES_RELEASE_DIFF")


def _github_get_json(api_url: str, headers: dict, timeout: int = 10):
    """GET + JSON-decode a GitHub API URL. Raises on any transport/parse error;
    callers degrade gracefully."""
    req = Request(api_url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _previous_release_tag(releases: list, current_tag: str) -> Optional[str]:
    """Given GitHub's newest-first releases list, return the published release
    immediately preceding current_tag (skipping drafts/prereleases), or None."""
    tags = [
        r.get("tag_name")
        for r in (releases or [])
        if isinstance(r, dict) and r.get("tag_name") and not r.get("draft") and not r.get("prerelease")
    ]
    try:
        idx = tags.index(current_tag)
    except ValueError:
        return None
    return tags[idx + 1] if idx + 1 < len(tags) else None


# Release-plumbing commit subjects that carry no operator-facing signal. Mirrors
# the ship classifier's chore/ci/build title filtering, plus release-bot churn
# (version-bump commits and their reverts) that dominates auto-published tags.
_COMMIT_NOISE = re.compile(
    r"^(?:revert\b|merge\b|chore\(release\)|chore\(deps\)|chore:|ci:|build:|"
    r"bump |release version|version bump)",
    re.IGNORECASE,
)


def _compare_commit_lines(compare_json: dict, limit: int = 8) -> str:
    """Distill a GitHub compare response into de-duped commit subject bullets.

    Used when a release ships with thin/empty notes: the commit subjects between
    the two tags are the only substance available. Drops merges, release-bot
    churn, and the same boilerplate clean_release_notes strips; trims trailing
    "(#123)" refs."""
    commits = compare_json.get("commits") or []
    lines: List[str] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        msg = ((c.get("commit") or {}).get("message") or "").strip()
        if not msg:
            continue
        subject = msg.splitlines()[0].strip()
        if not subject or _COMMIT_NOISE.search(subject) or RELEASE_NOTE_NOISE.search(subject):
            continue
        subject = re.sub(r"\s*\(#\d+\)\s*$", "", subject).strip()
        if subject and subject not in lines:
            lines.append(subject)
        if len(lines) >= limit:
            break
    return "\n".join(f"- {ln}" for ln in lines)


def _compose_release_diff(body: str, prev_tag: str, commit_block: str) -> str:
    """Assemble diff-aware grounding: a 'changes since <prev>' header, the
    cleaned release body when present, and commit bullets when the body was
    thin. Bounded to the same ~900 char budget fetch_release_body uses."""
    parts = [f"(changes since {prev_tag})"]
    if body:
        parts.append(body)
    if commit_block:
        parts.append(f"Commits since {prev_tag}:\n{commit_block}")
    out = "\n".join(parts).strip()
    return out[:900]


def _augment_with_release_diff(owner: str, repo: str, tag: str, body: str, headers: dict) -> str:
    """Add previous-version context to a release body. Returns body unchanged on
    any failure so ship grounding never regresses below the non-diff path."""
    try:
        releases = _github_get_json(
            f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30", headers
        )
    except Exception:
        return body
    prev_tag = _previous_release_tag(releases, tag)
    if not prev_tag:
        return body
    commit_block = ""
    if len(body) < 80:  # thin/empty notes → pull commit subjects for substance
        try:
            comp = _github_get_json(
                f"https://api.github.com/repos/{owner}/{repo}/compare/{prev_tag}...{tag}", headers
            )
            commit_block = _compare_commit_lines(comp)
        except Exception:
            commit_block = ""
    return _compose_release_diff(body, prev_tag, commit_block)


def fetch_release_body(url: str) -> str:
    """Fetch and distill GitHub release notes for LLM grounding."""
    # https://github.com/owner/repo/releases/tag/v1.0 -> https://api.github.com/repos/owner/repo/releases/tags/v1.0
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/releases/tag/(.+)", url)
    if not m:
        return ""
    owner, repo, tag = m.group(1), m.group(2), m.group(3)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    headers = {"User-Agent": "ClawBytes/1.0"}
    # Unauthenticated GitHub API is 60 req/hr per IP — exhausted fast on shared
    # egress, which silently degrades ship summaries to "vX released".
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        body = clean_release_notes((_github_get_json(api_url, headers).get("body") or "").strip())
        if len(body) > 900:
            body = body[:897] + "..."
        if _release_diff_enabled():
            body = _augment_with_release_diff(owner, repo, tag, body, headers)
        return body
    except Exception:
        return ""


def fetch_changelog_markdown(url: str) -> str:
    """Mintlify-style docs serve the real page as raw markdown at <page>.md.

    Release-notes / changelog pages (Devin, Factory, Claude platform, xAI)
    render as JS shells in HTML — fetch_article_snippet gets nothing usable,
    so the curator only saw a date and wrote a generic 'check the changelog'
    blurb. The .md sibling carries the actual entries. Returns cleaned
    markdown (newest entries lead) or '' if there's no real markdown there.
    """
    base = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not base:
        return ""
    md_url = base if base.endswith(".md") else base + ".md"
    try:
        req = Request(md_url, headers={"User-Agent": "ClawBytes/1.0", "Accept": "text/markdown, text/plain, */*"})
        with urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    head = text[:200].lstrip().lower()
    if not text or head.startswith("<!doctype") or head.startswith("<html") or "<head" in head:
        return ""  # got an HTML shell, not markdown
    # Mintlify prepends a "> ## Documentation Index ..." blockquote callout;
    # skip leading blockquote/blank lines so the truncation keeps real entries.
    lines, skipping = [], True
    for ln in text.splitlines():
        if skipping and (not ln.strip() or ln.lstrip().startswith(">")):
            continue
        skipping = False
        lines.append(ln)
    text = "\n".join(lines).strip()
    return text[:1800] if text else ""


def _looks_like_changelog(url: str) -> bool:
    return ("/release-notes" in url or "/changelog" in url or "://docs." in url)


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


def grounding_for_item(category: str, item: dict) -> str:
    """Source-grounding text for one bundle item, '' when none applies.

    GitHub release URLs get distilled release notes in every lane; anything
    else gets an article snippet (paper abstracts, advisories, HN targets).
    Reddit thread pages ground poorly (login walls, comment noise) and the
    fetch burns rate limit — skip them; their engagement stats already ride
    in the prompt.
    """
    url = item.get("url", "") or ""
    if not url or "reddit.com" in url:
        return ""
    if "github.com" in url and "/releases/tag/" in url:
        body = fetch_release_body(url)
        return f"RELEASE NOTES: {body}" if body else ""
    if _looks_like_changelog(url):
        md = fetch_changelog_markdown(url)
        if md:
            return f"RELEASE NOTES: {md}"
    snippet = fetch_article_snippet(url)
    return f"ARTICLE SNIPPET: {snippet}" if snippet else ""


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
    item_emoji = {"ship": "📦", "watch": "🚨", "read": "📚", "community": "💬"}.get(category, meta["emoji"])
    prompt = f"""You write @clawbytes on Telegram — covering the AI agent ecosystem. Short, sharp, opinionated. No corporate filler.

Write a {meta['label']} lane post with {len(items)} items.

FORMAT (strict HTML):
{meta['emoji']} <b>{meta['label']}</b> — N items

{item_emoji} <a href="URL">ACTUAL TITLE</a> — 1 punchy sentence on why this matters

Rules:
- Use <b> for bold, <a href="URL">title text</a> for links — use the ACTUAL model/project name as link text, never "Link" or "thread"
- Ship: what changed, why operators care. Use the release notes if provided — don't invent features.
- Watch: the risk, what to check, what breaks
- Read: describe WHAT the piece IS (paper, framework, deep-dive, critique) and its core claim — but only if the source data states it. Never invent numbers, percentages, or specific findings. "A paper proposing formal metrics for agent reliability" beats "Defines 3 key metrics for agent failure rates" if you don't actually know there are 3.
- Community: the sentiment, the discovery, the real user signal. If multiple threads cover the same topic, MERGE them into one bullet (e.g. "3 threads on cost and access (58↑ total)") instead of listing each separately. NEVER repeat the raw summary text — write fresh, specific descriptions.
- MAX 150 chars per item summary. No filler. No "notable" or "worth watching." No "offering insights" or "highlights." No soft verbs: "breaks down", "unpacks", "dives into", "rages on", "sparks debate" are all banned.
- Do NOT parrot the raw summary text provided in the item data. Write original descriptions based on the actual title and topic.
- If release notes are missing or say nothing: just state what the project IS and what version dropped. 1 sentence max. Never speculate with "might," "could," or "should." Example: "IronClaw 0.1.0 — First skills release from the NEAR AI safety team." 
- Start EVERY item with {item_emoji} (this lane\u2019s emoji) and use the SAME emoji for every item. Never use another lane\u2019s emoji or a topical/decorative emoji.
- For HN items: reference the community signal (e.g. "171 points on HN") when it adds weight
- NEVER fabricate statistics, metrics, or specific findings. If unsure, describe the piece's ambition, not its results.
- Copy version numbers character-for-character from the item title or release notes. Never shorten, round, or invent a version.

Items:"""

    # Prefetch grounding in parallel — sequential fetches would add up to
    # ~10s/item of wall-clock to every bundle render.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        groundings = list(pool.map(lambda it: grounding_for_item(category, it), items))

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
        item_line = f"\n{i+1}. {title} | {url} | {raw_summary} | source: {source}"
        if groundings[i]:
            item_line += f" | {groundings[i]}"
        prompt += item_line

    prompt += "\n\nWrite the post now:"

    data = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "temperature": 0.4,
    }).encode()

    # One retry: enrichment failures degrade the channel to the bare fallback
    # renderer, so a transient proxy hiccup is worth a second attempt — and a
    # logged reason, since a silent None made this failure mode invisible.
    for attempt in range(2):
        try:
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
                print(f"llm_summarize({category}): rejected output (len={len(content)})", file=sys.stderr)
                return None
            return content
        except Exception as e:
            print(f"llm_summarize({category}) attempt {attempt + 1} failed: {e}", file=sys.stderr)
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
    if source_type == "reddit":
        return "discussion"
    if source_type == "moltbook":
        return "community"
    if source_type == "hackernews":
        return "discussion"
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


def format_category_bundle(category: str, limit: Optional[int] = None, use_llm: bool = True) -> str:
    """Format category bundle with optional LLM enrichment."""
    meta = CATEGORY_META[category]
    bundle = [hydrate_item(item) for item in bundle_for_category(category, limit)]
    
    if category == "ship":
        bundle = compress_ship_bundle(bundle)
    elif category == "community":
        bundle = compress_community_bundle(bundle)
    
    if not bundle:
        return f"{meta['emoji']} <b>{meta['label']}</b> — Nothing new"
    
    # Try LLM enrichment first
    if use_llm and LLM_API_KEY:
        llm_result = llm_summarize(bundle, category)
        if llm_result:
            # The LLM fills the "— N items" template literally and writes
            # "1 items" for a single-item lane; fix the singular.
            return re.sub(r"\b1 items\b", "1 item", llm_result)
    
    # Fallback: static template format
    lines = [f"{meta['emoji']} <b>{meta['label']}</b> — {len(bundle)} item{'s' if len(bundle) > 1 else ''}"]
    lines.append("")
    
    for item in bundle:
        title = display_title(item)
        url = item['url']
        
        # Short summary: first sentence, max 80 chars
        # Truncate at a word boundary with an ellipsis — a hard [:80] slice
        # published mid-word lines ("…from VLMs to wo") when enrichment fell
        # back to this renderer.
        summary = re.sub(r"\s+", " ", (item.get('summary') or '').split('.')[0].strip())
        if len(summary) > 110:
            summary = summary[:110].rsplit(" ", 1)[0] + "…"
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


def telegram_html_to_mrkdwn(text: str) -> str:
    """Convert our own Telegram HTML (only <b>/<i>/<code>/<a href>) to Slack
    mrkdwn. Slack shares the &amp;/&lt;/&gt; entity escapes, so those stay;
    &quot; is not a Slack escape and unescapes back to a quote."""
    out = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"<\1|\2>", text)
    out = re.sub(r"</?b>", "*", out)
    out = re.sub(r"</?i>", "_", out)
    out = re.sub(r"</?code>", "`", out)
    return out.replace("&quot;", '"')


def mirror_to_slack(message: str) -> None:
    """Mirror a channel post to the Slack audience channel.

    Best-effort by contract: Slack being down or unconfigured must never
    block or fail a Telegram publish. Delegates the HTTP + mrkdwn conversion
    to the shared publish core (which converts the same <b>/<i>/<code>/<a>
    subset this channel emits).
    """
    _ensure_publisher().mirror_to_slack(message)


# --- Telegram send hardening -------------------------------------------------
# A publish path that raises on a transient Telegram blip aborts the whole
# autopublish loop and skips every later lane. These helpers make the send
# fail-soft (bool, never raise), truncable (so an oversize bundle can't 400),
# retryable (429/5xx honoring Retry-After), and gated (channel-harm content is
# rejected before it can 400). Mirrors modelbytes' send_telegram_post. Slack
# mirroring fires only on a successful Telegram send so the two audience
# surfaces never desync.
TELEGRAM_MAX_CHARS = 4096  # re-exported from the shared core for tests/callers that read it


def _truncate_for_telegram(message: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Truncate at the last newline before Telegram's 4096-char limit, with a
    marker. Delegates to the shared publish core; kept as a module function so
    existing callers and tests are unchanged."""
    from ss_publish import truncate_for_telegram
    return truncate_for_telegram(message, limit)


def validate_lane_for_publish(body: str) -> Tuple[bool, List[str]]:
    """Channel-harm content gate. Returns (ok, errors). ERROR-only: things that
    would make Telegram 400 (empty body, unbalanced markup). Format drift is
    NOT blocked — the deterministic renderer has no fallback, so blocking it
    would silence the lane. On (False, [...]) the caller must NOT send or
    mark_posted; the lane retries next cycle.

    Length is handled by _truncate_for_telegram inside send_telegram, not here.
    """
    stripped = (body or "").strip()
    if not stripped:
        return (False, ["empty body"])
    errors: List[str] = []
    open_stack: List[str] = []
    # Only the tags we actually emit (see telegram_html_to_mrkdwn): <b>/<i>/<code>/<a href>.
    # User content is html_escape()'d, so any literal '<' is an entity — the only
    # '<...>' tokens here are our own tags.
    token_re = re.compile(r"</?(b|i|code|a)(\s[^>]*)?>", re.IGNORECASE)
    for m in token_re.finditer(stripped):
        token = m.group(0)
        tag = m.group(1).lower()
        if token.startswith("</"):
            if not open_stack or open_stack[-1] != tag:
                errors.append(f"unbalanced closing </{tag}>")
                break
            open_stack.pop()
        else:
            open_stack.append(tag)
    if not errors and open_stack:
        errors.append(f"unclosed tag(s): <{'>, <'.join(open_stack)}>")
    return (not errors, errors)


def send_telegram(message: str) -> bool:
    """Send one message to the @clawbytes channel; mirror to Slack on success.

    Returns True on success, False on any failure — and never raises. A raising
    send aborts the whole autopublish loop and skips later lanes; a False return
    leaves the lane un-marked-posted so it retries next cycle. Delegates the
    HTTP mechanics (truncate to 4096, retry 429/5xx honoring Retry-After,
    fail-soft) to the shared publish core. The Slack mirror fires only on a
    successful Telegram send so the two audience surfaces never desync.
    """
    global _publisher
    if _publisher is None:
        _publisher = _ensure_publisher()
    pub = _publisher
    if not pub.telegram_token:
        print("Telegram bot token not found", file=sys.stderr)
        return False
    result = pub.send_telegram(message)
    if not result.ok:
        from ss_publish import redact_secrets
        print(redact_secrets(f"Telegram send error: {result.error}", pub.secret_values),
              file=sys.stderr)
    return result.ok


def mark_posted(category: str, limit: Optional[int] = None, posted_items: Optional[List[dict]] = None) -> List[dict]:
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    bundle = posted_items if posted_items is not None else bundle_for_category(category, limit)
    posted_ids = {item.get("id") for item in bundle if item.get("id")}
    posted_urls_to_mark = {item.get("url") for item in bundle if item.get("url")}
    posted_urls = set(state.get("postedUrls", []))
    posted_backlog_ids = set(state.get("postedBacklogIds", []))

    for item in backlog.get("items", []):
        if item.get("id") in posted_ids or item.get("url") in posted_urls_to_mark:
            item["status"] = "posted"
            item["postedCategories"] = sorted(set(item.get("postedCategories", []) + [category]))
            posted_urls.add(item["url"])
            posted_backlog_ids.add(item["id"])

    posted_urls.update(posted_urls_to_mark)
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


def _curator_enabled_for(category: str) -> bool:
    """Whether autopublish should run the curator pass on this lane.

    Off by default. Enable with CLAWBYTES_USE_CURATOR=1; restrict to specific
    lanes with CLAWBYTES_CURATOR_LANES (comma-separated; default all four).
    The curator backend (Claude vs Ollama model) is chosen inside curator.py.
    """
    if os.environ.get("CLAWBYTES_USE_CURATOR", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    lanes = os.environ.get("CLAWBYTES_CURATOR_LANES", "ship,watch,read,community")
    return category in {l.strip().lower() for l in lanes.split(",") if l.strip()}


def _publish_lane(category: str, send: bool) -> tuple:
    """Publish one ready lane; return (sent, count).

    When the curator is enabled for the lane it runs the editorial pass and, on
    a successful curated result, sends the consolidated curated message. ANY
    curator failure, fallback marker, or whole-lane decline falls through to the
    deterministic renderer — channel reliability always wins over editorial
    purity. A whole-lane decline does NOT silence the lane; it posts the
    deterministic bundle (the curator can still drop weak individual items on
    approved lanes).

    Send failures (Telegram down, content-gate rejection) return (False, count)
    WITHOUT marking the lane posted, so it retries on the next autopublish cycle.
    Any unexpected crash here is also caught by autopublish() so it cannot abort
    the whole lane loop."""
    if _curator_enabled_for(category):
        timeout = int(os.environ.get("CLAWBYTES_CURATOR_TIMEOUT", "300"))
        curated = run_curator_subprocess(curator_input_bundle(category), timeout=timeout)
        if curated is not None:
            meta = curated.get("_curator", {})
            if not meta.get("fallback") and meta.get("approved", True):
                items = curated.get("items") or []
                if send and items:
                    message = format_curated_html(curated, category)
                    ok, errs = validate_lane_for_publish(message)
                    if ok and send_telegram(message):
                        mark_posted(category, None, items)
                        return (True, len(items))
                    if not ok:
                        print(f"[autopublish] curated {category} rejected by gate: "
                              f"{'; '.join(errs)}", file=sys.stderr)
                    return (False, len(items))
                return (False, len(items))
            if not meta.get("approved", True):
                # Breadth over purity: a whole-lane decline falls back to the
                # deterministic post rather than going silent. The curator still
                # improves approved lanes and drops weak *individual* items.
                print(f"[autopublish] curator declined {category}; using deterministic bundle", file=sys.stderr)
            # fallback marker or decline → fall through to deterministic
        # curated is None (curator failed) → fall through to deterministic

    message = format_category_bundle(category)
    bundle = bundle_for_category(category)
    if send and bundle:
        ok, errs = validate_lane_for_publish(message)
        if not ok:
            print(f"[autopublish] {category} rejected by gate: {'; '.join(errs)}", file=sys.stderr)
            return (False, len(bundle))
        if send_telegram(message):
            mark_posted(category)
            return (True, len(bundle))
        print(f"[autopublish] {category} Telegram send failed; not marked posted "
              f"(will retry next cycle)", file=sys.stderr)
        return (False, len(bundle))
    return (False, len(bundle))


def autopublish(send: bool = False) -> List[dict]:
    collect_into_backlog()
    state = load_json(THREAD_STATE_FILE, {})
    results = []
    for category in CATEGORY_META:
        ready = lane_ready(category, state)
        sent = False
        count = 0
        if ready["ready"]:
            try:
                sent, count = _publish_lane(category, send)
            except Exception as e:  # noqa: BLE001 - one lane must not abort the loop
                print(f"[autopublish] {category} crashed: {e!r}", file=sys.stderr)
                sent, count = False, 0
            if sent:
                state = load_json(THREAD_STATE_FILE, {})
        results.append({"category": category, **ready, "sent": sent, "count": count})
    return results


def _fetch_item_context(item: dict) -> dict:
    """Fetch real source content for a single bundle item.

    Curator output is only as good as its input. Without this, curator gets just
    titles and writes generic prose. With this, curator sees release notes /
    article excerpts and can extract a real operator-relevant
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

    # Mintlify-style changelog/release-notes pages — fetch the raw .md (the
    # HTML is a JS shell). Gives the curator the real entries, not just a date.
    elif _looks_like_changelog(url):
        md = fetch_changelog_markdown(url)
        if md:
            context["fetched"]["release_notes"] = md
            context["fetched"]["release_notes_source"] = "mintlify_md"
        else:
            snippet = fetch_article_snippet(url)
            if snippet:
                context["fetched"]["page_excerpt"] = snippet[:1200]
                context["fetched"]["page_excerpt_source"] = "page_text"

    # Other URLs — try article snippet (gracefully skips paywalls, JS-heavy sites, etc.)
    elif url.startswith("http"):
        snippet = fetch_article_snippet(url)
        if snippet:
            context["fetched"]["page_excerpt"] = snippet[:1200]
            context["fetched"]["page_excerpt_source"] = "page_text"

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

    The curator gets a WIDER candidate pool than a deterministic post (default
    CURATOR_INPUT_LIMIT) so that after it drops off-scope/noise items, enough
    in-scope ones remain to fill the lane (3-5). A lane like Read has dozens of
    candidates but only the top few are in-scope; feeding only `default_limit`
    starved it to one survivor.
    """
    meta = CATEGORY_META[category]
    effective_limit = limit if limit is not None else CURATOR_INPUT_LIMIT
    raw_items = [hydrate_item(item) for item in bundle_for_category(category, effective_limit)]

    if category == "ship":
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


def run_curator_subprocess(bundle: dict, timeout: int = 300) -> Optional[dict]:
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
    """Single consolidated message: lane header + one compact line per item +
    an optional editorial Take. Matches the deterministic lane style
    (emoji Title — blurb) so the channel gets ONE post with a few entries
    rather than a separate message per item.
    """
    meta = CATEGORY_META[category]
    item_emoji = {"ship": "📦", "watch": "🚨", "read": "📚", "community": "💬"}.get(category, meta["emoji"])
    items = curated.get("items") or []
    n = len(items)
    lines = [f"{meta['emoji']} <b>{meta['label']} — {n} item{'s' if n != 1 else ''}</b>"]
    for item in items:
        title = item.get("title") or ""
        url = item.get("url") or ""
        blurb = (item.get("blurb") or item.get("existing_blurb") or "").strip()
        line = f"\n{item_emoji} <a href=\"{url}\">{html_escape(title)}</a>"
        if blurb:
            line += f" — {html_escape(blurb)}"
        lines.append(line)
    take = (curated.get("take") or "").strip()
    if take:
        lines.append("")
        lines.append(f"<i>{html_escape(take)}</i>")
    return "\n".join(lines)


def format_curated_messages(curated: dict, category: str) -> List[str]:
    """Render the curated bundle as a LIST of Telegram messages — one per item.

    Each message is self-contained, focused on a single item. The lead signal
    (if present) attaches to the first item's message for context. The take,
    if present, becomes its own short closing message.
    """
    meta = CATEGORY_META[category]
    items = curated.get("items") or []
    if not items:
        return []

    messages: List[str] = []
    lead = (curated.get("lead_signal") or "").strip()

    for idx, item in enumerate(items):
        title = item.get("title") or ""
        url = item.get("url") or ""
        blurb = (item.get("blurb") or item.get("existing_blurb") or "").strip()

        lines = [f"{meta['emoji']} <b>{meta['label']}</b>"]

        # Lead signal only on the first item, if present
        if idx == 0 and lead:
            lines.append("")
            lines.append(f"<i>{html_escape(lead)}</i>")

        lines.append("")
        lines.append(f"<a href=\"{url}\">{html_escape(title)}</a>")
        if blurb:
            lines.append(html_escape(blurb))

        messages.append("\n".join(lines))

    take = (curated.get("take") or "").strip()
    if take:
        messages.append(f"{meta['emoji']} <i>{html_escape(take)}</i>")

    return messages


def send_telegram_message_list(messages: List[str], pace_seconds: float = 1.5) -> int:
    """Send a list of messages to the channel, pacing for readable order.

    Returns the count of messages successfully sent. A mid-list failure does
    not raise (send_telegram is fail-soft); remaining messages are still
    attempted so a single blip can't drop a whole multi-message post."""
    sent = 0
    for i, msg in enumerate(messages):
        if send_telegram(msg):
            sent += 1
        if i < len(messages) - 1:
            time.sleep(pace_seconds)
    return sent


def main() -> int:
    ensure_files()

    parser = argparse.ArgumentParser(description="ClawBytes category thread system")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--run-monitors", action="store_true", help="Run source monitors before collecting")
    p_collect.add_argument("--summary", action="store_true", help="Print compact counts without the full item payload")
    sub.add_parser("status")
    p_audit = sub.add_parser("audit", help="Explain source ingestion, classification, and bundle decisions")
    p_audit.add_argument("--run-monitors", action="store_true", help="Refresh monitor state before auditing")
    p_audit.add_argument("--collect-first", action="store_true", help="Collect current source state into the backlog before auditing")
    p_audit.add_argument("--category", choices=list(CATEGORY_META.keys()), help="Only show decisions for one lane")
    p_audit.add_argument("--limit", type=int, default=40, help="Maximum source-item decisions to print")
    p_audit.add_argument("--json", action="store_true", help="Print machine-readable audit JSON")
    p_auto = sub.add_parser("autopublish")
    p_auto.add_argument("--send", action="store_true")

    p_prev = sub.add_parser("preview")
    p_prev.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_prev.add_argument("--limit", type=int)
    p_prev.add_argument("--collect-first", action="store_true")
    p_prev.add_argument("--use-curator", action="store_true", help="Run Claude curator on the bundle and print the result")
    p_prev.add_argument("--curator-timeout", type=int, default=300, help="Curator subprocess timeout in seconds")

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--category", choices=list(CATEGORY_META.keys()), required=True)
    p_pub.add_argument("--limit", type=int)
    p_pub.add_argument("--collect-first", action="store_true")
    p_pub.add_argument("--send", action="store_true")
    p_pub.add_argument("--if-ready", action="store_true")
    p_pub.add_argument("--use-curator", action="store_true", help="Run Claude curator on the bundle before sending")
    p_pub.add_argument("--curator-timeout", type=int, default=300, help="Curator subprocess timeout in seconds")

    args = parser.parse_args()

    if args.cmd == "collect":
        if getattr(args, "run_monitors", False):
            run_monitors()
        result = collect_into_backlog()
        if getattr(args, "summary", False):
            print(json.dumps({"added": result["added"], "counts": result["counts"]}, indent=2))
        else:
            print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "status":
        print_status()
        return 0

    if args.cmd == "audit":
        if getattr(args, "run_monitors", False):
            run_monitors()
        if getattr(args, "collect_first", False):
            collect_into_backlog()
        report = audit_sources(category=args.category, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        else:
            print_audit(report)
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
            meta = curated.get("_curator", {})
            if meta.get("fallback"):
                print(format_category_bundle(args.category, args.limit))
                print("\n(curator fell back; printed deterministic bundle)", file=sys.stderr)
                print(json.dumps(meta, indent=2), file=sys.stderr)
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
                    if send_telegram(message):
                        mark_posted(args.category, args.limit)
                    else:
                        print("[publish] Telegram send failed; lane not marked posted", file=sys.stderr)
                        return 1
                return 0
            meta = curated.get("_curator", {})
            if meta.get("fallback"):
                print("(curator fell back; using deterministic bundle)", file=sys.stderr)
                print(json.dumps(meta, indent=2), file=sys.stderr)
                message = format_category_bundle(args.category, args.limit)
                print(message)
                bundle = bundle_for_category(args.category, args.limit)
                if args.send and bundle:
                    if send_telegram(message):
                        mark_posted(args.category, args.limit)
                    else:
                        print("[publish] Telegram send failed; lane not marked posted", file=sys.stderr)
                        return 1
                return 0
            if not meta.get("approved", True):
                # Curator explicitly skipped
                print("(curator declined to approve — skipping publish)", file=sys.stderr)
                print(json.dumps(meta, indent=2), file=sys.stderr)
                return 0
            # One consolidated message (header + compact item lines + take)
            message = format_curated_html(curated, args.category)
            print(message)
            print("\n--- curator metadata ---", file=sys.stderr)
            print(json.dumps(meta, indent=2), file=sys.stderr)
            items = curated.get("items") or []
            if args.send and items:
                if send_telegram(message):
                    print(f"[publish] sent consolidated {args.category} post ({len(items)} items) to Telegram", file=sys.stderr)
                    mark_posted(args.category, args.limit, items)
                else:
                    print("[publish] Telegram send failed; lane not marked posted", file=sys.stderr)
                    return 1
            return 0

        message = format_category_bundle(args.category, args.limit)
        print(message)
        bundle = bundle_for_category(args.category, args.limit)
        if args.send and bundle:
            if send_telegram(message):
                mark_posted(args.category, args.limit)
            else:
                print("[publish] Telegram send failed; lane not marked posted", file=sys.stderr)
                return 1
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
