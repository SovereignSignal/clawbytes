"""Draft the Tue/Fri ClawBytes Substack issue from the ai-wire registry feed.

    python -m ops.digest.draft --md-out issue.md --html-out issue.html
    python -m ops.digest.draft --dry-run            # no Claude call

The feed is $AI_WIRE_BASE_URL/feed.xml unless --feed-url or --feed-file is given.

Pipeline: fetch the registry RSS -> keep items inside the issue window ->
group by section -> cap each section (recency + source diversity) -> Claude
writes the copy from a structured JSON of the selected items -> deterministic
validation (every section present, every item covered, banned phrase gone) ->
render markdown + email-safe HTML. Links are attached by us from the feed, not
by the model, so every item always carries its primary source URL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

# Registry base URL; the feed is <base>/feed.xml. Kept out of the repo.
AI_WIRE_BASE_ENV = "AI_WIRE_BASE_URL"
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-1"
DEFAULT_MAX_TOKENS = 16000

BANNED_PHRASE = "Where AI is heading"
_BANNED_RE = re.compile(r"where\s+ai\s+is\s+heading", re.I)

# Required sections, in issue order. "also" is optional and small.
SECTIONS = [("agents", "Agents & tools"), ("models", "Models"), ("repos", "Repos")]
ALSO = ("also", "Also worth a look")
DEFAULT_CAPS = {"agents": 6, "models": 5, "repos": 6, "also": 3}

# news/paper items about agents or coding tools go to "Agents & tools";
# everything else lands in "also". Word-bounded to dodge substring traps.
_AGENT_RE = re.compile(
    r"\b(agents?|agentic|coding|code|cli|ide|copilot|codex|cursor|devin|kiro|"
    r"cline|opencode|harness|sdk|mcp|plugins?|extensions?|terminal|autofix|"
    r"tools?|tooling|workflows?|swe|vibe|developers?|devtools|source control|"
    r"version control)\b",
    re.I,
)

# USD per million tokens (input, output). Unknown models log tokens only.
PRICES = {
    "claude-opus-4-0": (15.0, 75.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5": (10.0, 50.0),
    "claude-fable-5-1": (10.0, 50.0),
}

NAVY = "#1B2A41"
RED = "#C8102E"
BG = "#F3F1EC"
INK = "#2B2F36"
MUTED = "#6B7280"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"


class DraftError(RuntimeError):
    """The model's draft could not be made valid."""


class ClaudeError(RuntimeError):
    """The Messages API call failed."""


def log(msg: str) -> None:
    print(f"digest: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------

@dataclass
class Item:
    title: str
    kind: str
    summary: str
    url: str          # primary source URL (fallback: site item page)
    link: str         # site item page
    published: dt.datetime
    id: str = ""
    section: str = ""

    @property
    def source(self) -> str:
        return source_key(self.url)


def _is_http(url: str) -> bool:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def parse_description(desc: str) -> tuple[str, str]:
    """Split "summary\\n\\nhttps://primary" into (summary, primary_url)."""
    lines = [ln.rstrip() for ln in (desc or "").strip().splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.fullmatch(r"https?://\S+", lines[-1].strip()):
        url = lines.pop().strip()
        return " ".join(ln.strip() for ln in lines if ln.strip()), url
    return " ".join(ln.strip() for ln in lines if ln.strip()), ""


def _parse_date(entry) -> dt.datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def feed_home(data: bytes | str) -> str:
    """The feed's channel <link> (the registry homepage), or "" if it isn't http(s)."""
    link = (feedparser.parse(data).feed.get("link") or "").strip()
    return link if _is_http(link) else ""


def parse_feed(data: bytes | str, site_url: str = "") -> list[Item]:
    """`site_url` is the last-resort link for an item with no source URL and no page."""
    parsed = feedparser.parse(data)
    items = []
    for e in parsed.entries:
        published = _parse_date(e)
        title = (e.get("title") or "").strip()
        if not published or not title:
            continue
        tags = e.get("tags") or []
        kind = (tags[0].get("term") if tags else "") or ""
        summary, primary = parse_description(e.get("summary") or e.get("description") or "")
        link = (e.get("link") or "").strip()
        if not _is_http(link):
            link = ""
        url = primary if _is_http(primary) else (link or site_url)
        items.append(Item(title=title, kind=kind.strip().lower(), summary=summary,
                          url=url, link=link, published=published))
    return items


def load_feed(source: str, get=requests.get) -> bytes:
    if source.startswith(("http://", "https://")):
        resp = get(source, timeout=30, headers={"User-Agent": "clawbytes-digest/1.0"})
        resp.raise_for_status()
        return resp.content
    return Path(source).read_bytes()


# --------------------------------------------------------------------------
# Window, grouping, ranking
# --------------------------------------------------------------------------

def default_days(issue_date: dt.date) -> int:
    """Tuesday covers Fri-Mon (4 days), Friday covers Tue-Thu (3)."""
    return 3 if issue_date.weekday() == 4 else 4


def issue_window(issue_date: dt.date, days: int | None = None,
                 since: dt.date | None = None) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) in UTC. The issue day itself is excluded."""
    end = dt.datetime.combine(issue_date, dt.time(), tzinfo=dt.timezone.utc)
    if since is not None:
        start = dt.datetime.combine(since, dt.time(), tzinfo=dt.timezone.utc)
    else:
        start = end - dt.timedelta(days=days if days is not None else default_days(issue_date))
    if start >= end:
        raise ValueError(f"window start {start.date()} is not before issue date {issue_date}")
    return start, end


def in_window(items: list[Item], start: dt.datetime, end: dt.datetime) -> list[Item]:
    return [it for it in items if start <= it.published < end]


def section_for(item: Item) -> str:
    if item.kind == "tool_release":
        return "agents"
    if item.kind == "model":
        return "models"
    if item.kind == "repo":
        return "repos"
    if item.kind in ("news", "paper") and _AGENT_RE.search(f"{item.title} {item.summary}"):
        return "agents"
    return "also"


def group_items(items: list[Item]) -> dict[str, list[Item]]:
    groups: dict[str, list[Item]] = {k: [] for k, _ in SECTIONS}
    groups[ALSO[0]] = []
    for it in items:
        it.section = section_for(it)
        groups[it.section].append(it)
    return groups


def source_key(url: str) -> str:
    """Coarse "who published this" key used for diversity ranking."""
    p = urlparse(url)
    host = (p.netloc or "").lower().removeprefix("www.")
    parts = [s for s in p.path.split("/") if s]
    if host == "github.com" and len(parts) >= 2:
        return f"github.com/{parts[0].lower()}/{parts[1].lower()}"
    if host == "huggingface.co" and parts:
        if parts[0] == "blog" and len(parts) >= 2:
            return f"huggingface.co/blog/{parts[1].lower()}"
        if parts[0] == "papers":
            return "huggingface.co/papers"
        return f"huggingface.co/{parts[0].lower()}"
    return host


def rank(items: list[Item], cap: int) -> list[Item]:
    """Newest first, but one item per source before any source repeats."""
    ordered = sorted(items, key=lambda it: (-it.published.timestamp(), it.title.lower()))
    picked, seen = [], set()
    for it in ordered:
        if len(picked) >= cap:
            break
        if it.source not in seen:
            picked.append(it)
            seen.add(it.source)
    for it in ordered:
        if len(picked) >= cap:
            break
        if it not in picked:
            picked.append(it)
    return sorted(picked, key=lambda it: (-it.published.timestamp(), it.title.lower()))


def select(items: list[Item], caps: dict[str, int]) -> dict[str, list[Item]]:
    groups = group_items(items)
    selected = {k: rank(v, caps.get(k, 0)) for k, v in groups.items()}
    n = 0
    for key in [k for k, _ in SECTIONS] + [ALSO[0]]:
        for it in selected[key]:
            n += 1
            it.id = f"i{n}"
    return selected


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You write ClawBytes, a newsletter that goes out every Tuesday and Friday about AI agents, coding tools, models and open-source repos. Readers are developers. Write in plain English with short, concrete sentences.

Rules:
1. Only use facts from the provided items. Do not add numbers, versions, dates, names, benchmarks, prices or claims that are not in an item's title or summary. If an item's summary is thin, say less rather than guessing.
2. Link every item: write exactly one entry for every input item, using its "id", so its source link is attached when the issue is published. Do not drop, merge or invent items, and keep each item in the section it was given in.
3. No hype. No superlatives such as "game-changing", "revolutionary" or "huge", no exclamation marks, no emojis.
4. Never use the phrase "{BANNED_PHRASE}", in any capitalization, anywhere in the output.

Return only a JSON object, with no markdown fences and no text before or after it, in this shape:
{{
  "title": "issue title, at most 8 words, naming the through-line",
  "opener": "one short paragraph (2-3 sentences) naming the through-line across these items",
  "sections": {{
    "<section key>": [{{"id": "<item id>", "headline": "short plain headline", "body": "1-2 plain-English sentences on what it is and why a developer might care"}}]
  }},
  "lookahead": "one sentence on what to watch next, grounded only in the items"
}}
Include a key in "sections" for every section present in the input, and only those."""


def build_payload(selected: dict[str, list[Item]], issue_date: dt.date,
                  start: dt.datetime, end: dt.datetime) -> dict:
    labels = dict(SECTIONS + [ALSO])
    sections = {}
    for key, items in selected.items():
        if not items:
            continue
        sections[key] = {
            "label": labels[key],
            "items": [{
                "id": it.id,
                "kind": it.kind,
                "title": it.title,
                "summary": it.summary,
                "source_url": it.url,
                "published": it.published.strftime("%Y-%m-%d"),
            } for it in items],
        }
    return {
        "issue_date": issue_date.isoformat(),
        "covers": {"from": start.date().isoformat(),
                   "to": (end - dt.timedelta(days=1)).date().isoformat()},
        "sections": sections,
    }


def build_user_prompt(payload: dict) -> str:
    return ("Draft the ClawBytes issue for "
            f"{payload['issue_date']} from these items:\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# Claude call
# --------------------------------------------------------------------------

def estimate_cost(model: str, usage: dict) -> float | None:
    base = re.sub(r"-\d{8}$", "", model)
    price = PRICES.get(base)
    if price is None:
        return None
    pin, pout = price
    return (usage.get("input_tokens", 0) * pin
            + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
            + usage.get("cache_read_input_tokens", 0) * pin * 0.1
            + usage.get("output_tokens", 0) * pout) / 1_000_000


def log_usage(model: str, usage: dict) -> None:
    cost = estimate_cost(model, usage)
    cost_s = f"${cost:.4f}" if cost is not None else "unknown (no price for model)"
    log(f"claude usage model={model} input_tokens={usage.get('input_tokens', 0)} "
        f"output_tokens={usage.get('output_tokens', 0)} "
        f"cache_read={usage.get('cache_read_input_tokens', 0)} "
        f"cache_write={usage.get('cache_creation_input_tokens', 0)} est_cost={cost_s}")


def call_claude(system: str, messages: list[dict], model: str, api_key: str,
                max_tokens: int = DEFAULT_MAX_TOKENS, post=requests.post) -> tuple[str, dict]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    try:
        resp = post(API_URL, headers=headers, json=body, timeout=600)
    except requests.RequestException as exc:
        raise ClaudeError(f"request failed: {type(exc).__name__}") from None
    if resp.status_code >= 300:
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        raise ClaudeError(f"HTTP {resp.status_code}: {detail}")
    data = resp.json()
    usage = data.get("usage") or {}
    log_usage(model, usage)
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise ClaudeError("model declined the request (stop_reason=refusal)")
    if stop == "max_tokens":
        raise ClaudeError("output truncated (stop_reason=max_tokens); raise --max-tokens")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, usage


# --------------------------------------------------------------------------
# Draft model + validation
# --------------------------------------------------------------------------

@dataclass
class Entry:
    item: Item
    headline: str
    body: str


@dataclass
class Draft:
    title: str
    opener: str
    lookahead: str
    sections: dict[str, list[Entry]] = field(default_factory=dict)


def strip_banned(text: str) -> str:
    """Remove the banned phrase and tidy what is left around it."""
    if not _BANNED_RE.search(text or ""):
        return text
    out = _BANNED_RE.sub("", text)
    out = re.sub(r"\(\s*\)|\[\s*\]|\"\s*\"|“\s*”", "", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*([,.;:])", r"\2", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" \t,;:-–—")


def contains_banned(text: str) -> bool:
    return bool(_BANNED_RE.search(text or ""))


def parse_model_json(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def validate(obj: dict, selected: dict[str, list[Item]]) -> list[str]:
    """Structural problems with a model draft (banned phrase checked separately)."""
    errors = []
    for key in ("title", "opener", "lookahead"):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            errors.append(f'missing or empty "{key}"')
    sections = obj.get("sections")
    if not isinstance(sections, dict):
        return errors + ['missing "sections" object']
    for key, items in selected.items():
        if not items:
            continue
        entries = sections.get(key)
        if not isinstance(entries, list) or not entries:
            errors.append(f'missing section "{key}"')
            continue
        got = {}
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                got[e["id"]] = e
        for it in items:
            e = got.get(it.id)
            if e is None:
                errors.append(f'section "{key}" is missing item {it.id} ({it.title})')
            elif not str(e.get("headline", "")).strip() or not str(e.get("body", "")).strip():
                errors.append(f"item {it.id} needs a non-empty headline and body")
    return errors


def draft_from_json(obj: dict, selected: dict[str, list[Item]]) -> Draft:
    sections = {}
    for key, items in selected.items():
        by_id = {e["id"]: e for e in obj["sections"].get(key, []) if isinstance(e, dict)}
        sections[key] = [Entry(it, str(by_id[it.id]["headline"]).strip(),
                               str(by_id[it.id]["body"]).strip())
                         for it in items if it.id in by_id]
    return Draft(title=obj["title"].strip(), opener=obj["opener"].strip(),
                 lookahead=obj["lookahead"].strip(), sections=sections)


def sanitize(draft: Draft, issue_date: dt.date) -> Draft:
    """Deterministic last line of defence: the banned phrase never ships."""
    draft.title = strip_banned(draft.title) or f"ClawBytes for {_long_date(issue_date)}"
    draft.opener = strip_banned(draft.opener)
    draft.lookahead = strip_banned(draft.lookahead)
    for entries in draft.sections.values():
        for e in entries:
            e.headline = strip_banned(e.headline) or "Untitled"
            e.body = strip_banned(e.body)
    return draft


def write_with_claude(selected: dict[str, list[Item]], payload: dict, model: str,
                      api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                      post=requests.post) -> Draft:
    """Ask Claude for the copy; one corrective retry, then fail or strip."""
    messages = [{"role": "user", "content": build_user_prompt(payload)}]
    total = Counter()
    for attempt in (1, 2):
        text, usage = call_claude(SYSTEM_PROMPT, messages, model, api_key, max_tokens, post)
        total.update({k: v for k, v in usage.items() if isinstance(v, int)})
        problems, obj = [], None
        try:
            obj = parse_model_json(text)
        except ValueError as exc:
            problems.append(f"output was not valid JSON ({exc})")
        if obj is not None:
            problems += validate(obj, selected)
        banned = contains_banned(text)
        if not problems and not banned:
            break
        if attempt == 2:
            if problems:
                raise DraftError("draft invalid after retry: " + "; ".join(problems))
            log(f'retry still used "{BANNED_PHRASE}"; stripping it')
            break
        if banned:
            problems.append(f'it uses the banned phrase "{BANNED_PHRASE}"; remove it everywhere')
        log("draft rejected, retrying once: " + "; ".join(problems))
        messages += [
            {"role": "assistant", "content": text or "{}"},
            {"role": "user", "content": "That draft has problems:\n- " + "\n- ".join(problems)
             + "\nReturn the full corrected JSON object only, following every rule."},
        ]
    cost = estimate_cost(model, total)
    log(f"claude total input_tokens={total.get('input_tokens', 0)} "
        f"output_tokens={total.get('output_tokens', 0)} "
        f"est_cost={'$%.4f' % cost if cost is not None else 'unknown'}")
    return draft_from_json(obj, selected)


# --------------------------------------------------------------------------
# Deterministic template (dry run)
# --------------------------------------------------------------------------

def _long_date(d: dt.date) -> str:
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def _short_date(d: dt.date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def next_issue_date(d: dt.date) -> dt.date:
    for i in range(1, 8):
        nd = d + dt.timedelta(days=i)
        if nd.weekday() in (1, 4):
            return nd
    return d + dt.timedelta(days=7)


def _source_label(key: str) -> str:
    if key.startswith("github.com/"):
        return key.split("/", 1)[1]
    return key


def _first_sentences(text: str, n: int = 2, limit: int = 280) -> str:
    if "•" in text:  # release notes arrive as a bullet list
        text = "; ".join(p.strip(" .") for p in text.split("•") if p.strip()) + "."
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:n]).strip()
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def template_draft(selected: dict[str, list[Item]], issue_date: dt.date,
                   start: dt.datetime, end: dt.datetime) -> Draft:
    counts = {k: len(v) for k, v in selected.items()}
    top = Counter(it.source for v in selected.values() for it in v).most_common(3)
    last = (end - dt.timedelta(days=1)).date()
    opener = (f"This issue covers {_short_date(start.date())} to {_short_date(last)}: "
              f"{counts['agents']} agent and tool updates, {counts['models']} models "
              f"and {counts['repos']} repos from the ClawBytes registry.")
    if top:
        opener += " Sources include " + ", ".join(_source_label(k) for k, _ in top) + "."
    sections = {
        key: [Entry(it, it.title, _first_sentences(it.summary) or "No summary in the registry yet.")
              for it in items]
        for key, items in selected.items()
    }
    return Draft(
        title=f"Agents, models and repos for {_short_date(issue_date)}",
        opener=opener,
        lookahead=f"Next issue: {_long_date(next_issue_date(issue_date))}.",
        sections=sections,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _md_text(s: str) -> str:
    return re.sub(r"([\\*\[\]<>])", r"\\\1", s.replace("\n", " ").strip())


def _md_url(u: str) -> str:
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def safe_url(entry_or_url, site_url: str = "") -> str:
    """An http(s) link for the item, else `site_url` (which may be "": render no link)."""
    if isinstance(entry_or_url, Entry):
        for u in (entry_or_url.item.url, entry_or_url.item.link):
            if u and _is_http(u):
                return u.strip()
        return site_url
    return entry_or_url.strip() if _is_http(entry_or_url or "") else site_url


def _ordered_sections(draft: Draft) -> list[tuple[str, str, list[Entry]]]:
    out = [(k, label, draft.sections.get(k, [])) for k, label in SECTIONS]
    if draft.sections.get(ALSO[0]):
        out.append((ALSO[0], ALSO[1], draft.sections[ALSO[0]]))
    return out


def _dateline(issue_date: dt.date, start: dt.datetime, end: dt.datetime) -> str:
    last = (end - dt.timedelta(days=1)).date()
    return (f"ClawBytes · {_long_date(issue_date)} · "
            f"Covers {_short_date(start.date())} to {_short_date(last)}")


EMPTY_SECTION = "Nothing new in this window."


def render_markdown(draft: Draft, issue_date: dt.date, start: dt.datetime,
                    end: dt.datetime, site_url: str = "") -> str:
    lines = [f"# ClawBytes: {_md_text(draft.title)}", "",
             f"*{_dateline(issue_date, start, end)}*", "",
             _md_text(draft.opener), ""]
    for _, label, entries in _ordered_sections(draft):
        lines += [f"## {label}", ""]
        if not entries:
            lines += [f"_{EMPTY_SECTION}_", ""]
        for e in entries:
            url = safe_url(e, site_url)
            lines += [f"**{_md_text(e.headline)}**", _md_text(e.body)]
            lines += [f"[Read more →]({_md_url(url)})", ""] if url else [""]
    lines += ["---", "", f"**Look ahead:** {_md_text(draft.lookahead)}", ""]
    return "\n".join(lines)


def _h(s: str) -> str:
    return html.escape(s or "", quote=True)


def render_html(draft: Draft, issue_date: dt.date, start: dt.datetime,
                end: dt.datetime, site_url: str = "", feed_url: str = "") -> str:
    """`site_url`/`feed_url` feed the closing call to action; without a site it is left out."""
    rows = [f'''<tr><td style="padding:26px 36px 0 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="background:#F7F4EE;border-left:4px solid {RED};padding:18px 20px;font-family:{SERIF};font-size:19px;line-height:28px;color:{NAVY};">{_h(draft.opener)}</td>
</tr></table></td></tr>''']
    for _, label, entries in _ordered_sections(draft):
        rows.append(f'''<tr><td style="padding:30px 36px 6px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="font-family:{FONT};font-size:12px;letter-spacing:2px;text-transform:uppercase;font-weight:700;color:{RED};padding-bottom:8px;border-bottom:2px solid {NAVY};">{_h(label)}</td>
</tr></table></td></tr>''')
        if not entries:
            rows.append(f'<tr><td style="padding:14px 36px 4px 36px;font-family:{FONT};font-size:15px;line-height:24px;color:{MUTED};font-style:italic;">{_h(EMPTY_SECTION)}</td></tr>')
        for e in entries:
            url = safe_url(e, site_url)
            more = (f'<div style="font-size:14px;line-height:20px;font-weight:600;"><a href="{_h(url)}" style="color:{RED};text-decoration:underline;">Read more &rarr;</a></div>\n'
                    if url else "")
            rows.append(f'''<tr><td style="padding:16px 36px 4px 36px;font-family:{FONT};">
<div style="font-size:17px;line-height:24px;font-weight:700;color:{NAVY};margin:0 0 6px 0;">{_h(e.headline)}</div>
<div style="font-size:15px;line-height:24px;color:{INK};margin:0 0 6px 0;">{_h(e.body)}</div>
{more}</td></tr>''')
    rows.append(f'''<tr><td style="padding:30px 36px 6px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="font-family:{FONT};font-size:12px;letter-spacing:2px;text-transform:uppercase;font-weight:700;color:{RED};padding-bottom:8px;border-bottom:2px solid {NAVY};">Look ahead</td>
</tr></table></td></tr>
<tr><td style="padding:12px 36px 4px 36px;font-family:{SERIF};font-size:18px;line-height:28px;color:{NAVY};font-style:italic;">{_h(draft.lookahead)}</td></tr>''')
    cta = ""
    if _is_http(site_url):
        rss = (f'<div style="font-size:12px;line-height:18px;color:#C9D1DC;margin-top:12px;">or subscribe via <a href="{_h(feed_url)}" style="color:#FFFFFF;text-decoration:underline;">RSS</a></div>\n'
               if _is_http(feed_url) else "")
        cta = f'''<tr><td style="padding:34px 36px 0 36px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td align="center" style="background:{NAVY};border-radius:6px;padding:22px 20px;font-family:{FONT};">
<div style="font-size:16px;line-height:24px;font-weight:700;color:#FFFFFF;margin-bottom:14px;">Follow the live feed</div>
<a href="{_h(site_url)}" style="display:inline-block;background:{RED};color:#FFFFFF;text-decoration:none;font-weight:700;font-size:14px;padding:11px 22px;border-radius:4px;">Open the ClawBytes feed</a>
{rss}</td></tr></table></td></tr>
'''
    preheader = _first_sentences(draft.opener, 1, 160)
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>ClawBytes: {_h(draft.title)}</title></head>
<body style="margin:0;padding:0;background:{BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_h(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG};"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:640px;background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E4E0D8;">
<tr><td style="background:{NAVY};padding:30px 36px 26px 36px;font-family:{FONT};">
<div style="font-size:12px;letter-spacing:3px;text-transform:uppercase;font-weight:700;color:{RED};">ClawBytes</div>
<div style="font-family:{SERIF};font-size:30px;line-height:37px;color:#FFFFFF;margin-top:10px;">{_h(draft.title)}</div>
<div style="font-size:13px;line-height:20px;color:#AEB8C6;margin-top:10px;">{_h(_dateline(issue_date, start, end))}</div>
</td></tr>
<tr><td style="height:4px;background:{RED};font-size:0;line-height:0;">&nbsp;</td></tr>
{chr(10).join(rows)}
{cta}<tr><td style="padding:22px 36px 30px 36px;font-family:{FONT};font-size:12px;line-height:19px;color:{MUTED};text-align:center;">ClawBytes is out every Tuesday and Friday.</td></tr>
</table>
</td></tr></table></body></html>
'''


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ops.digest.draft",
                                description="Draft the Tue/Fri ClawBytes issue from the ai-wire feed.")
    p.add_argument("--date", type=_date, help="issue date YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--days", type=int, help="days before the issue date to cover "
                   "(default: 4 on Tuesday, 3 on Friday, else 4)")
    p.add_argument("--since", type=_date, help="cover from this date (YYYY-MM-DD); overrides --days")
    feed = p.add_mutually_exclusive_group()
    feed.add_argument("--feed-url", help=f"registry feed URL (default: ${AI_WIRE_BASE_ENV}/feed.xml)")
    feed.add_argument("--feed-file", help="read the feed from a local file instead of fetching it")
    p.add_argument("--model", default=os.environ.get("DIGEST_MODEL") or DEFAULT_MODEL,
                   help="Claude model (default: $DIGEST_MODEL or %(default)s)")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--md-out", help="write markdown here (default: stdout)")
    p.add_argument("--html-out", help="write email-safe HTML here")
    p.add_argument("--dry-run", action="store_true",
                   help="no Claude call: print the prompt and render a template draft")
    for key in ("agents", "models", "repos", "also"):
        p.add_argument(f"--cap-{key}", type=int, default=DEFAULT_CAPS[key],
                       help=f"max items in {key} (default %(default)s)")
    return p


def main(argv: list[str] | None = None, get=requests.get, post=requests.post) -> int:
    args = build_parser().parse_args(argv)
    issue_date = args.date or dt.datetime.now(dt.timezone.utc).date()
    try:
        start, end = issue_window(issue_date, args.days, args.since)
    except ValueError as exc:
        log(str(exc))
        return 2

    base = (os.environ.get(AI_WIRE_BASE_ENV) or "").strip().rstrip("/")
    feed_url = args.feed_url or (f"{base}/feed.xml" if base else "")
    if not args.feed_file and not feed_url:
        log(f"{AI_WIRE_BASE_ENV} is not set: set it to the registry base URL, "
            "or pass --feed-url URL or --feed-file PATH")
        return 2

    try:
        data = load_feed(args.feed_file or feed_url, get=get)
        site_url = base or feed_home(data)
        items = parse_feed(data, site_url)
    except (requests.RequestException, OSError) as exc:
        log(f"feed fetch failed: {exc}")
        return 1
    if items and min(it.published for it in items) > start:
        log(f"warning: oldest feed item is {min(it.published for it in items):%Y-%m-%d %H:%M}Z, "
            f"after the window start {start:%Y-%m-%d}; earlier items may be missing")
    windowed = in_window(items, start, end)
    caps = {k: getattr(args, f"cap_{k}") for k in DEFAULT_CAPS}
    selected = select(windowed, caps)
    log(f"issue {issue_date} window {start:%Y-%m-%d}..{end - dt.timedelta(days=1):%Y-%m-%d}: "
        f"{len(windowed)}/{len(items)} feed items in window; selected "
        + ", ".join(f"{k}={len(v)}" for k, v in selected.items()))
    if not any(selected.values()):
        log("no items in the window; nothing to draft")
        return 1

    payload = build_payload(selected, issue_date, start, end)
    if args.dry_run:
        print("===== SYSTEM PROMPT =====")
        print(SYSTEM_PROMPT)
        print("===== USER PROMPT =====")
        print(build_user_prompt(payload))
        print(f"===== (dry run: no call to {args.model}) =====")
        draft = template_draft(selected, issue_date, start, end)
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            log("ANTHROPIC_API_KEY is not set (use --dry-run to skip the Claude call)")
            return 2
        try:
            draft = write_with_claude(selected, payload, args.model, api_key,
                                      args.max_tokens, post=post)
        except (ClaudeError, DraftError) as exc:
            log(f"draft failed: {exc}")
            return 2

    draft = sanitize(draft, issue_date)
    md = render_markdown(draft, issue_date, start, end, site_url)
    page = render_html(draft, issue_date, start, end, site_url, feed_url)
    if contains_banned(md) or contains_banned(page):
        log(f'internal error: "{BANNED_PHRASE}" survived sanitizing')
        return 2

    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
        log(f"wrote {args.md_out}")
    else:
        print(md)
    if args.html_out:
        Path(args.html_out).write_text(page, encoding="utf-8")
        log(f"wrote {args.html_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
