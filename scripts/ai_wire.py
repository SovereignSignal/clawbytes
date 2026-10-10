"""Push posted ClawBytes items to the AI Wire registry.

The client follows the ingest contract: POST ``{AI_WIRE_URL}/api/ingest/items``
with ``Authorization: Bearer {AI_WIRE_INGEST_TOKEN}`` and a body of
``{"items": [...]}`` (at most 100 per request).

It stays off unless ``AI_WIRE_ENABLED`` is ``1``, ``true``, ``yes``, or
``on``. A missing URL or token does not open a socket. The push uses the
stdlib only, times out after 5 seconds, retries once on transport errors and
408/429/5xx, and never raises. Logs are ``ai_wire push ok n=`` or
``ai_wire push failed:`` and do not include the token or the response body.
"""
from __future__ import annotations

import html
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from feed_filters import STATUS_WATCH_FEEDS

SOURCE_BOT = "clawbytes"
CHANNEL = "clawbytes"
TIMEOUT_SECONDS = 5.0
MAX_BATCH = 100
SUMMARY_LIMIT = 600

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_LANES = {
    "ship": "Ship",
    "watch": "Watch",
    "read": "Read",
    "community": "Community",
}
_GH_RELEASE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)",
    re.IGNORECASE,
)
_POST_URL = re.compile(r"^https://t\.me/clawbytes/(\d+)$")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def enabled() -> bool:
    """True only when the feature flag is explicitly on. Default is off."""
    return os.environ.get("AI_WIRE_ENABLED", "").strip().lower() in _TRUTHY


def telegram_post_url(message_id) -> Optional[str]:
    """Public channel URL for one Telegram message, or None."""
    if message_id is None or isinstance(message_id, bool):
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    return f"https://t.me/clawbytes/{mid}"


def normalize_url(url: str) -> str:
    """Lowercase URL without query, fragment, or trailing slash.

    Returns ``""`` when ``url`` is not an http(s) URL. The result is the
    part after ``url:`` in a non-release canonical key.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.netloc:
        return ""
    netloc = parts.netloc.lower()
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    host, sep, port = netloc.partition(":")
    if sep and ((scheme == "http" and port == "80") or (scheme == "https" and port == "443")):
        netloc = host
    path = urllib.parse.unquote(parts.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}".lower()


def _iso(value) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
            if dt is None:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value) -> Optional[datetime]:
    text = _iso(value)
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _source_name(item: dict) -> str:
    for key in ("sourceName", "source_name", "feed"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _github_release(url: str) -> Optional[tuple]:
    match = _GH_RELEASE.match((url or "").strip())
    if not match:
        return None
    owner, repo, tag = (urllib.parse.unquote(part) for part in match.groups())
    if not owner or not repo or not tag:
        return None
    return owner, repo, tag


def _is_release(item: dict, github: Optional[tuple]) -> bool:
    if github is not None:
        return True
    name = _source_name(item).lower()
    return "releases" in name or "release notes" in name


def _is_status(item: dict) -> bool:
    return _source_name(item).lower() in STATUS_WATCH_FEEDS


def _lane_label(lane: str) -> str:
    text = (lane or "").strip()
    if not text:
        return ""
    return _LANES.get(text, _LANES.get(text.lower(), ""))


def _plain(text: str, limit: int) -> str:
    raw = html.unescape(text or "")
    raw = _TAG.sub(" ", raw)
    raw = _SPACE.sub(" ", raw).strip()
    return raw[:limit]


def _post_url(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _POST_URL.fullmatch(text):
        return text
    return None


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None
    return value


def _tags(value) -> list:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for tag in value:
        if isinstance(tag, str) and tag.strip():
            out.append(tag.strip())
    return out


def map_item(item: dict, *, lane: str = "", channel_post_url: Optional[str] = None, posted_at=None) -> Optional[dict]:
    """Map one ClawBytes backlog or curator item onto an ingest row.

    Returns None when the row cannot satisfy the required fields (title and
    an http(s) url). Kind is ``tool_release`` for a GitHub release tag or a
    release / release-notes source, ``status`` for the three status feeds,
    and ``news`` otherwise. The GitHub release key is
    ``release:<owner>/<repo>@<tag>``; every other key is ``url:`` plus the
    normalized URL. The whole canonical key is lowercase.
    """
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    raw_url = item.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    url = raw_url.strip()
    normalized = normalize_url(url)
    if not normalized:
        return None

    # Status wins when a feed is one of the three incident sources, even if
    # the title mentions a release. A GitHub tag URL is a release even when
    # the source name is not a releases feed.
    github = _github_release(url)
    if _is_status(item):
        kind = "status"
        canonical = f"url:{normalized}"
    elif _is_release(item, github):
        kind = "tool_release"
        if github is not None:
            owner, repo, tag = github
            canonical = f"release:{owner}/{repo}@{tag}".lower()
        else:
            canonical = f"url:{normalized}"
    else:
        kind = "news"
        canonical = f"url:{normalized}"

    payload = {
        "source_bot": SOURCE_BOT,
        "kind": kind,
        "canonical_key": canonical.lower(),
        "title": title.strip(),
        "url": url,
        "channel": CHANNEL,
    }
    summary_src = item.get("blurb") or item.get("summary") or item.get("existing_blurb") or ""
    if not isinstance(summary_src, str):
        summary_src = ""
    summary = _plain(summary_src, SUMMARY_LIMIT)
    if summary:
        payload["summary"] = summary
    published = _iso(item.get("publishedAt") or item.get("published_at"))
    if published:
        payload["published_at"] = published
    posted = _iso(posted_at if posted_at is not None else (item.get("postedAt") or item.get("posted_at")))
    if posted:
        payload["posted_at"] = posted
    post_url = _post_url(channel_post_url) or _post_url(item.get("channelPostUrl") or item.get("channel_post_url"))
    if post_url:
        payload["channel_post_url"] = post_url
    label = _lane_label(lane) or _lane_label(str(item.get("primaryCategory") or item.get("lane") or ""))
    if label:
        payload["lane"] = label
    org = item.get("org")
    if isinstance(org, str) and org.strip():
        payload["org"] = org.strip()
    elif github is not None and kind == "tool_release":
        payload["org"] = github[0]
    tags = _tags(item.get("tags"))
    if tags:
        payload["tags"] = tags
    score = _score(item.get("score"))
    if score is not None:
        payload["score"] = score
    return payload


def _posted_stamp(item: dict) -> Optional[datetime]:
    for key in ("postedAt", "posted_at", "discoveredAt", "discovered_at", "publishedAt", "published_at"):
        stamp = _parse_dt(item.get(key))
        if stamp is not None:
            return stamp
    return None


def _lane_for_posted(item: dict) -> str:
    posted = [c for c in (item.get("postedCategories") or []) if isinstance(c, str)]
    primary = item.get("primaryCategory")
    if isinstance(primary, str) and primary in posted:
        return primary
    if posted:
        return posted[0]
    return primary if isinstance(primary, str) else ""


def select_posted(items, *, days: int, now: Optional[datetime] = None) -> list:
    """Posted backlog rows whose post time falls inside the last ``days`` days.

    ``postedAt`` is the channel post time. Older rows use ``discoveredAt``,
    which the story window already treats as the stand-in for post time, then
    ``publishedAt``. Newest first so a later dedupe keeps the latest copy.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = moment - timedelta(days=days)
    chosen = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("status") != "posted":
            continue
        stamp = _posted_stamp(item)
        if stamp is None or stamp < cutoff:
            continue
        chosen.append((stamp, item))
    chosen.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _stamp, item in chosen]


def payloads_for_backfill(items, *, days: int, now: Optional[datetime] = None) -> list:
    """Ingest rows for already-posted items in the window. No network."""
    mapped = []
    for item in select_posted(items, days=days, now=now):
        row = map_item(
            item,
            lane=_lane_for_posted(item),
            channel_post_url=item.get("channelPostUrl") or item.get("channel_post_url"),
            posted_at=item.get("postedAt") or item.get("posted_at") or item.get("discoveredAt") or item.get("discovered_at"),
        )
        if row:
            mapped.append(row)
    return _dedupe(mapped)


def _dedupe(items: list) -> list:
    seen = set()
    out = []
    for item in items:
        key = item.get("canonical_key")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        text = str(reason).lower()
        if "timed out" in text or "timeout" in text:
            return "timeout"
        return "network"
    return type(exc).__name__


def _retryable(reason: str) -> bool:
    if reason in {"timeout", "network"}:
        return True
    if not reason.startswith("http_"):
        return False
    try:
        code = int(reason.split("_", 1)[1])
    except ValueError:
        return False
    return code in {408, 429} or 500 <= code <= 599


def _upserted(body: bytes, fallback: int) -> int:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    count = data.get("upserted")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return fallback


def _post_once(endpoint: str, token: str, items: list, timeout: float) -> tuple:
    payload = json.dumps({"items": items}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ClawBytes/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536)
            status = getattr(resp, "status", 200)
            return int(status), body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:  # noqa: BLE001 - a broken error body is still an HTTP result
            body = b""
        return int(exc.code), body


def _post_batch(endpoint: str, token: str, items: list, timeout: float) -> tuple:
    """POST one batch. Returns ``(ok, detail)``. Detail is an int count or a reason.

    One retry, and only for timeout, network, 408, 429, and 5xx. Never raises.
    """
    last = "error"
    for attempt in range(2):
        try:
            status, body = _post_once(endpoint, token, items, timeout)
        except Exception as exc:  # noqa: BLE001 - timeout and DNS must not escape
            last = _failure_reason(exc)
            if attempt == 0 and _retryable(last):
                continue
            return False, last
        if 200 <= status < 300:
            return True, _upserted(body, len(items))
        last = f"http_{status}"
        if attempt == 0 and _retryable(last):
            continue
        return False, last
    return False, last


def push_mapped(items: list) -> None:
    """POST already-mapped rows. Flag-off and empty input are silent no-ops.

    Never raises. A missing URL or token with the flag on logs
    ``ai_wire push failed: unconfigured`` and does not open a socket.
    """
    try:
        if not enabled():
            return
        rows = [item for item in (items or []) if isinstance(item, dict) and item.get("canonical_key")]
        if not rows:
            return
        base = os.environ.get("AI_WIRE_URL", "").strip().rstrip("/")
        token = os.environ.get("AI_WIRE_INGEST_TOKEN", "").strip()
        if not base or not token:
            print("ai_wire push failed: unconfigured", file=sys.stderr)
            return
        endpoint = base + "/api/ingest/items"
        total = 0
        for chunk in _chunks(rows, MAX_BATCH):
            ok, detail = _post_batch(endpoint, token, chunk, TIMEOUT_SECONDS)
            if not ok:
                print(f"ai_wire push failed: {detail}", file=sys.stderr)
                return
            total += detail if isinstance(detail, int) else len(chunk)
        print(f"ai_wire push ok n={total}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the publish path must survive this
        print(f"ai_wire push failed: {type(exc).__name__}", file=sys.stderr)


def push_clawbytes_items(items, *, lane: str = "", channel_post_url: Optional[str] = None, posted_at=None) -> None:
    """Map a posted lane and push it. Flag-off is a no-op. Never raises."""
    try:
        if not enabled():
            return
        mapped = []
        for item in items or []:
            row = map_item(item, lane=lane, channel_post_url=channel_post_url, posted_at=posted_at)
            if row:
                mapped.append(row)
        push_mapped(_dedupe(mapped))
    except Exception as exc:  # noqa: BLE001 - mapping bugs must not fail a post
        print(f"ai_wire push failed: {type(exc).__name__}", file=sys.stderr)
