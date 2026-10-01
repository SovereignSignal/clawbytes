#!/usr/bin/env python3
"""Per-source health for collection and discovery runs.

One line per source after each run, plus a small JSON record under
CLAWBYTES_MEMORY_DIR. The file is created on first write. A missing or
corrupt file is treated as an empty record and rewritten.

Sustained empty/error (24h+) alerts through the caller's existing admin
sender when one is configured. This module does not read or invent env
vars of its own beyond the admin channel the process already uses.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

HEALTH_FILENAME = "claw-source-health.json"
REASON_LIMIT = 200
UNHEALTHY_ALERT_AFTER = timedelta(hours=24)

_log = logging.getLogger("clawbytes.source_health")

# Existing ops-channel credentials. Read only to decide whether an admin
# sender is configured and to scrub values out of reasons. No new vars.
_SECRET_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    "SLACK_BOT_TOKEN",
    "CLAWBYTES_CURATOR_API_KEY",
    "CLAWBYTES_LLM_API_KEY",
)

_SECRET_RES = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|password|secret)=([^\s&]+)"),
)

_INTERESTING = re.compile(
    r"(HTTP\s+\d{3}|URL Error|timed out|timeout|Traceback|Error\b|❌|status=\d{3}|\b(?:401|403|429|500|502|503)\b)",
    re.I,
)

_ECO_PARTS = re.compile(
    r"Found\s+(\d+)\s+new\s+(?:release(?:\(s\))?|story/stories|paper(?:\(s\))?|skill\s+item(?:\(s\))?)",
    re.I,
)
_SUMMARY_RES = (
    re.compile(r"Found\s+(\d+)\s+new\s+relevant\s+items", re.I),
    re.compile(r"Found\s+(\d+)\s+quality\s+posts", re.I),
    re.compile(r"Found\s+(\d+)\s+new\s+HN\s+items", re.I),
    re.compile(r"Found\s+(\d+)\s+new\s+project(?:\(s\))?", re.I),
    re.compile(
        r"(?:Bluesky|Leaderboards|Pagewatch|Advisories|Registries):\s+(\d+)\s+new",
        re.I,
    ),
)
_SAVED = re.compile(
    r"Saved\s+(\d+)\s+new\s+subreddits,\s+(\d+)\s+new\s+HN\s+queries",
    re.I,
)


def health_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / HEALTH_FILENAME


_REDDIT_MONITOR_NAME = "claw_reddit_monitor"


def _reddit_monitor():
    """Load claw-reddit-monitor.py once. The fetch flag lives there."""
    cached = sys.modules.get(_REDDIT_MONITOR_NAME)
    if cached is not None and hasattr(cached, "REDDIT_FETCH_ENABLED"):
        return cached
    path = Path(__file__).resolve().parent / "claw-reddit-monitor.py"
    spec = importlib.util.spec_from_file_location(_REDDIT_MONITOR_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_REDDIT_MONITOR_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def reddit_fetch_enabled() -> bool:
    """Same switch as REDDIT_FETCH_ENABLED. False when the module cannot load.

    Public Reddit JSON is HTTP 403 without OAuth. While this is false,
    collect and discovery do not fetch, and record_source_health does not
    track or alert on the reddit source.
    """
    try:
        return bool(_reddit_monitor().REDDIT_FETCH_ENABLED)
    except Exception:  # noqa: BLE001 - a broken loader must not resume fetches
        return False


def admin_channel_configured() -> bool:
    """True when an ops inbox already wired in this repo is set.

    Telegram DM (TELEGRAM_BOT_TOKEN + CLAWBYTES_ADMIN_CHAT_ID) or the
    existing Slack ops fallback (SLACK_BOT_TOKEN + CLAWBYTES_OPS_SLACK_CHANNEL_ID).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("CLAWBYTES_ADMIN_CHAT_ID", "").strip()
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_channel = os.environ.get("CLAWBYTES_OPS_SLACK_CHANNEL_ID", "").strip()
    return bool((token and chat) or (slack_token and slack_channel))


def redact_secrets(text: str) -> str:
    """Scrub token-shaped strings and known secret env values."""
    out = str(text or "")
    values = []
    for key in _SECRET_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if len(value) >= 8:
            values.append(value)
    for value in sorted(set(values), key=len, reverse=True):
        out = out.replace(value, "<redacted>")
    for pattern in _SECRET_RES:
        if pattern.groups == 2:
            out = pattern.sub(r"\1=<redacted>", out)
        else:
            out = pattern.sub("<redacted>", out)
    return out


def shorten_reason(text: str, limit: int = REASON_LIMIT) -> str:
    cleaned = redact_secrets(text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return "…"
    return cleaned[: limit - 1].rstrip() + "…"


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _json_item_count(stdout: str) -> Optional[int]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(stdout[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return None
    if "discoveryCount" in summary:
        try:
            return int(summary["discoveryCount"])
        except (TypeError, ValueError):
            return None
    keys = ("releaseCount", "hnCount", "skillCount", "hfCount")
    if not any(key in summary for key in keys):
        return None
    total = 0
    for key in keys:
        try:
            total += int(summary.get(key) or 0)
        except (TypeError, ValueError):
            return None
    return total


def extract_item_count(stdout: str, stderr: str) -> Optional[int]:
    """Item count from a source script's captured output, or None if unknown."""
    text = f"{stdout or ''}\n{stderr or ''}"
    parts = [int(n) for n in _ECO_PARTS.findall(text)]
    if parts:
        return sum(parts)
    for pattern in _SUMMARY_RES:
        found = pattern.findall(text)
        if found:
            return int(found[-1])
    saved = _SAVED.search(text)
    if saved:
        return int(saved.group(1)) + int(saved.group(2))
    return _json_item_count(stdout or "")


def _diagnostic_snippet(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or re.match(r"^Found\s+\d+", line):
            continue
        if _INTERESTING.search(line):
            lines.append(line)
        if len(lines) >= 4:
            break
    return " | ".join(lines)


def outcome_from_process(
    *,
    returncode: Optional[int],
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    crashed: str = "",
) -> tuple[str, int, str]:
    """Map a source-script result to status, item count, and a short reason."""
    stdout = _as_text(stdout)
    stderr = _as_text(stderr)
    items = extract_item_count(stdout, stderr)
    if items is None:
        items = 0
    snippet = _diagnostic_snippet(f"{stderr}\n{stdout}")
    if timed_out:
        reason = "timed out after 300s"
        if snippet:
            reason = f"{reason}: {snippet}"
        return "error", items, shorten_reason(reason) or "-"
    if crashed:
        return "error", items, shorten_reason(f"crashed: {crashed}") or "-"
    if returncode not in (0, None):
        reason = f"exit {returncode}"
        if snippet:
            reason = f"{reason}: {snippet}"
        elif stderr.strip():
            reason = f"{reason}: {stderr.strip()}"
        return "error", items, shorten_reason(reason) or "-"
    if items <= 0:
        return "empty", 0, shorten_reason(snippet) or "-"
    return "ok", items, "-"


def format_source_health_line(source: str, status: str, items: int, error: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(source or "")) or "unknown"
    if status not in {"ok", "empty", "error"}:
        status = "error"
    reason = shorten_reason(error) or "-"
    return f"source_health source={name} status={status} items={int(items)} error={reason}"


def load_health(path: Path) -> dict:
    """Return the health document. Missing, unreadable, or wrong-shaped files
    become an empty document; the caller rewrites it on the next save."""
    if not path.exists():
        return {"sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
        return {"sources": {}}
    if not isinstance(data, dict):
        return {"sources": {}}
    sources = data.get("sources")
    if not isinstance(sources, dict):
        data = {"sources": {}}
    else:
        data = {"sources": sources}
    return data


def save_health(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _alert_due(rec: dict, now: datetime) -> bool:
    since = _parse_iso(rec.get("unhealthySince"))
    if since is None or now - since < UNHEALTHY_ALERT_AFTER:
        return False
    last = _parse_iso(rec.get("lastAlertAt"))
    if last is not None and now - last < UNHEALTHY_ALERT_AFTER:
        return False
    return True


def _alert_body(source: str, status: str, items: int, error: str, since: datetime, now: datetime) -> str:
    hours = int((now - since).total_seconds() // 3600)
    detail = error if error and error != "-" else "no detail"
    return (
        f"⚠️ Source {source} has been {status} for {hours}h "
        f"(items={items}). {detail}"
    )


def _blank_record() -> dict:
    return {
        "lastOkAt": None,
        "consecutiveFailures": 0,
        "consecutiveEmpties": 0,
        "unhealthySince": None,
        "lastAlertAt": None,
    }


def record_source_health(
    source: str,
    *,
    status: str,
    items: int,
    error: str,
    memory_dir: Path,
    now: Optional[datetime] = None,
    alert_sender: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Log one health line, update the JSON record, maybe alert.

    ``alert_sender`` is the process's existing admin DM (returns True when
    the message was delivered). When no admin channel is configured, a
    ``source_health ALERT`` warning is logged instead, at most once per
    source per 24h. A configured channel that fails to deliver is not
    deduped, so the next run can retry.

    ``reddit`` is skipped while ``reddit_fetch_enabled()`` is false: no
    health line, no alert, and no rewrite of the health file. An existing
    reddit streak entry is left as stored (same document shape).
    """
    if source == "reddit" and not reddit_fetch_enabled():
        existing = load_health(health_path(memory_dir))["sources"].get("reddit")
        if isinstance(existing, dict):
            return existing
        return _blank_record()

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if status not in {"ok", "empty", "error"}:
        status = "error"
    items = max(0, int(items))
    error = shorten_reason(error) or "-"

    path = health_path(memory_dir)
    data = load_health(path)
    sources = data["sources"]
    rec = sources.get(source)
    if not isinstance(rec, dict):
        rec = _blank_record()
    else:
        merged = _blank_record()
        merged.update({k: rec.get(k) for k in merged})
        rec = merged
        try:
            rec["consecutiveFailures"] = int(rec["consecutiveFailures"] or 0)
        except (TypeError, ValueError):
            rec["consecutiveFailures"] = 0
        try:
            rec["consecutiveEmpties"] = int(rec["consecutiveEmpties"] or 0)
        except (TypeError, ValueError):
            rec["consecutiveEmpties"] = 0

    if status == "ok":
        rec["lastOkAt"] = now.isoformat()
        rec["consecutiveFailures"] = 0
        rec["consecutiveEmpties"] = 0
        rec["unhealthySince"] = None
    elif status == "error":
        rec["consecutiveFailures"] = int(rec["consecutiveFailures"]) + 1
        rec["consecutiveEmpties"] = 0
        if _parse_iso(rec.get("unhealthySince")) is None:
            rec["unhealthySince"] = now.isoformat()
    else:
        rec["consecutiveEmpties"] = int(rec["consecutiveEmpties"]) + 1
        rec["consecutiveFailures"] = 0
        if _parse_iso(rec.get("unhealthySince")) is None:
            rec["unhealthySince"] = now.isoformat()

    if status != "ok" and _alert_due(rec, now):
        since = _parse_iso(rec.get("unhealthySince")) or now
        body = _alert_body(source, status, items, error, since, now)
        warn = f"source_health ALERT source={source} status={status} items={items} error={error}"
        if admin_channel_configured() and alert_sender is not None:
            delivered = False
            try:
                delivered = bool(alert_sender(body))
            except Exception:  # noqa: BLE001 - alert delivery must not break collect
                delivered = False
            if delivered:
                rec["lastAlertAt"] = now.isoformat()
            else:
                _log.warning("%s", warn)
        else:
            _log.warning("%s", warn)
            rec["lastAlertAt"] = now.isoformat()

    sources[source] = rec
    try:
        save_health(path, {"sources": sources})
    except OSError:
        _log.warning("source_health could not write %s", HEALTH_FILENAME)

    print(format_source_health_line(source, status, items, error), flush=True)
    return rec
