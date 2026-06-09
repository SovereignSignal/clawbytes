#!/usr/bin/env python3
"""
ClawBytes HuggingFace Papers Monitor
Monitors HF Daily Papers for AI agent/LLM/model research and releases.

State file: memory/claw-hf-state.json
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-hf-state.json"

# HF API endpoints
DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"
PAPERS_URL = "https://huggingface.co/api/papers"

# Relevance keywords — used to score each paper for our ecosystem
RELEVANCE_KEYWORDS = {
    "agent": 3,
    "agents": 3,
    "mcp": 3,
    "autonomous": 3,
    "multi-agent": 3,
    "tool use": 2,
    "llm": 2,
    "language model": 2,
    "foundation model": 2,
    "distillation": 2,
    "distill": 2,
    "rl": 2,
    "reinforcement": 2,
    "reasoning": 2,
    "multimodal": 1,
    "architecture": 1,
    "efficient": 1,
    "inference": 1,
    "training": 1,
    "guardrail": 2,
    "safety": 2,
    "security": 2,
    "vulnerability": 2,
    "prompt injection": 2,
    "hallucination": 1,
    "openclaw": 3,
    "claw": 1,
    "codex": 1,
    "claude": 1,
}

# Lane mapping rules
LANE_RULES = [
    # Security / watch lane — highest priority. Keep this narrow so broad safety
    # papers don't crowd the operational Watch lane.
    {"lane": "watch", "keywords": ["security", "vulnerability", "exploit", "prompt injection", "jailbreak", "sandbox", "unauthorized", "adversarial"], "min_score": 2},
    # Read lane — architecture, distillation, reasoning, agents, tooling.
    {"lane": "read", "keywords": ["agent", "tool", "distill", "architecture", "reasoning", "rl", "reinforcement", "efficient", "multimodal", "mcp", "benchmark"], "min_score": 3, "min_upvotes": 3},
    # Community lane — high-attention papers that don't fit Read/Watch cleanly.
    {"lane": "community", "keywords": [], "min_score": 2, "min_upvotes": 12},
]


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seenIds": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_hf_papers(url=DAILY_PAPERS_URL, timeout=20):
    """Fetch papers from HF API."""
    req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  HF Papers fetch error: {e}")
        return None


def score_paper(paper):
    """Score paper relevance for our ecosystem."""
    title = (paper.get("title") or "").lower()
    summary = (paper.get("summary") or "").lower()
    ai_summary = (paper.get("ai_summary") or "").lower()
    ai_keywords = [k.lower() for k in paper.get("ai_keywords", [])]
    text = f"{title} {summary} {ai_summary} {' '.join(ai_keywords)}"
    score = sum(weight for kw, weight in RELEVANCE_KEYWORDS.items() if kw in text)
    return score, text


def assign_lane(paper, score, text):
    """Assign paper to a lane based on rules."""
    upvotes = paper.get("upvotes") or 0
    has_repo = bool(paper.get("githubRepo"))
    title_low = paper.get("title", "").lower()
    
    for rule in LANE_RULES:
        lane = rule["lane"]
        rule_kws = rule.get("keywords", [])
        min_score = rule.get("min_score", 0)
        min_upvotes = rule.get("min_upvotes", 0)
        requires_repo = rule.get("requires_repo", False)
        
        if score < min_score:
            continue
        if upvotes < min_upvotes:
            continue
        if requires_repo and not has_repo:
            continue
        if rule_kws:
            text_lower = text.lower()
            if not any(kw in text_lower for kw in rule_kws):
                continue
        return lane
    return None


def check_hf_papers(verbose=True):
    """Check HF Daily Papers for new relevant items."""
    state = load_state()
    seen_ids = set(state.get("seenIds", []))
    new_items = []
    
    data = fetch_hf_papers()
    if not data:
        if verbose:
            print("  No data from HF Papers API")
        return []
    
    if verbose:
        print(f"  Checking {len(data)} HF daily papers...")
    
    for item in data:
        paper = item.get("paper") or item  # daily_papers wraps in "paper" key
        if not paper:
            continue
            
        paper_id = paper.get("id") or paper.get("_id")
        if not paper_id or paper_id in seen_ids:
            continue
        
        # Score relevance
        score, text = score_paper(paper)
        if score < 1:
            continue  # Not relevant enough
        
        upvotes = paper.get("upvotes") or 0
        title = paper.get("title", "").strip()
        lane = assign_lane(paper, score, text)
        
        if not lane:
            # Low-bar fallback for high-upvote items
            if upvotes >= 30:
                lane = "community"
            else:
                continue
        
        paper_url = f"https://huggingface.co/papers/{paper_id}"
        github = paper.get("githubRepo")
        project_page = paper.get("projectPage")
        
        item_out = {
            "id": f"hf-{paper_id}",
            "title": title,
            "url": paper_url,
            "hf_id": paper_id,
            "githubRepo": github,
            "projectPage": project_page,
            "upvotes": upvotes,
            "score": score,
            "publishedAt": paper.get("publishedAt"),
            "found_at": datetime.now(timezone.utc).isoformat(),
            "category_hint": lane,
            "sourceType": "hf_papers",
            "ai_summary": paper.get("ai_summary"),
        }
        new_items.append(item_out)
        seen_ids.add(paper_id)
    
    # Update state
    state["seenIds"] = list(seen_ids)[-2000:]
    state["foundItems"] = (state.get("foundItems", []) + new_items)[-500:]
    save_state(state)
    
    if verbose:
        print(f"  HF Papers: {len(new_items)} new items found")
        by_lane = {}
        for it in new_items:
            by_lane.setdefault(it["category_hint"], 0)
            by_lane[it["category_hint"]] += 1
        for lane, count in sorted(by_lane.items()):
            print(f"    {lane}: {count}")
    
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor HuggingFace Papers for AI agent content")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        state = load_state()
        print(f"Last check: {state.get('lastCheck')}")
        print(f"Seen IDs: {len(state.get('seenIds', []))}")
        print(f"Found items: {len(state.get('foundItems', []))}")
        return

    new_items = check_hf_papers(verbose=not args.quiet)
    
    if args.quiet:
        print(json.dumps(new_items))
    else:
        print(f"Found {len(new_items)} new HF Papers items")


if __name__ == "__main__":
    main()
