"""Shared subprocess wrapper around Claude Code headless mode.

Used by curator.py (per-publish, time-budgeted) and supervisor.py (daily, agentic).
Routes all Claude usage through Sov's existing Claude Code subscription via
`claude -p --output-format json` rather than the Anthropic API directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPE_FILE = REPO_ROOT / "EDITORIAL_SCOPE.md"


def _materialize_credentials() -> None:
    """Write CLAUDE_CREDENTIALS env var into ~/.claude/.credentials.json if missing.

    Railway ignores Docker ENTRYPOINT and runs startCommand directly, so our
    container entrypoint.sh never gets to set up the credentials file.
    Instead, do it on first import of claude_common — runs in every cron
    service that uses Claude Code (curator, supervisor) before any
    `claude -p` subprocess fires.
    """
    creds_dir = Path(os.path.expanduser("~/.claude"))
    creds_file = creds_dir / ".credentials.json"
    if creds_file.exists():
        return
    raw = os.environ.get("CLAUDE_CREDENTIALS")
    if not raw:
        return
    try:
        creds_dir.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(raw)
        os.chmod(creds_file, 0o600)
    except OSError as e:
        print(f"[claude_common] WARNING: failed to write credentials file: {e}", file=sys.stderr)


_materialize_credentials()


class ClaudeCodeError(Exception):
    """Raised when Claude Code subprocess fails in a way the caller should handle."""

    def __init__(self, kind: str, message: str, stderr: str = ""):
        self.kind = kind  # "timeout" | "non_zero_exit" | "bad_json" | "auth_expired" | "quota_exhausted" | "missing_binary"
        self.stderr = stderr
        super().__init__(f"[{kind}] {message}")


@dataclass
class ClaudeResult:
    text: str  # primary textual answer (last assistant message)
    raw: dict  # full JSON from Claude Code
    duration_ms: int
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def load_scope() -> str:
    """Read EDITORIAL_SCOPE.md verbatim. Anchor for every Claude call."""
    if not SCOPE_FILE.exists():
        return "(EDITORIAL_SCOPE.md missing from repo root — running without scope anchor)"
    return SCOPE_FILE.read_text()


def call_claude(
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    timeout: int = 60,
    model: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> ClaudeResult:
    """Invoke `claude -p` and return the parsed result.

    Raises ClaudeCodeError on subprocess timeout, non-zero exit, malformed JSON,
    auth expiration, or quota exhaustion. Callers (curator, supervisor) handle
    these failure kinds explicitly.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json"]

    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if allowed_tools is not None:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]

    chosen_model = model or os.environ.get("CLAUDE_MODEL")
    if chosen_model:
        cmd += ["--model", chosen_model]

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        raise ClaudeCodeError(
            "missing_binary",
            "claude binary not found in PATH — is Claude Code installed in the container?",
        )
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError("timeout", f"claude -p exceeded {timeout}s budget")

    duration_ms = int((time.monotonic() - started) * 1000)

    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "unauthorized" in stderr_lower or "auth" in stderr_lower or "login" in stderr_lower:
            raise ClaudeCodeError(
                "auth_expired",
                "claude -p failed with auth error — credentials may have expired",
                stderr=result.stderr,
            )
        if "quota" in stderr_lower or "rate limit" in stderr_lower or "exceeded" in stderr_lower:
            raise ClaudeCodeError(
                "quota_exhausted",
                "claude -p failed with quota error",
                stderr=result.stderr,
            )
        raise ClaudeCodeError(
            "non_zero_exit",
            f"claude -p exited {result.returncode}",
            stderr=result.stderr,
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise ClaudeCodeError("bad_json", "claude -p returned empty stdout")

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCodeError(
            "bad_json",
            f"claude -p stdout was not valid JSON: {e}",
            stderr=stdout[:500],
        )

    text = _extract_text(raw)
    model_used = raw.get("model") or chosen_model
    usage = raw.get("usage") or {}

    return ClaudeResult(
        text=text,
        raw=raw,
        duration_ms=duration_ms,
        model=model_used,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


def _extract_text(raw: dict) -> str:
    """Pull the primary textual answer out of Claude Code's JSON output.

    Claude Code's --output-format json wraps the response. We tolerate several
    shapes since the format has changed across versions.
    """
    # Most common: top-level "result" or "text" field
    for key in ("result", "text", "response"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Sometimes: messages array with last assistant message
    msgs = raw.get("messages")
    if isinstance(msgs, list):
        for msg in reversed(msgs):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    if parts:
                        return "\n".join(parts).strip()

    # Fallback: stringify whole thing so caller has something
    return json.dumps(raw)


def parse_json_from_text(text: str) -> dict:
    """Extract a single JSON object from Claude's textual response.

    Claude sometimes returns ```json ... ``` fenced blocks even when asked for
    raw JSON. This strips fences and parses leniently.
    """
    s = text.strip()
    if s.startswith("```"):
        # Strip ```json ... ``` or ``` ... ``` fences
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return json.loads(s)


def auth_smoke_test(timeout: int = 15) -> tuple[bool, str]:
    """Quick check: can we invoke `claude -p` and get a response?

    Used by supervisor at the start of every cycle (responsibility 4.4b in the spec).
    Returns (ok, message).
    """
    try:
        result = call_claude(
            "Respond with the single word: ok",
            timeout=timeout,
        )
        text = result.text.lower().strip()
        if "ok" in text:
            return True, f"claude responded in {result.duration_ms}ms"
        return False, f"claude responded but text was unexpected: {text[:120]!r}"
    except ClaudeCodeError as e:
        return False, f"{e.kind}: {e}"


if __name__ == "__main__":
    # Hand-test from CLI: `python3 scripts/claude_common.py "what is 2+2?"`
    if len(sys.argv) < 2:
        print("usage: claude_common.py <prompt>", file=sys.stderr)
        sys.exit(1)
    try:
        r = call_claude(sys.argv[1])
        print(json.dumps({
            "text": r.text,
            "duration_ms": r.duration_ms,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
        }, indent=2))
    except ClaudeCodeError as e:
        print(json.dumps({"error_kind": e.kind, "message": str(e), "stderr": e.stderr[:500]}, indent=2), file=sys.stderr)
        sys.exit(2)
