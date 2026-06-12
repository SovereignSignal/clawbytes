#!/usr/bin/env python3
"""
ClawBytes Page Watcher
Covers vendors that publish real signal but expose no feed:

- Mintlify-style docs serve raw markdown at <page>.md — hash-watch it and
  emit one item per change (Claude platform release notes, Devin CLI
  changelog, xAI API release notes).
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


def fetch_text(url, timeout=30):
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def first_heading(text, pattern):
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else ""


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

    for watch in MD_WATCHES:
        try:
            text = fetch_text(watch["md"])
        except Exception as e:
            if verbose:
                print(f"  ! {watch['label']}: fetch failed ({e})")
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        old_digest = state["hashes"].get(watch["key"])
        heading = first_heading(text, watch["heading"])
        if old_digest and digest != old_digest:
            new_items.append({
                "id": f"pagewatch:{watch['key']}:{digest[:12]}",
                "watch": watch["label"],
                "title": f"{watch['label']} — {heading}" if heading else f"{watch['label']} updated",
                "url": watch["page"],
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

    for watch in SITEMAP_WATCHES:
        try:
            xml_text = fetch_text(watch["url"])
        except Exception as e:
            if verbose:
                print(f"  ! {watch['label']} sitemap: fetch failed ({e})")
            continue
        slugs = sitemap_slugs(xml_text, watch["prefixes"])
        if not slugs:
            if verbose:
                print(f"  ! {watch['label']} sitemap: no matching URLs parsed")
            continue
        old = state["slugs"].get(watch["key"], [])
        fresh = [u for u in slugs if u not in set(old)] if old else []
        for url in fresh[:MAX_SITEMAP_ITEMS_PER_RUN]:
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
        state["slugs"][watch["key"]] = slugs

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
