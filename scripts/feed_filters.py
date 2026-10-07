"""Pure filters for the RSS sources added 2026-10-07.

No network and no memory files. The RSS monitor decides what to emit; the
classifier decides the lane. Both call these functions so a state-file item
cannot skip the filter.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# history.rss for the three vendors whose incidents are in Watch scope.
# OpenAI and OpenRouter status RSS still 403; they stay off.
STATUS_FEED_NAMES = (
    "Claude Status",
    "Cursor Status",
    "GitHub Status",
)
STATUS_WATCH_FEEDS = frozenset(name.lower() for name in STATUS_FEED_NAMES)

# A resolved incident shorter than this is minute-scale noise.
STATUS_MIN_MINUTES = 30

TESTINGCATALOG_FEED = "testingcatalog"
KILO_BLOG_FEED = "kilo blog"
HAVOPTIC_FEED = "havoptic releases"

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_STATUS_PREFIX = re.compile(
    r"^(?:investigating|identified|monitoring|resolved|update|scheduled|"
    r"in progress|completed|verifying)\b\s*[-:–—]\s*",
    re.IGNORECASE,
)
_INCIDENT_URL = re.compile(
    r"(https?://[^/\s]+)/incidents/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_STAMP = re.compile(
    r"\b([A-Z][a-z]{2,8})\s+(\d{1,2})\s*,\s*(\d{1,2}:\d{2})\s*UTC\b"
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_VERSION = re.compile(r"\b((?:rust-)?v?\d+\.\d+(?:\.\d+)*)\b", re.IGNORECASE)

# GitHub tag URL when Havoptic only linked a bare changelog blob.
# prefix "" means the tag is the version with no added "v".
_HAVOPTIC_GITHUB = {
    "claude code": ("anthropics", "claude-code", "v"),
    "gemini cli": ("google-gemini", "gemini-cli", "v"),
    "openai codex cli": ("openai", "codex", ""),
    "github copilot cli": ("github", "copilot-cli", "v"),
    "antigravity cli": ("google-antigravity", "antigravity-cli", ""),
}

# "degraded functionality" is a bug report, not an outage. Require the
# phrasing vendors use for a real incident.
_SEVERE = (
    "elevated error",
    "partial outage",
    "major outage",
    "service degradation",
    "degraded performance",
    "degraded availability",
    "degradation",
    "disruption",
    "outage",
    "unavailable",
    "unable to complete",
)
_MAINTENANCE = ("scheduled maintenance", "maintenance window")

_REPORTED = re.compile(
    r"\b(?:prepar(?:e|es|ing)|reportedly|rumou?rs?|leak(?:ed|s)?|spotted|"
    r"in testing|tests?|may release|could launch|unreleased|pre-release|"
    r"prerelease|checkpoint)\b",
    re.IGNORECASE,
)
_LAUNCHED = re.compile(
    r"\b(?:launches|launched|releases|released|unveils|unveiled|ships|shipped|"
    r"rolls out|now available|is now|adds|added)\b",
    re.IGNORECASE,
)
_TC_TOOL = re.compile(
    r"\b(?:antigravity|claude code|cursor|copilot|codex|devin|kilo|kiro|"
    r"windsurf|aider|cline|opencode|gemini cli|computer use|coding agent|"
    r"agent harness|mistral vibe|kimi code|grok build|mimo)\b",
    re.IGNORECASE,
)
_TC_MODEL = re.compile(
    r"\b(?:claude|gemini|mistral|deepseek|grok|qwen|opus|sonnet|codestral|"
    r"llama|mimo|gpt-\d|gpt\s+\d)\b",
    re.IGNORECASE,
)
_TC_MODEL_RELEASE = re.compile(
    r"\b(?:claude\s+(?:opus|sonnet|haiku|fable|mythos)|"
    r"gpt[-\s]?\d|gemini\s+\d|mistral\s+(?:large|medium|small)|"
    r"grok\s+\d|deepseek|qwen|codestral|llama\s+\d|mimo[-\s])\b",
    re.IGNORECASE,
)
_TC_CODING = re.compile(
    r"\b(?:coding|codex|computer use|deepswe|swe-bench|harness|cli|api)\b",
    re.IGNORECASE,
)
_KILO_PRODUCT = re.compile(
    r"\b(?:introducing|launches|launched|shipped|changelog|desktop|"
    r"cloud agents|sign in with|swarm|can now)\b",
    re.IGNORECASE,
)
_KILO_OPINION = re.compile(
    r"\b(?:what i learned|my take|reflections|trying to keep up|response to)\b",
    re.IGNORECASE,
)


def plain_text(value: str) -> str:
    text = _TAG.sub(" ", value or "")
    text = html.unescape(text)
    return _SPACE.sub(" ", text).strip()


def element_text(el) -> str:
    """Text of an RSS element, including nested markup such as Statuspage <var>."""
    if el is None:
        return ""
    if list(el):
        return "".join(el.itertext())
    return el.text or ""


def incident_public_url(url: str) -> str:
    """One URL per incident. Update query strings are the same story."""
    raw = (url or "").strip()
    match = _INCIDENT_URL.search(raw)
    if not match:
        return raw.split("#", 1)[0].split("?", 1)[0]
    return f"{match.group(1)}/incidents/{match.group(2)}"


def status_story_title(title: str) -> str:
    """Drop a leading Investigating/Identified/Resolved prefix.

    The words are an update, not a new incident. The incident name stays.
    """
    text = plain_text(title)
    previous = None
    while text and previous != text:
        previous = text
        text = _STATUS_PREFIX.sub("", text).strip()
    return text or plain_text(title)


def status_display_title(feed_name: str, title: str) -> str:
    """Title that keeps distinct incidents from sharing the status boilerplate.

    Cursor files many incidents as "Investigating service degradation — X".
    The shared words would collapse them under the 7-day story rule. The
    component after the dash is the story.
    """
    cleaned = status_story_title(title)
    feed = (feed_name or "").lower().strip()
    vendor = {
        "claude status": "Claude",
        "cursor status": "Cursor",
        "github status": "GitHub",
    }.get(feed, "")
    subject = cleaned
    if feed == "cursor status" and "service degradation" in cleaned.lower():
        parts = re.split(r"\s+[—–-]\s+", cleaned, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            subject = parts[1].strip()
    if vendor and vendor.lower() not in subject.lower():
        return f"{vendor}: {subject}"
    return subject


def collapse_status_entries(entries: list) -> list:
    """One item per incident URL, with every update's text kept for the filter."""
    grouped = {}
    order = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        url = incident_public_url(entry.get("link") or entry.get("id") or "")
        key = url or (entry.get("id") or entry.get("title") or "")
        detail = plain_text(entry.get("detail") or entry.get("summary") or "")
        title = status_story_title(entry.get("title") or "")
        current = grouped.get(key)
        if current is None:
            order.append(key)
            grouped[key] = {
                **entry,
                "title": title,
                "link": url or (entry.get("link") or ""),
                "id": url or (entry.get("id") or ""),
                "detail": detail,
                "summary": detail[:500],
            }
            continue
        merged = f"{current.get('detail') or ''} {detail}".strip()
        current["detail"] = merged
        current["summary"] = merged[:500]
        if len(title) > len(current.get("title") or ""):
            current["title"] = title
    return [grouped[key] for key in order]


def _parse_published(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp_datetimes(detail: str, published: str) -> list:
    pub = _parse_published(published)
    year = pub.year if pub else datetime.now(timezone.utc).year
    found = []
    for match in _STAMP.finditer(detail or ""):
        month = _MONTHS.get(match.group(1).lower())
        if not month:
            continue
        day = int(match.group(2))
        hour, minute = (int(part) for part in match.group(3).split(":"))
        stamp_year = year
        if pub is not None and month > pub.month + 1:
            stamp_year = year - 1
        try:
            found.append(datetime(stamp_year, month, day, hour, minute, tzinfo=timezone.utc))
        except ValueError:
            continue
    return found


def incident_duration_minutes(detail: str, published: str = "") -> Optional[int]:
    """Minutes from the earliest status stamp to the latest, or None if unknown."""
    stamps = _stamp_datetimes(detail or "", published)
    if len(stamps) < 2:
        return None
    span = max(stamps) - min(stamps)
    return int(span.total_seconds() // 60)


def _resolved(detail: str) -> bool:
    return bool(re.search(r"\bresolved\b", detail or "", re.IGNORECASE))


def _severe(blob: str) -> bool:
    if any(phrase in blob for phrase in _MAINTENANCE):
        return False
    if re.search(r"\bminor\b", blob) and not any(
        phrase in blob for phrase in ("major outage", "partial outage", "elevated error")
    ):
        return False
    if any(phrase in blob for phrase in _SEVERE):
        return True
    return "incident with" in blob


def _too_short(detail: str, published: str) -> bool:
    """Resolved incidents that lasted under STATUS_MIN_MINUTES are noise.

    An incident that is still open is kept: the first poll may be during the
    event. A later update is the same incident id and is not posted again.
    """
    if not _resolved(detail):
        return False
    minutes = incident_duration_minutes(detail, published)
    if minutes is None:
        return False
    return minutes < STATUS_MIN_MINUTES


def _claude_in_scope(blob: str) -> bool:
    if any(phrase in blob for phrase in (
        "usage page", "usage data", "usage report", "google play",
        "office 365", "microsoft office",
    )):
        # Usage-page and billing surfaces name "API" without being the
        # inference API. A real Claude API line still counts below.
        usage_only = "claude api" not in blob and "claude code" not in blob and "claude.ai" not in blob
        if usage_only and "cowork" not in blob:
            return False
    if any(phrase in blob for phrase in ("credit", "subscription", "logging into", "log in", "login")):
        if "claude code" not in blob and "claude.ai" not in blob and "claude api" not in blob:
            return False
    if "claude code" in blob or "claude.ai" in blob or "cowork" in blob:
        return True
    if "claude api" in blob or re.search(r"\bthe api\b", blob):
        return True
    if re.search(r"\b(opus|sonnet|haiku|fable|mythos)\b", blob) and "elevated error" in blob:
        return True
    if "multiple models" in blob and ("elevated error" in blob or "degraded" in blob):
        return True
    return False


def _cursor_in_scope(blob: str) -> bool:
    # Upstream model mirrors that only cite Claude's status page double-post
    # that incident. Keep them when a Cursor surface is also named.
    cursor_surface = any(phrase in blob for phrase in (
        "cloud agent", "security reviewer", "review agent", "automation",
    ))
    if "status.claude.com" in blob and not cursor_surface:
        return False
    if cursor_surface:
        return True
    if re.search(r"\bgrok bot\b|\bxai\b|grok \d", blob):
        return False
    if "sign-in" in blob or "sign in" in blob or "dashboard" in blob:
        return False
    if any(phrase in blob for phrase in (
        "service degradation", "degraded performance", "degraded availability", "elevated error",
    )):
        return True
    return False


def _github_in_scope(title: str, blob: str) -> bool:
    """Copilot, Actions, or the GitHub API.

    The component has to be the incident, not a passing mention. A pull-request
    writeup that notes "Actions runs start after a merge" is not an Actions
    outage. A billing title stays out even when Copilot CLI was collateral.
    """
    title_l = title.lower()
    if re.search(r"\bbilling\b|\binvoice\b|\blicensing\b", title_l):
        return False
    if (
        "copilot" in title_l
        or re.search(r"\bactions\b", title_l)
        or re.search(r"\bapi\b", title_l)
    ):
        return True
    # Vague titles ("some GitHub services", a model name) still count when
    # the body says Copilot or the GitHub API failed.
    if "copilot" in blob and "pull request" not in title_l:
        return True
    if re.search(r"\bgithub api\b|\bapi requests\b|\bapi errors\b", blob):
        return True
    return False


def status_incident_allowed(feed_name: str, entry: dict) -> bool:
    """True for a partial/major/elevated incident on an in-scope component."""
    feed = (feed_name or "").lower().strip()
    if feed not in STATUS_WATCH_FEEDS:
        return False
    if not isinstance(entry, dict):
        return False
    detail = plain_text(entry.get("detail") or entry.get("summary") or "")
    title = plain_text(entry.get("title") or "")
    blob = f"{title} {detail}".lower()
    if any(phrase in blob for phrase in _MAINTENANCE):
        return False
    if not _severe(blob):
        return False
    if feed == "claude status" and not _claude_in_scope(blob):
        return False
    elif feed == "cursor status" and not _cursor_in_scope(blob):
        return False
    elif feed == "github status" and not _github_in_scope(title, blob):
        return False
    if _too_short(detail, entry.get("published") or ""):
        return False
    return True


def status_blurb(feed_name: str, entry: dict) -> str:
    """Deterministic Watch line. No calendar date (that trips the 3-day stale gate)."""
    detail = plain_text((entry or {}).get("detail") or (entry or {}).get("summary") or "")
    low = f"{(entry or {}).get('title') or ''} {detail}".lower()
    feed = (feed_name or "").lower().strip()
    parts = []
    if feed == "claude status":
        for label, phrase in (
            ("Claude", "claude.ai"),
            ("Claude Code", "claude code"),
            ("Claude Cowork", "cowork"),
            ("the Claude API", "claude api"),
        ):
            if phrase in low and label not in parts:
                parts.append(label)
        if not parts and re.search(r"\b(opus|sonnet|haiku|fable|mythos)\b", low):
            parts.append("Claude model requests")
        elif not parts and "multiple models" in low:
            parts.append("multiple Claude models")
    elif feed == "cursor status":
        subject = status_display_title(feed, (entry or {}).get("title") or "")
        subject = re.sub(r"^Cursor:\s*", "", subject)
        if subject:
            parts.append(subject)
    elif feed == "github status":
        for label, present in (
            ("Copilot", "copilot" in low),
            ("Actions", re.search(r"\bactions\b", low) is not None),
            ("the GitHub API", re.search(r"\bapi\b", low) is not None),
        ):
            if present:
                parts.append(label)
    if parts:
        body = "Affects " + ", ".join(parts)
    else:
        body = status_story_title((entry or {}).get("title") or "")
    minutes = incident_duration_minutes(detail, (entry or {}).get("published") or "")
    if _resolved(detail) and minutes is not None:
        body += f", resolved after {minutes} minutes"
    elif not _resolved(detail):
        body += ", still open"
    return body


# Queued status incidents older than this are backlog, not news. Watch posts
# twice a day, so a real incident still clears a window inside this span.
STATUS_QUEUE_MAX_AGE = timedelta(hours=24)


def status_incident_bounds(detail: str, published: str = "") -> tuple[Optional[datetime], Optional[datetime]]:
    """Earliest and latest stamps on an incident, when the body has them.

    ``published`` fills in when the body has no clock time. The latest stamp
    is a resolve time only when the text says the incident resolved.
    """
    text = detail or ""
    stamps = _stamp_datetimes(text, published or "")
    published_dt = _parse_published(published or "")
    start = min(stamps) if stamps else published_dt
    resolved = None
    if _resolved(text):
        resolved = max(stamps) if stamps else published_dt
    return start, resolved


def status_item_predates_watch(
    detail: str,
    published: str,
    baseline_at: Optional[datetime],
    now: datetime,
    summary: str = "",
) -> bool:
    """True when a queued status incident should not post.

    Drop it when it started or resolved before that feed's baseline, or when
    the incident itself is more than 24 hours old. Discovery time is not the
    incident's age.
    """
    text = detail or ""
    if summary and not _stamp_datetimes(text, published or ""):
        if not text:
            text = summary
        elif _resolved(summary) and not _resolved(text):
            text = f"{text} {summary}"
    start, resolved = status_incident_bounds(text, published or "")
    if baseline_at is not None:
        if start is not None and start < baseline_at:
            return True
        if resolved is not None and resolved < baseline_at:
            return True
    earliest = start or resolved
    if earliest is not None and (now - earliest) > STATUS_QUEUE_MAX_AGE:
        return True
    return False


def is_reported_claim(title: str, summary: str = "") -> bool:
    """True when the headline is a leak or pre-release, not a launch verb."""
    if _REPORTED.search(title or ""):
        return True
    if _LAUNCHED.search(title or ""):
        return False
    blob = f"{title or ''} {summary or ''}"
    return bool(_REPORTED.search(blob)) and not _LAUNCHED.search(blob)


def _categories(entry: dict) -> set:
    raw = entry.get("categories")
    if raw is None:
        raw = entry.get("category") or ""
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def testingcatalog_relevant(entry: dict) -> bool:
    """Coding-tool and coding-model items. Sponsored posts and consumer AI stay out."""
    if not isinstance(entry, dict):
        return False
    if "sponsored" in _categories(entry):
        return False
    blob = plain_text(f"{entry.get('title') or ''} {entry.get('summary') or ''} {entry.get('detail') or ''}")
    if _TC_TOOL.search(blob) or _TC_MODEL_RELEASE.search(blob):
        return True
    return bool(_TC_MODEL.search(blob) and _TC_CODING.search(blob))


def testingcatalog_blurb(entry: dict, reported: bool) -> str:
    sentence = plain_text(entry.get("summary") or entry.get("detail") or "")
    sentence = sentence.split(". ")[0].strip()
    if len(sentence) > 110:
        sentence = sentence[:110].rsplit(" ", 1)[0]
    if reported:
        prefix = "Reportedly"
        if sentence:
            return f"{prefix}: {sentence}"
        return "Reportedly spotted, not a confirmed launch"
    return sentence or "TestingCatalog launch note"


def kilo_product_post(entry: dict) -> bool:
    """Kilo's Substack mixes product posts with essays. Ship only the product posts."""
    title = (entry or {}).get("title") or ""
    if _KILO_OPINION.search(title):
        return False
    return bool(_KILO_PRODUCT.search(title))


def version_key(text: str) -> str:
    """Comparable version token. Leading v is stripped; rust- tags stay intact."""
    match = _VERSION.search(text or "")
    if not match:
        return ""
    token = match.group(1).lower()
    if token.startswith("rust-"):
        return token
    if token.startswith("v"):
        return token[1:]
    return token


def _bare_changelog(link: str) -> bool:
    """True for a shared changelog page, not a per-version path."""
    raw = (link or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "https://" + raw
    from urllib.parse import urlsplit
    path = (urlsplit(raw).path or "").rstrip("/").lower()
    return path.endswith("/changelog") or path.endswith("changelog.md")


def normalize_havoptic_entry(entry: dict) -> Optional[dict]:
    """Point the item at the vendor URL and name the feed after the tool.

    A bare changelog URL would let postedUrls swallow the next version
    (invariant 4). GitHub tools get the tag URL the primary atom already uses,
    so the same release dedupes. Other tools get a version fragment.
    """
    if not isinstance(entry, dict):
        return None
    category = (entry.get("category") or "").strip()
    if not category and isinstance(entry.get("categories"), list) and entry["categories"]:
        category = str(entry["categories"][0]).strip()
    title = plain_text(entry.get("title") or "")
    link = (entry.get("link") or "").strip()
    if not category or not title:
        return None
    version = version_key(title) or version_key(link)
    spec = _HAVOPTIC_GITHUB.get(category.lower())
    if "/releases/tag/" in link:
        vendor = link
    elif spec and version and _bare_changelog(link):
        owner, repo, prefix = spec
        if version.startswith("rust-"):
            tag = version
        elif prefix and not version.startswith(prefix):
            tag = prefix + version
        else:
            tag = version
        vendor = f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
    elif version and link and _bare_changelog(link):
        vendor = link.split("#", 1)[0].rstrip("/") + "#" + version
    else:
        vendor = link
    if not vendor:
        return None
    guid = (entry.get("id") or "").strip() or f"havoptic-{category}-{version or title}"
    return {
        **entry,
        "title": title,
        "link": vendor,
        "id": guid,
        "category": category,
        "summary": plain_text(entry.get("summary") or "")[:500],
        "_feed": f"Havoptic {category} Releases",
        "aggregator": "havoptic",
    }


def havoptic_feed(name: str) -> bool:
    return (name or "").lower().startswith("havoptic")


def urls_same_release(left: str, right: str) -> bool:
    """True when two URLs are the same release page.

    Fragments distinguish versions on a bare changelog. They must not compare
    equal, or the first Grok Build post would swallow the next one.
    """
    a = (left or "").strip()
    b = (right or "").strip()
    if not a or not b:
        return False
    if a.rstrip("/") == b.rstrip("/"):
        return True
    if "#" in a or "#" in b:
        return False
    return _canonical(a) == _canonical(b) and _canonical(a) != ""


def _canonical(url: str) -> str:
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    # Local import keeps this module free of the publisher.
    from urllib.parse import urlsplit
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    if not host:
        return ""
    return f"{host}{path}"
