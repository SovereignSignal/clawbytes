#!/usr/bin/env python3
"""claw-notion-sync.py — Bidirectional sync between Notion Claws market map and ClawBytes sources.

Direction 1 (Notion → Sources): Reads all subpages, extracts products/repos/categories,
adds new entries to claw-ecosystem-sources.json, security monitor, and RSS feeds.

Direction 2 (Sources → Notion): Finds products in sources that aren't in Notion,
creates new subpages with auto-populated metadata. Also appends signal updates
(new releases, security advisories) to existing entries.

Guardrails:
- Only APPENDS to existing pages, never overwrites
- Marks all additions with 🦀 ClawBack prefix
- Skips pages edited in last 24h (Claude Code might be active)
- Deduplicates signal blocks to avoid spam

Usage:
  python3 claw-notion-sync.py                        # dry run (Notion → Sources only)
  python3 claw-notion-sync.py --apply                # apply Notion → Sources changes
  python3 claw-notion-sync.py --apply --writeback     # also write new products/signal to Notion
  python3 claw-notion-sync.py --apply --report        # apply + send Telegram summary to Sov
  python3 claw-notion-sync.py --apply --writeback --report  # full bidirectional + report
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
SOURCES_FILE = WORKSPACE / "memory" / "claw-ecosystem-sources.json"
NOTION_PAGES_FILE = WORKSPACE / "memory" / "claw-notion-pages.json"
CACHE_FILE = WORKSPACE / "memory" / "claw-notion-cache.json"
SECURITY_MONITOR = WORKSPACE / "scripts" / "claw-security-monitor.py"
NOTION_PAGE_ID = "337000c0-d590-805d-af74-f27d19215184"

# Notion API key (ClawBack integration)
NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Category mapping from Notion sections to ClawBytes lanes
CATEGORY_MAP = {
    "Tiny Claws": "ship",
    "Self Hosted": "ship",
    "Enterprise Big Tech": "watch",
    "Enterprise Startups": "watch",
    "Infrastructure": "ship",
}


def notion_api(path: str, method="GET", data=None) -> dict:
    """Make a Notion API request."""
    url = f"https://api.notion.com/v1/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read())


def get_page_title(page_id: str) -> str:
    """Get the title of a Notion page."""
    try:
        import urllib.request
        import socket
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)
        data = notion_api(f"pages/{page_id}")
        for k, v in data.get("properties", {}).items():
            if v.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in v.get("title", []))
    except Exception:
        pass
    finally:
        try:
            socket.setdefaulttimeout(original_timeout)
        except NameError:
            pass
    return ""


def get_page_content(page_id: str) -> str:
    """Get all text content from a page (for repo URL extraction)."""
    try:
        import socket
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)
        data = notion_api(f"blocks/{page_id}/children?page_size=100")
        texts = []
        for block in data.get("results", []):
            btype = block.get("type", "")
            if btype in block and isinstance(block[btype], dict):
                for rt in block[btype].get("rich_text", []):
                    href = rt.get("href", "")
                    if href and "github.com" in href:
                        texts.append(href)
                    texts.append(rt.get("plain_text", ""))
        return "\n".join(texts)
    except Exception:
        return ""
    finally:
        try:
            socket.setdefaulttimeout(original_timeout)
        except NameError:
            pass


def extract_github_url(text: str) -> str | None:
    """Extract a GitHub repo URL from text. Be strict to avoid false positives."""
    # Match github.com/OWNER/REPO where OWNER and REPO are alphanumeric with hyphens/underscores
    # Reject obvious non-repo paths like /features, /blog, /docs, /about
    match = re.search(
        r'github\.com/([a-zA-Z0-9](?:[a-zA-Z0-9\-_.]*[a-zA-Z0-9])?/[a-zA-Z0-9](?:[a-zA-Z0-9\-_.]*[a-zA-Z0-9])?)',
        text
    )
    if match:
        repo = match.group(1)
        # Reject common non-repo paths
        excluded_prefixes = ('features/', 'blog/', 'docs/', 'about/', 'enterprise/', 'copilot/',
                             'collections/', 'topics/', 'trending/', 'explore/', 'pricing/')
        if any(repo.startswith(p) for p in excluded_prefixes):
            return None
        # Clean up trailing punctuation
        repo = re.sub(r'[.,;:)]+$', '', repo)
        # Validate it looks like owner/repo (has exactly one slash)
        if repo.count('/') != 1:
            return None
        return repo
    return None

def extract_any_url(text: str) -> str | None:
    """Extract any product URL from text."""
    # Match URLs
    matches = re.findall(r'https?://[^\s\)\]\>\"\'\`\,]+', text)
    for url in matches:
        # Skip GitHub non-repo URLs
        if 'github.com' in url:
            continue
        # Skip common irrelevant domains
        if any(d in url for d in ['google.com', 'microsoft.com', 'amazon.com', 'apple.com',
                                     'youtube.com', 'twitter.com', 'x.com']):
            continue
        return url.split('?')[0]  # Strip query params
    return None


def load_cached_entries() -> list[dict] | None:
    """Load cached Notion entries if cache is fresh (<24h)."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        timestamp = data.get("_cached_at", 0)
        age = (datetime.now(timezone.utc).timestamp() - timestamp)
        if age < 86400:  # < 24h old
            print(f"Using cached Notion data ({age/3600:.1f}h old)")
            return data.get("entries", [])
    except Exception:
        pass
    return None


def scrape_claws_page() -> list[dict]:
    """Scrape the Notion Claws page and return all entries with categories.
    Uses cached data if fresh (<24h)."""
    cached = load_cached_entries()
    if cached is not None:
        return cached

    print("Fetching fresh Claws page structure...")
    data = notion_api(f"blocks/{NOTION_PAGE_ID}/children?page_size=100")

    categories = {
        "Tiny Claws": [],
        "Self Hosted": [],
        "Enterprise Big Tech": [],
        "Enterprise Startups": [],
        "Infrastructure": [],
    }

    current_cat = None
    for block in data.get("results", []):
        btype = block.get("type", "")

        if btype == "heading_2":
            text = "".join(t.get("plain_text", "") for t in block[btype].get("rich_text", []))
            if "Tiny" in text:
                current_cat = "Tiny Claws"
            elif "Self Hosted" in text:
                current_cat = "Self Hosted"
            elif "Big Tech" in text:
                current_cat = "Enterprise Big Tech"
            elif "Startups" in text:
                current_cat = "Enterprise Startups"
            elif "Infrastructure" in text:
                current_cat = "Infrastructure"

        elif btype == "child_page" and current_cat:
            page_id = block["id"]
            title = get_page_title(page_id)
            content = get_page_content(page_id)
            repo = extract_github_url(content)

            categories[current_cat].append({
                "title": title,
                "page_id": page_id,
                "repo": repo,
                "category": current_cat,
            })

    # Flatten
    all_entries = []
    for cat, items in categories.items():
        all_entries.extend(items)

    print(f"Found {len(all_entries)} entries across {len(categories)} categories")
    # Save to cache
    try:
        cache_data = {"_cached_at": datetime.now(timezone.utc).timestamp(), "entries": all_entries}
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
        print(f"Cached {len(all_entries)} entries to {CACHE_FILE}")
    except Exception as e:
        print(f"Warning: failed to write cache: {e}")
    return all_entries


def load_sources() -> dict:
    """Load current source state."""
    if SOURCES_FILE.exists():
        return json.loads(SOURCES_FILE.read_text())
    return {"curated": [], "dynamic": [], "rss": [], "rssFeeds": [], "subreddits": [],
            "influencers": [], "awesomeLists": [], "websites": [], "hackerNewsQueries": []}


def save_sources(sources: dict):
    """Save source state."""
    SOURCES_FILE.write_text(json.dumps(sources, indent=2))


def diff_and_apply(entries: list[dict], apply: bool = False) -> list[str]:
    """Compare Notion entries against current sources and optionally apply changes."""
    sources = load_sources()
    existing_repos = {}
    for item in sources.get("curated", []) + sources.get("dynamic", []):
        r = item.get("repo", "")
        if r:
            existing_repos[r.lower()] = item

    changes = []
    added_repos = []
    updated_cats = []
    closed_source = []
    changes_made = False

    for entry in entries:
        repo = entry.get("repo")
        name = entry.get("title", "")
        cat = entry.get("category", "")
        page_id = entry.get("page_id", "")

        if not repo:
            # Track as closed-source product (no GitHub repo)
            closed_source.append(entry)
            changes.append(f"  📄 {name} ({cat}) — closed source, tracked as product")
            continue

        repo_lc = repo.lower()
        if repo_lc in existing_repos:
            existing = existing_repos[repo_lc]
            old_cat = existing.get("notion_category", "")
            if old_cat != cat:
                changes.append(f"  ↔ [{old_cat} → {cat}] {name}")
                if apply:
                    existing["notion_category"] = cat
                    updated_cats.append(repo_lc)
            continue

        changes.append(f"  + [{cat}] {name} → {repo}")
        added_repos.append(entry)

        if apply:
            sources["curated"].append({
                "repo": repo,
                "name": name,
                "description": f"Notion Claws: {cat}",
                "stars": 0,
                "exists": True,
                "type": "ecosystem",
                "source": "curated",
                "notion_category": cat,
                "notion_page_id": page_id,
                "discoveredAt": datetime.now(timezone.utc).isoformat(),
            })

            # Add RSS feed
            rss_url = f"https://github.com/{repo}/releases.atom"
            existing_urls = {f.get("url", "") for f in sources.get("rss", [])}
            if rss_url not in existing_urls:
                sources["rss"].append({"name": f"{name} Releases", "url": rss_url, "type": "releases"})

            existing_repos[repo_lc] = sources["curated"][-1]

    if apply and (added_repos or updated_cats):
        save_sources(sources)
        add_to_security_monitor([e["repo"] for e in added_repos if e.get("repo")])
        changes_made = True

    if not changes:
        changes.append("  ✅ No changes — Notion and local sources are in sync")

    return changes, closed_source, changes_made


def add_to_security_monitor(repos: list[str]):
    """Add repos to the security monitor's WATCHED_REPOS list."""
    with open(SECURITY_MONITOR) as f:
        content = f.read()

    match = re.search(r'WATCHED_REPOS = \[(.*?)\]', content, re.DOTALL)
    if not match:
        return

    current = match.group(1)
    existing = set()
    for line in current.strip().split('\n'):
        line = line.strip().strip('"').strip("'").strip(',').strip()
        if line and not line.startswith('#'):
            existing.add(line)

    new_lines = []
    for repo in repos:
        if repo not in existing:
            new_lines.append(f'    "{repo}",')

    if new_lines:
        insert_idx = content.rfind(']', content.index('WATCHED_REPOS'))
        content = content[:insert_idx] + '\n'.join(new_lines) + '\n' + content[insert_idx:]
        with open(SECURITY_MONITOR, "w") as f:
            f.write(content)


# ─── Write-back: ClawBytes → Notion ───────────────────────────────────────

def fetch_github_repo_info(repo: str) -> dict | None:
    """Fetch repo metadata from GitHub API."""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "User-Agent": "ClawBack/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"
    try:
        url = f"https://api.github.com/repos/{repo}"
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read())
        return {
            "name": data.get("name", ""),
            "full_name": data.get("full_name", repo),
            "description": data.get("description", ""),
            "stars": data.get("stargazers_count", 0),
            "language": data.get("language", ""),
            "topics": data.get("topics", []),
            "html_url": data.get("html_url", f"https://github.com/{repo}"),
            "homepage": data.get("homepage", ""),
            "created_at": data.get("created_at", ""),
            "pushed_at": data.get("pushed_at", ""),
        }
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"  GitHub API rate limit hit — set GITHUB_TOKEN env var for higher limits")
        elif e.code == 404:
            print(f"  GitHub repo not found: {repo}")
        else:
            print(f"  GitHub API error for {repo}: {e}")
        return None
    except Exception as e:
        print(f"  GitHub API error for {repo}: {e}")
        return None


def infer_category(repo_info: dict) -> str:
    """Infer which Notion category a repo belongs to based on topics and metadata."""
    topics = set(t.lower() for t in repo_info.get("topics", []))
    stars = repo_info.get("stars", 0)
    desc = (repo_info.get("description", "") or "").lower()
    name = repo_info.get("name", "").lower()

    # Tiny claws: lightweight, small codebase, self-hostable
    tiny_keywords = {"tiny", "minimal", "lightweight", "micro", "nano", "smol", "simple"}
    if topics & tiny_keywords or any(k in name for k in ["nano", "micro", "smol", "pico", "tiny"]):
        return "Tiny Claws"

    # Self-hosted: security, audit, sandbox, self-hosted
    self_hosted_keywords = {"self-hosted", "sandbox", "audit", "encryption", "privacy", "local-first"}
    if topics & self_hosted_keywords:
        return "Self Hosted"

    # Infrastructure: framework, sdk, toolkit, orchestration
    infra_keywords = {"framework", "sdk", "toolkit", "orchestration", "workflow", "automation",
                      "langchain", "llm", "mcp", "agent-framework", "coding-agent"}
    if topics & infra_keywords:
        return "Infrastructure"

    # Enterprise startups: larger stars, commercial products
    startup_keywords = {"saas", "platform", "commercial", "enterprise"}
    if topics & startup_keywords or stars > 10000:
        return "Enterprise Startups"

    # Default to infrastructure for open-source tooling
    return "Infrastructure"


def create_notion_page(name: str, category: str, repo_info: dict) -> str | None:
    """Create a new subpage under the Claws page for a discovered product.
    
    Uses `ntn pages create` for clean Markdown-based page creation.
    """
    import subprocess
    import shutil
    
    ntn = shutil.which("ntn") or os.path.expanduser("~/.local/bin/ntn")
    env = {**os.environ, "NOTION_API_TOKEN": NOTION_KEY}
    
    repo = repo_info.get("full_name", "")
    desc = repo_info.get("description", "No description available")
    stars = repo_info.get("stars", 0)
    language = repo_info.get("language", "Unknown")
    topics = repo_info.get("topics", [])
    html_url = repo_info.get("html_url", "")
    homepage = repo_info.get("homepage", "")
    today = datetime.now().strftime("%Y-%m-%d")

    # Build Markdown content
    md = f"""{desc or 'No description available.'}

### What it is
{name} is a {language}-based project in the {category} category of the Claw landscape.

### Quick facts
- ⭐ {stars:,} stars
- 💻 Language: {language}
- 🏷️ Topics: {', '.join(topics[:8]) if topics else 'none'}

### Links
- [GitHub]({html_url})
"""
    if homepage:
        md += f"- [Website]({homepage})\n"

    md += f"\n---\n**🦀 Drafted by ClawBack** — Auto-generated {today}. Awaiting human review."

    try:
        result = subprocess.run(
            [ntn, "pages", "create", "--parent", f"page:{NOTION_PAGE_ID}", "--content", md],
            capture_output=True, text=True, timeout=30, env=env
        )
        if result.returncode != 0:
            raise Exception(f"ntn pages create failed: {result.stderr}")
        page_id = result.stdout.strip()
        print(f"  Created Notion page via ntn: {name} → {page_id}")
        return page_id
    except FileNotFoundError:
        # ntn not found, fall back to API
        print(f"  ntn not found, falling back to API for {name}")
        return _create_notion_page_api(name, category, repo_info)
    except Exception as e:
        print(f"  Failed to create Notion page for {name}: {e}")
        # Fallback to API
        try:
            return _create_notion_page_api(name, category, repo_info)
        except Exception as e2:
            print(f"  API fallback also failed for {name}: {e2}")
            return None


def _create_notion_page_api(name: str, category: str, repo_info: dict) -> str | None:
    """Fallback: create Notion page using raw API (when ntn unavailable)."""
    repo = repo_info.get("full_name", "")
    desc = repo_info.get("description", "No description available")
    stars = repo_info.get("stars", 0)
    language = repo_info.get("language", "Unknown")
    topics = repo_info.get("topics", [])
    html_url = repo_info.get("html_url", "")
    homepage = repo_info.get("homepage", "")

    children = [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": desc or "No description available."}}]}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "What it is"}}]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"{name} is a {language}-based project in the {category} category of the Claw landscape."}}]}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "Quick facts"}}]}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": f"⭐ {stars:,} stars"}}]}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": f"💻 Language: {language}"}}]}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": f"🏷️ Topics: {', '.join(topics[:8]) if topics else 'none'}"}}]}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "Links"}}]}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": "GitHub: "}}, {"type": "text", "text": {"content": html_url, "link": {"url": html_url}}}]}},
    ]
    if homepage:
        children.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": "Website: "}}, {"type": "text", "text": {"content": homepage, "link": {"url": homepage}}}]}}, )
    children.extend([
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "🦀 Drafted by ClawBack — "}, "annotations": {"bold": True}}, {"type": "text", "text": {"content": f"Auto-generated {datetime.now().strftime('%Y-%m-%d')}. Awaiting human review."}}]}},
    ])

    result = notion_api("pages", method="POST", data={
        "parent": {"page_id": NOTION_PAGE_ID},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": name}}]}},
        "children": children,
    })
    page_id = result.get("id", "")
    print(f"  Created Notion page via API: {name} → {page_id}")
    return page_id


def append_signal_to_page(page_id: str, signal_text: str):
    """Append a 🦀 ClawBack Signal block to an existing Notion page.
    
    Uses block-level API for append (safe — doesn't rewrite page content).
    Uses ntn pages get for dedup checking.
    Only appends if the page hasn't been edited in the last 24h.
    """
    ntn = shutil.which("ntn") or os.path.expanduser("~/.local/bin/ntn")
    env = {**os.environ, "NOTION_API_TOKEN": NOTION_KEY}

    # Check last edited time
    try:
        page_data = notion_api(f"pages/{page_id}")
        last_edited = page_data.get("last_edited_time", "")
        if last_edited:
            edited_dt = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - edited_dt).total_seconds()
            if age < 86400:  # edited in last 24h
                print(f"  Skipping signal for {page_id} — edited {age/3600:.1f}h ago (likely active editing)")
                return False
    except Exception:
        pass

    # Dedup: check if we already appended this signal (via ntn pages get)
    try:
        result = subprocess.run(
            [ntn, "pages", "get", page_id],
            capture_output=True, text=True, timeout=15, env=env
        )
        if result.returncode == 0 and signal_text[:50] in result.stdout:
            print(f"  Skipping signal for {page_id} — similar content already exists")
            return False
    except Exception:
        pass

    # Append the signal via block-level API (safe append, doesn't rewrite page)
    try:
        notion_api(f"blocks/{page_id}/children", method="PATCH", data={
            "children": [
                {
                    "object": "block",
                    "type": "divider",
                    "divider": {}
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": f"🦀 ClawBack Signal — {datetime.now().strftime('%Y-%m-%d')}"}, "annotations": {"bold": True}},
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": signal_text}}]
                    }
                },
            ]
        })
        print(f"  Appended signal to {page_id}")
        return True
    except Exception as e:
        print(f"  Failed to append signal to {page_id}: {e}")
        return False


def discover_and_writeback(entries: list[dict], apply: bool = False) -> list[str]:
    """Check for new products in sources that aren't in Notion, and create pages.
    
    Also check for significant signal on existing entries and append updates.
    """
    sources = load_sources()
    notion_names = {e["title"].lower().strip() for e in entries}
    notion_repos = {e.get("repo", "").lower() for e in entries if e.get("repo")}

    changes = []

    # Also match by known aliases (e.g. "claude-code" in sources = "Claude Agent SDK" in Notion)
    notion_aliases = {
        "anthropics/claude-code": "claw code",  # "Claw Code" entry in Notion = Claude Code
        "anthropics/claude-agent-sdk-python": "claude agent sdk",
        "modelcontextprotocol/servers": "mcp servers",
        "composiohq/composio": "composio",
        "composiohq/agent-orchestrator": "composio",
        "langchain-ai/langchain": "langchain",
        "langchain-ai/langgraph": "langchain",
        "openclaw/openclaw": "openclaw",
        "significan-gravitas/autogpt": "autogpt",
        "significant-gravitas/autogpt": "autogpt",
        "crewaiinc/crewai": "crewai",
        "langflow-ai/langflow": "langflow",
        "langgenius/dify": "dify",
        "browser-use/browser-use": "browser use",
        "mem0ai/mem0": "mem0",
        "vllm-project/vllm": "vllm",
        "deepseek-ai/deepseek-v3": "deepseek",
        "qwenlm/qwen": "qwen",
        "aider-ai/aider": "aider",
        "continuedev/continue": "continue",
        "yamadashy/repomix": "repomix",
        "unclecode/crawl4ai": "crawl4ai",
        "mendableai/firecrawl": "firecrawl",
        "block/goose": "goose",
        "copilotkit/copilotkit": "copilotkit",
        "letta-ai/letta": "letta",
        "cline/cline": "cline",
        "pydantic/pydantic-ai": "pydantic ai",
        "vercel/ai": "vercel ai",
        "deepset-ai/haystack": "haystack",
        "run-llama/llama_index": "llamaindex",
    }
    # Expand notion_names with aliases
    notion_names_extended = set(notion_names)
    for alias, target in notion_aliases.items():
        if target in notion_names:
            notion_names_extended.add(alias.lower().strip())
    # Also add repo names to the extended set
    notion_repos_extended = set(notion_repos)
    for alias_key, alias_val in notion_aliases.items():
        if alias_val in notion_names:
            notion_repos_extended.add(alias_key.lower())

    new_products = []
    for item in sources.get("curated", []) + sources.get("dynamic", []):
        repo = item.get("repo", "")
        name = item.get("name", "")

        if not repo or repo.lower() in notion_repos_extended:
            continue
        if name.lower().strip() in notion_names_extended:
            continue

        # Skip if it was already drafted by ClawBack
        if item.get("notion_drafted"):
            continue

        # Fetch GitHub info for categorization
        repo_info = fetch_github_repo_info(repo)
        if not repo_info:
            continue

        stars = repo_info.get("stars", 0)

        # Only auto-draft repos with meaningful traction
        # Curated sources: 500+ stars (hand-picked, but still need real traction)
        # Dynamic sources: 2000+ stars (auto-discovered, must be significant)
        min_stars = 500 if item.get("source") == "curated" else 2000
        if stars < min_stars:
            continue

        # Skip obvious low-signal repos (awesome lists, dotfiles, templates, forks, android ports)
        skip_patterns = ["awesome-", "dotfiles", "-template", "-starter", "-example",
                          "-docker", "-helm", "-plugin", "-theme", "-icons",
                          "-android", "-ios", "-macos", "-desktop"]
        if any(p in repo.lower() for p in skip_patterns):
            continue

        # Skip repos that are obviously wrappers/stacks around existing products
        skip_name_patterns = ["openclaw-", "-openclaw"]
        if any(p in name.lower() for p in skip_name_patterns) and stars < 500:
            continue

        # Skip repos with generic/low-signal names
        skip_name_exact = {"poco-claw", "nullclaw", "flyenv", "safeclaw", "nanoclaw-py",
                           "clawcrew", "claw-code-agent", "fastclaw"}
        if name.lower().strip() in skip_name_exact:
            continue

        category = item.get("notion_category") or infer_category(repo_info)
        new_products.append({
            "name": name or repo_info.get("name", ""),
            "repo": repo,
            "category": category,
            "repo_info": repo_info,
            "source_item": item,
        })

    for prod in new_products:
        changes.append(f"  ✏️  New product → Notion: [{prod['category']}] {prod['name']} ({prod['repo']})")

        if apply:
            page_id = create_notion_page(
                name=prod["name"],
                category=prod["category"],
                repo_info=prod["repo_info"],
            )
            if page_id:
                # Mark as drafted in sources
                prod["source_item"]["notion_drafted"] = True
                prod["source_item"]["notion_page_id"] = page_id
                prod["source_item"]["notion_category"] = prod["category"]

    if apply and new_products:
        save_sources(sources)

    # 2. Signal checking — append significant repo signals to existing Notion pages
    if apply:
        for entry in entries:
            repo = entry.get("repo", "")
            page_id = entry.get("notion_page_id", "")
            if not repo or not page_id:
                continue
            signal_text = check_repo_signal(repo)
            if signal_text:
                ok = append_signal_to_page(page_id, signal_text)
                if ok:
                    changes.append(f"  📡 Signal appended to {entry['title']}: {signal_text[:80]}")

    if not changes:
        changes.append("  ✅ No write-back changes — Notion is up to date")

    return changes


def check_repo_signal(repo: str) -> str | None:
    """Check for significant signal on a repo (security advisory, major release).
    
    Returns a signal string if found, None otherwise.
    """
    signals = []

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_headers = {
        "User-Agent": "ClawBack/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if gh_token:
        gh_headers["Authorization"] = f"token {gh_token}"

    # Check GitHub releases (last 7 days)
    try:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=3"
        req = urllib.request.Request(url, headers=gh_headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            releases = json.loads(resp.read())

        now = datetime.now(timezone.utc)
        for rel in releases:
            published = rel.get("published_at", "")
            if not published:
                continue
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if (now - pub_dt).days <= 7:
                tag = rel.get("tag_name", "")
                name = rel.get("name", tag)
                is_pre = rel.get("prerelease", False)
                label = f"{'(pre-release) ' if is_pre else ''}{name}"
                signals.append(f"New release: {label}")
    except Exception:
        pass

    # Check security advisories
    try:
        url = f"https://api.github.com/repos/{repo}/security-advisories?per_page=3"
        req = urllib.request.Request(url, headers=gh_headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            advisories = json.loads(resp.read())

        now = datetime.now(timezone.utc)
        for adv in advisories:
            published = adv.get("published_at", "")
            if not published:
                continue
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if (now - pub_dt).days <= 7:
                cve = adv.get("cve_id", "")
                severity = adv.get("severity", "")
                summary = adv.get("summary", "")[:100]
                signals.append(f"⚠️ Security advisory ({severity}): {cve} — {summary}")
    except Exception:
        # 404 = no advisories API, which is normal
        pass

    return "\n".join(signals) if signals else None


def send_telegram_report(changes: list[str]):
    """Send a summary to Sov via Telegram."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token:
        print("No Telegram bot token — skipping report")
        return

    text = f"🦀 Notion Claws Sync — {datetime.now().strftime('%Y-%m-%d')}\n\n"
    text += '\n'.join(changes)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            print("Telegram report sent")
    except Exception as e:
        print(f"Telegram send failed: {e}")


def main():
    apply = "--apply" in sys.argv
    report = "--report" in sys.argv

    if apply:
        print("Mode: APPLY (changes will be written)")
    else:
        print("Mode: DRY RUN (no changes will be made)")

    writeback = "--writeback" in sys.argv

    entries = scrape_claws_page()

    # Save raw Notion data
    NOTION_PAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTION_PAGES_FILE.write_text(json.dumps(entries, indent=2))
    print(f"Saved {len(entries)} entries to {NOTION_PAGES_FILE}")

    # Direction 1: Notion -> ClawBytes sources
    changes, closed_source, sources_changed = diff_and_apply(entries, apply=apply)

    print("\nNotion -> Sources changes:")
    for c in changes:
        print(c)

    # Direction 2: ClawBytes -> Notion write-back
    writeback_changes = []
    if writeback:
        print("\nSources -> Notion write-back:")
        wb_changes = discover_and_writeback(entries, apply=apply)
        for c in wb_changes:
            print(c)
        writeback_changes.extend(wb_changes)
        changes.extend(wb_changes)

        # Direction 3: Enrich existing Notion pages from backlog data
        print("\nBacklog -> Notion enrichment:")
        enrich_changes = enrich_from_backlog(entries, apply=apply)
        for c in enrich_changes:
            print(c)
        writeback_changes.extend(enrich_changes)
        changes.extend(enrich_changes)

    # Early exit if no changes (unless --force-writeback)
    force_writeback = "--force-writeback" in sys.argv
    if not sources_changed and not writeback_changes and not force_writeback:
        print("\nNo changes needed - skipping writeback")
        if report and apply:
            send_telegram_report(changes)
        return

    if report and apply:
        send_telegram_report(changes)


def enrich_from_backlog(entries: list[dict], apply: bool = False) -> list[str]:
    """Scan ClawBytes backlog for posted items and enrich Notion pages with editorial context."""
    changes = []
    backlog_file = Path(WORKSPACE) / "memory" / "clawbytes-backlog.json"
    if not backlog_file.exists():
        return changes

    try:
        backlog = json.loads(backlog_file.read_text())
    except Exception:
        return changes

    items = backlog.get("items", [])
    # Only enrich posted items from the last 7 days
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    # Build name -> Notion page_id map from entries
    notion_lookup = {}
    for entry in entries:
        title = (entry.get("title") or "").lower().strip()
        page_id = entry.get("notion_page_id", "")
        repo = (entry.get("repo") or "").lower()
        if page_id:
            notion_lookup[title] = page_id
            if repo:
                notion_lookup[repo] = page_id

    for item in items:
        if item.get("status") != "posted":
            continue

        # Check discovery time
        discovered = item.get("discoveredAt", "")
        if discovered:
            try:
                disc_dt = datetime.fromisoformat(discovered.replace("Z", "+00:00"))
                if disc_dt < cutoff:
                    continue
            except Exception:
                continue

        # Match to Notion entry
        title = item.get("title", "")
        url = item.get("url", "")
        source_name = item.get("sourceName", "")
        summary = item.get("summary", "")

        # Try matching by title/name
        matched_page_id = None
        for key, pid in notion_lookup.items():
            if key in title.lower() or key in source_name.lower():
                matched_page_id = pid
                break

        # Also try matching URL repo pattern
        if not matched_page_id and "github.com" in url:
            repo_match = re.search(r'github\.com/([^/]+/[^/]+)', url)
            if repo_match:
                repo_key = repo_match.group(1).lower()
                matched_page_id = notion_lookup.get(repo_key)

        if not matched_page_id:
            continue

        # Build signal text from the backlog item
        if not summary:
            continue

        signal_text = f"ClawBytes {item.get('primaryCategory', 'signal')}: {summary[:300]}"

        if apply:
            ok = append_signal_to_page(matched_page_id, signal_text)
            if ok:
                changes.append(f"  Enriched {source_name}: {signal_text[:80]}")
        else:
            changes.append(f"  Would enrich {source_name}: {signal_text[:80]}")

    return changes


if __name__ == "__main__":
    sys.exit(main())