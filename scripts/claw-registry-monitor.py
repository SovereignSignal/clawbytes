#!/usr/bin/env python3
"""
ClawBytes Registry Monitor
Watches machine registries where new models appear before any blog covers
them, and emits an item per genuinely-new entry:

1. OpenRouter /api/v1/models — id-set diff every run; a new listing means a
   model became callable, often within minutes of release.
2. LiteLLM model_prices_and_context_window.json — cross-provider pricing
   registry (2,700+ entries); sha-gated via the GitHub contents API, key
   diff on change. Catches Azure/Bedrock/Vertex SKUs before press.
3. Hugging Face trending models — weekly top-20 diff, filtered to coding/
   agent models created in the last 30 days.

First sighting of each registry records a baseline and emits nothing.

State file: memory/claw-registry-state.json
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-registry-state.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
LITELLM_REPO = "BerriAI/litellm"
LITELLM_PATH = "model_prices_and_context_window.json"
HF_TRENDING_URL = "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=20"

MAX_ITEMS_PER_RUN = 5          # per registry, guards against listing floods
HF_CHECK_EVERY_DAYS = 6        # trending churns daily; weekly diff is signal
HF_CODE_AGENT = re.compile(r"code|coder|coding|agent|cli|swe", re.IGNORECASE)


def _fetch_json(url, headers=None, timeout=30):
    base = {"User-Agent": "ClawBytes/1.0"}
    base.update(headers or {})
    req = Request(url, headers=base)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _github_headers():
    headers = {"User-Agent": "ClawBytes/1.0", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def diff_new_keys(old_keys, new_keys):
    """New entries vs baseline; empty old set = baseline, never news."""
    if not old_keys:
        return []
    return sorted(set(new_keys) - set(old_keys))


def hf_trending_picks(models, known_ids, now=None):
    """New-to-top-20 coding/agent models created within the last 30 days."""
    now = now or datetime.now(timezone.utc)
    picks = []
    for model in models:
        model_id = model.get("id") or ""
        if not model_id or model_id in known_ids:
            continue
        haystack = " ".join([model_id] + [str(t) for t in model.get("tags", [])])
        if not HF_CODE_AGENT.search(haystack):
            continue
        created = model.get("createdAt") or ""
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - created_dt > timedelta(days=30):
            continue
        picks.append(model)
    return picks


def _item(registry, key, title, url, summary, now_iso):
    day = now_iso[:10].replace("-", "")
    return {
        "id": f"{registry}:{key}",
        "registry": registry,
        "title": title,
        "url": url,
        "summary": summary,
        "found_at": now_iso,
    }


def check_openrouter(state, now_iso, verbose):
    try:
        models = _fetch_json(OPENROUTER_URL).get("data", [])
    except Exception as e:
        if verbose:
            print(f"  ! OpenRouter fetch failed: {e}")
        return []
    by_id = {m.get("id"): m for m in models if m.get("id")}
    old = state.get("openrouterIds", [])
    new_ids = diff_new_keys(old, by_id.keys())
    items = []
    for model_id in new_ids[:MAX_ITEMS_PER_RUN]:
        model = by_id[model_id]
        name = model.get("name") or model_id
        desc = (model.get("description") or "").split(".")[0][:140]
        items.append(_item(
            "OpenRouter", model_id,
            f"{name} now live on OpenRouter",
            f"https://openrouter.ai/{model_id}",
            desc or "New model listing",
            now_iso,
        ))
    if len(new_ids) > MAX_ITEMS_PER_RUN and verbose:
        print(f"  ⚠️ OpenRouter: {len(new_ids)} new listings, emitting first {MAX_ITEMS_PER_RUN}")
    state["openrouterIds"] = sorted(by_id.keys())
    if verbose:
        print(f"  = OpenRouter: {len(by_id)} models, {len(new_ids)} new" + (" (baseline)" if not old else ""))
    return items if old else []


def check_litellm(state, now_iso, verbose):
    meta_url = f"https://api.github.com/repos/{LITELLM_REPO}/contents/{LITELLM_PATH}"
    try:
        sha = _fetch_json(meta_url, headers=_github_headers()).get("sha", "")
    except Exception:
        sha = ""
    if sha and sha == state.get("litellmSha") and state.get("litellmKeys"):
        if verbose:
            print("  = LiteLLM registry: unchanged (sha match)")
        return []
    try:
        data = _fetch_json(f"https://raw.githubusercontent.com/{LITELLM_REPO}/main/{LITELLM_PATH}", timeout=60)
    except Exception as e:
        if verbose:
            print(f"  ! LiteLLM fetch failed: {e}")
        return []
    keys = [k for k in data.keys() if k != "sample_spec"]
    old = state.get("litellmKeys", [])
    new_keys = diff_new_keys(old, keys)
    items = []
    if old and new_keys:
        shown = ", ".join(new_keys[:6]) + ("…" if len(new_keys) > 6 else "")
        items.append(_item(
            "LiteLLM registry", f"batch:{now_iso[:10]}:{len(new_keys)}",
            f"{len(new_keys)} new model(s) priced in the LiteLLM registry",
            "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
            shown,
            now_iso,
        ))
    state["litellmKeys"] = keys
    if sha:
        state["litellmSha"] = sha
    if verbose:
        print(f"  = LiteLLM registry: {len(keys)} entries, {len(new_keys)} new" + (" (baseline)" if not old else ""))
    return items


def check_hf_trending(state, now_iso, verbose):
    last = state.get("hfTrendingCheckedAt")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last_dt < timedelta(days=HF_CHECK_EVERY_DAYS):
                if verbose:
                    print("  = HF trending: checked recently, skipping")
                return []
        except ValueError:
            pass
    try:
        models = _fetch_json(HF_TRENDING_URL)
    except Exception as e:
        if verbose:
            print(f"  ! HF trending fetch failed: {e}")
        return []
    known = set(state.get("hfTrendingSeen", []))
    first_run = not known
    picks = hf_trending_picks(models, known)
    items = []
    for model in picks[:MAX_ITEMS_PER_RUN]:
        model_id = model["id"]
        items.append(_item(
            "HF trending", model_id,
            f"{model_id} trending on Hugging Face",
            f"https://huggingface.co/{model_id}",
            f"New coding/agent model — {model.get('downloads', 0)} downloads, {model.get('likes', 0)} likes",
            now_iso,
        ))
    state["hfTrendingSeen"] = sorted(known | {m.get("id") for m in models if m.get("id")})[-500:]
    state["hfTrendingCheckedAt"] = datetime.now(timezone.utc).isoformat()
    if verbose:
        print(f"  = HF trending: {len(picks)} qualifying new entrant(s)" + (" (baseline)" if first_run else ""))
    return items if not first_run else []


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_registries(verbose=True):
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []
    new_items += check_openrouter(state, now_iso, verbose)
    new_items += check_litellm(state, now_iso, verbose)
    new_items += check_hf_trending(state, now_iso, verbose)
    if new_items:
        state["foundItems"] = (state.get("foundItems", []) + new_items)[-200:]
    save_state(state)
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor model registries")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    items = check_registries(verbose=not args.quiet)
    print(f"Registries: {len(items)} new item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
