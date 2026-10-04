#!/usr/bin/env python3
"""ClawBytes curator — per-publish editorial review.

Reads a candidate bundle JSON from stdin, asks the configured model to review it,
and returns the (possibly modified) bundle JSON on stdout. The OpenAI-compatible
backend tries CLAWBYTES_CURATOR_MODEL_FALLBACK once on timeout, HTTP error, empty
content, unparseable JSON, or a post that introduces a number, version, or
date that was not in the bundle. If that also fails, the original bundle is returned
with a fallback marker so the publisher can keep posting.

The publisher (clawbytes_threads.py with --use-curator) invokes this and uses
the returned bundle. See docs/curator-prompt.md for the system prompt; see
the design spec at docs/superpowers/specs/2026-05-20-clawbytes-architecture-design.md
for the contract.

Exit codes:
  0 = success, valid curated bundle on stdout
  2 = unrecoverable error; caller should fall back to deterministic bundle
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from claude_common import (  # noqa: E402
    ClaudeCodeError,
    ClaudeResult,
    call_claude,
    load_scope,
    parse_json_from_text,
)
from completion_diag import empty_content_message  # noqa: E402


# Second OpenAI-compatible model tried once after the primary fails.
# Unset uses this default. An empty value skips the extra call.
DEFAULT_CURATOR_FALLBACK_MODEL = "deepseek-v4.1-flash"


class CuratorBackendError(Exception):
    """A retryable OpenAI-compatible curator failure."""

    def __init__(self, kind: str, message: str, text: str = ""):
        super().__init__(message)
        self.kind = kind
        self.text = text


def _ollama_curator_configured() -> bool:
    """True when an OpenAI-compatible curator backend is configured.

    Lets the curator run on a strong Ollama-cloud model (e.g. deepseek-v4-pro)
    instead of the Claude CLI, without code changes — just env vars.
    """
    return bool(
        os.environ.get("CLAWBYTES_CURATOR_URL")
        and os.environ.get("CLAWBYTES_CURATOR_MODEL")
        and (os.environ.get("CLAWBYTES_CURATOR_API_KEY") or os.environ.get("CLAWBYTES_LLM_API_KEY"))
    )


def _positive_int_env(name: str, default: int) -> int:
    """Positive integer env override. Blank, zero, and invalid keep ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _curator_models() -> list[str]:
    """Primary model, then one fallback when it names a different model."""
    primary = os.environ["CLAWBYTES_CURATOR_MODEL"]
    fallback = os.environ.get(
        "CLAWBYTES_CURATOR_MODEL_FALLBACK", DEFAULT_CURATOR_FALLBACK_MODEL
    ).strip()
    if fallback and fallback != primary:
        return [primary, fallback]
    return [primary]


def _curator_call_timeout(total: int, index: int, count: int, elapsed: float) -> int:
    """Seconds for this attempt inside curate()'s timeout.

    The parent process is killed about 10s after that timeout, so a primary
    hang must not be given the whole budget. Later attempts keep a slice, and
    the last attempt receives whatever time is still left after a fast failure.
    """
    reserve = 15
    remaining = int(max(0, total - reserve - elapsed))
    later = count - index - 1
    if later <= 0:
        return remaining
    hold = max(30, remaining // (later + 1))
    return max(0, remaining - hold * later)


def _openai_message_content(data: dict) -> str:
    """Assistant `content` only. Empty content is a failure.

    Reasoning models (GLM and others) may fill `reasoning` or
    `reasoning_content` and leave `content` empty. That is not a reply.
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"empty content: malformed completion ({e})") from e
    if not isinstance(message, dict):
        raise ValueError("empty content: message was not an object")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(empty_content_message(data))
    return content.strip()


def _strip_json_fence(content: str) -> str:
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
    return content.strip()


def _log_bad_json(text: str, error: BaseException) -> None:
    print(f"[curator] BAD JSON parse error: {error}", file=sys.stderr)
    print(f"[curator] text length: {len(text)}", file=sys.stderr)
    print(f"[curator] text[:400]: {text[:400]!r}", file=sys.stderr)
    match = re.search(r"char (\d+)", str(error))
    if match:
        pos = int(match.group(1))
        lo = max(0, pos - 200)
        hi = min(len(text), pos + 200)
        print(f"[curator] text[{lo}:{hi}] (error at char {pos}): {text[lo:hi]!r}", file=sys.stderr)
    print(f"[curator] text[-400:]: {text[-400:]!r}", file=sys.stderr)


def _curate_via_openai(system_prompt: str, user_prompt: str, timeout: int, model: str) -> ClaudeResult:
    """Run the curator pass against an OpenAI-compatible chat endpoint.

    Returns a ClaudeResult so the rest of curate() is backend-agnostic. This
    backend has NO web tools (unlike the Claude path), so the prompt tells the
    model to curate strictly from the provided bundle. Reasoning models put
    chain-of-thought in a separate field; we read only `content`. Empty
    content raises CuratorBackendError so the caller can try the fallback model.
    """
    import urllib.error
    import urllib.request

    base = os.environ["CLAWBYTES_CURATOR_URL"].rstrip("/")
    key = os.environ.get("CLAWBYTES_CURATOR_API_KEY") or os.environ.get("CLAWBYTES_LLM_API_KEY", "")
    max_tokens = _positive_int_env("CLAWBYTES_CURATOR_MAX_TOKENS", 16000)

    user = (
        user_prompt
        + "\n\nNOTE: You have no web access on this backend — curate strictly from "
        "the bundle above. Return ONLY the curated bundle JSON, no prose or markdown."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    effort = os.environ.get("CLAWBYTES_CURATOR_REASONING_EFFORT", "").strip()
    if effort:
        payload["reasoning_effort"] = effort
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except TimeoutError as e:
        raise CuratorBackendError("timeout", str(e) or "timeout") from e
    except urllib.error.HTTPError as e:
        raise CuratorBackendError("http_error", f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        kind = "timeout" if isinstance(reason, TimeoutError) else "http_error"
        raise CuratorBackendError(kind, str(e)) from e
    duration_ms = int((time.time() - start) * 1000)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise CuratorBackendError("bad_json", f"completion was not JSON: {e}") from e
    try:
        content = _strip_json_fence(_openai_message_content(data))
    except ValueError as e:
        raise CuratorBackendError("empty_reply", str(e)) from e
    if not content:
        raise CuratorBackendError("empty_reply", empty_content_message(data))
    usage = data.get("usage", {})
    return ClaudeResult(
        text=content,
        raw=data,
        duration_ms=duration_ms,
        model=model,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
    )


CURATOR_PROMPT_FILE = REPO_ROOT / "docs" / "curator-prompt.md"


def memory_dir() -> Path:
    """Persistent state directory, same rule as the rest of the bot.

    ``CLAWBYTES_MEMORY_DIR`` when set (the Railway volume). Otherwise
    ``<repo>/memory``, matching supervisor.py and the monitors. Read on each
    call so writes follow the process environment rather than the image-local
    ``memory/`` directory, which a redeploy wipes.
    """
    return Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(REPO_ROOT / "memory")))


def degraded_log_path() -> Path:
    return memory_dir() / "degraded_publishes.json"


def discovered_refs_path() -> Path:
    return memory_dir() / "discovered_references.json"


def build_user_prompt(bundle: dict) -> str:
    """The user-turn prompt: 'here is the bundle, review it, return JSON.'"""
    return (
        "Review this ClawBytes lane bundle. Apply the editorial scope and curator "
        "responsibilities defined in your system prompt. Return only the curated "
        "bundle JSON on stdout — no prose, no markdown, no commentary.\n\n"
        "Use only facts present in the bundle. Copy numbers, version strings, and "
        "dates verbatim. Do not add opinion.\n\n"
        f"BUNDLE:\n{json.dumps(bundle, indent=2)}"
    )


_CURATOR_FACT_KEYS = (
    "title",
    "url",
    "source",
    "source_name",
    "summary",
    "existing_blurb",
    "published_at",
    "blurb",
)


def _curator_fact_source(bundle: dict) -> str:
    """Facts the number guard may cite. Prompt examples are not facts."""
    items = bundle.get("items") if isinstance(bundle, dict) else None
    if not isinstance(items, list):
        items = []
    parts = [str(len(items))]
    for item in items:
        if not isinstance(item, dict):
            continue
        chunks = []
        for key in _CURATOR_FACT_KEYS:
            value = item.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                chunks.append(str(value))
        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            chunks.append(str(score))
        fetched = item.get("fetched")
        if isinstance(fetched, dict):
            for value in fetched.values():
                if isinstance(value, str):
                    chunks.append(value)
        elif isinstance(fetched, str):
            chunks.append(fetched)
        parts.append("\n".join(chunks))
    return "\n".join(parts)


def _curator_post_text(curated: dict) -> str:
    """Channel copy the curator wrote: titles, blurbs, lead, and take."""
    if not isinstance(curated, dict):
        return ""
    parts = []
    for key in ("lead_signal", "take"):
        value = curated.get(key)
        if isinstance(value, str):
            parts.append(value)
    for item in curated.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("title", "blurb"):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _reject_ungrounded_post(curated: dict, bundle: dict, model: str) -> None:
    """Same facts-only number/version/date guard as the writer.

    Raises CuratorBackendError so the OpenAI path can try the fallback model.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import clawbytes_threads as threads

    missing = threads._ungrounded_numbers(_curator_post_text(curated), _curator_fact_source(bundle))
    if not missing:
        return
    listed = ", ".join(sorted(missing))
    print(f"[curator] number guard rejected model={model} missing={listed}", file=sys.stderr)
    raise CuratorBackendError("ungrounded_number", f"number guard missing={listed}")


def build_system_prompt() -> str:
    """Curator system prompt + scope constitution, concatenated."""
    if not CURATOR_PROMPT_FILE.exists():
        raise FileNotFoundError(f"curator prompt missing: {CURATOR_PROMPT_FILE}")
    curator_prompt = CURATOR_PROMPT_FILE.read_text()
    scope = load_scope()
    return f"{curator_prompt}\n\n---\n\n# Editorial Scope (from EDITORIAL_SCOPE.md)\n\n{scope}"


def fallback_bundle(bundle: dict, reason: str, error_kind: str = "fallback") -> dict:
    """Return the original bundle annotated with curator metadata explaining the fallback."""
    out = dict(bundle)
    out["_curator"] = {
        "approved": True,
        "dropped_item_ids": [],
        "drop_reasons": {},
        "rewrote_blurbs": [],
        "rewrote_take": False,
        "discovered_references": [],
        "anchor_check": "skipped",
        "notes": f"FALLBACK: {reason}",
        "fallback": True,
        "fallback_kind": error_kind,
    }
    return out


def log_degraded(lane: str, kind: str, message: str) -> None:
    """Append a degraded-publish event for supervisor to inspect."""
    path = degraded_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = []
    existing.append({
        "lane": lane,
        "kind": kind,
        "message": message[:500],
        "at": int(time.time()),
    })
    existing = existing[-200:]  # keep last 200 events
    path.write_text(json.dumps(existing, indent=2))


def persist_discovered_references(refs: list) -> None:
    """Append curator-flagged references to the queue supervisor drains."""
    if not refs:
        return
    path = discovered_refs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = []
    for ref in refs:
        if isinstance(ref, dict):
            ref = dict(ref)
            ref.setdefault("discovered_at", int(time.time()))
            existing.append(ref)
    existing = existing[-500:]  # bound the queue
    path.write_text(json.dumps(existing, indent=2))


def curate(bundle: dict, *, timeout: int = 180, dry_run: bool = False) -> dict:
    """Run the curator pass on a single bundle. Always returns a usable bundle."""
    lane = bundle.get("lane", "unknown")

    if dry_run:
        out = fallback_bundle(bundle, "dry-run mode — bundle passed through unchanged", "dry_run")
        return out

    try:
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(bundle)
    except FileNotFoundError as e:
        log_degraded(lane, "missing_prompt", str(e))
        return fallback_bundle(bundle, f"missing prompt file: {e}", "missing_prompt")

    if _ollama_curator_configured():
        # Timeout, HTTP error, empty content, unparseable JSON, or an
        # ungrounded number from the primary tries the fallback model once.
        # After both fail, the bundle still carries the posting fallback marker.
        result = None
        curated = None
        failures: list[tuple[str, CuratorBackendError]] = []
        models = _curator_models()
        started = time.monotonic()
        for index, model in enumerate(models):
            call_timeout = _curator_call_timeout(timeout, index, len(models), time.monotonic() - started)
            if call_timeout < 15:
                err = CuratorBackendError("timeout", "not enough time left for this model")
                failures.append((model, err))
                print(f"[curator] model={model} failed (timeout): {err}", file=sys.stderr)
                continue
            try:
                result = _curate_via_openai(system_prompt, user_prompt, call_timeout, model)
                try:
                    parsed = parse_json_from_text(result.text)
                except (json.JSONDecodeError, ValueError) as e:
                    raise CuratorBackendError("bad_json", str(e), result.text) from e
                if not isinstance(parsed, dict):
                    raise CuratorBackendError(
                        "bad_json", "curator JSON was not an object", result.text
                    )
                _reject_ungrounded_post(parsed, bundle, model)
                curated = parsed
            except Exception as e:  # noqa: BLE001 - unknown backend failure still falls back
                if not isinstance(e, CuratorBackendError):
                    e = CuratorBackendError("ollama_error", str(e))
                failures.append((model, e))
                print(f"[curator] model={model} failed ({e.kind}): {e}", file=sys.stderr)
                continue
            print(f"[curator] answered by model={result.model}", file=sys.stderr)
            break
        if curated is None:
            detail = "; ".join(f"{model} {err.kind}: {err}" for model, err in failures)
            last = failures[-1][1]
            log_degraded(lane, last.kind, detail)
            print(f"[curator] ollama backend error: {detail}", file=sys.stderr)
            if last.kind == "bad_json" and last.text:
                _log_bad_json(last.text, last)
            return fallback_bundle(bundle, f"ollama curator error: {detail}", last.kind)
    else:
        try:
            result = call_claude(
                user_prompt,
                system_prompt=system_prompt,
                # Curator has research powers: WebSearch to find what shipped today
                # in the agent ecosystem, WebFetch to pull primary sources, Read to
                # consult EDITORIAL_SCOPE.md and the repo's own docs.
                # When the input bundle is weak, the curator is expected to actively
                # find better signal rather than skip the publish.
                allowed_tools=["WebSearch", "WebFetch", "Read"],
                timeout=timeout,
            )
        except ClaudeCodeError as e:
            log_degraded(lane, e.kind, str(e))
            # Emit stderr to our own stderr so Railway logs capture it for diagnosis
            print(f"[curator] ClaudeCodeError kind={e.kind} msg={e}", file=sys.stderr)
            if e.stderr:
                print(f"[curator] claude stderr (first 2000 chars):\n{e.stderr[:2000]}", file=sys.stderr)
            return fallback_bundle(bundle, f"claude error: {e}", e.kind)

        try:
            curated = parse_json_from_text(result.text)
        except (json.JSONDecodeError, ValueError) as e:
            log_degraded(lane, "bad_json", f"curator returned non-JSON: {result.text[:300]!r}")
            _log_bad_json(result.text, e)
            return fallback_bundle(bundle, f"curator returned non-JSON: {e}", "bad_json")
        if not isinstance(curated, dict):
            log_degraded(lane, "bad_json", "curator JSON was not an object")
            _log_bad_json(result.text, ValueError("curator JSON was not an object"))
            return fallback_bundle(bundle, "curator JSON was not an object", "bad_json")
        try:
            _reject_ungrounded_post(curated, bundle, result.model or "")
        except CuratorBackendError as e:
            log_degraded(lane, e.kind, str(e))
            print(f"[curator] model={result.model} failed ({e.kind}): {e}", file=sys.stderr)
            return fallback_bundle(bundle, f"claude error: {e}", e.kind)

    # Enrich curator metadata with telemetry
    meta = curated.setdefault("_curator", {})
    meta.setdefault("approved", True)
    meta["model"] = result.model
    meta["tokens_used"] = {
        "input": result.input_tokens,
        "output": result.output_tokens,
    }
    meta["duration_ms"] = result.duration_ms
    meta["fallback"] = False

    # Persist any discovered references for supervisor to drain
    refs = meta.get("discovered_references") or []
    if refs:
        persist_discovered_references(refs)

    return curated


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawBytes curator (stdin=bundle JSON, stdout=curated bundle JSON)")
    parser.add_argument("--timeout", type=int, default=180, help="Claude Code subprocess timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Pass bundle through unchanged with fallback marker (no Claude call)")
    args = parser.parse_args()

    try:
        bundle = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"stdin not valid JSON: {e}"}), file=sys.stderr)
        return 2

    curated = curate(bundle, timeout=args.timeout, dry_run=args.dry_run)
    print(json.dumps(curated, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
