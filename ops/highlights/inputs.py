"""Pluggable inputs for the highlights-reel collector.

An input is any object with a ``name``, an ``enabled(ctx)`` check and a
``run(ctx)`` method returning ``SourceRun`` rows (one per thing it fetched,
so a three-channel Telegram input reports three sources). Register new ones
in ``INPUTS``. Inputs never raise out of ``run`` for a single bad source:
they record the error on that source's ``SourceRun`` and carry on.

Every item is a plain dict with exactly the keys in ``ITEM_KEYS``.
"""
from __future__ import annotations

import email.utils
import imaplib
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlencode, urlsplit

ITEM_KEYS = ("source", "channel", "post_url", "posted_at", "text", "links", "kind")

# AI Wire registry base URL; the feed is <base>/feed.xml. Kept out of the repo.
AI_WIRE_BASE_ENV = "AI_WIRE_BASE_URL"

# (source name on items, t.me slug, kind when the post carries no lane header)
TELEGRAM_CHANNELS = (
    ("clawbytes", "clawbytes", "post"),
    ("modelbytes", "ModelBytes", "post"),
    ("interesting-repos", "interestingrepos", "repo"),
)

# ClawBytes lane bundles open with e.g. "📚 Read — 2 items".
_LANE_RE = re.compile(r"^\W*(ship|watch|read|community)\b", re.I)


@dataclass
class Window:
    """One calendar day in ``tz``, as a half-open [start, end) UTC interval."""

    date: str
    tz: str
    start: datetime
    end: datetime

    def contains(self, dt: datetime | None) -> bool:
        return dt is not None and self.start <= dt < self.end


@dataclass
class Context:
    window: Window
    fetch: Callable[[str], str]
    max_pages: int = 10
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    extra_inputs: list[str] = field(default_factory=list)
    registry_url: str = ""  # explicit feed URL (--feed-url); else derived from AI_WIRE_BASE_URL


def registry_feed_url(ctx: Context) -> str:
    """``ctx.registry_url`` if set, else ``$AI_WIRE_BASE_URL/feed.xml``, else ""."""
    if ctx.registry_url:
        return ctx.registry_url
    base = (ctx.env.get(AI_WIRE_BASE_ENV) or "").strip().rstrip("/")
    return f"{base}/feed.xml" if base else ""


@dataclass
class SourceRun:
    name: str
    items: list[dict] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None


class Input(Protocol):
    name: str

    def enabled(self, ctx: Context) -> tuple[bool, str | None]:
        """Return (enabled, reason-if-disabled)."""

    def run(self, ctx: Context) -> list[SourceRun]:
        """Fetch and return one SourceRun per underlying source."""


def make_item(source, channel, post_url, posted_at, text, links, kind) -> dict:
    if isinstance(posted_at, datetime):
        posted_at = posted_at.astimezone(timezone.utc).isoformat()
    return {
        "source": source,
        "channel": channel,
        "post_url": post_url,
        "posted_at": posted_at,
        "text": (text or "").strip(),
        "links": list(dict.fromkeys(l for l in (links or []) if l)),
        "kind": kind,
    }


def parse_dt(value: str | None) -> datetime | None:
    """ISO-8601 or RFC 2822 to an aware UTC datetime; None if unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Telegram public preview (t.me/s/<channel>)
# ---------------------------------------------------------------------------

class _TgPageParser(HTMLParser):
    """Pull messages out of a t.me/s page without a third-party HTML parser."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._msg: dict | None = None
        self._msg_depth = 0      # open <div>s inside the current message
        self._text_depth = 0     # open <div>s inside its text block (0 = not in text)
        self._skip_depth = 0     # open <div>s inside a reply quote (ignored)
        self._skip_anchor = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and self._msg is None:
            if "tgme_widget_message" in classes and a.get("data-post"):
                self._msg = {"post": a["data-post"], "time": None, "text": [], "links": []}
                self._msg_depth = 1
            return
        if self._msg is None:
            return
        if tag == "br" and self._text_depth:
            self._msg["text"].append("\n")
            return
        if tag == "time" and self._msg["time"] is None and a.get("datetime"):
            self._msg["time"] = a["datetime"]
        if tag == "a" and self._text_depth and not self._skip_depth:
            href = a.get("href") or ""
            if href.startswith(("http://", "https://")):
                self._msg["links"].append(href)
        if tag == "div":
            self._msg_depth += 1
            if self._skip_depth:
                self._skip_depth += 1
            elif self._text_depth:
                self._text_depth += 1
            elif "tgme_widget_message_reply" in classes:
                self._skip_depth = 1
            elif "tgme_widget_message_text" in classes:
                self._text_depth = 1
        elif tag == "a" and "tgme_widget_message_reply" in classes:
            # Reply quotes are <a> blocks; their inner text div must not count.
            self._skip_depth = max(self._skip_depth, 1)
            self._skip_anchor = True

    def handle_endtag(self, tag):
        if self._msg is None:
            return
        if tag == "a" and self._skip_anchor:
            self._skip_anchor = False
            self._skip_depth = 0
            return
        if tag != "div":
            return
        self._msg_depth -= 1
        if self._skip_depth:
            self._skip_depth -= 1
        elif self._text_depth:
            self._text_depth -= 1
            if self._text_depth == 0:
                self._msg["text"].append("\n")
        if self._msg_depth == 0:
            self.messages.append(self._msg)
            self._msg = None

    def handle_data(self, data):
        if self._msg is not None and self._text_depth and not self._skip_depth:
            self._msg["text"].append(data)


def parse_tg_page(html: str) -> list[dict]:
    """Return [{id, post, posted_at, text, links}] for each message on the page."""
    p = _TgPageParser()
    p.feed(html)
    p.close()
    out = []
    for m in p.messages:
        try:
            msg_id = int(m["post"].rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        text = "".join(m["text"])
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        out.append({
            "id": msg_id,
            "post": m["post"],
            "posted_at": parse_dt(m["time"]),
            "text": text,
            "links": [l for l in m["links"] if urlsplit(l).netloc not in ("t.me", "telegram.me")],
        })
    return out


def tg_page_url(slug: str, before: int | None = None) -> str:
    url = f"https://t.me/s/{slug}"
    return f"{url}?{urlencode({'before': before})}" if before else url


def collect_tg_channel(ctx: Context, source: str, slug: str, default_kind: str) -> SourceRun:
    run = SourceRun(name=f"telegram:{slug.lower()}")
    before = None
    seen: set[int] = set()
    for _ in range(max(1, ctx.max_pages)):
        try:
            html = ctx.fetch(tg_page_url(slug, before))
        except Exception as exc:  # noqa: BLE001 - recorded on the source
            # A failed later page keeps what earlier pages found.
            run.error = f"{type(exc).__name__}: {exc}"
            break
        msgs = [m for m in parse_tg_page(html) if m["id"] not in seen]
        if not msgs:
            break
        for m in msgs:
            seen.add(m["id"])
            if not ctx.window.contains(m["posted_at"]):
                continue
            lane = _LANE_RE.match(m["text"])
            run.items.append(make_item(
                source=source,
                channel=f"t.me/{slug}",
                post_url=f"https://t.me/{m['post']}",
                posted_at=m["posted_at"],
                text=m["text"],
                links=m["links"],
                kind=lane.group(1).lower() if lane else default_kind,
            ))
        dated = [m["posted_at"] for m in msgs if m["posted_at"]]
        if dated and min(dated) < ctx.window.start:
            break  # the page already reaches back past the day
        before = min(m["id"] for m in msgs)
        if before <= 1:
            break
    if run.error and run.items:
        run.error = f"partial: {run.error}"
    return run


class TelegramInput:
    name = "telegram"

    def __init__(self, channels=TELEGRAM_CHANNELS):
        self.channels = channels

    def enabled(self, ctx):
        return True, None

    def run(self, ctx):
        return [collect_tg_channel(ctx, src, slug, kind) for src, slug, kind in self.channels]


# ---------------------------------------------------------------------------
# AI Wire registry RSS
# ---------------------------------------------------------------------------

def parse_registry_feed(xml_text: str) -> list[dict]:
    """Return [{title, link, posted_at, kind, summary, source_url}] from the registry RSS."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        desc = (it.findtext("description") or "").strip()
        lines = [l.strip() for l in desc.splitlines()]
        source_url = ""
        if lines and lines[-1].startswith(("http://", "https://")):
            source_url = lines.pop()
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "posted_at": parse_dt(it.findtext("pubDate")),
            "kind": (it.findtext("category") or "").strip() or "item",
            "summary": "\n".join(lines).strip(),
            "source_url": source_url,
        })
    return out


class RegistryInput:
    """AI Wire registry. ``repo`` items are filed under interesting-repos."""

    name = "registry"

    def enabled(self, ctx):
        if not registry_feed_url(ctx):
            return False, f"{AI_WIRE_BASE_ENV} not set (or pass --feed-url)"
        return True, None

    def run(self, ctx):
        run = SourceRun(name="registry")
        try:
            entries = parse_registry_feed(ctx.fetch(registry_feed_url(ctx)))
        except Exception as exc:  # noqa: BLE001
            run.error = f"{type(exc).__name__}: {exc}"
            return [run]
        for e in entries:
            if not ctx.window.contains(e["posted_at"]):
                continue
            text = e["title"] + (f"\n\n{e['summary']}" if e["summary"] else "")
            run.items.append(make_item(
                source="interesting-repos" if e["kind"] == "repo" else "registry",
                channel="ai-wire",
                post_url=e["link"],
                posted_at=e["posted_at"],
                text=text,
                links=[e["source_url"], e["link"]],
                kind=e["kind"],
            ))
        return [run]


# ---------------------------------------------------------------------------
# Credentialed inputs: off unless their env vars are set. These cannot run in
# CI (no credentials there); tests cover them with mocked transports only.
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class GmailInput:
    """Newsletters from a Gmail label over IMAP (stdlib ``imaplib``).

    Needs ``GMAIL_USER`` and ``GMAIL_APP_PASSWORD`` (an app password, not the
    account password). Optional ``GMAIL_LABEL`` (default ``INBOX``) and
    ``GMAIL_QUERY`` (Gmail search syntax, ANDed with the day's date range).
    """

    name = "gmail"
    imap_factory = staticmethod(lambda: imaplib.IMAP4_SSL("imap.gmail.com"))
    max_messages = 50

    def enabled(self, ctx):
        missing = [k for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD") if not ctx.env.get(k)]
        if missing:
            return False, f"{', '.join(missing)} not set"
        return True, None

    def run(self, ctx):
        run = SourceRun(name="gmail")
        try:
            run.items = self._fetch(ctx)
        except Exception as exc:  # noqa: BLE001
            run.error = f"{type(exc).__name__}: {exc}"
        return [run]

    def _fetch(self, ctx):
        import email as email_mod
        from email.policy import default as default_policy

        w = ctx.window
        local = datetime.fromisoformat(w.date)
        # Gmail's after:/before: are day-granular; the exact window filter below trims.
        query = f"after:{(local - timedelta(days=1)):%Y/%m/%d} before:{(local + timedelta(days=2)):%Y/%m/%d}"
        if ctx.env.get("GMAIL_QUERY"):
            query = f"{ctx.env['GMAIL_QUERY']} {query}"
        imap = self.imap_factory()
        items = []
        try:
            imap.login(ctx.env["GMAIL_USER"], ctx.env["GMAIL_APP_PASSWORD"])
            imap.select(f'"{ctx.env.get("GMAIL_LABEL") or "INBOX"}"', readonly=True)
            typ, data = imap.search(None, "X-GM-RAW", f'"{query}"')
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed: {typ}")
            ids = (data[0] or b"").split()[-self.max_messages:]
            for msg_id in ids:
                typ, parts = imap.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email_mod.message_from_bytes(parts[0][1], policy=default_policy)
                posted = parse_dt(msg.get("Date"))
                if not w.contains(posted):
                    continue
                body = msg.get_body(preferencelist=("plain", "html"))
                content = body.get_content() if body else ""
                if body is not None and body.get_content_type() == "text/html":
                    content = re.sub(r"<[^>]+>", " ", content)
                links = _URL_RE.findall(content)
                mid = (msg.get("Message-ID") or "").strip("<> ")
                items.append(make_item(
                    source="gmail",
                    channel=str(msg.get("From") or ""),
                    post_url=links[0] if links else f"mid:{mid}",
                    posted_at=posted,
                    text=f"{msg.get('Subject') or ''}\n\n{content[:2000]}",
                    links=links[:20],
                    kind="newsletter",
                ))
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
        return items


class XInput:
    """Recent posts from watched X accounts via the v2 recent-search API.

    Needs ``X_BEARER_TOKEN``; accounts come from ``X_HIGHLIGHTS_USERS``
    (comma-separated handles, default ``clawbytes``). Recent search only
    covers the last 7 days.
    """

    name = "x"
    search_url = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, get_json: Callable[[str, dict, dict], dict] | None = None):
        self._get_json = get_json

    def enabled(self, ctx):
        if not ctx.env.get("X_BEARER_TOKEN"):
            return False, "X_BEARER_TOKEN not set"
        return True, None

    def _default_get_json(self, url, params, headers):
        import requests

        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def run(self, ctx):
        run = SourceRun(name="x")
        users = [u.strip().lstrip("@") for u in (ctx.env.get("X_HIGHLIGHTS_USERS") or "clawbytes").split(",") if u.strip()]
        params = {
            "query": " OR ".join(f"from:{u}" for u in users) + " -is:retweet",
            "start_time": ctx.window.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": min(ctx.window.end, datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "max_results": 100,
            "tweet.fields": "created_at,entities,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        headers = {"Authorization": f"Bearer {ctx.env['X_BEARER_TOKEN']}"}
        try:
            data = (self._get_json or self._default_get_json)(self.search_url, params, headers)
        except Exception as exc:  # noqa: BLE001 - never echo headers
            run.error = type(exc).__name__ + (f": HTTP {exc.response.status_code}" if getattr(exc, "response", None) is not None else "")
            return [run]
        names = {u["id"]: u["username"] for u in (data.get("includes") or {}).get("users", [])}
        for t in data.get("data") or []:
            handle = names.get(t.get("author_id"), "i")
            urls = [u.get("expanded_url") for u in (t.get("entities") or {}).get("urls", [])]
            urls = [u for u in urls if u and "x.com/" not in u and "twitter.com/" not in u]
            post_url = f"https://x.com/{handle}/status/{t['id']}"
            run.items.append(make_item(
                source="x",
                channel=f"x.com/{handle}",
                post_url=post_url,
                posted_at=parse_dt(t.get("created_at")),
                text=t.get("text", ""),
                links=urls,
                kind="x_post",
            ))
        return [run]


class JsonFileInput:
    """Merge items gathered elsewhere (e.g. a Gmail or X export).

    Each ``--extra-input`` file is either a list of items or ``{"items": [...]}``.
    Items use the output item shape; missing ``source`` defaults to the file
    stem, and items outside the day window are dropped when ``posted_at`` is set.
    """

    name = "jsonfile"

    def enabled(self, ctx):
        if not ctx.extra_inputs:
            return False, "no --extra-input files"
        return True, None

    def run(self, ctx):
        runs = []
        for path in ctx.extra_inputs:
            run = SourceRun(name=f"jsonfile:{Path(path).name}")
            try:
                data = json.loads(Path(path).read_text())
                rows = data.get("items", []) if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    raise ValueError("expected a list or {'items': [...]}")
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    posted = parse_dt(r.get("posted_at"))
                    if r.get("posted_at") and not ctx.window.contains(posted):
                        continue
                    links = r.get("links") or []
                    if isinstance(links, str):
                        links = [links]
                    post_url = r.get("post_url") or (links[0] if links else "")
                    if not post_url:
                        continue
                    run.items.append(make_item(
                        source=r.get("source") or Path(path).stem,
                        channel=r.get("channel") or "",
                        post_url=post_url,
                        posted_at=posted or r.get("posted_at"),
                        text=r.get("text") or r.get("title") or "",
                        links=links,
                        kind=r.get("kind") or "item",
                    ))
            except Exception as exc:  # noqa: BLE001
                run.error = f"{type(exc).__name__}: {exc}"
            runs.append(run)
        return runs


INPUTS: dict[str, Callable[[], Input]] = {
    "telegram": TelegramInput,
    "registry": RegistryInput,
    "gmail": GmailInput,
    "x": XInput,
    "jsonfile": JsonFileInput,
}
DEFAULT_INPUTS = ("telegram", "registry", "gmail", "x", "jsonfile")
