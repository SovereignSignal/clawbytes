#!/usr/bin/env python3
"""
ClawBytes Page Watcher
Covers vendors that publish real signal but expose no feed:

- Mintlify-style docs serve raw markdown at <page>.md (Claude platform
  release notes, Devin CLI changelog, xAI API release notes). Hash the
  newest entry — its heading plus that entry's body — not the whole file.
- HTML SPAs with no markdown sibling (Google Antigravity, Kiro) hash that
  same newest entry, not the full page (bundle hashes change on deploys).
  If no heading matches, fall back to the previous whole-page or
  heading-set hash for that watch.
- Sites with sitemaps but no RSS (anthropic.com, api-docs.deepseek.com,
  claude.com product articles) — diff the sitemap slug set and emit one
  item per new URL. Claude articles emit only launch slugs; customer
  stories and tutorials are remembered and skipped.
- The Anthropic news index (https://www.anthropic.com/news). Same URLs
  the sitemap already diffs, so item ids match the sitemap watch and
  collect keeps one.
- OpenAI product release notes, Codex entries only. The developers
  changelog RSS does not carry "Composer predictions". The official page
  is Cloudflare-challenged from a datacenter; a free reader fallback is
  used when the page yields no Codex entries. No API key.

First sighting of each watch records a baseline and emits nothing.

State file: memory/claw-pagewatch-state.json
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from title_text import MONTH_NAMES, first_stated_date, flatten_inline_markup, format_stated_date

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-pagewatch-state.json"

MAX_SITEMAP_ITEMS_PER_RUN = 5

MD_WATCHES = [
    {
        "key": "claude-release-notes",
        "label": "Claude platform release notes",
        "md": "https://docs.claude.com/en/release-notes/overview.md",
        "page": "https://docs.claude.com/en/release-notes/overview",
        "heading": r"^###\s+(.+)$",
        "lane": "ship",
    },
    {
        "key": "devin-cli-changelog",
        "label": "Devin CLI",
        "md": "https://docs.devin.ai/cli/changelog/stable.md",
        "page": "https://docs.devin.ai/cli/changelog/stable",
        "heading": r"^##\s+(.+)$",
        "lane": "ship",
    },
    {
        "key": "xai-release-notes",
        "label": "xAI API release notes",
        "md": "https://docs.x.ai/developers/release-notes.md",
        "page": "https://docs.x.ai/developers/release-notes",
        "heading": r"^##\s+(.+)$",
        "lane": "ship",
    },
]

# Feedless HTML SPAs — hash the newest heading's entry, not the full page
# (Astro/Next bundle hashes change on unrelated deploys). First sighting is silent.
HTML_WATCHES = [
    {
        "key": "antigravity-changelog",
        "label": "Google Antigravity",
        "html": "https://antigravity.google/changelog",
        "page": "https://antigravity.google/changelog",
        "heading": r"<h3[^>]*>(.*?)</h3>",
        "fingerprint": "headings",
        "lane": "ship",
    },
    {
        "key": "kiro-changelog",
        "label": "Kiro",
        "html": "https://kiro.dev/changelog",
        "page": "https://kiro.dev/changelog",
        # Root changelog is an HTML SPA (no RSS, no .md sibling — probed 2026-09).
        # Hash the newest h2 plus its body; first sighting is a silent baseline.
        "heading": r"<h2[^>]*>(.*?)</h2>",
        "fingerprint": "headings",
        "lane": "ship",
    },
]


SITEMAP_WATCHES = [
    {
        "key": "anthropic",
        "label": "Anthropic",
        "url": "https://www.anthropic.com/sitemap.xml",
        "prefixes": ("https://www.anthropic.com/news/", "https://www.anthropic.com/engineering/"),
    },
    # Own key so the first run baselines the existing research URLs and emits
    # nothing. Folding /research/ into the anthropic key would treat every
    # URL already on the sitemap as a new page.
    {
        "key": "anthropic-research",
        "label": "Anthropic research",
        "url": "https://www.anthropic.com/sitemap.xml",
        "prefixes": ("https://www.anthropic.com/research/",),
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "url": "https://api-docs.deepseek.com/sitemap.xml",
        "prefixes": ("https://api-docs.deepseek.com/news/",),
    },
    # English articles only. Locale copies live under /de/, /fr/, …
    # and do not match this prefix. Launch slugs emit; the rest are
    # remembered so a customer-story burst cannot fill the cap.
    {
        "key": "claude-articles",
        "label": "Claude",
        "url": "https://claude.com/sitemap.xml",
        "prefixes": ("https://claude.com/resources/articles/",),
    },
]


# HTML index. Ids use the sitemap watch key (`anthropic`) so a URL that
# also shows up in the sitemap is one source key.
NEWS_INDEX_WATCHES = [
    {
        "key": "anthropic-news",
        "label": "Anthropic",
        "page": "https://www.anthropic.com/news",
        "id_key": "anthropic",
    },
]


# Codex rows on the product release notes. The developers changelog RSS
# is a different feed and did not contain "Composer predictions" on
# 2026-10-15. The official page 403s from datacenter IPs (Cloudflare).
# The reader is a no-key fallback used only when the official body has
# no Codex entries.
OPENAI_RELEASE_NOTES_URL = "https://openai.com/products/release-notes/"
OPENAI_RELEASE_NOTES_READER = "https://r.jina.ai/https://openai.com/products/release-notes/"
RELEASE_NOTE_WATCHES = [
    {
        "key": "codex-release-notes",
        "label": "Codex",
        "lane": "ship",
    },
]

# Product launches on claude.com/resources/articles. "how-…" customer
# stories mention managed agents and must not match.
_CLAUDE_LAUNCH_TOKENS = (
    "introducing",
    "announcing",
    "redesigned",
    "dynamic-workflow",
    "whats-new",
    "new-in-",
    "now-available",
    "generally-available",
)
_NEWS_HREF = re.compile(
    r'href="(?:https://www\.anthropic\.com)?(/news/[a-z0-9][a-z0-9-]*)/?[?#"]',
    re.IGNORECASE,
)
_RELEASE_DATE = re.compile(
    r"^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{1,2}),\s+(\d{4})$",
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"\]\((https://[^)\s]+)\)")
_HTML_HREF = re.compile(r'href="(https://[^"]+)"', re.IGNORECASE)

FAILURES = []


def _log_fetch(key, detail):
    """One INFO line for the research sitemap, including under --quiet.

    Collect captures this script's stdout. The line is how we confirm the
    new watch fetched: baseline on first sighting, otherwise the slug count.
    """
    if key == "anthropic-research":
        print(f"INFO {key}: {detail}", flush=True)


def _fail(message):
    """A failed fetch or parse goes to stderr even under --quiet; main() exits 1.

    No change is this monitor's resting state, so the exit code is the only
    thing that tells a broken watch from a quiet day.
    """
    FAILURES.append(message)
    print(f"  ! {message}", file=sys.stderr)


def watch_fetch_url(watch):
    return watch.get("md") or watch.get("html")


def _heading_search(pattern, html):
    """Regex plus flags. HTML headings include text inside inline tags."""
    flags = re.MULTILINE | re.IGNORECASE
    compiled = pattern or ""
    if html:
        flags |= re.DOTALL
        compiled = compiled.replace(r"([^<]+)", r"(.*?)")
    return compiled, flags


def watch_fingerprint(text, watch):
    """Stable content for hashing. Heading-only for HTML SPAs."""
    if watch.get("fingerprint") == "headings":
        pattern = watch.get("heading") or ""
        compiled, flags = _heading_search(pattern, True)
        found = re.findall(compiled, text, flags)
        cleaned = [flatten_inline_markup(h) for h in found]
        return "\n".join(c for c in cleaned if c)
    return text


def fetch_text(url, timeout=30):
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def first_heading(text, pattern):
    if not text or not pattern:
        return ""
    compiled, flags = _heading_search(pattern, "<" in pattern)
    m = re.search(compiled, text, flags)
    if not m:
        return ""
    return flatten_inline_markup(m.group(1))


def _is_html_watch(watch):
    pattern = watch.get("heading") or ""
    return watch.get("fingerprint") == "headings" or "<" in pattern


def _stable_entry_body(raw, html):
    """Prose of one changelog entry, minus markup and deploy-only noise.

    An empty result means the body is not stable enough to mix into the
    digest; callers then hash the heading alone.
    """
    text = raw or ""
    if html:
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\b[a-f0-9]{8,}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def newest_entry_fingerprint(text, watch):
    """Heading plus stable body of the newest entry, or None if no heading.

    The digest is this string. A change below the current top entry does not
    alter it, so the ``#updated-`` fragment stays put until a new top entry
    appears. None tells the caller to fall back to the whole-page hash.
    """
    pattern = watch.get("heading") or ""
    if not pattern or not text:
        return None
    html = _is_html_watch(watch)
    compiled, flags = _heading_search(pattern, html)
    matches = list(re.finditer(compiled, text, flags))
    if not matches:
        return None
    heading = first_heading(text, pattern)
    if not heading:
        return None
    start = matches[0].end()
    end = matches[1].start() if len(matches) > 1 else len(text)
    body = _stable_entry_body(text[start:end], html)
    return heading if not body else f"{heading}\n{body}"


def _norm_identity(value):
    return re.sub(r"\s+", " ", (value or "")).strip().casefold()


def same_top_entry(state, key, identity):
    """True when this page's newest heading was already recorded.

    ``topEntries`` is the additive field. Legacy files only have ``headings``
    (the first heading from the last run); that still counts, so a deploy
    does not repost the release already on the page.
    """
    current = _norm_identity(identity)
    if not current or not isinstance(state, dict):
        return False
    tops = state.get("topEntries")
    if isinstance(tops, dict) and _norm_identity(tops.get(key)):
        return _norm_identity(tops.get(key)) == current
    headings = state.get("headings")
    if isinstance(headings, dict) and _norm_identity(headings.get(key)):
        return _norm_identity(headings.get(key)) == current
    return False


def remember_top_entry(state, key, identity):
    """Record the newest heading. Adds ``topEntries``; does not rewrite other keys."""
    if not identity or not isinstance(state, dict):
        return
    tops = state.get("topEntries")
    if tops is None:
        tops = {}
        state["topEntries"] = tops
    if isinstance(tops, dict):
        tops[key] = identity


def sitemap_slugs(xml_text, prefixes):
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text)
    return sorted(u for u in urls if u.startswith(prefixes))


def slug_title(url):
    """https://site/news/some-thing-here -> 'Some thing here'."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    words = slug.replace("-", " ").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else url


def lane_for_slug(url):
    """Engineering and research are Read. A research slug about cyber or
    security is Watch. News and other prefixes stay Ship.
    """
    if "/research/" in (url or ""):
        low = url.lower()
        if any(token in low for token in ("cyber", "security", "vulnerab", "exploit", "jailbreak")):
            return "watch"
        return "read"
    return "read" if "/engineering/" in (url or "") else "ship"


def claude_article_launch(url):
    """True for a Claude product-launch article slug.

    Customer stories start with ``how-`` or ``working-`` and stay out even
    when the slug mentions managed agents. ``projects-redesigned`` and
    ``introducing-dynamic-workflows-in-claude-code`` match.
    """
    slug = (url or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if not slug or slug.startswith("how-") or slug.startswith("working-"):
        return False
    if any(token in slug for token in _CLAUDE_LAUNCH_TOKENS):
        return True
    return slug.startswith("claude-managed-agent") or slug.startswith("managed-agent")


def news_index_urls(html, page="https://www.anthropic.com/news"):
    """Article URLs on the Anthropic news index. The index URL itself is not one."""
    found = []
    seen = set()
    for match in _NEWS_HREF.finditer(html or ""):
        path = "/" + match.group(1).lstrip("/")
        path = path.rstrip("/")
        if path == "/news":
            continue
        url = "https://www.anthropic.com" + path
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
    return sorted(found)


def _followed_by_date(lines, start):
    for line in lines[start:]:
        text = line.strip()
        if not text:
            continue
        return bool(_RELEASE_DATE.match(text))
    return False


def _product_boundary(lines, index):
    """A release-notes product label is a short line whose next text is a date."""
    text = lines[index].strip()
    if not text or len(text) > 24:
        return False
    if not re.fullmatch(r"[A-Z][A-Za-z0-9+.-]*", text):
        return False
    return _followed_by_date(lines, index + 1)


def _expand_release_date(line):
    """``Oct 9, 2026`` → ``October 9, 2026``, the form the stale-date gate reads."""
    match = _RELEASE_DATE.match((line or "").strip())
    if not match:
        return ""
    token = match.group(1).lower()
    for name in MONTH_NAMES:
        if name.lower().startswith(token[:3]):
            return f"{name} {int(match.group(2))}, {match.group(3)}"
    return ""


def _entry_from_release_chunk(chunk):
    block = "\n".join(chunk)
    title_match = re.search(r"^##\s+(.+?)\s*$", block, re.MULTILINE)
    if not title_match:
        title_match = re.search(r"<h2[^>]*>\s*(.*?)\s*</h2>", block, re.IGNORECASE | re.DOTALL)
    if not title_match:
        return None
    title = flatten_inline_markup(title_match.group(1))
    if not title:
        return None
    links = _MD_LINK.findall(block) + _HTML_HREF.findall(block)
    url = ""
    for link in links:
        if "#" in link:
            url = link
            break
    if not url and links:
        url = links[0]
    if not url:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        url = f"{OPENAI_RELEASE_NOTES_URL}#{slug}" if slug else OPENAI_RELEASE_NOTES_URL
    after = block.split(title_match.group(0), 1)[-1]
    summary = ""
    for para in re.split(r"\n\s*\n", after):
        text = flatten_inline_markup(para)
        if not text or text.startswith("http") or text.lower().startswith("view source"):
            continue
        summary = text
        break
    stated = ""
    for line in chunk:
        stated = _expand_release_date(line)
        if stated:
            break
    if stated:
        summary = f"{stated} — {summary or title}"
    return {"title": title, "url": url, "summary": summary or title}


def codex_release_note_entries(text):
    """Codex-labeled entries from the product release notes.

    Accepts the official HTML when it has the same ``##`` headings, and the
    reader markdown (a product name on its own line, then a date, then the
    heading). API and ChatGPT sections are not entries. The fragment on the
    help-center link is the item URL so two notes do not share a key.
    """
    body = text or ""
    marker = "Markdown Content:"
    if marker in body:
        body = body.split(marker, 1)[1]
    lines = body.splitlines()
    entries = []
    index = 0
    while index < len(lines):
        if lines[index].strip() == "Codex" and _product_boundary(lines, index):
            index += 1
            chunk = []
            while index < len(lines) and not _product_boundary(lines, index):
                chunk.append(lines[index])
                index += 1
            entry = _entry_from_release_chunk(chunk)
            if entry:
                entries.append(entry)
            continue
        index += 1
    return entries


def fetch_codex_release_notes():
    """Official page first. Reader only when that body has no Codex entries.

    The reader (r.jina.ai) needs no key and is not called when the official
    page already parsed. A 403 from Cloudflare is that failure.
    """
    errors = []
    for url in (OPENAI_RELEASE_NOTES_URL, OPENAI_RELEASE_NOTES_READER):
        try:
            text = fetch_text(url)
        except Exception as exc:
            errors.append(f"{url} ({exc})")
            continue
        entries = codex_release_note_entries(text)
        if entries:
            return entries
        errors.append(f"{url} (no Codex entries)")
    raise OSError("OpenAI Codex release notes: " + "; ".join(errors))


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"hashes": {}, "headings": {}, "slugs": {}, "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def summary_with_entry_date(summary, heading, blob):
    """Attach the entry's own calendar date when the heading does not.

    Devin's stable changelog has no markdown heading; the date sits on the
    ``<Update description="September 22, 2026">`` tag. Without it, a page
    change looks new even when the top entry is weeks old.
    """
    if first_stated_date(heading or ""):
        return summary
    found = first_stated_date(blob or "")
    if not found:
        return summary
    return f"{summary} — {format_stated_date(found)}"


def check_pages(verbose=True):
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []

    for watch in MD_WATCHES + HTML_WATCHES:
        url = watch_fetch_url(watch)
        try:
            text = fetch_text(url)
        except Exception as e:
            _fail(f"{watch['label']}: fetch failed ({e})")
            continue
        heading = first_heading(text, watch["heading"])
        entry = newest_entry_fingerprint(text, watch) if heading else None
        if entry:
            # Newest entry only. Lower-page edits must not mint a new fragment.
            computed = hashlib.sha256(entry.encode()).hexdigest()
            identity = heading
        else:
            # No top entry to anchor on — previous whole-page / heading-set hash.
            computed = hashlib.sha256(watch_fingerprint(text, watch).encode()).hexdigest()
            identity = ""
        old_digest = state["hashes"].get(watch["key"])
        # Same heading: keep the stored digest so a body tweak or a legacy
        # whole-page hash cannot post the release again.
        if old_digest and same_top_entry(state, watch["key"], identity):
            digest = old_digest
        else:
            digest = computed
        if old_digest and digest != old_digest:
            title = f"{watch['label']} — {heading}" if heading else f"{watch['label']} updated"
            date_blob = entry or ("" if heading else text[:4000])
            new_items.append({
                "id": f"pagewatch:{watch['key']}:{digest[:12]}",
                "watch": watch["label"],
                "title": title,
                # Unique fragment per new top entry — publish dedup is
                # URL-keyed, so the bare page URL would let only the first
                # change post (see CLAUDE.md invariant 4). The fragment is
                # inert in a browser; the page still loads. It changes only
                # when the newest entry changes.
                "url": f"{watch['page']}#updated-{digest[:8]}",
                "summary": summary_with_entry_date("Changelog page updated", heading, date_blob),
                "lane": watch["lane"],
                "found_at": now_iso,
            })
            if verbose:
                print(f"  🔄 {watch['label']}: changed — {heading}")
        elif verbose:
            print(f"  = {watch['label']}: {'baseline recorded' if not old_digest else 'unchanged'}")
        state["hashes"][watch["key"]] = digest
        state["headings"][watch["key"]] = heading
        remember_top_entry(state, watch["key"], identity)

    for watch in SITEMAP_WATCHES:
        try:
            xml_text = fetch_text(watch["url"])
        except Exception as e:
            _fail(f"{watch['label']} sitemap: fetch failed ({e})")
            continue
        slugs = sitemap_slugs(xml_text, watch["prefixes"])
        if not slugs:
            _fail(f"{watch['label']} sitemap: no matching URLs parsed")
            continue
        old = state["slugs"].get(watch["key"], [])
        if old:
            _log_fetch(watch["key"], f"{len(slugs)} items")
        else:
            _log_fetch(watch["key"], "baseline written")
        fresh = [u for u in slugs if u not in set(old)] if old else []
        # Launch slugs only. Other new article URLs are remembered below so
        # they do not occupy the cap and hide a launch that sorts later.
        rejected = []
        if old and watch["key"] == "claude-articles":
            rejected = [u for u in fresh if not claude_article_launch(u)]
            fresh = [u for u in fresh if claude_article_launch(u)]
        # The cap is a rate limit. Persist only what we emitted plus the
        # previous baseline; overflow stays unseen for the next run.
        emitted = fresh[:MAX_SITEMAP_ITEMS_PER_RUN] if old else []
        for url in emitted:
            lane = lane_for_slug(url)
            new_items.append({
                "id": f"pagewatch:{watch['key']}:{url}",
                "watch": watch["label"],
                "title": f"{watch['label']}: {slug_title(url)}",
                "url": url,
                "summary": "New page on vendor site",
                "lane": lane,
                "found_at": now_iso,
            })
            if verbose:
                print(f"  🆕 {watch['label']}: {url}")
        if verbose and not fresh:
            print(f"  = {watch['label']} sitemap: {'baseline recorded' if not old else 'no new pages'} ({len(slugs)} tracked)")
        elif verbose and len(fresh) > len(emitted):
            print(f"  ⚠️ {watch['label']} sitemap: {len(fresh) - len(emitted)} new URL(s) held for the next run")
        if not old:
            state["slugs"][watch["key"]] = slugs
        else:
            merged = list(old)
            known = set(merged)
            for url in list(rejected) + list(emitted):
                if url not in known:
                    merged.append(url)
                    known.add(url)
            state["slugs"][watch["key"]] = merged

    for watch in NEWS_INDEX_WATCHES:
        try:
            html = fetch_text(watch["page"])
        except Exception as e:
            _fail(f"{watch['label']} news: fetch failed ({e})")
            continue
        slugs = news_index_urls(html, watch["page"])
        if not slugs:
            _fail(f"{watch['label']} news: no article links parsed")
            continue
        old = state["slugs"].get(watch["key"], [])
        fresh = [u for u in slugs if u not in set(old)] if old else []
        emitted = fresh[:MAX_SITEMAP_ITEMS_PER_RUN] if old else []
        id_key = watch.get("id_key") or watch["key"]
        for url in emitted:
            new_items.append({
                "id": f"pagewatch:{id_key}:{url}",
                "watch": watch["label"],
                "title": f"{watch['label']}: {slug_title(url)}",
                "url": url,
                "summary": "New page on vendor site",
                "lane": "ship",
                "found_at": now_iso,
            })
            if verbose:
                print(f"  🆕 {watch['label']} news: {url}")
        if verbose and not fresh:
            print(f"  = {watch['label']} news: {'baseline recorded' if not old else 'no new pages'} ({len(slugs)} tracked)")
        if not old:
            state["slugs"][watch["key"]] = slugs
        else:
            merged = list(old)
            known = set(merged)
            for url in emitted:
                if url not in known:
                    merged.append(url)
                    known.add(url)
            state["slugs"][watch["key"]] = merged

    for watch in RELEASE_NOTE_WATCHES:
        try:
            entries = fetch_codex_release_notes()
        except Exception as e:
            _fail(f"{watch['label']} release notes: fetch failed ({e})")
            continue
        by_url = []
        seen_urls = set()
        for entry in entries:
            url = entry.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            by_url.append(entry)
        if not by_url:
            _fail(f"{watch['label']} release notes: no Codex entries parsed")
            continue
        slugs = [entry["url"] for entry in by_url]
        old = state["slugs"].get(watch["key"], [])
        known_old = set(old)
        fresh = [entry for entry in by_url if entry["url"] not in known_old] if old else []
        emitted = fresh[:MAX_SITEMAP_ITEMS_PER_RUN] if old else []
        for entry in emitted:
            new_items.append({
                "id": f"pagewatch:{watch['key']}:{entry['url']}",
                "watch": watch["label"],
                "title": entry["title"],
                "url": entry["url"],
                "summary": entry.get("summary") or "Codex release note",
                "lane": watch.get("lane") or "ship",
                "found_at": now_iso,
            })
            if verbose:
                print(f"  🆕 {watch['label']} release notes: {entry['title']}")
        if verbose and not fresh:
            print(f"  = {watch['label']} release notes: {'baseline recorded' if not old else 'no new notes'} ({len(slugs)} tracked)")
        if not old:
            state["slugs"][watch["key"]] = slugs
        else:
            merged = list(old)
            known = set(merged)
            for entry in emitted:
                if entry["url"] not in known:
                    merged.append(entry["url"])
                    known.add(entry["url"])
            state["slugs"][watch["key"]] = merged

    if new_items:
        state["foundItems"] = (state.get("foundItems", []) + new_items)[-200:]
    save_state(state)
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Watch feedless vendor pages")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    items = check_pages(verbose=not args.quiet)
    print(f"Pagewatch: {len(items)} new item(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
