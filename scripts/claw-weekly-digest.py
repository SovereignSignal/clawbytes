#!/usr/bin/env python3
"""
ClawBytes Weekly Digest Generator
Uses the same Tenspire LLM pipeline as daily threads for consistent quality.
Reads from the threads backlog (not separate state files) to avoid duplication.

State file: memory/clawbytes-thread-state.json + memory/clawbytes-backlog.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Workspace path
WORKSPACE = Path(__file__).parent.parent
MEMORY_DIR = WORKSPACE / "memory"
BACKLOG_FILE = MEMORY_DIR / "clawbytes-backlog.json"
STATE_FILE = MEMORY_DIR / "clawbytes-thread-state.json"

# LLM config (same as daily threads)
LLM_URL = os.environ.get("CLAWBYTES_LLM_URL", "https://api.tenspire.ai/v1")
LLM_MODEL = os.environ.get("CLAWBYTES_LLM_MODEL", "gemma4:31b-cloud")
LLM_API_KEY = os.environ.get("CLAWBYTES_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

# Telegram config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# Category metadata
CATEGORY_META = {
    "ship": {"emoji": "📦", "label": "Ship"},
    "watch": {"emoji": "🚨", "label": "Watch"},
    "read": {"emoji": "📚", "label": "Read"},
    "community": {"emoji": "💬", "label": "Community"},
}


def load_backlog():
    """Load the threads backlog."""
    if BACKLOG_FILE.exists():
        with open(BACKLOG_FILE) as f:
            return json.load(f)
    return {"items": [], "seenKeys": []}


def load_thread_state():
    """Load thread state for dedup."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def get_weekly_items(backlog, days=7):
    """Get items from the past week, excluding already-posted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    # Get posted items from the week
    posted = [
        i for i in backlog.get("items", [])
        if i.get("status") == "posted"
        and i.get("discoveredAt", "") > cutoff
    ]
    
    # Also get high-scoring queued items that haven't been posted yet
    queued = [
        i for i in backlog.get("items", [])
        if i.get("status") == "queued"
        and i.get("discoveredAt", "") > cutoff
    ]
    
    # Combine: posted items (what went out) + top queued (what's brewing)
    all_items = posted + [i for i in queued if (i.get("score") or 0) >= 40]
    
    # Dedupe by url
    seen_urls = set()
    deduped = []
    for item in all_items:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(item)
    
    # Sort by score descending
    deduped.sort(key=lambda x: -(x.get("score") or 0))
    return deduped


def categorize_weekly(items):
    """Sort items into weekly sections."""
    sections = {
        "ship": [],
        "watch": [],
        "read": [],
        "community": [],
    }
    
    for item in items:
        cat = item.get("primaryCategory", "read")
        if cat in sections:
            sections[cat].append(item)
    
    return sections


def fetch_release_body(url):
    """Fetch GitHub release body text."""
    import re
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/releases/tag/(.+)", url)
    if not m:
        return ""
    owner, repo, tag = m.group(1), m.group(2), m.group(3)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        req = Request(api_url, headers={"User-Agent": "ClawBytes/1.0"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        body = (data.get("body") or "").strip()
        if len(body) > 300:
            body = body[:297] + "..."
        return body
    except Exception:
        return ""


def fetch_article_snippet(url):
    """Fetch first ~500 chars of text from an article URL for LLM grounding."""
    try:
        import re
        req = Request(url, headers={"User-Agent": "ClawBytes/1.0", "Accept": "text/html"})
        with urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 500:
            text = text[:497] + "..."
        return text
    except Exception:
        return ""


def prefetch_item_data(section_items):
    """Prefetch release bodies and article snippets in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    # Only prefetch for items that need it
    to_fetch = []
    for item in section_items:
        url = item.get("url", "")
        cat = item.get("primaryCategory", "read")
        source = item.get("sourceType", "")
        if cat == "ship" and "github.com" in url:
            to_fetch.append((item, "release", url))
        elif cat == "read" and source == "rss" and url:
            to_fetch.append((item, "snippet", url))
    
    results = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        for item, kind, url in to_fetch:
            if kind == "release":
                futures[executor.submit(fetch_release_body, url)] = (item, kind)
            else:
                futures[executor.submit(fetch_article_snippet, url)] = (item, kind)
        
        for future in as_completed(futures):
            item, kind = futures[future]
            try:
                results[id(item)] = (kind, future.result())
            except Exception:
                results[id(item)] = (kind, "")
    
    return results

def display_title(item):
    """Get a clean display title."""
    title = item.get("title", "")
    # Truncate very long titles
    if len(title) > 80:
        title = title[:77] + "..."
    return title


def llm_weekly_digest(sections, total_items):
    """Generate the weekly digest using Tenspire LLM."""
    if not LLM_API_KEY:
        return None
    
    # Prefetch data in parallel first
    all_items = []
    for cat in ["ship", "watch", "read", "community"]:
        all_items.extend(sections.get(cat, [])[:8])
    prefetched = prefetch_item_data(all_items)
    
    # Build context for each section
    context_lines = []
    for cat in ["ship", "watch", "read", "community"]:
        items = sections.get(cat, [])
        if not items:
            continue
        meta = CATEGORY_META[cat]
        context_lines.append(f"\n{meta['emoji']} {meta['label'].upper()} ({len(items)} items)")
        for i, item in enumerate(items[:8]):  # Top 8 per section
            title = display_title(item)
            url = item.get("url", "")
            raw_summary = item.get("summary", "")
            source = item.get("sourceType", "")
            score = item.get("score", 0)
            
            line = f"\n{i+1}. {title} | {url} | score={score} | source: {source}"
            if raw_summary:
                line += f" | {raw_summary}"
            
            # Use prefetched release notes for ship items
            if cat == "ship" and "github.com" in url:
                release_body = prefetched.get(id(item), ("", ""))[1]
                if release_body:
                    line += f" | RELEASE NOTES: {release_body}"
            
            # Use prefetched article snippets for read items
            if cat == "read" and source == "rss" and url:
                snippet = prefetched.get(id(item), ("", ""))[1]
                if snippet:
                    line += f" | ARTICLE SNIPPET: {snippet}"
            
            # HN engagement data
            if source == "hackernews":
                pts = item.get("rawScore", 0)
                comments = item.get("rawComments", 0)
                line += f" | {pts}pts / {comments} comments on HN"
            
            # Reddit engagement
            if source == "reddit":
                score_r = item.get("rawScore", item.get("score", 0))
                comments_r = item.get("rawComments", 0)
                line += f" | {score_r}↑ / {comments_r}💬 on Reddit"
            
            context_lines.append(line)
    
    prompt = f"""You are the editor of @clawbytes, a Telegram channel covering AI agents, LLMs, and the OpenClaw ecosystem.

Generate a WEEKLY DIGEST post in strict HTML format for Telegram. This covers the top {total_items} items from the past week.

FORMAT (strict HTML):
📰 <b>ClawBytes Weekly</b> — {datetime.now().strftime("%B %d, %Y")}

<b>🚢 What Shipped</b>
[emoji] <a href="URL">TITLE</a> — 1 punchy sentence on what changed and why operators care

<b>🚨 What to Watch</b>
[emoji] <a href="URL">TITLE</a> — 1 sentence on the risk, what breaks, what to check

<b>📚 What to Read</b>
[emoji] <a href="URL">TITLE</a> — 1 sentence naming the specific insight or technique. Never "explores" or "reveals" — say WHAT it found or proposes. If you don't know specifics, describe what the piece IS and its ambition.

<b>💬 Community Signal</b>
[emoji] <a href="URL">TITLE</a> — 1 sentence on the real user signal, the sentiment, the discovery

Rules:
- Use <b> for bold, <a href="URL">title text</a> for links — use the ACTUAL name as link text, never "Link" or "thread"
- MAX 120 chars per item summary. No filler. No "notable" or "worth watching." No "offering insights" or "highlights."
- For HN items: reference community signal (e.g. "171 points on HN") when it adds weight
- For Reddit items: include vote/comment counts (e.g. "45↑ / 12💬")
- If a section has 0 items, write: "Quiet week for [section name]."
- MAX 5 items per section (pick the highest-scored)
- NEVER fabricate statistics, metrics, or specific findings. If unsure, describe the piece's ambition, not its results.
- BANNED VERBS: explores, explored, exploring, reveals, revealed, revealing, highlights, highlighted, highlighting, dives into, broke down, breaking down, unpacks, unpacked, unpacking, delves into, delving into, examines, examined, examining, offers, offered, offering, showcases, showcased, showcasing, demonstrates, demonstrated, demonstrating
- End with: #ClawBytes #WeeklyDigest

Items:{"".join(context_lines)}

Write the post now:"""

    try:
        data = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500,
            "temperature": 0.4,
        }).encode()

        req = Request(
            f"{LLM_URL}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
        )
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"]["content"].strip()
        if len(content) < 50 or "I cannot" in content:
            return None
        # Strip banned verbs
        content = strip_banned_verbs(content)
        # Telegram HTML uses newlines, not <br> tags
        content = content.replace('<br>', '').replace('<br/>', '').replace('<br />', '')
        return content
    except Exception as e:
        print(f"LLM error: {e}", file=sys.stderr)
        return None


# Banned verb replacement
BANNED_VERBS = {
    "explores": "maps", "explored": "mapped", "exploring": "mapping",
    "reveals": "finds", "revealed": "found", "revealing": "finding",
    "highlights": "flags", "highlighted": "flagged", "highlighting": "flagging",
    "dives into": "tackles", "broke down": "cut through", "breaking down": "cutting through",
    "unpacks": "traces", "unpacked": "traced", "unpacking": "tracing",
    "delves into": "traces", "delving into": "tracing",
    "examines": "audits", "examined": "audited", "examining": "auditing",
    "offers": "delivers", "offered": "delivered", "offering": "delivering",
    "showcases": "ships", "showcased": "shipped", "showcasing": "shipping",
    "demonstrates": "shows", "demonstrated": "showed", "demonstrating": "showing",
}


def strip_banned_verbs(text):
    """Replace banned soft verbs with stronger alternatives."""
    import re
    result = text
    for verb, replacement in BANNED_VERBS.items():
        pattern = re.compile(r'\b' + re.escape(verb) + r'\b', re.IGNORECASE)
        match = pattern.search(result)
        if match:
            matched = match.group()
            if matched[0].isupper():
                fixed = replacement[0].upper() + replacement[1:]
            else:
                fixed = replacement
            result = pattern.sub(fixed, result, count=1)
    return result


def send_to_telegram(message, bot_token=None, channel_id=None):
    """Send message to Telegram channel via HTML parse mode."""
    token = bot_token or TELEGRAM_BOT_TOKEN
    channel = channel_id or TELEGRAM_CHANNEL_ID
    
    if not token:
        return False, "No bot token"
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": channel,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    
    try:
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("ok", False), result
    except Exception as e:
        return False, str(e)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate weekly ClawBytes digest using Tenspire LLM")
    parser.add_argument("--send", action="store_true", help="Send to Telegram channel")
    parser.add_argument("--preview", action="store_true", help="Preview without sending")
    parser.add_argument("--status", action="store_true", help="Show digest status")
    args = parser.parse_args()
    
    if args.status:
        state = load_thread_state()
        backlog = load_backlog()
        posted = [i for i in backlog["items"] if i.get("status") == "posted"]
        print(f"Last weekly: {state.get('lastWeekly', 'never')}")
        print(f"Backlog items: {len(backlog['items'])}")
        print(f"Posted items: {len(posted)}")
        return
    
    # Load data
    backlog = load_backlog()
    
    # Get this week's items
    items = get_weekly_items(backlog, days=7)
    
    if not items:
        print("No items from the past week. Nothing to digest.")
        return
    
    print(f"Found {len(items)} items from the past week")
    
    # Categorize
    sections = categorize_weekly(items)
    for cat, cat_items in sections.items():
        print(f"  {cat}: {len(cat_items)} items")
    
    # Generate with LLM
    total = sum(len(v) for v in sections.values())
    print(f"\nGenerating weekly digest with {LLM_MODEL}...")
    digest_text = llm_weekly_digest(sections, total)
    
    if digest_text:
        print(f"\n{'='*50}")
        print(digest_text)
        print(f"{'='*50}")
        
        if args.send:
            print("\n📤 Sending to Telegram...")
            success, result = send_to_telegram(digest_text)
            if success:
                print("✅ Sent successfully!")
                # Update state
                state = load_thread_state()
                state["lastWeekly"] = datetime.now(timezone.utc).isoformat()
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f, indent=2)
            else:
                print(f"❌ Failed: {result}")
    else:
        print("❌ LLM generation failed")
        # Fallback: simple template
        lines = [f"📰 <b>ClawBytes Weekly</b> — {datetime.now().strftime('%B %d, %Y')}\n"]
        for cat in ["ship", "watch", "read", "community"]:
            meta = CATEGORY_META[cat]
            cat_items = sections.get(cat, [])[:5]
            lines.append(f"\n<b>{meta['emoji']} {meta['label']}</b>")
            if cat_items:
                for item in cat_items:
                    title = display_title(item)
                    lines.append(f"{meta['emoji']} <a href=\"{item.get('url','')}\">{title}</a>")
            else:
                lines.append(f"Quiet week for {meta['label'].lower()}.")
        lines.append("\n#ClawBytes #WeeklyDigest")
        fallback = "\n".join(lines)
        print(fallback)
        if args.send:
            send_to_telegram(fallback)


if __name__ == "__main__":
    main()