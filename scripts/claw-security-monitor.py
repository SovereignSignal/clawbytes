#!/usr/bin/env python3
"""
ClawBytes Security Monitor
HIGH PRIORITY: Monitors for security incidents in OpenClaw ecosystem.

Security findings always get posted regardless of other filters.

State file: memory/claw-security-state.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus

# Workspace path
WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-security-state.json"
CREDS_FILE = WORKSPACE / "CREDS.md"

# Security search queries
SECURITY_QUERIES = [
    "openclaw security vulnerability 2026",
    "openclaw CVE",
    "clawhub malicious skill",
    "claw agent security issue",
    "openclaw supply chain attack",
    "openclaw compromised",
    "nanoclaw security vulnerability",
    "ironclaw security issue",
    "mcp server malicious",
]

# GitHub repos to check for security advisories
WATCHED_REPOS = [
    # Claw ecosystem
    "openclaw/openclaw",
    "RightNow-AI/openfang",
    "sipeed/picoclaw",
    "qwibitai/nanoclaw",
    "nearai/ironclaw",
    "moltis-org/moltis",
    "NousResearch/hermes-agent",
    # Adjacent AI agent ecosystem
    "anthropic/claude-code",
    "openai/codex",
    "langchain-ai/langchain",
    "langchain-ai/langgraph",
    "pytorch/pytorch",
    "huggingface/transformers",
    "modelcontextprotocol/servers",
    # High-value agent/automation repos (from 2026 top list)
    "Significant-Gravitas/AutoGPT",
    "geekan/MetaGPT",
    "crewAIInc/crewAI",
    "langflow-ai/langflow",
    "langgenius/dify",
    "browser-use/browser-use",
    "mem0ai/mem0",
    "composiohq/composio",
    # Model/inference
    "vllm-project/vllm",
    "ollama/ollama",
    "deepseek-ai/DeepSeek-V3",
    "QwenLM/Qwen",
]

# Security keywords to flag
SECURITY_KEYWORDS = [
    "cve", "vulnerability", "malicious", "supply chain", 
    "compromised", "attack", "exploit", "backdoor",
    "rce", "injection", "xss", "csrf", "security advisory",
    "security issue", "security bug", "credential leak",
    "data breach", "unauthorized access"
]

# Brave Search API
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def get_cred(label):
    """Read a simple credential from CREDS.md."""
    if not CREDS_FILE.exists():
        return ""

    text = CREDS_FILE.read_text()
    import re

    patterns = {
        "BRAVE_API_KEY": r"## Brave Search(?: API)?\n(?:.*\n)*?-\s*(?:\*\*)?API Key(?:\*\*)?:\s*([^\n]+)",
        "GITHUB_TOKEN": r"## GitHub API\n(?:.*\n)*?-\s*Token:\s*([^\n]+)",
    }

    pattern = patterns.get(label)
    if not pattern:
        return ""

    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""

def load_state():
    """Load state from file or return default."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "seenUrls": [],
        "lastCheck": None,
        "alerts": [],
        "lastAlertAt": None,
        "allClear": True
    }

def save_state(state):
    """Save state to file."""
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def brave_search(query, api_key, count=5):
    """Search using Brave Search API."""
    import gzip
    url = f"{BRAVE_API_URL}?q={quote_plus(query)}&count={count}&freshness=pw"  # Past week
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key
    }
    req = Request(url, headers=headers)
    
    try:
        with urlopen(req, timeout=15) as response:
            content = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                content = gzip.decompress(content)
            data = json.loads(content.decode("utf-8"))
            return data.get("web", {}).get("results", [])
    except HTTPError as e:
        if e.code == 429:
            print("  ⚠️ Rate limited")
        return []
    except Exception as e:
        print(f"  Error: {e}")
        return []

def fetch_github_advisories(repo, github_token=""):
    """Fetch security advisories from GitHub repo."""
    url = f"https://api.github.com/repos/{repo}/security-advisories"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ClawBytes/1.0"
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    req = Request(url, headers=headers)
    
    try:
        with urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        # 404 = no advisories API access (common for most repos)
        if e.code == 404:
            return []
        print(f"  GitHub {e.code}: {repo}")
        return []
    except Exception as e:
        print(f"  Error: {e}")
        return []

def check_github_repo_security(repo, github_token=""):
    """Check GitHub repo's security tab via HTML scraping fallback."""
    # Try the public security advisories page
    url = f"https://github.com/{repo}/security/advisories"
    headers = {
        "User-Agent": "ClawBytes/1.0",
        "Accept": "text/html"
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    req = Request(url, headers=headers)
    
    try:
        with urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")
            
            # Look for advisory indicators
            advisories = []
            if "GHSA-" in html:
                # Extract GHSA IDs
                import re
                ghsa_ids = re.findall(r'GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}', html)
                for ghsa_id in set(ghsa_ids):
                    advisories.append({
                        "id": ghsa_id,
                        "repo": repo,
                        "url": f"https://github.com/advisories/{ghsa_id}",
                        "type": "github_advisory"
                    })
            
            return advisories
    except HTTPError as e:
        return []
    except Exception as e:
        return []

def is_security_relevant(title, description):
    """Check if content is security-related."""
    text = f"{title} {description}".lower()
    
    # Must contain security keywords
    for keyword in SECURITY_KEYWORDS:
        if keyword in text:
            return True
    
    return False

def calculate_severity(title, description):
    """Estimate severity level."""
    text = f"{title} {description}".lower()
    
    critical_keywords = ["rce", "remote code execution", "critical", "actively exploited"]
    high_keywords = ["vulnerability", "cve", "exploit", "backdoor", "security advisory"]
    watching_keywords = ["security issue", "security bug", "credential leak", "unauthorized access"]
    
    for keyword in critical_keywords:
        if keyword in text:
            return "CRITICAL"
    
    for keyword in high_keywords:
        if keyword in text:
            return "HIGH"

    for keyword in watching_keywords:
        if keyword in text:
            return "WATCHING"

    return "MEDIUM"

def check_security(api_key=None, verbose=True):
    """Run security check across all sources."""
    if not api_key:
        api_key = os.environ.get("BRAVE_API_KEY") or get_cred("BRAVE_API_KEY")

    github_token = os.environ.get("GITHUB_TOKEN") or get_cred("GITHUB_TOKEN")
    
    if not api_key:
        print("❌ BRAVE_API_KEY not set")
        return [], "no_api_key"
    
    state = load_state()
    alerts = []
    seen_urls = set(state.get("seenUrls", []))
    
    if verbose:
        print("🔒 Running Security Check...\n")
    
    # 1. Brave Search for security issues
    if verbose:
        print("📡 Searching for security mentions...")
    
    for query in SECURITY_QUERIES:
        if verbose:
            print(f"  Query: {query[:40]}...")
        
        results = brave_search(query, api_key, count=3)
        time.sleep(0.5)  # Rate limiting
        
        for result in results:
            url = result.get("url", "")
            title = result.get("title", "")
            description = result.get("description", "")
            
            if url in seen_urls:
                continue
            
            if not is_security_relevant(title, description):
                continue
            
            severity = calculate_severity(title, description)
            
            alert = {
                "type": "web_mention",
                "severity": severity,
                "title": title,
                "url": url,
                "description": description[:300],
                "query": query,
                "found_at": datetime.now(timezone.utc).isoformat()
            }
            
            alerts.append(alert)
            seen_urls.add(url)
            
            if verbose:
                emoji = "🚨" if severity == "CRITICAL" else "⚠️" if severity == "HIGH" else "ℹ️"
                print(f"  {emoji} [{severity}] {title[:50]}")
    
    # 2. Check GitHub security advisories
    if verbose:
        print("\n🐙 Checking GitHub security advisories...")
    
    for repo in WATCHED_REPOS:
        if verbose:
            print(f"  Repo: {repo}")
        
        advisories = fetch_github_advisories(repo, github_token)
        if not advisories:
            advisories = check_github_repo_security(repo, github_token)
        
        for advisory in advisories:
            adv_id = advisory.get("id", "")
            url = advisory.get("url", "")
            
            if url in seen_urls:
                continue
            
            severity = "HIGH" if adv_id else "WATCHING"
            alert = {
                "type": "github_advisory",
                "severity": severity,
                "title": f"Security Advisory: {adv_id}",
                "url": url,
                "repo": repo,
                "advisory_id": adv_id,
                "found_at": datetime.now(timezone.utc).isoformat()
            }
            
            alerts.append(alert)
            seen_urls.add(url)
            
            if verbose:
                print(f"  🚨 Advisory found: {adv_id}")
    
    # Update state
    state["seenUrls"] = list(seen_urls)[-500:]
    state["allClear"] = len(alerts) == 0
    
    if alerts:
        state["alerts"] = state.get("alerts", []) + alerts
        state["alerts"] = state["alerts"][-50:]  # Keep last 50
        state["lastAlertAt"] = datetime.now(timezone.utc).isoformat()
    
    save_state(state)
    
    return alerts, "ok"

def format_telegram_message(alerts):
    """Format security alerts for Telegram - URGENT formatting."""
    if not alerts:
        return None
    
    lines = ["🚨 *SECURITY ALERT*\n"]
    
    # Sort by severity
    severity_order = {"CRITICAL": 0, "HIGH": 1, "WATCHING": 2, "MEDIUM": 3}
    alerts_sorted = sorted(alerts, key=lambda x: severity_order.get(x.get("severity", "MEDIUM"), 3))
    
    for alert in alerts_sorted[:10]:
        severity = alert.get("severity", "MEDIUM")
        emoji = "🔴" if severity == "CRITICAL" else "🟠" if severity == "HIGH" else "🟡" if severity == "WATCHING" else "🔹"
        
        title = alert["title"][:80].replace("[", "\\[").replace("]", "\\]")
        lines.append(f"{emoji} *{severity}*: [{title}]({alert['url']})")
        
        if alert.get("repo"):
            lines.append(f"   Repo: `{alert['repo']}`")
        
        lines.append("")
    
    lines.append("⚠️ *Review immediately*")
    lines.append("\n#ClawBytes #Security #Alert")
    return "\n".join(lines)

def format_all_clear():
    """Format all-clear message."""
    return "✅ *Security Check Complete*\n\nNo new security issues detected.\n\n#ClawBytes #Security"

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor for OpenClaw security issues")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--telegram", action="store_true", help="Output Telegram message format")
    parser.add_argument("--status", action="store_true", help="Show security status")
    args = parser.parse_args()
    
    if args.status:
        state = load_state()
        print(f"Last check: {state.get('lastCheck', 'never')}")
        print(f"All clear: {state.get('allClear', True)}")
        print(f"Last alert: {state.get('lastAlertAt', 'never')}")
        print(f"Total alerts tracked: {len(state.get('alerts', []))}")
        return
    
    alerts, status = check_security(verbose=not args.quiet)
    
    print(f"\n{'='*50}")
    if alerts:
        print(f"⚠️ FOUND {len(alerts)} SECURITY ISSUES")
    else:
        print("✅ All clear - no security issues found")
    
    if args.telegram:
        if alerts:
            msg = format_telegram_message(alerts)
        else:
            msg = format_all_clear()
        print(f"\n--- Telegram Message ---\n{msg}")

if __name__ == "__main__":
    main()
