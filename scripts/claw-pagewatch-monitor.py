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
- Sites with sitemaps but no RSS (anthropic.com, api-docs.deepseek.com) —
  diff the sitemap slug set and emit one item per new news/engineering URL.

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
        "heading": r"<h3[^>]*>([^<]+)</h3>",
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
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "url": "https://api-docs.deepseek.com/sitemap.xml",
        "prefixes": ("https://api-docs.deepseek.com/news/",),
    },
]

FAILURES = []


def _fail(message):
    """A failed fetch or parse goes to stderr even under --quiet; main() exits 1.

    No change is this monitor's resting state, so the exit code is the only
    thing that tells a broken watch from a quiet day.
    """
    FAILURES.append(message)
    print(f"  ! {message}", file=sys.stderr)


def watch_fetch_url(watch):
    return watch.get("md") or watch.get("html")


def watch_fingerprint(text, watch):
    """Stable content for hashing. Heading-only for HTML SPAs."""
    if watch.get("fingerprint") == "headings":
        pattern = watch.get("heading") or ""
        found = re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)
        cleaned = [re.sub(r"<[^>]+>", "", h).strip() for h in found]
        return "\n".join(c for c in cleaned if c)
    return text


def fetch_text(url, timeout=30):
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def first_heading(text, pattern):
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


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
    flags = re.MULTILINE | re.IGNORECASE
    html = _is_html_watch(watch)
    if html:
        flags |= re.DOTALL
    matches = list(re.finditer(pattern, text, flags))
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
    return "read" if "/engineering/" in url else "ship"


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
            new_items.append({
                "id": f"pagewatch:{watch['key']}:{digest[:12]}",
                "watch": watch["label"],
                "title": f"{watch['label']} — {heading}" if heading else f"{watch['label']} updated",
                # Unique fragment per new top entry — publish dedup is
                # URL-keyed, so the bare page URL would let only the first
                # change post (see CLAUDE.md invariant 4). The fragment is
                # inert in a browser; the page still loads. It changes only
                # when the newest entry changes.
                "url": f"{watch['page']}#updated-{digest[:8]}",
                "summary": "Changelog page updated",
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
        fresh = [u for u in slugs if u not in set(old)] if old else []
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
            for url in emitted:
                if url not in known:
                    merged.append(url)
                    known.add(url)
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
