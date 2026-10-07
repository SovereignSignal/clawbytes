#!/usr/bin/env python3
"""
ClawBytes RSS Feed Monitor
Monitors RSS/Atom feeds for OpenClaw ecosystem content.

State file: memory/claw-rss-state.json
"""

import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from feed_filters import (
    STATUS_FEED_NAMES,
    collapse_status_entries,
    element_text,
    is_reported_claim,
    kilo_product_post,
    normalize_havoptic_entry,
    status_incident_allowed,
    testingcatalog_relevant,
)

# Workspace path
WORKSPACE = Path(__file__).parent.parent
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-rss-state.json"

# One INFO line so a collect log can confirm these feeds actually returned.
# First sighting says the baseline was written; later runs say how many
# entries the fetch parsed. Other feeds stay quiet.
_FETCH_LOG_NAMES = {
    "DeepSeek Harness Releases": "deepseek-harness",
    "claude.dev Blog": "claude.dev",
    "Claude Status": "status-claude",
    "Cursor Status": "status-cursor",
    "GitHub Status": "status-github",
    "TestingCatalog": "testingcatalog",
    "Kilo Blog": "kilo-blog",
    "Havoptic Releases": "havoptic",
}
_STATUS_FEEDS = set(STATUS_FEED_NAMES)

# RSS feeds to monitor
RSS_FEEDS = [
    # Primary/maintainer sources — feed Read lane
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/", "tags": ["security", "technical"], "high_signal": True},
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "tags": ["official", "models"], "high_signal": True},
    {"name": "OpenAI Developers", "url": "https://developers.openai.com/rss.xml", "tags": ["official", "developers"], "high_signal": True},
    {"name": "Google DeepMind Blog", "url": "https://deepmind.google/blog/rss.xml", "tags": ["official", "models"], "high_signal": True},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml", "tags": ["open-source", "models"], "high_signal": True},
    {"name": "LangChain Blog", "url": "https://www.langchain.com/blog/rss.xml", "tags": ["frameworks", "agents"]},
    {"name": "Interconnects", "url": "https://www.interconnects.ai/feed", "tags": ["analysis", "models"]},
    {"name": "AI Snake Oil", "url": "https://aisnakeoil.substack.com/feed", "tags": ["analysis", "criticism"]},
    {"name": "Latent Space", "url": "https://latent.space/feed", "tags": ["podcast", "analysis"]},
    {"name": "GitHub Changelog", "url": "https://github.blog/changelog/feed/", "tags": ["developer-tools", "official"], "high_signal": True},
    {"name": "Cursor Changelog", "url": "https://cursor.com/changelog/rss.xml", "tags": ["coding-agent", "official"], "high_signal": True},
    # Research & papers
    {"name": "ArXiv cs.AI", "url": "https://rss.arxiv.org/rss/cs.AI", "tags": ["research", "papers"], "high_signal": True},
    {"name": "ArXiv cs.CL", "url": "https://rss.arxiv.org/rss/cs.CL", "tags": ["research", "papers"], "high_signal": True},
    # GitHub release feeds for core and adjacent operator-facing repos
    {"name": "OpenClaw Releases", "url": "https://github.com/openclaw/openclaw/releases.atom", "tags": ["releases", "official"]},
    {"name": "Hermes Agent Releases", "url": "https://github.com/NousResearch/hermes-agent/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "Nanoclaw Releases", "url": "https://github.com/qwibitai/nanoclaw/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "IronClaw Releases", "url": "https://github.com/nearai/ironclaw/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "OpenFang Releases", "url": "https://github.com/RightNow-AI/openfang/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "PicoClaw Releases", "url": "https://github.com/sipeed/picoclaw/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "Moltis Releases", "url": "https://github.com/moltis-org/moltis/releases.atom", "tags": ["releases", "ecosystem"]},
    {"name": "Codex Releases", "url": "https://github.com/openai/codex/releases.atom", "tags": ["releases", "adjacent"]},
    {"name": "Claude Code Releases", "url": "https://github.com/anthropics/claude-code/releases.atom", "tags": ["releases", "coding-agent"], "high_signal": True},
    {"name": "Claude Code Action Releases", "url": "https://github.com/anthropics/claude-code-action/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "LangGraph Releases", "url": "https://github.com/langchain-ai/langgraph/releases.atom", "tags": ["releases", "frameworks"], "high_signal": True},
    {"name": "Microsoft Agent Framework Releases", "url": "https://github.com/microsoft/agent-framework/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "CrewAI Releases", "url": "https://github.com/crewAIInc/crewAI/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Browser Use Releases", "url": "https://github.com/browser-use/browser-use/releases.atom", "tags": ["releases", "browser-automation"]},
    {"name": "vLLM Releases", "url": "https://github.com/vllm-project/vllm/releases.atom", "tags": ["releases", "inference"]},
    {"name": "Ollama Releases", "url": "https://github.com/ollama/ollama/releases.atom", "tags": ["releases", "local-models"]},
    {"name": "openai-agents Releases", "url": "https://github.com/openai/openai-agents-python/releases.atom", "tags": ["releases", "agent-sdk"], "high_signal": True},
    {"name": "mcp Servers Releases", "url": "https://github.com/modelcontextprotocol/servers/releases.atom", "tags": ["releases", "mcp"]},
    {"name": "mcp Python SDK Releases", "url": "https://github.com/modelcontextprotocol/python-sdk/releases.atom", "tags": ["releases", "mcp"]},
    {"name": "mcp TypeScript SDK Releases", "url": "https://github.com/modelcontextprotocol/typescript-sdk/releases.atom", "tags": ["releases", "mcp"]},
    {"name": "vercel-ai Releases", "url": "https://github.com/vercel/ai/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "continue Releases", "url": "https://github.com/continuedev/continue/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "e2b Releases", "url": "https://github.com/e2b-dev/E2B/releases.atom", "tags": ["releases", "sandbox"]},
    {"name": "opencode Releases", "url": "https://github.com/anomalyco/opencode/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "gemini CLI Releases", "url": "https://github.com/google-gemini/gemini-cli/releases.atom", "tags": ["releases", "coding-agent"]},
    # Harness ecosystem widening (2026-06)
    {"name": "GitHub Copilot Changelog", "url": "https://github.blog/changelog/label/copilot/feed/", "tags": ["coding-agent", "official"], "high_signal": True},
    {"name": "Aider Releases", "url": "https://github.com/Aider-AI/aider/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Cline Releases", "url": "https://github.com/cline/cline/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Roo Code Releases", "url": "https://github.com/RooCodeInc/Roo-Code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Goose Releases", "url": "https://github.com/aaif-goose/goose/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "OpenHands Releases", "url": "https://github.com/All-Hands-AI/OpenHands/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Crush Releases", "url": "https://github.com/charmbracelet/crush/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Qwen Code Releases", "url": "https://github.com/QwenLM/qwen-code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Smolagents Releases", "url": "https://github.com/huggingface/smolagents/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Claude Agent SDK Python Releases", "url": "https://github.com/anthropics/claude-agent-sdk-python/releases.atom", "tags": ["releases", "agent-sdk"], "high_signal": True},
    {"name": "Claude Agent SDK TypeScript Releases", "url": "https://github.com/anthropics/claude-agent-sdk-typescript/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "Zed Blog", "url": "https://zed.dev/blog.rss", "tags": ["coding-agent", "official"]},
    # claude.dev/rss.xml (verified 2026-10-04). Not a changelog: posts are Read.
    {"name": "claude.dev Blog", "url": "https://claude.dev/rss.xml", "tags": ["official", "coding-agent"]},
    # 2026-06-12 widening round 2 — vendor changelogs/blogs (all endpoint-verified)
    {"name": "Devin Release Notes", "url": "https://docs.devin.ai/release-notes/overview/rss.xml", "tags": ["coding-agent", "official"], "high_signal": True},
    {"name": "Factory Release Notes", "url": "https://docs.factory.ai/changelog/release-notes/rss.xml", "tags": ["coding-agent", "official"]},
    {"name": "Amp News", "url": "https://ampcode.com/news.rss", "tags": ["coding-agent", "official"], "high_signal": True},
    # Windsurf Blog removed 2026-09-23: stale since 2026-05-12 (last item
    # "Opus 4.7 (fast mode) is now available in Windsurf"). Devin Release Notes
    # is the live Cognition changelog.
    {"name": "Warp Blog", "url": "https://www.warp.dev/blog/feed.xml", "tags": ["coding-agent"]},
    {"name": "Replit Blog", "url": "https://blog.replit.com/feed.xml", "tags": ["coding-agent"]},
    {"name": "Augment Code Blog", "url": "https://augmentcode.com/blog/rss.xml", "tags": ["coding-agent"]},
    {"name": "JetBrains AI Blog", "url": "https://blog.jetbrains.com/ai/feed/", "tags": ["coding-agent", "official"]},
    {"name": "JetBrains Junie Blog", "url": "https://blog.jetbrains.com/junie/feed/", "tags": ["coding-agent", "official"]},
    {"name": "Sourcegraph Blog", "url": "https://sourcegraph.com/blog/rss.xml", "tags": ["security", "agents"]},
    {"name": "Mistral AI Blog", "url": "https://mistral.ai/rss.xml", "tags": ["official", "models"]},
    {"name": "lobste.rs AI", "url": "https://lobste.rs/t/ai.rss", "tags": ["community", "technical"]},
    {"name": "IndyDevDan (YouTube)", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x36zCEGilGpB1m-V4gmjg", "tags": ["coding-agent", "video"]},
    # Core SDK releases — model-id constants and feature flags land here first
    {"name": "anthropic-sdk-python Releases", "url": "https://github.com/anthropics/anthropic-sdk-python/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "anthropic-sdk-typescript Releases", "url": "https://github.com/anthropics/anthropic-sdk-typescript/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "openai-python Releases", "url": "https://github.com/openai/openai-python/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "python-genai Releases", "url": "https://github.com/googleapis/python-genai/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "Agent Client Protocol Releases", "url": "https://github.com/agentclientprotocol/agent-client-protocol/releases.atom", "tags": ["releases", "agent-sdk"]},
    # 2026-09 harness widening — first-party release atoms, all endpoint-verified.
    # Feed names stay compound so repo_name_from_feed never keys on substring
    # traps (bare "pi"⊂picoclaw/api, "agno"⊂agnostic, "tau"/"kilo"/"vibe").
    {"name": "Pi Coding Agent Releases", "url": "https://github.com/earendil-works/pi/releases.atom", "tags": ["releases", "coding-agent"]},
    # npm @deepseek-ai/dsh publishes from this repo (verified 2026-10-04).
    # Current tags are alphas; the existing prerelease filter still drops those.
    {"name": "DeepSeek Harness Releases", "url": "https://github.com/deepseek-ai/deepseek-harness/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Oh My Pi Releases", "url": "https://github.com/can1357/oh-my-pi/releases.atom", "tags": ["releases", "coding-agent"]},
    # Compound name: bare "fx" is a substring trap (firefox). 0.0.x patch tags
    # are low-signal and land in Read, not Ship. A .0 minor still Ships.
    # Verified atom 2026-09-23.
    {"name": "fx Coding Agent Releases", "url": "https://github.com/vercel-labs/fx/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Herdr Releases", "url": "https://github.com/herdrdev/herdr/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Kilo Code Releases", "url": "https://github.com/Kilo-Org/kilocode/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Kimi Code Releases", "url": "https://github.com/MoonshotAI/kimi-code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Mistral Vibe Releases", "url": "https://github.com/mistralai/mistral-vibe/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Open Interpreter Releases", "url": "https://github.com/openinterpreter/openinterpreter/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Deep Agents Releases", "url": "https://github.com/langchain-ai/deepagents/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Codewhale Releases", "url": "https://github.com/Hmbown/CodeWhale/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "MiMo Code Releases", "url": "https://github.com/XiaomiMiMo/MiMo-Code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "AGNO-AGI Releases", "url": "https://github.com/agno-agi/agno/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Tau Coding Agent Releases", "url": "https://github.com/huggingface/tau/releases.atom", "tags": ["releases", "coding-agent"]},
    # Status-page history (2026-10-07). OpenAI and OpenRouter still 403.
    # classify_rss keeps partial/major/elevated incidents on Claude, Cursor,
    # and GitHub Copilot/Actions/API. Empty days are healthy (source "status").
    {"name": "Claude Status", "url": "https://status.claude.com/history.rss", "tags": ["status", "official"]},
    {"name": "Cursor Status", "url": "https://status.cursor.com/history.rss", "tags": ["status", "official"]},
    {"name": "GitHub Status", "url": "https://www.githubstatus.com/history.rss", "tags": ["status", "official"]},
    # Leak and launch wire. Relevance is coding-tool/model only; leak headlines
    # are marked reported so the writer does not state them as launches.
    {"name": "TestingCatalog", "url": "https://testingcatalog.com/rss/", "tags": ["leak", "models"]},
    # Kilo's GitHub atom missed "Introducing Kilo Desktop". Product posts Ship;
    # the Substack's essays do not.
    {"name": "Kilo Blog", "url": "https://blog.kilo.ai/feed", "tags": ["coding-agent", "official"], "high_signal": True},
    # Third-party changelog backstop. Vendor URL when the entry has one.
    # Dedupe against primary atoms happens in collect (prefer the vendor item).
    {"name": "Havoptic Releases", "url": "https://havoptic.com/feed.xml", "tags": ["releases", "aggregator"]},
]

# Keywords for relevance filtering (lowercase)
RELEVANCE_KEYWORDS = [
    # Core ecosystem
    "openclaw", "claw", "ai agent", "hermes agent", "nemoclaw", 
    "moltis", "ironclaw", "nanoclaw", "picoclaw", "openfang", "nanobot",
    "mcp", "personal ai", "coding agent", "claude code", "codex",
    "agentic", "moltbook", "clawhub", "skill marketplace", "agent security",
    # Agent/LLM fundamentals
    "llm agent", "agent framework", "tool use", "function calling",
    "prompt engineering", "rag", "vector database", "embedding",
    "safety alignment", "rlhf", "constitution", "guardrails",
    "open weights", "open source model", "local llm", "self-hosted",
    # Major model families & launches
    "gpt-", "o1", "o3", "o4", "claude", "gemini", "llama", "mistral",
    "deepseek", "codestral", "qwen", "gemma", "phi-",
    "grok", "command r", "dbrx", "jamba",
    # Major AI events & releases
    "model release", "model launch", "model announcement",
    "frontier model", "foundation model", "large language model",
    "multimodal", "vision language", "code generation",
    # AI industry & research
    "ai regulation", "ai policy", "ai safety",
    "chatgpt", "copilot", "perplexity",
    "benchmark", "leaderboard", "eval",
    "reasoning model", "chain of thought", "thinking model",
    "fine-tune", "rlhf", "dpo", "distill",
    # Infrastructure
    "inference", "serving", "deployment", "quantization",
    "on-device", "edge ai", "ai chip", "gpu shortage", "datacenter",
    # Harness ecosystem (2026-06 widening). Substring-matched — only
    # unambiguous tokens here (bare "amp"/"cline"/"zed" match inside
    # ordinary words). Release feeds bypass this gate entirely.
    "subagent", "agent harness", "coding harness", "windsurf",
    "roo code", "claude agent sdk", "agent sdk", "computer use",
    "context engineering", "mcp server", "github copilot",
    "zed editor", "warp terminal", "openhands", "smolagents",
    # 2026-06-12 round 2: vendor blog vocabulary (anchored compounds for
    # substring-trap tokens; see READ_TERMS note in clawbytes_threads.py)
    "opus 4", "opus 5", "devin", "junie", "mellum", "codestral",
    "replit agent", "agent mode", "augment code", "amp news",
    "warp blog", "jetbrains", "sourcegraph",
    "devin desktop", "antigravity", "agent client protocol",
    # 2026-09 widening. Compounds only — bare "pi"/"agno"/"kilo"/"tau"/"vibe"
    # live inside common words (picoclaw, agnostic, kilobyte, status-adjacent).
    "kiro", "kilo code", "kimi code", "mistral vibe", "grok build",
    "pi coding", "pi-mono", "oh-my-pi", "oh my pi", "omp.sh", "herdr",
    "fx coding",  # never bare "fx" — ⊂ firefox
    "open interpreter", "deep agents", "deepagents",
    "codewhale", "mimo code", "agno-agi", "tau coding", "tau-ai",
]

# ArXiv cs.AI / cs.CL: bare "agent" is too wide. Require a harness compound.
# Feed names must not contain "releases" or this gate never runs.
ARXIV_HARNESS_TERMS = (
    "coding agent",
    "coding harness",
    "agent harness",
    "tool use",
    "tool-use",
    "function calling",
    "claude code",
    "mcp",
    "subagent",
    "computer use",
)

def load_state():
    """Load state from file or return default."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"lastSeenByFeed": {}, "lastCheck": None, "foundItems": []}

def save_state(state):
    """Save state atomically (temp file + rename). A torn write aborts collect."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)

def fetch_feed(url, timeout=15):
    """Fetch RSS/Atom feed content."""
    headers = {
        "User-Agent": "ClawBytes/1.0 (RSS Monitor; +https://github.com/ClawBack1)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read()
            # Some feed hosts gzip the body without being asked; urllib
            # hands it back still compressed.
            if response.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body.decode("utf-8")
    except HTTPError as e:
        print(f"  HTTP {e.code}: {url}")
        return None
    except URLError as e:
        print(f"  URL Error: {e.reason}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

def parse_atom(xml_text):
    """Parse Atom feed entries."""
    entries = []
    try:
        # Handle namespaces
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        
        # Try with namespace first
        for entry in root.findall("atom:entry", ns):
            title = entry.find("atom:title", ns)
            link = entry.find("atom:link[@rel='alternate']", ns)
            if link is None:
                link = entry.find("atom:link", ns)
            published = entry.find("atom:published", ns)
            if published is None:
                published = entry.find("atom:updated", ns)
            summary = entry.find("atom:summary", ns)
            if summary is None:
                summary = entry.find("atom:content", ns)
            entry_id = entry.find("atom:id", ns)
            detail = element_text(summary)
            
            entries.append({
                "title": (title.text or "").strip() if title is not None else "",
                "link": link.get("href") if link is not None else "",
                "published": published.text if published is not None else "",
                "summary": detail[:500],
                "detail": detail[:8000],
                "id": entry_id.text if entry_id is not None else ""
            })
        
        # Fallback without namespace
        if not entries:
            for entry in root.findall(".//entry"):
                title = entry.find("title")
                link = entry.find("link[@rel='alternate']") or entry.find("link")
                published = entry.find("published") or entry.find("updated")
                summary = entry.find("summary") or entry.find("content")
                entry_id = entry.find("id")
                detail = element_text(summary)
                
                entries.append({
                    "title": (title.text or "").strip() if title is not None else "",
                    "link": link.get("href") if link is not None else "",
                    "published": published.text if published is not None else "",
                    "summary": detail[:500],
                    "detail": detail[:8000],
                    "id": entry_id.text if entry_id is not None else ""
                })
    except ET.ParseError as e:
        print(f"  Parse error: {e}")
    return entries

def parse_rss(xml_text):
    """Parse RSS 2.0 feed entries."""
    entries = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item"):
            title = item.find("title")
            link = item.find("link")
            pubDate = item.find("pubDate")
            description = item.find("description")
            guid = item.find("guid")
            detail = element_text(description)
            categories = [element_text(node).strip() for node in item.findall("category")]
            categories = [node for node in categories if node]
            
            entries.append({
                "title": (title.text or "").strip() if title is not None else "",
                "link": link.text if link is not None else "",
                "published": pubDate.text if pubDate is not None else "",
                "summary": detail[:500],
                "detail": detail[:8000],
                "id": guid.text if guid is not None else (link.text if link is not None else ""),
                "category": categories[0] if categories else "",
                "categories": categories,
            })
    except ET.ParseError as e:
        print(f"  Parse error: {e}")
    return entries

def parse_feed(xml_text):
    """Parse feed (auto-detect Atom vs RSS)."""
    if xml_text is None:
        return []
    
    # Detect format
    if "<feed" in xml_text[:500]:
        return parse_atom(xml_text)
    elif "<rss" in xml_text[:500] or "<channel>" in xml_text[:500]:
        return parse_rss(xml_text)
    else:
        # Try both
        entries = parse_atom(xml_text)
        if not entries:
            entries = parse_rss(xml_text)
        return entries

def is_relevant(entry, feed_name, tags=None):
    """Check if entry is relevant to OpenClaw ecosystem."""
    # Release, release-notes, and coding-agent changelog/news feeds are always
    # relevant: their entry titles are versions, dates, or feature names that
    # often carry no keywords. Status history, TestingCatalog, and Kilo Blog
    # have their own gates below; they are not release feeds.
    # Do NOT bypass the generic "GitHub Changelog" — only Cursor/Copilot
    # changelogs and coding-agent-tagged news/changelog feeds.
    low_name = feed_name.lower()
    tagset = {str(t).lower() for t in (tags or [])}
    if any(marker in low_name for marker in ("releases", "release notes")):
        return True
    if "coding-agent" in tagset and (
        "changelog" in low_name or low_name.endswith(" news") or " news" in f" {low_name}"
    ):
        return True
    if "changelog" in low_name and any(v in low_name for v in ("cursor", "copilot")):
        return True
    if low_name == "amp news":
        return True
    # First-party Claude build log. Post titles often omit the keyword list
    # ("Building with Claude Sonnet 5.5") and would otherwise never be emitted.
    if low_name == "claude.dev blog":
        return True
    # Status history is a firehose of maintenance and minute-long blips.
    # These feed names do not contain "releases", so this gate is what runs.
    if feed_name in _STATUS_FEEDS:
        return status_incident_allowed(feed_name, entry)
    if low_name == "testingcatalog":
        return testingcatalog_relevant(entry)
    if low_name == "kilo blog":
        return kilo_product_post(entry)

    # ArXiv is a research firehose. Bare "agent" (and the general keyword
    # list) lets adjacent ML through. Require a harness compound.
    if low_name in ("arxiv cs.ai", "arxiv cs.cl"):
        text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
        return any(term in text for term in ARXIV_HARNESS_TERMS)
    
    # Check title and summary for keywords
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    
    for keyword in RELEVANCE_KEYWORDS:
        if keyword in text:
            return True
    
    return False


def _log_fetch(name, detail):
    label = _FETCH_LOG_NAMES.get(name)
    if label:
        print(f"INFO {label}: {detail}", flush=True)


def check_feeds(filter_relevant=True, verbose=True):
    """Check all RSS feeds for new content."""
    state = load_state()
    new_items = []
    feed_status = {}
    
    # Merge dynamic feeds with hardcoded feeds
    dynamic_path = MEMORY_DIR / "clawbytes-dynamic-feeds.json"
    all_feeds = list(RSS_FEEDS)
    if dynamic_path.exists():
        try:
            dynamic = json.loads(dynamic_path.read_text())
            for feed in dynamic.get("rss_feeds", []):
                # Skip if URL already in hardcoded feeds
                if feed.get("url") not in {f["url"] for f in all_feeds}:
                    all_feeds.append(feed)
        except Exception:
            pass
    
    status_failures = []
    saw_status_feed = False
    for feed in all_feeds:
        name = feed["name"]
        url = feed["url"]
        tags = feed.get("tags", [])
        high_signal = feed.get("high_signal", False)
        if name in _STATUS_FEEDS:
            saw_status_feed = True
        
        if verbose:
            print(f"\n📡 Checking: {name}")
        
        xml_text = fetch_feed(url)
        
        if xml_text is None:
            feed_status[name] = "failed"
            if name in _STATUS_FEEDS:
                status_failures.append(name)
            continue
        
        entries = parse_feed(xml_text)
        if name in _STATUS_FEEDS:
            # One story per incident, before the baseline id list is written.
            entries = collapse_status_entries(entries)
        elif name == "Havoptic Releases":
            entries = [shaped for shaped in (normalize_havoptic_entry(entry) for entry in entries) if shaped]
        if verbose:
            print(f"  Found {len(entries)} entries")
        
        feed_status[name] = f"ok ({len(entries)} entries)"
        
        # First sighting of a feed name records ids and emits nothing.
        # Otherwise a newly added atom dumps its in-TTL backlog as news.
        seen_map = state.setdefault("lastSeenByFeed", {})
        if name not in seen_map:
            seen_ids = [e.get("id") or e.get("link") for e in entries[:50]]
            seen_map[name] = seen_ids
            _log_fetch(name, "baseline written")
            if verbose:
                print(f"  baseline recorded ({len(seen_ids)} ids), emitting nothing")
            continue

        last_seen = seen_map.get(name, [])
        # Havoptic mixes every tool. A Cursor release can sit past the
        # usual 10-item window on a busy day.
        scan_limit = 20 if name == "Havoptic Releases" else 10
        
        for entry in entries[:scan_limit]:
            entry_id = entry.get("id") or entry.get("link") or entry.get("title")
            
            if not entry_id or entry_id in last_seen:
                continue
            
            # Check relevance
            if filter_relevant and not is_relevant(entry, name, tags):
                continue

            item = {
                "feed": entry.get("_feed") or name,
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "tags": tags,
                "high_signal": high_signal,
                "id": entry_id,
                "found_at": datetime.now(timezone.utc).isoformat()
            }
            if name in _STATUS_FEEDS:
                item["detail"] = entry.get("detail") or ""
            if name == "TestingCatalog":
                item["reported"] = is_reported_claim(entry.get("title") or "", entry.get("summary") or "")
            if entry.get("aggregator"):
                item["aggregator"] = entry["aggregator"]
            if entry.get("categories"):
                item["categories"] = entry["categories"]
            new_items.append(item)
            
            if verbose:
                signal = "🔥 HIGH SIGNAL: " if high_signal else "  → "
                print(f"{signal}{entry.get('title', 'No title')[:60]}")

        _log_fetch(name, f"{len(entries)} items")

        # Update last seen (keep last 50 IDs per feed)
        seen_ids = [e.get("id") or e.get("link") for e in entries[:50]]
        seen_map[name] = seen_ids
    
    # Store new items for digest
    if new_items:
        state["foundItems"] = state.get("foundItems", []) + new_items
        # Keep only last 7 days of items
        state["foundItems"] = state["foundItems"][-500:]
    
    save_state(state)
    if saw_status_feed:
        status_new = sum(1 for item in new_items if item.get("feed") in _STATUS_FEEDS)
        if status_failures:
            err = ", ".join(status_failures) + " fetch failed"
            print(f"STATUS_HEALTH status=error items={status_new} reason={err}", flush=True)
        elif status_new == 0:
            print("STATUS_HEALTH status=empty items=0 reason=-", flush=True)
        else:
            print(f"STATUS_HEALTH status=ok items={status_new} reason=-", flush=True)
    
    return new_items, feed_status

def format_telegram_message(items):
    """Format new items for Telegram."""
    if not items:
        return None
    
    lines = ["📰 *New OpenClaw Content*\n"]
    
    # Group by high signal vs regular
    high_signal = [i for i in items if i.get("high_signal")]
    regular = [i for i in items if not i.get("high_signal")]
    
    if high_signal:
        lines.append("🔥 *High Signal*")
        for item in high_signal[:5]:
            title = item["title"][:80].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"• [{title}]({item['link']})")
        lines.append("")
    
    if regular:
        lines.append("📋 *Latest*")
        for item in regular[:10]:
            title = item["title"][:80].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"• [{title}]({item['link']})")
    
    lines.append("\n#ClawBytes #RSS")
    return "\n".join(lines)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor RSS feeds for OpenClaw content")
    parser.add_argument("--all", action="store_true", help="Show all entries, not just relevant ones")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--telegram", action="store_true", help="Output Telegram message format")
    parser.add_argument("--status", action="store_true", help="Show feed status only")
    args = parser.parse_args()
    
    if args.status:
        _, status = check_feeds(filter_relevant=False, verbose=False)
        print("\n📊 Feed Status:")
        for name, s in status.items():
            emoji = "✅" if "ok" in s else "❌"
            print(f"  {emoji} {name}: {s}")
        return
    
    new_items, status = check_feeds(
        filter_relevant=not args.all, 
        verbose=not args.quiet
    )
    
    print(f"\n{'='*50}")
    print(f"Found {len(new_items)} new relevant items")
    
    if args.telegram and new_items:
        msg = format_telegram_message(new_items)
        print(f"\n--- Telegram Message ---\n{msg}")
    
    # Summary
    working = sum(1 for s in status.values() if "ok" in s)
    print(f"\nFeeds: {working}/{len(RSS_FEEDS)} working")

if __name__ == "__main__":
    main()
