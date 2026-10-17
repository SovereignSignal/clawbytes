"""Draft highlights-reel text from a collected day using Claude (plain HTTPS).

The API key comes from ``ANTHROPIC_API_KEY`` and is only ever placed in the
request header; it is never logged. Token usage and an estimated USD cost go
to stderr after every call.
"""
from __future__ import annotations

import os
import sys

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"

# USD per million tokens (input, output). Longest prefix wins, so a dated
# snapshot id still prices. Unknown models log usage with cost "unknown".
PRICING = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

MAX_PROMPT_ITEMS = 80
ITEM_TEXT_CHARS = 500

SYSTEM_PROMPT = """You write the script for a short daily highlights reel covering the AI \
coding-agent and model ecosystem. The source posts come from the ClawBytes, ModelBytes and \
Interesting Repos Telegram channels and the AI Wire registry.

Output markdown only, in this shape:

# <one-line hook that names the day's biggest story>

- **<short title>**: <one sentence on why it matters>. <link>
(5 to 8 bullets in total)

Rules:
- Pick the 5-8 most significant items. Favour launches, releases and model news over \
minor repos; cover more than one source when the day allows it.
- Each bullet is one line and ends with exactly one link copied verbatim from that item's links.
- Use only facts present in the items. Do not invent numbers, versions, dates or names.
- No preamble or closing remarks."""


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    for key in sorted(PRICING, key=len, reverse=True):
        if model.startswith(key):
            inp, out = PRICING[key]
            return (input_tokens * inp + output_tokens * out) / 1_000_000
    return None


def build_prompt(collected: dict) -> str:
    items = collected.get("items", [])[:MAX_PROMPT_ITEMS]
    lines = [f"Date: {collected.get('date')}", f"Items: {len(items)}", ""]
    for i, it in enumerate(items, 1):
        text = " ".join((it.get("text") or "").split())
        if len(text) > ITEM_TEXT_CHARS:
            text = text[: ITEM_TEXT_CHARS - 1] + "…"
        links = ", ".join((it.get("links") or [it.get("post_url")])[:3])
        lines.append(f"[{i}] source={it.get('source')} kind={it.get('kind')} posted_at={it.get('posted_at')}")
        lines.append(f"    links: {links}")
        lines.append(f"    post: {it.get('post_url')}")
        lines.append(f"    {text}")
    lines += ["", "Write the reel now."]
    return "\n".join(lines)


def call_claude(prompt: str, *, model: str, api_key: str, max_tokens: int = 2000,
                post=None, log=None) -> str:
    """POST to the Messages API and return the text. Raises RuntimeError on failure."""
    if post is None:
        import requests

        post = requests.post
    log = log or (lambda msg: print(msg, file=sys.stderr))
    resp = post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if resp.status_code >= 300:
        # The response body never contains the key; headers are not echoed.
        raise RuntimeError(f"Claude API HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    usage = data.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cost = estimate_cost(data.get("model") or model, in_tok, out_tok)
    log(
        f"highlights writer: model={data.get('model') or model} input_tokens={in_tok} "
        f"output_tokens={out_tok} est_cost_usd="
        + (f"{cost:.4f}" if cost is not None else "unknown")
    )
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined the request (stop_reason=refusal)")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError(f"Claude returned no text (stop_reason={data.get('stop_reason')})")
    return text


def write_reel(collected: dict, *, model: str | None = None, api_key: str | None = None,
               post=None, log=None) -> str:
    api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = model or os.environ.get("HIGHLIGHTS_MODEL") or DEFAULT_MODEL
    return call_claude(build_prompt(collected), model=model, api_key=api_key, post=post, log=log)
