"""Notion Claws page signal extractor.

Reads the Notion Claws page (updated by your Claude instance) and extracts
editorial insights as signal candidates for the ClawBytes backlog.

When a Notion page is updated with new analysis, that's a signal.
When a Notion page shows a project crossing a threshold (stars, new capability),
that's a signal.

This is NOT a product catalog reader — it's an editorial source.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
CREDS = WORKSPACE / "CREDS.md"
MEMORY = WORKSPACE / "memory"

NOTION_PAGE_ID = "337000c0-d590-805d-af74-f27d19215184"
CACHE_FILE = MEMORY / "clawbytes-notion-signals.json"
STATE_FILE = MEMORY / "clawbytes-notion-signal-state.json"


def cred(section: str, key: str) -> str:
    text = CREDS.read_text() if CREDS.exists() else ""
    pattern = rf"## {re.escape(section)}\n(?:.*\n)*?-\s*(?:\*\*)?{re.escape(key)}(?:\*\*)?:\s*([^\n]+)"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


NOTION_KEY = os.environ.get("NOTION_API_KEY", cred("Notion", "API Key"))
HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def notion_api(path: str, method="GET", data=None) -> dict:
    url = f"https://api.notion.com/v1/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read())


def get_page_blocks(page_id: str) -> list[dict]:
    """Fetch child blocks of a page."""
    try:
        data = notion_api(f"blocks/{page_id}/children?page_size=100")
        return data.get("results", [])
    except Exception:
        return []


def get_page_title(page_id: str) -> str:
    """Get page title from Notion."""
    try:
        data = notion_api(f"pages/{page_id}")
        for k, v in data.get("properties", {}).items():
            if v.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in v.get("title", []))
    except Exception:
        pass
    return ""


def get_text(block: dict) -> str:
    """Extract plain text from a Notion block."""
    btype = block.get("type", "")
    content = block.get(btype, {})
    rich = content.get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def get_last_edited(block_or_page: dict) -> datetime | None:
    """Parse last_edited_time from Notion response."""
    ts = block_or_page.get("last_edited_time", "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_insight_sections(blocks: list[dict]) -> dict:
    """Extract structured insight from a page's text blocks.

    We look for specific editorial sections that your Claude instance writes:
    - 'Why It Matters' / 'Why Explore It' / 'Why This Matters'
    - 'Best For'
    - 'Strengths'
    - 'What It Is' (first paragraph after heading)
    - 'Architecture Details'
    - 'How It Works'

    Returns a dict with keys like 'why_it_matters', 'best_for', 'strengths', 'what_it_is'.
    """
    insights = {}
    current_section = None

    for block in blocks:
        btype = block.get("type", "")
        text = get_text(block)
        text_lower = text.lower()

        # Detect section headers
        if btype.startswith("heading"):
            if any(k in text_lower for k in ["why it matters", "why explore", "why this matters"]):
                current_section = "why_it_matters"
            elif "best for" in text_lower:
                current_section = "best_for"
            elif any(k in text_lower for k in ["strengths", "strength", "advantages"]):
                current_section = "strengths"
            elif any(k in text_lower for k in ["what it is", "overview", "description"]):
                current_section = "what_it_is"
            elif any(k in text_lower for k in ["architecture", "how it works", "design"]):
                current_section = "architecture"
            elif any(k in text_lower for k in ["recent changes", "what changed", "updates", "latest"]):
                current_section = "recent_changes"
            else:
                current_section = None
            continue

        if btype in ("paragraph", "bulleted_list_item", "numbered_list_item", "callout", "toggle"):
            if current_section and text:
                if current_section not in insights:
                    insights[current_section] = []
                insights[current_section].append(text)

    # Join paragraphs in each section
    for key in insights:
        insights[key] = " ".join(insights[key])[:500]  # Cap length

    return insights


def extract_repo_from_blocks(blocks: list[dict]) -> str | None:
    """Try to find a GitHub repo URL in the page content."""
    for block in blocks:
        text = get_text(block)
        # Match github.com/owner/repo pattern
        m = re.search(r"github\.com/([^/\s]+)/([^/\s]+)", text)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def extract_version_from_blocks(blocks: list[dict]) -> str | None:
    """Try to find a version number in the page content."""
    for block in blocks:
        text = get_text(block)
        # Look for version patterns like v1.2.3, 1.2.3, etc.
        m = re.search(r"(?:Version|version|v|V):?\s*(\d+\.\d+(?:\.\d+)?)", text)
        if m:
            return m.group(1)
        # Also try date-based versions
        m = re.search(r"\b(20\d{2}\.\d{1,2}\.\d{1,2})\b", text)
        if m:
            return m.group(1)
    return None


def load_cached_signals() -> list[dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return []


def save_cached_signals(signals: list[dict]):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(signals, indent=2))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "lastCheck": None,
        "seenPageEdits": {},  # page_id -> last_edited_time
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def score_notion_signal(insights: dict, page_title: str, last_edit: datetime) -> float:
    """Score a Notion page signal based on richness and recency."""
    base = 60  # Notion editorial content is inherently higher quality

    # Boost for rich insight sections
    if insights.get("why_it_matters"):
        base += 20
    if insights.get("best_for"):
        base += 10
    if insights.get("strengths"):
        base += 15
    if insights.get("what_it_is"):
        base += 10
    if insights.get("recent_changes"):
        base += 25  # This is the highest-value signal

    # Recency boost (fresher = higher)
    hours_old = (datetime.now(timezone.utc) - last_edit).total_seconds() / 3600
    recency = max(0, 48 - hours_old)
    base += recency

    # Cap
    return min(base, 140)


def build_summary_from_insights(insights: dict, page_title: str, repo: str | None) -> str:
    """Build a 1-sentence editorial summary from Notion insights.

    Priority:
    1. recent_changes (what changed and why)
    2. why_it_matters (strategic significance)
    3. best_for (use case positioning)
    4. what_it_is (identity/context)
    """
    # Use the most specific insight available
    if insights.get("recent_changes"):
        text = insights["recent_changes"][:200]
        return f"Notion update: {text}"

    if insights.get("why_it_matters"):
        text = insights["why_it_matters"][:200]
        return f"Why it matters: {text}"

    if insights.get("best_for"):
        text = insights["best_for"][:200]
        return f"Best for: {text}"

    if insights.get("what_it_is"):
        text = insights["what_it_is"][:200]
        return f"Context: {text}"

    return f"Notion page updated with new analysis"


def find_notion_signals(hours_back: int = 48) -> list[dict]:
    """Find Notion pages that were edited recently and extract signals.

    Returns signal candidates ready for the ClawBytes backlog.
    NOTE: No local dedup — ClawBytes backlog handles dedup via seen_source_keys.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_back)

    # Get the root page children
    try:
        root_data = notion_api(f"blocks/{NOTION_PAGE_ID}/children?page_size=100")
    except Exception as e:
        print(f"Error fetching Notion root: {e}")
        return []

    signals = []

    for block in root_data.get("results", []):
        # Only process child pages (sub-pages under Claws)
        if block.get("type") != "child_page":
            continue

        page_id = block["id"]
        page_title = block.get("child_page", {}).get("title", "")
        last_edit = get_last_edited(block)

        if not last_edit:
            continue

        # Skip if too old
        if last_edit < cutoff:
            continue

        # Fetch the page content
        blocks = get_page_blocks(page_id)
        insights = extract_insight_sections(blocks)
        repo = extract_repo_from_blocks(blocks)
        version = extract_version_from_blocks(blocks)

        # Skip if no meaningful insight content
        if not insights:
            continue

        # Build the signal
        score = score_notion_signal(insights, page_title, last_edit)
        summary = build_summary_from_insights(insights, page_title, repo)

        # URL: link to GitHub releases if we have a repo, otherwise Notion page
        if repo:
            if version:
                url = f"https://github.com/{repo}/releases"
            else:
                url = f"https://github.com/{repo}"
        else:
            url = f"https://www.notion.so/{page_id}"

        signal = {
            "notion_page_id": page_id,
            "notion_page_title": page_title,
            "notion_url": f"https://www.notion.so/{page_id}",
            "repo": repo,
            "version": version,
            "insights": insights,
            "score": round(score, 2),
            "summary": summary,
            "last_edit": last_edit.isoformat(),
            "sourceType": "notion",
            "sourceName": page_title,
            "sourceId": page_id,
            "url": url,
            "title": f"{page_title} — Notion update",
        }

        signals.append(signal)

    return signals


def to_backlog_candidates(signals: list[dict]) -> list[dict]:
    """Convert Notion signals to ClawBytes backlog candidates."""
    from datetime import datetime, timedelta, timezone

    candidates = []
    for sig in signals:
        # Determine category based on content
        insights = sig.get("insights", {})
        primary = "ship"
        categories = ["ship"]

        # If there are "recent changes" insights, treat as ship signal
        # If there are architecture/security insights, could be watch
        if any(k in insights for k in ["security", "vulnerability", "risk"]):
            primary = "watch"
            categories = ["watch", "ship"]

        score = sig["score"]
        last_edit = datetime.fromisoformat(sig["last_edit"])

        # Boost recent edits
        hours_old = (datetime.now(timezone.utc) - last_edit).total_seconds() / 3600
        if hours_old < 12:
            score += 20

        candidate = {
            "primaryCategory": primary,
            "categories": categories,
            "score": round(score, 2),
            "summary": sig["summary"][:200],
            "expiresAt": (last_edit + timedelta(hours=72)).isoformat(),
            "publishedAt": last_edit.isoformat(),
            "sourceType": "notion",
            "sourceName": sig["notion_page_title"],
            "sourceId": sig["notion_page_id"],
            "url": sig["url"],
            "title": sig["title"],
            "notion_page_id": sig["notion_page_id"],
            "notion_insights": {k: v[:300] for k, v in insights.items()},
        }
        candidates.append(candidate)

    return candidates


def enrich_ship_with_notion(ship_items: list[dict]) -> list[dict]:
    """Given a list of ship backlog items, enrich them with Notion insights.

    Uses the cached Notion signals file (written during collect) to avoid
    repeated API calls. Fast path: no network requests.
    """
    if not CACHE_FILE.exists():
        return ship_items
    
    try:
        cached_signals = json.loads(CACHE_FILE.read_text())
    except Exception:
        return ship_items

    # Build repo -> insights map from cached signals
    notion_map = {}
    for sig in cached_signals:
        repo = sig.get("repo", "")
        insights = sig.get("notion_insights", {})
        if repo and insights:
            notion_map[repo.lower()] = {
                "title": sig.get("notion_page_title", ""),
                "insights": insights,
                "page_id": sig.get("notion_page_id", ""),
            }

    enriched = []
    for item in ship_items:
        url = item.get("url", "")
        repo = None
        if "github.com/" in url:
            parts = url.split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                repo = f"{parts[0]}/{parts[1].split('#')[0].split('?')[0]}"

        if repo and repo.lower() in notion_map:
            page = notion_map[repo.lower()]
            insights = page["insights"]
            
            enriched_summary = ""
            if insights.get("recent_changes"):
                enriched_summary = insights["recent_changes"][:200]
            elif insights.get("why_it_matters"):
                enriched_summary = insights["why_it_matters"][:200]
            elif insights.get("what_it_is"):
                enriched_summary = insights["what_it_is"][:200]

            if enriched_summary:
                item = dict(item)
                item["summary"] = enriched_summary
                item["notion_enriched"] = True
                item["notion_page_title"] = page["title"]
                item["notion_page_id"] = page["page_id"]
                item["score"] = min(item.get("score", 0) + 15, 150)

        enriched.append(item)

    return enriched


def main():
    """CLI entry point for testing."""
    signals = find_notion_signals(hours_back=48)
    candidates = to_backlog_candidates(signals)
    print(f"Found {len(candidates)} Notion signal candidates")
    for c in candidates:
        print(f"\n  {c['title']} (score: {c['score']})")
        print(f"    URL: {c['url']}")
        print(f"    Summary: {c['summary'][:120]}...")
        if c.get("notion_insights"):
            for k, v in list(c["notion_insights"].items())[:2]:
                print(f"    Insight [{k}]: {v[:100]}...")


if __name__ == "__main__":
    raise SystemExit(main())
