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
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ss_publish import Publisher as _SsPublisher

_SCRIPTS_DIR = str(Path(__file__).resolve().parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from completion_diag import empty_content_message  # noqa: E402
from feed_filters import (  # noqa: E402
    STATUS_FEED_NAMES,
    STATUS_WATCH_FEEDS,
    havoptic_feed,
    incident_public_url,
    is_reported_claim,
    kilo_product_post,
    status_blurb,
    status_display_title,
    status_incident_allowed,
    status_item_predates_watch,
    testingcatalog_blurb,
    testingcatalog_relevant,
    urls_same_release,
    version_key,
)
from title_text import first_stated_date, flatten_inline_markup  # noqa: E402

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
    "kiro": 64,
    "gemini": 62,
    "factory": 62,
    "windsurf": 62,
    "grok build": 62,  # vocab for HN/Reddit; GitHub atom is empty (2026-09)
    "opencode": 60,
    "pi coding": 60,  # never bare "pi" — ⊂ picoclaw, api
    "deepseek harness": 60,  # npm @deepseek-ai/dsh. Never bare "dsh"
    "fx coding": 60,  # vercel-labs/fx. Never bare "fx" — ⊂ firefox
    "oh my pi": 60,  # OMP (can1357/oh-my-pi). Never bare "omp" — ⊂ compile/complete
    "herdr": 60,
    "openai-agents": 60,
    "agent client protocol": 60,  # compound — not bare "acp"
    "openhands": 58,
    "aider": 58,
    "mcp": 58,
    "warp blog": 58,
    "replit": 58,
    "augment code": 58,
    "cline": 56,
    "kilo code": 58,
    "kilo blog": 58,  # blog.kilo.ai product posts. Never bare "kilo" — ⊂ kilobyte
    "kimi code": 58,
    "open interpreter": 56,
    "deep agents": 56,
    "mistral vibe": 56,
    "vercel-ai": 56,
    "codewhale": 54,
    "mimo code": 54,
    "agno-agi": 54,  # never bare "agno" — ⊂ agnostic
    "tau coding": 52,  # never bare "tau"
    "roo code": 55,
    "continue": 54,
    "goose": 54,
    "qwen code": 54,
    "smolagents": 54,
    "junie": 56,  # before "jetbrains"
    "jetbrains": 56,
    "zed": 58,  # clears Ship's morning bar without an age bonus; not a READ_TERMS token
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
# Exact names that Ship. Marketing blogs (Warp, Replit, Augment, JetBrains,
# Zed, Windsurf) are not in this tuple: a keyword hit on those feeds is Read.
# Cursor Changelog, Copilot Changelog, and Amp News stay on the Ship path.
# Devin/Factory Release Notes Ship because the feed name contains "release notes".
CHANGELOG_SHIP_FEED_NAMES = (
    "cursor changelog",
    "github copilot changelog",
    "amp news",
    "kilo blog",
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
    # 2026-09 widening. Compounds only — see REPO_PRIORITY traps.
    "kiro", "kilo code", "kimi code", "mistral vibe", "grok build",
    "pi coding", "oh-my-pi", "oh my pi", "omp.sh", "herdr", "fx coding",
    "open interpreter", "deep agents", "codewhale",
    "mimo code", "agno-agi", "tau coding",
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
    """Read JSON. A torn write (collect overlapping a monitor) is an empty read.

    Callers retry next cycle. Raising here aborts collect_into_backlog, which
    is the first thing autopublish does, so that hour's lanes would not post.
    """
    if path.exists():
        try:
            text = path.read_text()
        except OSError:
            return default
        if text.strip():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return default
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
    """Parse an ISO-8601 or RFC 822 timestamp into aware UTC.

    RSS pubDate values are RFC 822 (`Fri, 18 Sep 2026 13:42:27 +0000` or
    `GMT`). A naive ISO timestamp is treated as UTC so age_score can subtract
    it from now_utc() without a TypeError that aborts the whole collect.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(text)
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


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
        "zed": "Zed",
        "agent client protocol": "ACP",
        "kiro": "Kiro",
        "pi coding": "Pi",
        "deepseek harness": "DeepSeek Harness",
        "fx coding": "fx",
        "oh my pi": "OMP",
        "herdr": "Herdr",
        "kilo code": "Kilo Code",
        "kilo blog": "Kilo",
        "kimi code": "Kimi Code",
        "grok build": "Grok Build",
        "open interpreter": "Open Interpreter",
        "deep agents": "Deep Agents",
        "mistral vibe": "Mistral Vibe",
        "codewhale": "Codewhale",
        "mimo code": "MiMo Code",
        "agno-agi": "AGNO",
        "tau coding": "Tau",
    }.get(repo, repo.title())


def normalize_release_title(repo: str, title: str) -> str:
    clean = flatten_inline_markup(title or "")
    repo_label = display_repo_name(repo)

    if not clean:
        return repo_label

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

    # A changelog sentence often uses the product name in the middle
    # ("Dynamic workflows in Copilot CLI and the Copilot app"). Deleting
    # every occurrence leaves "in  CLI and the  app". Match whole words so
    # a short repo key does not hit inside an unrelated word.
    named = False
    for label in (repo_label, repo):
        if label and re.search(rf"\b{re.escape(label)}\b", clean, re.IGNORECASE):
            named = True
            break
    if named:
        return clean

    if re.match(r"^(v?\d+[\w.\-]*(?:\s*[-–]\s*\d{4}-\d{2}-\d{2})?)$", clean):
        return f"{repo_label} {clean}"

    if clean.startswith("0.") or clean.startswith("1.") or clean.startswith("2."):
        return f"{repo_label} {clean}"

    return clean


def age_score(dt: Optional[datetime], max_hours: int) -> float:
    if not dt:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        hours = max(0.0, (now_utc() - dt).total_seconds() / 3600)
    except TypeError:
        return 0.0
    return max(0.0, max_hours - hours)


def is_acp_crate_churn(feed: str, title: str) -> bool:
    """ACP's GitHub atom mixes Schema tags with Rust crate bumps.

    The monorepo ships both on the same day (3–4 crate/schema pairs a week).
    Keep stable Schema v1.* only — v2 alphas and rust-crate titles are churn.
    """
    if "agent client protocol" not in (feed or "").lower():
        return False
    return not re.search(r"\bschema\s+v?1\.", (title or "").lower())


def is_deepagents_sidecar_churn(feed: str, title: str) -> bool:
    """LangChain Deep Agents atom mixes the coding CLI with ACP/talon packages.

    Keep `deepagents-code` and the core `deepagents==` SDK; drop sidecar bumps.
    """
    if "deep agents" not in (feed or "").lower():
        return False
    low = (title or "").lower()
    return "deepagents-acp" in low or "deepagents-talon" in low


def is_prerelease_title(title: str) -> bool:
    """True for a real pre-release tag, not those letters inside a stable title.

    Drops `v2.0.0-alpha.2`, `v7.7.0 (pre-release)`, and `Preview build …`.
    Keeps `Alphabetical tool index v2.0.0` and `v2.0.0 Adds preview of
    background agents` — the token has to sit on the version, or lead the title.
    """
    low = (title or "").lower()
    if re.search(r"\b(?:pre-release|prerelease)\b", low):
        return True
    if re.match(r"\s*(?:preview|nightly|canary|alpha|beta|staging)\b", low):
        return True
    # Separators only. A run of whitespace must not swallow later words, or
    # "v2.0.0 Adds preview …" matches `preview` and the stable release drops.
    if re.search(
        r"v?\d+\.\d+(?:\.\d+)?(?:[-.]+|\s+)?(?:alpha|beta|rc|preview|nightly|canary|staging)\b",
        low,
    ):
        return True
    # Compact pre-release tags (PEP 440: 1.14.6a2, 2.0rc1, 1.0b3, v0.22.0rc2).
    if re.search(r"\d+\.\d+(?:\.\d+)?(?:a|b|rc)\d+", low):
        return True
    if re.search(r"(?:python|rust|node|js)-v?\d+\.\d+\.\d+(?:a|b|rc)\d+", low):
        return True
    if re.search(r"(?<![a-z])rc[.\-]?\d", low):
        return True
    return False


def is_minor_release(title: str) -> bool:
    """Demote patches and real pre-release tags. Do not demote on substrings.

    `dev` inside `developer`, `fix` inside `prefix`/`fixed`, and `patch`
    inside `dispatch` are not patch releases. `X.Y.Z` with Z>0 still is.
    """
    low = (title or "").lower()
    if is_prerelease_title(title):
        return True
    if re.search(r"\b(?:dev|experimental|hotfix|patch|fix|minor)\b", low):
        return True
    # Patch releases: any X.Y.Z with Z>0 (e.g. 2.1.160, 1.2.4, 0.4.2) — demote so
    # routine patches don't headline. Keep .0 minor/major releases (1.2.0, 2.0.0)
    # headline-worthy, and preserve the first-cut 0.0.1 exception.
    if re.search(r"v?\d+\.\d+\.[1-9]\d*\b", low) and not re.search(r"v?0\.0\.1\b", low):
        return True
    # Date-based versioning (e.g. 20260413.04, 20260409.01) — treat .XX suffix as patch
    if re.search(r"20\d{6}\.\d+", low):
        return True
    return False


# OpenClaw publishes opaque `release-publish/<digits>` tags next to real
# CalVer releases. Hermes publishes asset bundles whose release name is
# "Pinned inputs N" (or "Pinned inputs a/b/9") and whose git tag is
# `inputs-8` / `inputs-b`. The atom `<title>` is whichever GitHub stored.
# Post #968 (tag inputs-8) used the tag as the title, which the name-only
# fullmatch did not see. Neither shape is a product release.
_JUNK_RELEASE_TAG = re.compile(r"release-publish/\d+", re.IGNORECASE)
_HERMES_ASSET_TITLE = re.compile(
    r"(?i)^(?:.*?(?:[:\u2014\u2013]| - )\s*)?pinned inputs(?:\s+\S+)?$"
)
_HERMES_INPUTS_TAG = re.compile(r"(?i)^inputs[-_][0-9a-z][0-9a-z._-]*$")
_RELEASE_TAG_IN_URL = re.compile(r"/releases/tag/([^/?#]+)", re.IGNORECASE)


def _release_tag(url: str) -> str:
    match = _RELEASE_TAG_IN_URL.search(url or "")
    if not match:
        return ""
    return unquote(match.group(1)).strip()


def is_junk_release_title(title: str, url: str = "") -> bool:
    text = (title or "").strip()
    if text and _JUNK_RELEASE_TAG.search(text):
        return True
    if text and _HERMES_ASSET_TITLE.fullmatch(text):
        return True
    # The #968 path: the feed title is the tag itself (`inputs-8`), or the
    # tag is only in the release URL while the title is a generic label.
    if text and _HERMES_INPUTS_TAG.fullmatch(text):
        return True
    tag = _release_tag(url)
    return bool(tag and _HERMES_INPUTS_TAG.fullmatch(tag))


def is_coding_agent_changelog(item: dict) -> bool:
    """True when this RSS item is a closed-source harness changelog or news feed.

    Cursor Changelog / Amp News / Copilot Changelog are not named "releases",
    so they need this path to reach Ship. A `coding-agent` tag plus the word
    `blog` is not enough — that shipped Warp/Replit/Augment/JetBrains/Zed
    marketing into a 4-item lane. LangChain, Mistral, and DeepMind blogs stay
    on the Read path. The name tuple covers backlog items that landed without tags.
    """
    feed = (item.get("feed") or "").lower().strip()
    if feed in CHANGELOG_SHIP_FEED_NAMES:
        return True
    tags = {str(t).lower() for t in (item.get("tags") or [])}
    if "coding-agent" not in tags:
        return False
    return "changelog" in feed or feed.endswith(" news") or " news" in f" {feed}"


# ArXiv cs.AI / cs.CL are research firehoses. Bare "agent" (a READ_TERM) is too
# wide; require a harness compound. Do not put "releases" in those feed names
# or the keyword gate turns off.
ARXIV_FEEDS = ("arxiv cs.ai", "arxiv cs.cl")
ARXIV_HARNESS_TERMS = (
    "coding agent",
    "coding harness",
    "agent harness",
    "tool use",
    "tool-use",
    "function calling",
    "claude code",
    "mcp",
    "subagent",
    "computer use",
)

# Short READ_TERMS that are substrings of unrelated words.
_BOUNDARY_READ_TERMS = {"cursor", "aider"}


def _read_term_in(text: str, term: str) -> bool:
    if term in _BOUNDARY_READ_TERMS:
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def arxiv_harness_hit(text: str) -> bool:
    low = (text or "").lower()
    return any(term in low for term in ARXIV_HARNESS_TERMS)


def _reddit_term_in(text: str, term: str) -> bool:
    """Watch/Read reddit terms. `bug` is not the prefix of `debug`; `vs` is not `devs`."""
    if term == "bug":
        return re.search(r"\bbug\b", text) is not None
    if term == "vs":
        return re.search(r"\bvs\.?\b", text) is not None or " versus " in f" {text} "
    return term in text


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
        # Anything except the three history feeds stays dropped. A status title
        # can contain a READ_TERM ("opus 4"); falling through would file it as Read.
        # The three feeds reach Watch only after the incident filter.
        if feed_low not in STATUS_WATCH_FEEDS or not status_incident_allowed(feed, item):
            return None
        display = status_display_title(feed, title)
        blurb = status_blurb(feed, item)
        score = 64 + age_score(dt, 96) / 10
        return {
            "primaryCategory": "watch",
            "categories": ["watch"],
            "score": round(score, 2),
            "summary": trim(blurb, 140),
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["watch"]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": display,
        }

    if feed_low == "kilo blog" and not kilo_product_post(item):
        return None

    if feed_low == "testingcatalog":
        if not testingcatalog_relevant(item):
            return None
        reported = bool(item.get("reported")) or is_reported_claim(title, item.get("summary") or "")
        blob = f"{title} {item.get('summary') or ''}".lower()
        outage = any(word in blob for word in ("outage", "disruption", "degraded"))
        primary = "watch" if outage and not reported else "ship"
        repo = repo_name_from_feed(f"{title} {item.get('summary') or ''}")
        base = REPO_PRIORITY[repo] if repo in REPO_PRIORITY else 60
        if reported:
            base = min(base, 60)
        score = base + age_score(dt, 96) / 8
        candidate = {
            "primaryCategory": primary,
            "categories": [primary],
            "score": round(score, 2),
            "summary": trim(testingcatalog_blurb(item, reported), 140),
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META[primary]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": title,
        }
        if reported:
            candidate["reported"] = True
        return candidate

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
        if is_junk_release_title(title, url):
            return None
        if is_prerelease_title(title):
            return None
        # Skip chore/ci/internal/dependency release titles
        if any(x in low for x in ["chore:", "ci:", "build:", "internal", "rusty-v8", "dependency"]):
            return None
        if is_acp_crate_churn(feed, title):
            return None
        if is_deepagents_sidecar_churn(feed, title):
            return None
        repo = repo_name_from_feed(feed)
        display_title = normalize_release_title(repo, title)
        base_score = REPO_PRIORITY.get(repo, 50)
        # Penalize minor releases that stay on Ship (CalVer day trains, prose
        # like "minor"). Patch and pre-release rows are moved to Read at intake.
        if is_minor_release(title):
            base_score = max(20, base_score - 30)
        score = base_score + age_score(dt, 96) / 8
        summary = "New release" if repo == "openclaw" else f"New {repo.title()} release"
        candidate = {
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
        if havoptic_feed(feed) or item.get("aggregator") == "havoptic":
            candidate["aggregator"] = "havoptic"
        return candidate

    if feed_low.strip() in ARXIV_FEEDS and not arxiv_harness_hit(low):
        return None

    if feed_low.strip() == "claude.dev blog":
        # First-party build log. Titles are often "Building with Claude
        # Sonnet 5.5" and miss READ_TERMS, but the feed itself is the scope.
        score = 40 + age_score(dt, 168) / 10 + (15 if item.get("high_signal") else 5)
        return {
            "primaryCategory": "read",
            "categories": ["read"],
            "score": round(score, 2),
            "summary": richer_read_summary(title, feed),
            "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["read"]["ttl_hours"]),
            "publishedAt": dt,
            "sourceType": "rss",
            "sourceName": feed,
            "sourceId": item.get("id", url),
            "url": url,
            "title": title,
        }

    if any(_read_term_in(low, term) for term in READ_TERMS):
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
    if any(term in low for term in SECURITY_TERMS) or any(_reddit_term_in(low, term) for term in WATCH_REDDIT_TERMS):
        categories = ["watch", "community"]
    elif any(_reddit_term_in(low, term) for term in READ_REDDIT_TERMS):
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
    elif any(t in low for t in ["architecture", "framework", "why ", "protocol", "deep dive"]):
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
    """Classify HuggingFace Daily Papers into Read, plus Community on an explicit hint."""
    url = item.get("url", "")
    title = item.get("title", "")
    if not url or not title:
        return None
    dt = parse_dt(item.get("publishedAt", "") or item.get("found_at", ""))
    upvotes = int(item.get("upvotes", 0) or 0)
    relevance = int(item.get("score", 0) or 0)
    summary_text = item.get("ai_summary") or "HF Daily Papers signal"

    hint = item.get("category_hint") if item.get("category_hint") in CATEGORY_META else "read"
    # HF papers are context/research signals, not Ship releases. GitHub/project
    # links add confidence, but should not route the item into Ship.
    if hint in {"ship", "watch"}:
        hint = "read"
    # Papers never route to Watch — that lane is for actionable incidents and
    # advisories (security monitor, status feeds), not academic research. Even
    # security-flavored papers belong in Read. Keeps Watch tight and Read full.
    # Community is added only for an explicit community hint already on the
    # item. Keyword hits ("agent", "benchmark", "tool", "harness") used to
    # dual-tag nearly every paper into Community and flooded that lane.
    if item.get("lane") == "community":
        hint = "community"
    categories = ["read"]
    if hint == "community":
        categories = ["community", "read"]

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


def classify_advisory(item: dict) -> Optional[dict]:
    """GitHub Advisory Database hit on an allowlisted package → Watch.

    The monitor baselines silently and caps emits, so arrivals here are
    already filtered. Score clears Watch's morning bar (25) without an age bonus.
    """
    url = item.get("url") or ""
    title = item.get("title") or ""
    if not url or not title:
        return None
    dt = parse_dt(item.get("published") or item.get("found_at") or "")
    score = 48 + age_score(dt, 168) / 10
    package = item.get("package") or "GitHub Advisory"
    return {
        "primaryCategory": "watch",
        "categories": ["watch"],
        "score": round(score, 2),
        "summary": item.get("summary") or f"Security advisory in {package}",
        "expiresAt": (dt or now_utc()) + timedelta(hours=CATEGORY_META["watch"]["ttl_hours"]),
        "publishedAt": dt,
        "sourceType": "advisory",
        "sourceName": package,
        "sourceId": item.get("id") or url,
        "url": url,
        "title": title,
    }


def classify_ecosystem_release(item: dict) -> Optional[dict]:
    """A discovered-repo GitHub release, scored on the same path as release atoms.

    The shell monitor baselines a repo's first tag (emits nothing) and leaves
    later tags unseen until this collect hands them to the backlog.
    """
    repo = (item.get("repo") or "").strip()
    tag = (item.get("tag") or "").strip()
    title = (item.get("name") or "").strip() or tag
    url = item.get("url") or ""
    if not repo or not tag or not url or not title:
        return None
    return classify_rss({
        "feed": f"{repo} Releases",
        "title": title,
        "link": url,
        "published": item.get("published") or "",
        "id": f"{repo}:{tag}",
    })


def _mark_ecosystem_releases_seen(releases: List[dict]) -> None:
    """Record tags only after collect has handed them to the classifier.

    The shell does not mark an emitted tag seen. If this write doesn't happen,
    the next monitor run emits the same tag again.
    """
    if not releases:
        return
    path = MEMORY / "claw-ecosystem-state.json"
    state = load_json(path, None)
    if not isinstance(state, dict):
        return
    seen = state.get("lastSeenReleases")
    if not isinstance(seen, dict):
        seen = {}
    changed = False
    for rel in releases:
        repo = rel.get("repo")
        tag = rel.get("tag")
        if repo and tag and seen.get(repo) != tag:
            seen[repo] = tag
            changed = True
    if not changed:
        return
    state["lastSeenReleases"] = seen
    save_json(path, state)


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
    if candidate.get("weeklyRollup"):
        item["weeklyRollup"] = True
    if candidate.get("reported"):
        item["reported"] = True
    if candidate.get("aggregator"):
        item["aggregator"] = candidate["aggregator"]
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


def _import_source_health():
    scripts_dir = str(Path(__file__).resolve().parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import source_health
    return source_health


def _source_health_alert(text: str) -> bool:
    """Ops DM via the publisher already wired to CLAWBYTES_ADMIN_CHAT_ID."""
    try:
        return bool(_ensure_publisher().send_ops_alert(text))
    except Exception:  # noqa: BLE001 - health visibility must not break collect
        return False


_WATCHLIST_FETCH_LOGS = (
    "INFO deepseek-harness:",
    "INFO claude.dev:",
    "INFO anthropic-research:",
    "INFO status-claude:",
    "INFO status-cursor:",
    "INFO status-github:",
    "INFO testingcatalog:",
    "INFO kilo-blog:",
    "INFO havoptic:",
)


def _status_health_observation(stdout: str):
    """Parse the RSS monitor's status-feed line, or None when it did not run.

    Status history is empty most days. The ``status`` source treats that
    empty line as healthy. A fetch failure is ``status=error`` and still pages.
    """
    match = re.search(
        r"^STATUS_HEALTH status=(ok|empty|error) items=(\d+) reason=(.*)$",
        stdout or "",
        re.MULTILINE,
    )
    if not match:
        return None
    error = (match.group(3) or "").strip() or "-"
    return match.group(1), int(match.group(2)), error


def _reprint_watchlist_fetch_logs(stdout: str) -> None:
    """Monitor stdout is captured. These lines are the fetch confirmation."""
    if not isinstance(stdout, str):
        return
    for raw in stdout.splitlines():
        text = raw.strip()
        if text.startswith(_WATCHLIST_FETCH_LOGS):
            print(text, flush=True)


def run_monitors() -> None:
    """Run source monitors to refresh state files before collecting.

    Each monitor runs isolated: a timeout, a nonzero exit, or an unexpected
    crash in ONE monitor must not starve the rest of the batch. (Previously a
    single subprocess.TimeoutExpired raised out and skipped every later
    monitor for that collect cycle.) Uses cwd= + arg list rather than
    shell=True — same pattern scheduler.py already uses, and avoids
    interpolated-shell command-injection risk.

    stdout/stderr are captured (not discarded). Each source then emits one
    ``source_health`` line and updates the on-disk health record.
    Watchlist fetch lines are reprinted from that capture so a collect log
    shows whether deepseek-harness, claude.dev, and anthropic-research returned.
    """
    source_health = _import_source_health()
    monitors = [
        ("rss", ["python3", "scripts/claw-rss-monitor.py"]),
        # Stays in the list so REDDIT_FETCH_ENABLED (claw-reddit-monitor.py)
        # puts it back. Off: public JSON is HTTP 403; needs Reddit OAuth.
        ("reddit", ["python3", "scripts/claw-reddit-monitor.py"]),
        ("hn", ["python3", "scripts/claw-hn-monitor.py", "--quiet"]),
        ("moltbook", ["python3", "scripts/claw-moltbook-monitor.py"]),
        ("leaderboard", ["python3", "scripts/claw-leaderboard-monitor.py", "--quiet"]),
        ("registry", ["python3", "scripts/claw-registry-monitor.py", "--quiet"]),
        ("pagewatch", ["python3", "scripts/claw-pagewatch-monitor.py", "--quiet"]),
        ("bsky", ["python3", "scripts/claw-bsky-monitor.py", "--quiet"]),
        ("advisory", ["python3", "scripts/claw-advisory-monitor.py", "--quiet"]),
        ("ecosystem", ["bash", "scripts/claw-ecosystem-monitor.sh", "--mode", "check"]),
    ]
    if not source_health.reddit_fetch_enabled():
        monitors = [item for item in monitors if item[0] != "reddit"]
    for name, cmd in monitors:
        stdout = stderr = ""
        returncode = None
        timed_out = False
        crashed = ""
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(WORKSPACE),
                text=True,
                capture_output=True,
                timeout=300,
                errors="replace",
            )
            stdout = getattr(proc, "stdout", "") or ""
            stderr = getattr(proc, "stderr", "") or ""
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = getattr(exc, "stdout", "") or ""
            stderr = getattr(exc, "stderr", "") or ""
        except Exception as exc:  # noqa: BLE001 - one bad monitor must not starve the rest
            crashed = repr(exc)
        _reprint_watchlist_fetch_logs(stdout)
        status, items, error = source_health.outcome_from_process(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            crashed=crashed,
        )
        try:
            source_health.record_source_health(
                name,
                status=status,
                items=items,
                error=error,
                memory_dir=MEMORY,
                alert_sender=_source_health_alert,
            )
        except Exception as exc:  # noqa: BLE001 - health bookkeeping must not starve monitors
            print(f"source_health record failed source={name}: {exc!r}", file=sys.stderr)
        if name == "rss":
            observed = _status_health_observation(stdout)
            if observed is not None:
                s_status, s_items, s_error = observed
                try:
                    source_health.record_source_health(
                        "status",
                        status=s_status,
                        items=s_items,
                        error=s_error,
                        memory_dir=MEMORY,
                        alert_sender=_source_health_alert,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"source_health record failed source=status: {exc!r}", file=sys.stderr)


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
    advisory = load_json(MEMORY / "claw-advisory-state.json", {}).get("foundItems", [])
    ecosystem = load_json(MEMORY / "claw-ecosystem-new-items.json", {})
    ecosystem_releases: List[dict] = []
    if isinstance(ecosystem, dict):
        hf_papers = _unique_items(hf_papers + ecosystem.get("newHFPapers", []), ("id", "hf_id", "url"))
        ecosystem_hn = ecosystem.get("newHNStories", [])
        ecosystem_releases = [
            rel for rel in ecosystem.get("newReleases", [])
            if isinstance(rel, dict) and rel.get("repo") and rel.get("tag") and not rel.get("baseline")
        ]
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
        "advisory": advisory,
        "ecosystem_release": ecosystem_releases,
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
    if kind == "advisory":
        return classify_advisory(item)
    if kind == "ecosystem_release":
        return classify_ecosystem_release(item)
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
    if kind == "advisory":
        return item.get("package") or "GitHub Advisory"
    if kind == "ecosystem_release":
        return item.get("repo") or "github-release"
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
        low_signal = apply_ship_low_signal_policy(candidate)
        if low_signal:
            row["primaryCategory"] = candidate.get("primaryCategory")
            row["categories"] = candidate.get("categories", [])
            row["score"] = candidate.get("score")
            row["summary"] = candidate.get("summary")
            expires = candidate.get("expiresAt")
            row["expiresAt"] = expires.isoformat() if hasattr(expires, "isoformat") else expires
            row.update({"status": "would_add", "reason": f"ship_filtered:{low_signal}"})
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
        "claw-advisory-state.json",
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
        "bySourceName": rollup_by_source_name(rows),
        "currentBundles": {cat: [{"title": item.get("title"), "score": item.get("score"), "source": item.get("sourceName")} for item in bundle_for_category(cat)] for cat in CATEGORY_META},
        "unconsumedStateFiles": unconsumed_state_report(),
        "items": rows[:limit],
    }


YIELD_HISTORY_LIMIT = 8
SOURCE_YIELD_FILE = MEMORY / "claw-source-yield.json"


def rollup_by_source_name(rows: list) -> Dict[str, dict]:
    """Per-feed/subreddit counts from a full audit row list (before item limit)."""
    out: Dict[str, dict] = {}
    for row in rows:
        name = row.get("sourceName") or "(unnamed)"
        bucket = out.get(name)
        if bucket is None:
            bucket = {
                "sourceType": row.get("sourceType") or "",
                "total": 0,
                "would_add": 0,
                "skipped": 0,
                "rejected": 0,
            }
            out[name] = bucket
        bucket["total"] += 1
        status = row.get("status")
        if status in ("would_add", "skipped", "rejected"):
            bucket[status] += 1
        lane = row.get("primaryCategory")
        if lane and status in {"would_add", "skipped"}:
            lanes = bucket.setdefault("lanes", {})
            lanes[lane] = lanes.get(lane, 0) + 1
    return out


def source_yield_snapshot(report: dict, *, written_at: Optional[str] = None) -> dict:
    """Compact audit rollup — no per-item payload, never meant to be posted."""
    return {
        "writtenAt": written_at or now_utc().isoformat(),
        "lastCollectedAt": report.get("lastCollectedAt"),
        "rawItems": report.get("rawItems", 0),
        "statusCounts": report.get("statusCounts") or {},
        "reasonCounts": report.get("reasonCounts") or {},
        "sourceCounts": report.get("sourceCounts") or {},
        "laneCounts": report.get("laneCounts") or {},
        "bySourceName": report.get("bySourceName") or {},
        "unconsumedStateFiles": report.get("unconsumedStateFiles") or [],
    }


def write_source_yield(report: Optional[dict] = None, *, written_at: Optional[str] = None) -> dict:
    """Persist a weekly yield snapshot. Success is silent: no DM, no Slack.

    Keeps the last YIELD_HISTORY_LIMIT snapshots in `history` so a coverage
    round can compare weeks without scraping Railway logs.
    """
    if report is None:
        report = audit_sources(limit=0)
    snapshot = source_yield_snapshot(report, written_at=written_at)
    existing = load_json(SOURCE_YIELD_FILE, {"latest": None, "history": []})
    history = list(existing.get("history") or [])
    prev = existing.get("latest")
    if prev:
        history.append(prev)
    payload = {"latest": snapshot, "history": history[-YIELD_HISTORY_LIMIT:]}
    save_json(SOURCE_YIELD_FILE, payload)
    return payload


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


# How many new Ship items one collect may queue. Collect runs every 30
# minutes; Ship posts at most twice a day. These bound a single run so one
# atom dump cannot enqueue its whole page. Optional overrides follow the
# same int-from-env pattern as CLAWBYTES_CURATOR_TIMEOUT / MAX_TOKENS.
# Unset, blank, or non-integer values keep the defaults. Zero admits nothing.
SHIP_INTAKE_PER_SOURCE = 2
SHIP_INTAKE_PER_RUN = 6

_CALVER_RE = re.compile(r"\b20\d{2}\.\d{1,2}\.\d{1,2}\b")
_SEMVER_RE = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)\b")
_DEV_PRERELEASE_RE = re.compile(
    r"v?\d+\.\d+(?:\.\d+)?(?:[-._]+)dev\d*\b",
    re.IGNORECASE,
)
_DATESTAMP_VER_RE = re.compile(r"20\d{6}\.\d+")
_REGISTRY_DIFF_RE = re.compile(r"\b\d+\s+new models?\b", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    """Integer env override. Blank or invalid keeps ``default``. Negatives too."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    if parsed < 0:
        return default
    return parsed


def _positive_env_int(name: str, default: int) -> int:
    """Positive integer env override. Blank, zero, and invalid keep ``default``."""
    value = _env_int(name, default)
    return value if value > 0 else default


def ship_intake_per_source() -> int:
    return _env_int("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", SHIP_INTAKE_PER_SOURCE)


def ship_intake_per_run() -> int:
    return _env_int("CLAWBYTES_SHIP_INTAKE_PER_RUN", SHIP_INTAKE_PER_RUN)


def ship_low_signal_reason(candidate: dict) -> Optional[str]:
    """Why a Ship candidate should not enter the Ship queue, or None.

    Patch / pre-release / SDK patch / registry-diff items are low-signal.
    Semver minors and majors (x.y.0, including the 0.0.1 first cut) stay.
    OpenClaw-style CalVer ``YYYY.M.D`` is a real release train, not a semver
    patch — the third component is the day. Leaderboard moves stay Ship.
    """
    if not isinstance(candidate, dict) or candidate.get("primaryCategory") != "ship":
        return None
    if candidate.get("sourceType") == "leaderboard":
        return None
    title = candidate.get("title") or ""
    source = (candidate.get("sourceName") or "").lower()
    if candidate.get("sourceType") == "registry" and "litellm" in source:
        return "registry_diff"
    if candidate.get("sourceType") == "registry" and _REGISTRY_DIFF_RE.search(title):
        return "registry_diff"
    if is_prerelease_title(title) or _DEV_PRERELEASE_RE.search(title):
        return "prerelease"
    # Leading "dev" tag ("dev build …"). "developer …" is not a boundary match.
    if re.match(r"\s*dev\b", title.lower()):
        return "prerelease"
    if _DATESTAMP_VER_RE.search(title):
        return "patch"
    calver_spans = [match.span() for match in _CALVER_RE.finditer(title)]
    for match in _SEMVER_RE.finditer(title):
        span = match.span()
        if any(start <= span[0] and span[1] <= end for start, end in calver_spans):
            continue
        major, _minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if major >= 2000:
            continue
        if (major, _minor, patch) == (0, 0, 1):
            continue
        if patch > 0:
            return "patch"
    return None


def apply_ship_low_signal_policy(candidate: dict) -> Optional[str]:
    """Move a low-signal Ship candidate to Read. Return the reason, or None.

    Read is the existing lower lane. Watch and Community are different
    topics, and there is no patch lane to add. The score stays under Read's
    first publish bar so a version bump cannot open that lane by itself.
    """
    reason = ship_low_signal_reason(candidate)
    if not reason:
        return None
    published = candidate.get("publishedAt")
    if not isinstance(published, datetime):
        published = now_utc()
    candidate["primaryCategory"] = "read"
    candidate["categories"] = ["read"]
    candidate["expiresAt"] = published + timedelta(hours=CATEGORY_META["read"]["ttl_hours"])
    ceiling = CATEGORY_META["read"]["min_top_score"][0] - 1
    candidate["score"] = round(min(float(candidate.get("score") or 0), ceiling), 2)
    summary = (candidate.get("summary") or "Release").strip()
    candidate["summary"] = trim(f"{summary} (low-signal {reason})", 140)
    return reason


def format_ship_intake_line(by_source: Dict[str, Dict[str, int]]) -> str:
    """One collect-run line: Ship added / capped / filtered, per source.

    Sits beside ``source_health`` lines (same stdout, different prefix).
    Counts are ``added/capped/filtered``. Sources with all zeros are omitted.
    """
    added = capped = filtered = 0
    parts = []
    for name in sorted(by_source):
        counts = by_source[name]
        got = int(counts.get("added") or 0)
        held = int(counts.get("capped") or 0)
        dropped = int(counts.get("filtered") or 0)
        added += got
        capped += held
        filtered += dropped
        if got or held or dropped:
            token = re.sub(r"[\r\n;]+", " ", str(name)).strip() or "unknown"
            parts.append(f"{token}={got}/{held}/{dropped}")
    listing = ";".join(parts) if parts else "-"
    return f"ship_intake added={added} capped={capped} filtered={filtered} by_source={listing}"


def _ship_rank(row: tuple) -> tuple:
    candidate = row[1]
    published = candidate.get("publishedAt")
    stamp = 0.0
    if isinstance(published, datetime):
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        stamp = published.timestamp()
    return (-float(candidate.get("score") or 0), -stamp)


def _empty_intake() -> Dict[str, int]:
    return {"added": 0, "capped": 0, "filtered": 0}


def canonical_story_url(url: str) -> str:
    """Host + path, ignoring scheme, www, query, and a trailing slash.

    Ship items and HN article links rarely share a query string. The HN
    discussion URL is not used here: every story would collapse to
    news.ycombinator.com/item.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    if not host:
        return ""
    return f"{host}{path}"


def is_major_x0_release(title: str) -> bool:
    """True for a tracked tool's major release: 1.0, 2.0, 1.0.0, 2.0.0.

    A minor that happens to end in .0 (1.2.0) stays in score order. CalVer
    years (2026.9.7) are not majors. Patches are not majors.
    """
    text = title or ""
    for match in _SEMVER_RE.finditer(text):
        major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if major >= 2000:
            continue
        if major >= 1 and minor == 0 and patch == 0:
            return True
    for match in re.finditer(r"\bv?(\d+)\.(\d+)\b", text, re.IGNORECASE):
        end = match.end()
        if end < len(text) and text[end] == "." and end + 1 < len(text) and text[end + 1].isdigit():
            continue
        major, minor = int(match.group(1)), int(match.group(2))
        if major >= 2000:
            continue
        if major >= 1 and minor == 0:
            return True
    return False


def _is_tracked_major_release(item: dict) -> bool:
    repo = repo_name_from_feed(item.get("sourceName") or "")
    if repo not in REPO_PRIORITY:
        return False
    return is_major_x0_release(item.get("title") or "")


def load_hn_front_page_urls() -> set:
    """Article URLs from the latest relevant HN front-page pass."""
    state = load_json(MEMORY / "claw-hn-state.json", {})
    urls = set()
    if not isinstance(state, dict):
        return urls
    for row in state.get("frontPage") or []:
        if not isinstance(row, dict):
            continue
        canon = canonical_story_url(row.get("url") or "")
        if canon:
            urls.add(canon)
    return urls


def ship_bypass_rank(item: dict, front_page_urls: Optional[set] = None) -> int:
    """Sort key for the Ship queue. Higher leaves the backlog sooner.

    2: a tracked tool's major/x.0 release, or a Ship item whose URL is also
    on the relevant HN front page.
    1: the weekly Claude Code patch roll-up.
    0: everything else, still ordered by score.

    This does not add a post. Ship still publishes on its two daily windows.
    """
    if not isinstance(item, dict):
        return 0
    categories = item.get("categories") or []
    if item.get("primaryCategory") != "ship" and "ship" not in categories:
        return 0
    urls = front_page_urls or set()
    if urls and canonical_story_url(item.get("url") or "") in urls:
        return 2
    if _is_tracked_major_release(item):
        return 2
    if item.get("weeklyRollup"):
        return 1
    return 0


def _iso_week_key(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    year, week, _day = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _version_sort_key(version: str) -> tuple:
    nums = []
    for part in (version or "").split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    return tuple(nums)


def is_claude_code_product_release(source_name: str) -> bool:
    """The Claude Code product atom, not the Action or Agent SDK feeds."""
    low = (source_name or "").lower()
    if "claude code action" in low or "claude agent sdk" in low:
        return False
    return "claude code" in low


def _claude_code_patches_path() -> Path:
    return MEMORY / "claw-claude-code-patches.json"


def _load_claude_code_patches() -> dict:
    data = load_json(_claude_code_patches_path(), {})
    if not isinstance(data, dict):
        return {}
    return data


def _save_claude_code_patches(data: dict) -> None:
    save_json(_claude_code_patches_path(), data)


def _rotate_claude_code_week(state: dict, now: datetime) -> bool:
    """Move a finished week's patches onto the pending list. True if changed."""
    if not state or not state.get("week"):
        return False
    week = _iso_week_key(now)
    if state.get("week") == week:
        return False
    patches = list(state.get("patches") or [])
    if patches:
        pending = list(state.get("pending") or [])
        pending.append({"week": state.get("week"), "patches": patches})
        state["pending"] = pending[-4:]
    state["week"] = week
    state["patches"] = []
    return True


def _remember_claude_code_patch(state: dict, candidate: dict, now: datetime) -> bool:
    version_match = _SEMVER_RE.search(candidate.get("title") or "")
    if not version_match:
        return False
    version = f"{int(version_match.group(1))}.{int(version_match.group(2))}.{int(version_match.group(3))}"
    week = _iso_week_key(now)
    if state.get("week") != week:
        if state.get("week") and state.get("patches"):
            pending = list(state.get("pending") or [])
            pending.append({"week": state["week"], "patches": list(state.get("patches") or [])})
            state["pending"] = pending[-4:]
        state["week"] = week
        state["patches"] = []
    patches = list(state.get("patches") or [])
    if any(row.get("version") == version for row in patches):
        state["patches"] = patches
        return False
    patches.append({"version": version, "url": candidate.get("url") or ""})
    state["patches"] = patches
    state["week"] = week
    return True


def _claude_code_rollup_candidate(batch: dict) -> Optional[dict]:
    versions = sorted(
        {row.get("version") for row in (batch.get("patches") or []) if row.get("version")},
        key=_version_sort_key,
    )
    week = batch.get("week") or ""
    if not versions or not week:
        return None
    if len(versions) == 1:
        title = f"Claude Code {versions[0]} weekly patches"
    else:
        title = f"Claude Code {versions[0]}–{versions[-1]}"
    published = now_utc()
    return {
        "primaryCategory": "ship",
        "categories": ["ship"],
        "score": REPO_PRIORITY.get("claude", 66),
        "summary": "Weekly patch roll-up: " + ", ".join(versions),
        "expiresAt": published + timedelta(hours=CATEGORY_META["ship"]["ttl_hours"]),
        "publishedAt": published,
        "sourceType": "rss",
        "sourceName": "Claude Code Releases",
        "sourceId": f"claude-code-patches:{week}",
        "url": (
            "https://github.com/anthropics/claude-code/releases"
            f"#patches-{week}-{versions[0]}-{versions[-1]}"
        ),
        "title": title,
        "weeklyRollup": True,
    }


def _primary_release_index(candidates: Dict[str, List[dict]], posted_urls: set) -> tuple:
    """Vendor URLs and (repo, version) pairs Havoptic must not repost."""
    urls = {url for url in posted_urls if url}
    versions = set()
    for kind, items in candidates.items():
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if kind == "rss":
                feed = item.get("feed") or ""
                if havoptic_feed(feed):
                    continue
                link = item.get("link") or ""
                if link:
                    urls.add(link)
                repo = repo_name_from_feed(feed)
                ver = version_key(item.get("title") or "") or version_key(link)
                if repo in REPO_PRIORITY and ver:
                    versions.add((repo, ver))
            elif kind == "ecosystem_release":
                link = item.get("url") or ""
                if link:
                    urls.add(link)
                repo = repo_name_from_feed((item.get("repo") or "").replace("/", " ").replace("-", " "))
                ver = version_key(item.get("tag") or "") or version_key(item.get("name") or "")
                if repo in REPO_PRIORITY and ver:
                    versions.add((repo, ver))
    return urls, versions


def havoptic_already_covered(candidate: dict, urls: set, versions: set) -> bool:
    """True when a primary source already has this Havoptic release.

    The vendor link wins. A fragment on a bare changelog is a different
    version, so it does not match the page URL of an earlier post.
    """
    if not isinstance(candidate, dict) or candidate.get("aggregator") != "havoptic":
        return False
    url = candidate.get("url") or ""
    for other in urls:
        if urls_same_release(url, other):
            return True
    repo = repo_name_from_feed(candidate.get("sourceName") or "")
    ver = version_key(candidate.get("title") or "") or version_key(url)
    return bool(repo in REPO_PRIORITY and ver and (repo, ver) in versions)


def _status_source_name(item: dict) -> str:
    name = (item.get("sourceName") or "").strip()
    if name in STATUS_FEED_NAMES:
        return name
    lowered = name.lower()
    for feed in STATUS_FEED_NAMES:
        if feed.lower() == lowered:
            return feed
    return ""


def _status_details_by_url(rss_state: dict) -> Dict[str, str]:
    details = {}
    for raw in rss_state.get("foundItems") or []:
        if not isinstance(raw, dict):
            continue
        if not _status_source_name({"sourceName": raw.get("feed") or ""}):
            continue
        url = incident_public_url(raw.get("link") or raw.get("id") or "")
        detail = raw.get("detail") or ""
        if url and detail:
            details[url] = detail
    return details


def drop_prebaseline_status_items(now: Optional[datetime] = None) -> List[str]:
    """Retire queued status incidents from before that feed's baseline.

    Safe to run on every process start and every collect. Only status-feed
    rows are touched. A missing baseline clock is stamped once (pending until
    the RSS monitor completes a successful fetch) and is never moved forward.
    """
    ensure_files()
    moment = now or now_utc()
    rss_path = MEMORY / "claw-rss-state.json"
    rss_state = load_json(rss_path, {})
    if not isinstance(rss_state, dict):
        rss_state = {}
    baselines = rss_state.get("feedBaseline")
    if not isinstance(baselines, dict):
        baselines = {}
        rss_state["feedBaseline"] = baselines
    rss_changed = False
    for name in STATUS_FEED_NAMES:
        rec = baselines.get(name)
        if not isinstance(rec, dict):
            baselines[name] = {"at": moment.isoformat(), "pending": True}
            rss_changed = True
            continue
        if not rec.get("at"):
            rec["at"] = moment.isoformat()
            rss_changed = True
        if not rec.get("url") and not rec.get("pending"):
            rec["pending"] = True
            rss_changed = True

    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    if not isinstance(backlog, dict):
        backlog = {"items": []}
    if not isinstance(state, dict):
        state = {}
    items = backlog.get("items")
    if not isinstance(items, list):
        items = []
        backlog["items"] = items
    seen = state.get("seenSourceKeys")
    if not isinstance(seen, list):
        seen = []
    seen_set = set(seen)
    details = _status_details_by_url(rss_state)
    seen_map = rss_state.get("lastSeenByFeed")
    if not isinstance(seen_map, dict):
        seen_map = {}
        rss_state["lastSeenByFeed"] = seen_map
    dropped: List[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "queued":
            continue
        feed = _status_source_name(item)
        if not feed:
            continue
        rec = baselines.get(feed) if isinstance(baselines.get(feed), dict) else {}
        baseline_at = parse_dt(rec.get("at") or "")
        url = incident_public_url(item.get("url") or "")
        detail = details.get(url) or ""
        published = item.get("publishedAt") or ""
        if not isinstance(published, str):
            published = published.isoformat() if isinstance(published, datetime) else ""
        if not status_item_predates_watch(
            detail,
            published,
            baseline_at,
            moment,
            summary=item.get("summary") or "",
        ):
            continue
        item["status"] = "retired"
        item["retiredReason"] = "status_before_baseline"
        key = source_key("rss", item.get("sourceId") or url, url)
        if key not in seen_set:
            seen.append(key)
            seen_set.add(key)
        if url:
            bucket = seen_map.get(feed)
            if not isinstance(bucket, list):
                bucket = []
            if url not in bucket:
                bucket.append(url)
                rss_changed = True
            seen_map[feed] = bucket[-50:]
        dropped.append(url or item.get("url") or "")

    if rss_changed or dropped:
        rss_state["feedBaseline"] = baselines
        rss_state["lastSeenByFeed"] = seen_map
        save_json(rss_path, rss_state)
    if dropped:
        state["seenSourceKeys"] = seen[-5000:]
        save_json(BACKLOG_FILE, backlog)
        save_json(THREAD_STATE_FILE, state)
    shown = " ".join(url for url in dropped if url) or "-"
    print(f"INFO status-queue-cleanup dropped={len(dropped)} urls={shown}", flush=True)
    return dropped


def collect_into_backlog() -> dict:
    ensure_files()
    drop_prebaseline_status_items()

    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    collected_at = now_utc()
    patch_book = _load_claude_code_patches()
    patches_dirty = _rotate_claude_code_week(patch_book, collected_at)

    seen_source_keys = set(state.get("seenSourceKeys", []))
    existing_ids = {item["id"] for item in backlog.get("items", [])}
    posted_titles = recent_posted_titles(backlog.get("items", []), collected_at)

    added = []
    candidates = collect_candidates()
    primary_urls, primary_versions = _primary_release_index(
        candidates, set(state.get("postedUrls") or [])
    )
    acked_releases: List[dict] = []
    intake: Dict[str, Dict[str, int]] = {}
    pending_keys = set()
    ship_ready: List[tuple] = []
    other_ready: List[tuple] = []
    per_source_cap = ship_intake_per_source()
    per_run_cap = ship_intake_per_run()

    def _bump(source: str, field: str) -> None:
        bucket = intake.setdefault(source, _empty_intake())
        bucket[field] += 1

    def _queue(candidate: dict, key: str) -> bool:
        """Mark seen and append when the backlog id is new. True if appended."""
        seen_source_keys.add(key)
        queued = backlog_item(candidate)
        if queued["id"] in existing_ids:
            return False
        backlog["items"].append(queued)
        existing_ids.add(queued["id"])
        added.append(queued)
        return True

    for kind, items in candidates.items():
        for item in items:
            try:
                candidate = classify_source_candidate(kind, item)
            except Exception as exc:  # noqa: BLE001 - one bad item must not abort collect
                print(f"[collect] skipped {kind} item ({exc!r})", file=sys.stderr)
                continue
            eco = item if kind == "ecosystem_release" else None
            if eco is not None and not candidate:
                # Handed to the classifier. The shell left the tag unseen so a
                # crash before this ack retries next cycle. A reject is final.
                acked_releases.append(eco)
            if not candidate:
                continue
            if not is_fresh(candidate):
                if eco is not None:
                    acked_releases.append(eco)
                continue
            key = source_key(kind, candidate["sourceId"], candidate["url"])
            if key in seen_source_keys or key in pending_keys:
                # Ack only when this tag was already handled. A duplicate of a
                # row still waiting on the cap must not mark the tag seen.
                if eco is not None and key in seen_source_keys:
                    acked_releases.append(eco)
                continue
            # Stale calendar dates and a story already posted inside the
            # window never take a Ship slot. Mark seen and ack the ecosystem
            # tag so the monitor does not re-emit it every collect.
            if is_stale_dated_item(candidate, collected_at) or title_repeats_story(
                candidate.get("title") or "", posted_titles
            ) or havoptic_already_covered(candidate, primary_urls, primary_versions):
                seen_source_keys.add(key)
                if eco is not None:
                    acked_releases.append(eco)
                continue
            pending_keys.add(key)
            source = candidate.get("sourceName") or kind
            low_signal = apply_ship_low_signal_policy(candidate)
            row = (kind, candidate, key, source, eco)
            if low_signal:
                # Counted as filtered from Ship, then queued on Read.
                # Claude Code patches are summarized once a week instead of
                # each tag competing for Ship.
                if low_signal == "patch" and is_claude_code_product_release(source):
                    if _remember_claude_code_patch(patch_book, candidate, collected_at):
                        patches_dirty = True
                _bump(source, "filtered")
                other_ready.append(row)
            elif candidate.get("primaryCategory") == "ship":
                ship_ready.append(row)
            else:
                other_ready.append(row)

    ship_ready.sort(key=_ship_rank)
    admitted_run = 0
    admitted_source: Dict[str, int] = {}
    for _kind, candidate, key, source, eco in ship_ready:
        if key in seen_source_keys:
            if eco is not None:
                acked_releases.append(eco)
            continue
        over_source = admitted_source.get(source, 0) >= per_source_cap
        over_run = admitted_run >= per_run_cap
        if over_source or over_run:
            # Leave unseen so the next collect can admit it. Do not ack an
            # ecosystem tag: the shell re-emits until collect takes it.
            _bump(source, "capped")
            continue
        if _queue(candidate, key):
            admitted_source[source] = admitted_source.get(source, 0) + 1
            admitted_run += 1
            _bump(source, "added")
        if eco is not None:
            acked_releases.append(eco)

    for _kind, candidate, key, source, eco in other_ready:
        if key in seen_source_keys:
            if eco is not None:
                acked_releases.append(eco)
            continue
        _queue(candidate, key)
        if eco is not None:
            acked_releases.append(eco)

    pending_rollups = list(patch_book.get("pending") or [])
    if pending_rollups:
        rollup = _claude_code_rollup_candidate(pending_rollups[0])
        if rollup:
            rollup_key = source_key("rss", rollup["sourceId"], rollup["url"])
            if rollup_key not in seen_source_keys and _queue(rollup, rollup_key):
                _bump(rollup.get("sourceName") or "Claude Code Releases", "added")
        patch_book["pending"] = pending_rollups[1:]
        patches_dirty = True
    if patches_dirty:
        _save_claude_code_patches(patch_book)

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
    _mark_ecosystem_releases_seen(acked_releases)

    counts = {c: 0 for c in CATEGORY_META}
    for item in added:
        counts[item["primaryCategory"]] += 1

    print(format_ship_intake_line(intake), flush=True)
    ship_added = sum(bucket["added"] for bucket in intake.values())
    ship_capped = sum(bucket["capped"] for bucket in intake.values())
    ship_filtered = sum(bucket["filtered"] for bucket in intake.values())
    return {
        "added": len(added),
        "counts": counts,
        "items": added,
        "shipIntake": {
            "added": ship_added,
            "capped": ship_capped,
            "filtered": ship_filtered,
            "bySource": intake,
        },
    }


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


# Same product + same topic, across lanes and URLs. Not a URL compare:
# pagewatch fragments are unique on purpose (CLAUDE.md invariant 4) and must
# stay unique when the entry actually changed. Dotted versions that differ
# are different stories (Claude Code 2.1.290 vs 2.1.291).
STORY_WINDOW_DAYS = 7
STALE_AFTER_DAYS = 3
_STORY_STOP = {
    "the", "and", "for", "with", "from", "into", "your", "this", "that", "new",
    "now", "how", "why", "what", "using", "use", "guide", "update", "updated",
    "updates", "release", "releases", "plugin", "plugins", "tool", "tools",
    "introducing", "introduce", "introduced", "announces", "announce",
    "announced", "available", "across", "experimental", "support", "supports",
    "about", "over", "under", "after", "before", "more", "most", "than", "then",
    "also", "just", "only", "item", "items", "post", "posts", "blog", "page",
    "notes", "note", "changelog", "platform", "discussion", "thread", "today",
    "yesterday", "its", "are", "was", "been", "have", "has", "not", "but",
    "you", "our", "out", "all", "can", "via", "per",
    "in", "on", "of", "by", "at", "or", "to", "as", "an", "is",
}
_STORY_VENDORS = {
    "kiro", "claude", "copilot", "devin", "openai", "github", "hermes",
    "cursor", "codex", "mistral", "anthropic", "google", "deepseek", "xai",
}
_STORY_WEAK = {"web", "app", "ide", "cli", "api", "pro", "max", "plus", "hub"}
_DOTTED_VERSION = re.compile(r"\b\d+\.\d+(?:\.\d+)*\b")
_BARE_NUMBER = re.compile(r"\b\d{1,4}\b")


def _story_tokens(title: str) -> set:
    text = re.sub(r"[^a-z0-9.+]+", " ", (title or "").lower())
    out = set()
    for raw in text.split():
        token = raw
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) < 2 or token in _STORY_STOP:
            continue
        out.add(token)
    return out


def _story_vendors(title: str) -> set:
    return _story_tokens(title) & _STORY_VENDORS


def _dotted_versions(title: str) -> set:
    return set(_DOTTED_VERSION.findall((title or "").lower()))


def _bare_numbers(title: str) -> set:
    return set(_BARE_NUMBER.findall(title or ""))


def same_story_titles(left: str, right: str) -> bool:
    """True when two headlines are the same product and the same topic.

    A different dotted version is a different story. A different bare number
    (Mistral Large 3 vs Mistral Large 4) is too. Shared vendor words alone
    are not a match.
    """
    if not left or not right:
        return False
    left_versions = _dotted_versions(left)
    right_versions = _dotted_versions(right)
    if left_versions and right_versions and left_versions.isdisjoint(right_versions):
        return False
    left_nums = _bare_numbers(left)
    right_nums = _bare_numbers(right)
    if left_nums and right_nums and left_nums.isdisjoint(right_nums):
        return False
    left_vendors = _story_vendors(left)
    right_vendors = _story_vendors(right)
    if left_vendors and right_vendors and left_vendors.isdisjoint(right_vendors):
        return False
    left_specific = _story_tokens(left) - _STORY_VENDORS - left_versions
    right_specific = _story_tokens(right) - _STORY_VENDORS - right_versions
    overlap = (left_specific & right_specific) - _STORY_WEAK
    if not overlap:
        return False
    smaller = min(len(left_specific), len(right_specific))
    if smaller <= 0:
        return False
    return len(left_specific & right_specific) / smaller >= 0.5


def title_repeats_story(title: str, recent_titles: List[str]) -> bool:
    return any(same_story_titles(title, other) for other in recent_titles if other)


def _story_stamp(item: dict) -> Optional[datetime]:
    return parse_dt(item.get("discoveredAt") or item.get("publishedAt") or "")


def recent_posted_titles(backlog_items: List[dict], now: Optional[datetime] = None) -> List[str]:
    """Titles posted inside the story window.

    Posted backlog rows already carry the title. ``discoveredAt`` stands in
    for the post time so this does not add a state-file field. Ship's TTL is
    7 days, so a row that is still posted was discovered inside that span.
    """
    moment = now or now_utc()
    window = timedelta(days=STORY_WINDOW_DAYS)
    titles = []
    for item in backlog_items or []:
        if not isinstance(item, dict) or item.get("status") != "posted":
            continue
        stamp = _story_stamp(item)
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if moment - stamp <= window:
            title = (item.get("title") or "").strip()
            if title:
                titles.append(title)
    return titles


def is_stale_dated_item(item: dict, now: Optional[datetime] = None) -> bool:
    """True when the item's own calendar date is older than the stale window.

    Discovery time is not the item's date. Pagewatch sets ``publishedAt`` to
    the moment we noticed the page, so a September 22 changelog first seen
    on October 5 would otherwise read as new. No stated date means not stale.
    A future date (a retirement deadline) is not stale.
    """
    if not isinstance(item, dict):
        return False
    blob = f"{item.get('title') or ''} {item.get('summary') or ''}"
    stated = first_stated_date(blob)
    if stated is None:
        return False
    moment = now or now_utc()
    age = (moment.date() - stated).days
    return age > STALE_AFTER_DAYS


def queue_for_category(category: str) -> List[dict]:
    ensure_files()
    backlog = load_json(BACKLOG_FILE, {"items": []})
    state = load_json(THREAD_STATE_FILE, {})
    posted_urls = set(state.get("postedUrls", []))
    now = now_utc()
    recent_titles = recent_posted_titles(backlog.get("items", []), now)
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
        # Hermes asset bundles already in the backlog (classified before the
        # tag filter, or titled with the tag `inputs-8`) must not still post.
        if is_junk_release_title(item.get("title") or "", item.get("url") or ""):
            continue
        if is_stale_dated_item(item, now):
            continue
        if title_repeats_story(item.get("title") or "", recent_titles):
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
    # Headline releases lead the Ship bundle. The two daily windows are unchanged.
    if category == "ship" and out:
        front_urls = load_hn_front_page_urls()
        out.sort(key=lambda x: -ship_bypass_rank(x, front_urls))
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
    picked_titles: List[str] = []
    bucket_counts: Dict[str, int] = {}
    bucket_cap = 1 if category == "ship" else 2 if category == "watch" else 3

    for item in items:
        bucket = source_bucket(item)
        if bucket_counts.get(bucket, 0) >= bucket_cap:
            continue
        if category != "ship" and _same_topic(item, picked):
            continue
        # Ship does not use the loose token overlap above. The story key
        # still collapses two headlines of the same product and topic inside
        # one bundle, including Ship.
        if title_repeats_story(item.get("title") or "", picked_titles):
            continue
        picked.append(item)
        picked_titles.append(item.get("title") or "")
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


def focus_changelog_section(text: str, title: str) -> str:
    """Keep the changelog section the item is about.

    The raw page leads with every recent heading. A writer given that whole
    prefix restates an older section inside today's item (post #968 put the
    September 30 Sonnet 4.5 deprecation into the October 1 notes). When the
    title names a heading, return that section only. No match leaves the
    text unchanged.
    """
    if not text or not title:
        return text or ""
    hints = []
    for sep in (" — ", " – ", " - "):
        if sep in title:
            hints.append(title.split(sep, 1)[1].strip())
            break
    hints.append((title or "").strip())
    hints = [hint for hint in hints if len(hint) >= 4]
    if not hints:
        return text
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    chosen = None
    for index, level, heading in headings:
        low = heading.lower()
        if any(hint.lower() in low or low in hint.lower() for hint in hints):
            chosen = (index, level)
            break
    if chosen is None:
        return text
    start, level = chosen
    end = len(lines)
    for index, heading_level, _heading in headings:
        if index > start and heading_level <= level:
            end = index
            break
    section = "\n".join(lines[start:end]).strip()
    if not section:
        return text
    return section[:1800]


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
            md = focus_changelog_section(md, item.get("title") or "")
            return f"RELEASE NOTES: {md}" if md else ""
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


# Second writer model, tried once after the primary fails or the number guard
# rejects its draft. Unset uses this default. An empty value skips the extra call.
DEFAULT_LLM_FALLBACK_MODEL = "glm-5.3-flash"
# Comma groups and hyphen/slash/dot runs are one token so a reordered date
# or a split thousands-group is not treated as grounded.
_NUMBER_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[./\-]\d+)+|\d+")


def _ungrounded_numbers(output: str, source: str) -> set[str]:
    """Number and version tokens in output that are absent from source."""
    return set(_NUMBER_TOKEN.findall(output or "")) - set(_NUMBER_TOKEN.findall(source or ""))


def numbers_grounded(output: str, source: str) -> bool:
    """True when every number or version token in output also appears in source."""
    return not _ungrounded_numbers(output, source)


def _writer_models() -> List[str]:
    primary = LLM_MODEL
    fallback = os.environ.get("CLAWBYTES_LLM_MODEL_FALLBACK", DEFAULT_LLM_FALLBACK_MODEL).strip()
    if fallback and fallback != primary:
        return [primary, fallback]
    return [primary]


def _completion_text(result: dict) -> str:
    """Assistant `content` only. Empty content is a failure.

    Reasoning models may fill `reasoning` or `reasoning_content` while leaving
    `content` empty. That is not a post.
    """
    try:
        message = result["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"empty content: malformed completion ({e})") from e
    if not isinstance(message, dict):
        raise ValueError("empty content")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(empty_content_message(result))
    return content.strip()


def _writer_inputs(items: List[dict], category: str) -> Tuple[str, str]:
    """Return the writer prompt and the fact text the number guard may cite.

    The fact text is the item count plus the fields the model is shown. Prompt
    instructions and item indexes are not facts, so a number that appears only
    there does not license the post.
    """
    meta = CATEGORY_META[category]
    item_emoji = {"ship": "📦", "watch": "🚨", "read": "📚", "community": "💬"}.get(category, meta["emoji"])
    prompt = f"""You write @clawbytes on Telegram — covering the AI agent ecosystem. Short, sharp, factual. No corporate filler.

Write a {meta['label']} lane post with {len(items)} items.

FORMAT (strict HTML):
{meta['emoji']} <b>{meta['label']}</b> — N items

{item_emoji} <a href="URL">ACTUAL TITLE</a> — one factual sentence drawn from the item

Lane — this post is {meta['label']} only:
- Ship: something an operator can install, enable, or call. A release, a changelog entry, or a model showing up inside a coding tool. Not a funding announcement, not an opinion, not a paper.
- Watch: an incident that needs action — outage, CVE, exploit, malicious package, sandbox escape. Not a trial, not a status filed as an improvement, not a CEO opinion.
- Read: an essay, paper, benchmark, or explainer. Not release notes and not a version bump.
- Community: a discussion builders are having. Not a paper with no thread, and not a vendor release.

Rules:
- Use <b> for bold, <a href="URL">title text</a> for links — use the ACTUAL model/project name as link text, never "Link" or "thread"
- Do not write bare domains (127.0.0.1, claude.dev, github.com/name) or @handles. Telegram turns them into links. Put a host in the href. If the host itself is the fact, wrap it in <code>.
- Use only facts present in the item lines below. Do not add claims, context, or background from anywhere else.
- Copy numbers, version strings, and dates verbatim. Do not round, shorten, reformat, or invent them.
- Keep the source's own verbs. Do not write "launches" or "opens" unless the source text says so.
- If an item line says REPORTED, it is a leak or pre-release. Write "reportedly" or "spotted". Do not state it as launched, shipped, or generally available.
- No hype or opinion words: massive, game-changing, groundbreaking, revolutionary, unprecedented, one of the largest.
- Ship: what changed, using the release notes if provided. Do not invent features.
- Watch: the risk, what to check, what breaks — only when the item text says so.
- Read: describe what the piece is and its core claim only when the source data states it.
- Community: the sentiment and the user signal that are in the item. If multiple threads cover the same topic, merge them into one bullet using counts that appear in the item lines.
- Every item must name one concrete change (what shipped, what broke, what was deprecated, what the source measured). If the notes do not say that, omit the item. Do not write "release notes at the link", "changelog details what's improved", or a sentence that only repeats the title.
- MAX 150 chars per item summary. No filler. No "notable" or "worth watching." No "offering insights" or "highlights." No soft verbs: "breaks down", "unpacks", "dives into", "rages on", "sparks debate" are all banned.
- Reuse the source's wording when it states the fact. Do not add a claim that is not in the item line.
- If release notes are missing or say nothing concrete: omit the item. Do not invent a meaning for the tag. Never speculate with "might," "could," or "should."
- Do not add a closing paragraph. The only line after the items is one sentence that connects two or more of them using facts already in those items. If you cannot, stop after the last item. Never restate a single bullet.
- Start EVERY item with {item_emoji} (this lane\u2019s emoji) and use the SAME emoji for every item. Never use another lane\u2019s emoji or a topical/decorative emoji.
- For HN items: include the point and comment counts from the item line when they are present.
- NEVER fabricate statistics, metrics, or specific findings.

Items:"""

    # Prefetch grounding in parallel — sequential fetches would add up to
    # ~10s/item of wall-clock to every bundle render.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        groundings = list(pool.map(lambda it: grounding_for_item(category, it), items))

    fact_parts = [str(len(items))]
    for i, item in enumerate(items):
        title = display_title(item)
        url = item.get("url", "") or ""
        raw_summary = item.get("summary", "") or ""
        source = item.get("sourceType", "") or ""
        # Add HN engagement data to help LLM contextualize
        if source == "hackernews":
            pts = item.get("rawScore", 0)
            comments = item.get("rawComments", 0)
            raw_summary = f"{pts}pts / {comments} comments on HN"
        body = f"{title} | {url} | {raw_summary} | source: {source}"
        if groundings[i]:
            body += f" | {groundings[i]}"
        if item.get("reported"):
            body += " | REPORTED leak or pre-release; say reportedly or spotted; do not state a launch"
        # The item index stays out of fact_parts. Separators add no digits, so
        # the guard sees the same number tokens as the fields themselves.
        fact_parts.append(body)
        prompt += f"\n{i+1}. {body}"

    prompt += "\n\nWrite the post now:"
    return prompt, "\n".join(fact_parts)


def _writer_complete(prompt: str, model: str) -> str:
    # Reasoning models on Ollama's OpenAI-compatible endpoint count thinking
    # tokens against max_tokens. 1200 came back as empty content.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _positive_env_int("CLAWBYTES_LLM_MAX_TOKENS", 6000),
        "temperature": 0.2,
    }
    effort = os.environ.get("CLAWBYTES_LLM_REASONING_EFFORT", "").strip()
    if effort:
        payload["reasoning_effort"] = effort
    data = json.dumps(payload).encode()
    req = Request(
        f"{LLM_URL}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    with urlopen(req, timeout=_positive_env_int("CLAWBYTES_LLM_TIMEOUT", 90)) as resp:
        result = json.loads(resp.read().decode())
    return _completion_text(result)


def llm_summarize(items: List[dict], category: str) -> Optional[str]:
    """Use LLM to generate lane copy from the item facts only.

    The primary model is tried once. Timeout, HTTP error, empty content, a
    rejected draft, or a number that was not in the item text tries
    CLAWBYTES_LLM_MODEL_FALLBACK once. If that also fails, return None so the
    caller renders the deterministic template.
    """
    if not LLM_API_KEY:
        return None
    if not items:
        return None

    prompt, fact_source = _writer_inputs(items, category)
    for model in _writer_models():
        try:
            content = _writer_complete(prompt, model)
        except Exception as e:
            print(f"llm_summarize({category}) model={model} failed: {e}", file=sys.stderr)
            continue
        content = strip_banned_verbs(content)
        if len(content) < 50 or "I cannot" in content:
            print(
                f"llm_summarize({category}): rejected output from model={model} (len={len(content)})",
                file=sys.stderr,
            )
            continue
        missing = _ungrounded_numbers(content, fact_source)
        if missing:
            print(
                f"llm_summarize({category}): number guard rejected model={model} missing={', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            continue
        print(f"llm_summarize({category}): answered by model={model}", file=sys.stderr)
        return content
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
    if item.get("weeklyRollup"):
        # normalize_release_title keeps only the first semver, which would
        # collapse "2.1.284–2.1.289" to a single patch.
        return item.get("title", "")
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


# Opaque publisher ids such as release-publish/004549970362-1790888386.
# A human title has spaces, or it is a version. This shape is neither.
_MACHINE_ID_TITLE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*/\d{6,}(?:-\d+)+")
_TEMPLATE_VERSION = re.compile(
    r"\bv?(\d+\.\d+\.\d+(?:[-.]?(?:alpha|beta|rc)[-.]?\d+)?)\b",
    re.IGNORECASE,
)


def _is_machine_id_title(title: str) -> bool:
    text = (title or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    return _MACHINE_ID_TITLE.fullmatch(text) is not None


def _version_in_text(text: str) -> str:
    """A date version or semver, including a leading v glued to the number."""
    dated = re.search(r"(20\d{2}\.\d{1,2}\.\d{1,2}(?:-\d+)?)", text or "")
    if dated:
        return dated.group(1)
    semver = _TEMPLATE_VERSION.search(text or "")
    return semver.group(1) if semver else ""


def _template_product_label(item: dict) -> str:
    source = item.get("sourceName") or ""
    repo = repo_name_from_feed(source)
    if repo in REPO_PRIORITY:
        return display_repo_name(repo)
    url = item.get("url") or ""
    match = re.search(r"github\.com/[^/]+/([^/#?]+)", url)
    if match:
        slug = match.group(1)
        known = repo_name_from_feed(slug.replace("-", " ").replace("_", " "))
        if known in REPO_PRIORITY:
            return display_repo_name(known)
        pretty = re.sub(r"[-_]+", " ", slug).strip()
        if pretty:
            return pretty[:1].upper() + pretty[1:]
    if repo and repo != "misc":
        label = display_repo_name(repo)
        if label and not _is_machine_id_title(label):
            return label
    return ""


def template_item_title(item: dict) -> Optional[str]:
    """Headline for the deterministic renderer.

    A raw machine id is replaced with the product name plus a version from
    the url, summary, or feed. When those are missing, the line is skipped.
    """
    raw = item.get("title") or ""
    shown = display_title(item)
    if not _is_machine_id_title(raw) and not _is_machine_id_title(shown):
        text = (shown or "").strip()
        return text or None
    label = _template_product_label(item)
    version = _version_in_text(" ".join([
        str(item.get("url") or ""),
        str(item.get("summary") or ""),
        str(item.get("sourceName") or ""),
    ]))
    if label and version:
        return f"{label} {version}"
    return None


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
    
    # Fallback: static template format. Skip lines whose only title is a
    # machine id we cannot turn into product + version.
    emoji = "📦" if category == "ship" else "🚨" if category == "watch" else "📚" if category == "read" else "💬"
    body: List[str] = []
    for item in bundle:
        title = template_item_title(item)
        if not title:
            continue
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

        body.append(f"{emoji} <a href=\"{url}\">{html_escape(title)}</a>{html_escape(summary)}")

    if not body:
        return f"{meta['emoji']} <b>{meta['label']}</b> — Nothing new"

    count = len(body)
    lines = [f"{meta['emoji']} <b>{meta['label']}</b> — {count} item{'s' if count > 1 else ''}", ""]
    lines.extend(body)
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


_CURATOR_REASON_LIMIT = 300
_LOG_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._\-]{8,}|sk-[A-Za-z0-9_\-]{8,}|(?:api[_-]?key|token|secret)\s*[:=]\s*\S+)"
)


def _redact_log_text(text: str) -> str:
    """One line, with credential-shaped fragments removed. Never a prompt dump."""
    collapsed = " ".join(str(text or "").split())
    return _LOG_SECRET_RE.sub("[redacted]", collapsed)


def _curator_drop_summary(drop_reasons) -> str:
    if not isinstance(drop_reasons, dict):
        return ""
    bits = []
    for item_id, reason in drop_reasons.items():
        reason_text = _redact_log_text(reason)
        if not reason_text:
            continue
        label = _redact_log_text(item_id)
        bits.append(f"{label}: {reason_text}" if label else reason_text)
    if not bits:
        return ""
    return "drops: " + "; ".join(bits)


def _format_curator_decline_reason(meta: dict) -> str:
    """skip_reason, per-item drops, then notes, capped so the log stays one line.

    Notes shrink first so a long note cannot hide the skip or drop reasons.
    """
    if not isinstance(meta, dict):
        return "no reason given"
    skip = _redact_log_text(meta.get("skip_reason") or "")
    notes = _redact_log_text(meta.get("notes") or "")
    if notes and notes == skip:
        notes = ""
    drops = _curator_drop_summary(meta.get("drop_reasons"))
    head = [part for part in (skip, drops) if part]
    if notes:
        room = _CURATOR_REASON_LIMIT - len("; ".join(head)) - (2 if head else 0)
        if room >= 8:
            if len(notes) > room:
                notes = notes[: room - 3] + "..."
            head.append(notes)
    text = "; ".join(head) if head else "no reason given"
    if len(text) > _CURATOR_REASON_LIMIT:
        text = text[: _CURATOR_REASON_LIMIT - 3] + "..."
    return text


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


def _curator_lane_declined(meta: dict) -> bool:
    """True only for an explicit editorial rejection of the whole lane.

    A decline is a parsed curator result whose ``_curator.approved`` is JSON
    false and that is not a fallback. Anything else still uses the deterministic
    writer:

    - subprocess failure (``run_curator_subprocess`` returns None): timeout,
      nonzero exit, or unparseable stdout
    - ``_curator.fallback`` true: backend error, bad JSON, missing prompt, or
      dry-run inside curator.py — even if ``approved`` is also false
    - ``approved`` omitted or true, including an approved empty item list
    - content-gate or Telegram rejection of an approved curated post
    """
    if not isinstance(meta, dict):
        return False
    if meta.get("fallback"):
        return False
    return meta.get("approved") is False


def _log_curator_lane_skipped(category: str, meta: dict) -> None:
    """Log the #23 decline reason and a stable skip line. Does not post."""
    reason = _format_curator_decline_reason(meta if isinstance(meta, dict) else {})
    print(f"[autopublish] curator declined {category}: {reason}", file=sys.stderr)
    print(
        f"lane_skipped lane={category} reason=curator_declined detail={reason}",
        file=sys.stderr,
    )


_FILLER_PHRASE = re.compile(
    r"release notes at the link|changelog details what|details what(?:'s| is) improved|"
    r"\bat the link\b",
    re.IGNORECASE,
)
_FILLER_GENERIC = {
    "post", "posts", "announces", "announce", "announced", "introduces",
    "introducing", "introduced", "release", "releases", "notes", "note",
    "changelog", "updated", "update", "link", "the", "a", "an", "on", "of",
    "and", "with", "for", "to", "in", "its", "is", "new", "ships", "shipped",
    "about", "page", "details", "improved", "tagged", "published",
}
_FILLER_SOURCE = {
    "openai", "mistral", "anthropic", "github", "hn", "hacker", "news",
    "google", "devin", "kiro", "copilot", "claude", "simon",
}
_CHANGE_VERB = re.compile(
    r"\b(?:fix|fixes|fixed|add|adds|added|remove|removes|removed|deprecat\w*|"
    r"break|breaks|breaking|support|supports|enable|enables|disable|disables|"
    r"patch|secur\w*|migrat\w*|now|can)\b",
    re.IGNORECASE,
)
_ITEM_ANCHOR = re.compile(
    r'<a href="[^"]*">(.*?)</a>\s*(?:—|–|-)\s*(.*)\s*\Z',
    re.DOTALL,
)


def _filler_words(text: str) -> set:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = re.sub(r"&[a-z]+;", " ", plain)
    words = re.findall(r"[a-z0-9][a-z0-9.+-]*", plain.lower())
    cleaned = set()
    for word in words:
        word = word.strip(".")
        if word not in _FILLER_GENERIC and len(word) > 1:
            cleaned.add(word)
    return cleaned


def blurb_is_filler(title: str, blurb: str) -> bool:
    """True when the line does not state a concrete change.

    A paraphrase of the title ("Mistral announces Mistral Large 4") and a
    canned "release notes at the link" with no change verb are filler.
    A blurb that names a fix, addition, or deprecation is not.
    """
    text = (blurb or "").strip()
    if not text:
        return True
    if _FILLER_PHRASE.search(text) and not _CHANGE_VERB.search(_FILLER_PHRASE.sub(" ", text)):
        return True
    extra = _filler_words(text) - _filler_words(title) - _FILLER_SOURCE
    return len(_filler_words(text)) > 0 and len(extra) == 0


def _closing_tokens(text: str) -> set:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return {word for word in re.findall(r"[a-z0-9]+", plain.lower()) if len(word) > 2}


def _closing_duplicates_one_bullet(closing: str, bullets: List[str]) -> bool:
    """Drop a closing line that restates a single bullet.

    A line that overlaps two bullets is the cross-item connection the prompt
    allows ("Mistral drew 680 comments; ColonistOne drew 80") and stays.
    """
    closing_tokens = _closing_tokens(closing)
    if len(closing_tokens) < 4 or not bullets:
        return False
    scores = []
    for bullet in bullets:
        bullet_tokens = _closing_tokens(bullet)
        if not bullet_tokens:
            scores.append(0.0)
            continue
        scores.append(len(closing_tokens & bullet_tokens) / min(len(closing_tokens), len(bullet_tokens)))
    if not scores or max(scores) < 0.72:
        return False
    return sum(1 for score in scores if score >= 0.45) < 2


def polish_lane_post(message: str) -> str:
    """Drop filler item lines and a closing line that repeats one bullet.

    Messages that are not lane HTML (no item link) pass through unchanged.
    An all-filler lane comes back empty so the caller can skip the send.
    """
    raw = message or ""
    if '<a href="' not in raw:
        return raw
    blocks = re.split(r"\n\s*\n", raw.strip())
    if not blocks:
        return raw
    header = blocks[0]
    items = []
    closings = []
    for block in blocks[1:]:
        if "<a href=" in block:
            items.append(block.strip())
        else:
            closings.append(block.strip())
    kept = []
    for block in items:
        match = _ITEM_ANCHOR.search(block.replace("\n", " "))
        if not match:
            kept.append(block)
            continue
        title = re.sub(r"<[^>]+>", "", match.group(1))
        blurb = re.sub(r"<[^>]+>", "", match.group(2))
        if blurb_is_filler(title, blurb):
            continue
        kept.append(block)
    if not kept:
        return ""
    closing = "\n\n".join(part for part in closings if part)
    if closing and _closing_duplicates_one_bullet(closing, kept):
        closing = ""
    count = len(kept)
    label = "item" if count == 1 else "items"
    header = re.sub(r"\b\d+\s+items?\b", f"{count} {label}", header, count=1)
    parts = [header, *kept]
    if closing:
        parts.append(closing)
    return "\n\n".join(parts)


def _log_no_substance(category: str) -> None:
    print(
        f"lane_skipped lane={category} reason=no_substance",
        file=sys.stderr,
    )


def _publish_lane(category: str, send: bool) -> tuple:
    """Publish one ready lane; return (sent, count).

    When the curator is enabled for the lane it runs the editorial pass and, on
    a successful curated result, sends the consolidated curated message. A
    curator failure or fallback marker (timeout, backend error, unparseable
    output) falls through to the deterministic writer. An explicit whole-lane
    decline does not: that lane is skipped for this run, nothing is sent, and
    queue/seen state is left untouched so the existing TTL can expire the
    items. The curator can still drop weak individual items on approved lanes.

    Send failures (Telegram down, content-gate rejection) return (False, count)
    WITHOUT marking the lane posted, so it retries on the next autopublish cycle.
    Any unexpected crash here is also caught by autopublish() so it cannot abort
    the whole lane loop."""
    if _curator_enabled_for(category):
        timeout = int(os.environ.get("CLAWBYTES_CURATOR_TIMEOUT", "300"))
        curated = run_curator_subprocess(curator_input_bundle(category), timeout=timeout)
        if curated is not None:
            meta = curated.get("_curator") or {}
            if _curator_lane_declined(meta):
                # Not a failure. Skip this run and do not call mark_posted, so
                # the items stay queued exactly as if the lane did not post.
                _log_curator_lane_skipped(category, meta)
                return (False, 0)
            fallback = bool(meta.get("fallback"))
            approved = bool(meta.get("approved", True)) and not fallback
            items = curated.get("items") or []
            # An approved empty list is not an explicit decline (approved
            # stayed true; models often return items: [] that way). A curated
            # body the gate or Telegram rejects is a send failure, not a
            # decline. Both still use the deterministic writer.
            if approved and items and send:
                raw = format_curated_html(curated, category)
                message = polish_lane_post(raw)
                # Filler-only lanes skip. Do not fall through to the
                # deterministic bundle, which would post the same empty lines.
                if '<a href="' in raw and not message:
                    _log_no_substance(category)
                    return (False, 0)
                ok, errs = validate_lane_for_publish(message)
                if ok and send_telegram(message):
                    mark_posted(category, None, items)
                    return (True, len(items))
                if not ok:
                    print(f"[autopublish] curated {category} rejected by gate: "
                          f"{'; '.join(errs)}; using deterministic bundle", file=sys.stderr)
                else:
                    print(f"[autopublish] curated {category} Telegram send failed; "
                          f"using deterministic bundle", file=sys.stderr)
            elif approved and items and not send:
                return (False, len(items))
            elif approved and not items:
                print(f"[autopublish] curator returned no items for {category}; "
                      f"using deterministic bundle", file=sys.stderr)
            # fallback marker, empty approved items, gate rejection, or send
            # failure → fall through to the deterministic writer
        else:
            # Curator subprocess error or empty result. Not a decline: post the
            # deterministic bundle, and say so. Do not dump prompts or stderr.
            print(
                f"[autopublish] curator error for {category}; using deterministic bundle",
                file=sys.stderr,
            )

    raw = format_category_bundle(category)
    message = polish_lane_post(raw)
    bundle = bundle_for_category(category)
    if send and bundle:
        if '<a href="' in raw and not message:
            _log_no_substance(category)
            return (False, 0)
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
            md = focus_changelog_section(md, item.get("title") or "")
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
        if item.get("reported"):
            slim["reported"] = True
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
        line = f"\n{item_emoji} <a href=\"{html_escape(url)}\">{html_escape(title)}</a>"
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
        lines.append(f"<a href=\"{html_escape(url)}\">{html_escape(title)}</a>")
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
    sub.add_parser("yield-snapshot", help="Write compact per-source yield JSON (no notify)")
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

    if args.cmd == "yield-snapshot":
        payload = write_source_yield()
        print(json.dumps(payload["latest"], indent=2))
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
