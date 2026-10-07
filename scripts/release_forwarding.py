"""Queue and forward qualified GitHub releases to Release Bot.

Forwarding is inert unless all three are set:

* ``RELEASE_EVENTS_URL``
* ``RELEASE_EVENTS_TOKEN``
* ``CLAWBYTES_RELEASE_FORWARDING=1`` (also true/yes/on)

The first enabled run writes ``release-forwarding-watermark.json`` and forwards
nothing. Later runs forward listed targets whose ``published`` time is after
that watermark and whose batch file is from this collect (timestamp within
45 minutes). Unknown repos are not forwarded. Legacy-owned repos are not
forwarded even if their map row says ``forward: true``.

Transport state lives in ``release-events-outbox.json`` on
``CLAWBYTES_MEMORY_DIR``. A failure here must not raise into collect.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_events import (  # noqa: E402
    CONFIG_ERROR,
    DELIVERED,
    DUPLICATE,
    REJECTED,
    RETRYABLE,
    SCHEMA,
    send_release_event,
)

log = logging.getLogger("clawbytes.release_forwarding")

TARGETS_PATH = REPO_ROOT / "release_targets.json"
OUTBOX_NAME = "release-events-outbox.json"
WATERMARK_NAME = "release-forwarding-watermark.json"
BATCH_NAME = "claw-ecosystem-new-items.json"

STALE_BATCH = timedelta(minutes=45)
FUTURE_SKEW = timedelta(minutes=10)
MAX_OUTBOX = 200
MAX_PER_RUN = 20
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 6 * 3600

# Phase 0 ownership. These stay with the legacy poller until Sov removes
# them here. The map's forward flag is not enough on its own.
LEGACY_OWNED = frozenset({
    "openclaw/openclaw",
    "nousresearch/hermes-agent",
    "openai/codex",
    "anthropics/claude-code",
})

# Tag shape shared with the reviewed map. rust-v is Codex's GitHub prefix.
VERSION = re.compile(r"^(?:rust-)?v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
# Token boundaries, tag only. Do not run this against release titles:
# rc\d* inside "Source" / "March" / "Architecture" is not a prerelease.
PRERELEASE_TAG = re.compile(
    r"(?i)(?:^|[-_.+])(preview|nightly|snapshot|canary|alpha|beta|rc|dev|pre)"
    r"(?:[-_.+]?\d+)?(?:$|[-_.+])"
)
NON_PRODUCT_PREFIX = re.compile(
    r"(?i)^(?:inputs|nightly|snapshot|canary|preview)[-_]"
)
INPUTS_PREFIX = re.compile(r"(?i)^inputs[-_]")

# The ecosystem monitor used to hard-cut release bodies at 500 characters,
# and build_event then sliced again at 1200, so the second cap never saw the
# rest. Keep this above the monitor's raw slice (4000) only if you also raise
# that slice — the monitor must send at least this many characters or the
# boundary cut below cannot recover text it never received. Release Bot's own
# message cap is 4096; 3000 leaves room for the alert chrome.
RELEASE_BODY_LIMIT = 3000

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ACTIVE = frozenset({"queued", "retryable"})


def forwarding_enabled() -> bool:
    url = os.environ.get("RELEASE_EVENTS_URL", "").strip()
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "").strip()
    flag = os.environ.get("CLAWBYTES_RELEASE_FORWARDING", "").strip().lower()
    return bool(url and token and flag in _TRUTHY)


def memory_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("CLAWBYTES_MEMORY_DIR", "").strip()
    if env:
        return Path(env)
    return REPO_ROOT / "memory"


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return False


def normalize_tag(tag: str) -> str:
    return re.sub(r"^(?:rust-)?v", "", tag or "", flags=re.IGNORECASE)


def is_prerelease_tag(tag: str) -> bool:
    return PRERELEASE_TAG.search(tag or "") is not None


def load_targets(path: Path | None = None) -> dict:
    """Reviewed ``owner/repo`` map. Missing file → empty (forward nothing)."""
    target_path = path or TARGETS_PATH
    try:
        raw = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("forward_release_events: target map unreadable")
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("targets"), dict):
        table = raw["targets"]
    elif isinstance(raw, dict):
        table = {key: value for key, value in raw.items() if isinstance(value, dict)}
    else:
        return {}
    compiled = {}
    for repo, row in table.items():
        if not isinstance(row, dict):
            continue
        pattern = row.get("tag_pattern")
        if pattern:
            try:
                re.compile(pattern)
            except re.error:
                log.warning("forward_release_events: bad tag_pattern for %s", repo)
                continue
        compiled[str(repo)] = row
    return compiled


def _target_for(repo: str, targets: dict) -> dict | None:
    wanted = repo.lower()
    for key, row in targets.items():
        if str(key).lower() == wanted:
            return row
    return None


def target_is_forwarded(repo: str, target: dict | None) -> bool:
    if target is None:
        return False
    if repo.lower() in LEGACY_OWNED or _flag(target.get("legacy_owned")):
        return False
    return _flag(target.get("forward"))


def is_qualified(item: dict, targets: dict) -> bool:
    """True when this batch row may be forwarded under the reviewed map."""
    if not isinstance(item, dict):
        return False
    if _flag(item.get("baseline")) or _flag(item.get("draft")):
        return False
    repo = str(item.get("repo") or "").strip()
    tag = str(item.get("tag") or "").strip()
    url = str(item.get("url") or "").strip()
    if not repo or not tag or not url:
        return False
    target = _target_for(repo, targets)
    if not target_is_forwarded(repo, target):
        return False
    if INPUTS_PREFIX.search(tag) or NON_PRODUCT_PREFIX.search(tag):
        return False
    if not VERSION.match(tag):
        return False
    pattern = str(target.get("tag_pattern") or "")
    if pattern and re.search(pattern, tag) is None:
        return False
    if (is_prerelease_tag(tag) or _flag(item.get("prerelease"))) and not _flag(target.get("prereleases")):
        return False
    return True


def clip_release_body(text: str, limit: int = RELEASE_BODY_LIMIT) -> str:
    """Keep about ``limit`` characters, ending on a sentence or bullet.

    A hard slice stops mid-word ("Subcon", "tool-r"). When the text is longer
    than ``limit``, cut at the last sentence end, paragraph break, or bullet
    start inside the window, then add an ellipsis. Shorter bodies pass through
    unchanged, with no ellipsis.
    """
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    window = raw[:limit]
    floor = int(limit * 0.6)
    boundaries = []
    for match in re.finditer(r"[.!?…](?:\s|$)", window):
        boundaries.append(match.end())
    for match in re.finditer(r"\n\n|\n(?=\s*(?:[-*]|\d+[.)]|#{1,6}\s))", window):
        boundaries.append(match.start())
    usable = [point for point in boundaries if point >= floor]
    if usable:
        cut = max(usable)
    else:
        cut = max(window.rfind(" "), window.rfind("\n"))
        if cut < floor:
            cut = limit
    # Don't end inside an unclosed markdown link or code span.
    head = window[:cut]
    if head.count("`") % 2 == 1:
        tick = head.rfind("`")
        if tick >= floor:
            cut = tick
            head = window[:cut]
    open_link = head.rfind("[")
    close_link = head.rfind("]")
    if open_link > close_link and open_link >= floor:
        cut = open_link
    clipped = window[:cut].rstrip()
    if not clipped.endswith(("…", "...")):
        clipped += "…"
    return clipped


def build_event(item: dict, targets: dict) -> dict:
    """Event fields. ``name`` is the map display name, never the release title."""
    repo = str(item.get("repo") or "").strip()
    tag = str(item.get("tag") or "").strip()
    target = _target_for(repo, targets) or {}
    name = str(target.get("name") or repo).strip() or repo
    return {
        "schema": SCHEMA,
        "id": f"software:github:{repo.lower()}:{tag.lower()}",
        "kind": "software",
        "name": name,
        "version": normalize_tag(tag),
        "source": "clawbytes",
        "source_type": "github_release",
        "url": str(item.get("url") or "").strip(),
        "published_at": item.get("published"),
        "summary": clip_release_body(str(item.get("body") or "")),
        "metadata": {
            "repo": repo,
            "tag": tag,
            "prerelease": _flag(item.get("prerelease")),
            "draft": _flag(item.get("draft")),
        },
    }


def atomic_write_json(path: Path, payload) -> None:
    """Unique temp file in the destination directory, fsync, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _load_json_file(path: Path):
    """Return parsed JSON, ``None`` if the file is missing, or raise ValueError if corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(path.name) from exc


def load_watermark(path: Path) -> datetime | None:
    """Missing file → None (caller baselines). Corrupt file raises ValueError."""
    data = _load_json_file(path)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(path.name)
    parsed = parse_utc(data.get("watermark"))
    if parsed is None:
        raise ValueError(path.name)
    return parsed


def load_outbox(path: Path) -> list:
    """Missing file → empty list. Corrupt file raises ValueError (do not overwrite)."""
    data = _load_json_file(path)
    if data is None:
        return []
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return list(data["events"])
    raise ValueError(path.name)


def trim_outbox(events: list, limit: int) -> list:
    """Bound the file. Drop the oldest terminal rows first; keep active rows."""
    if len(events) <= limit:
        return events
    active = [row for row in events if row.get("status") in _ACTIVE]
    terminal = [row for row in events if row.get("status") not in _ACTIVE]
    if len(active) >= limit:
        active.sort(key=lambda row: row.get("updated_at") or "")
        return active[-limit:]
    terminal.sort(key=lambda row: row.get("updated_at") or "")
    room = limit - len(active)
    kept = {id(row) for row in active}
    kept.update(id(row) for row in terminal[-room:])
    return [row for row in events if id(row) in kept]


def save_outbox(path: Path, events: list, limit: int = MAX_OUTBOX) -> None:
    atomic_write_json(path, {"version": 1, "events": trim_outbox(events, limit)})


def _backoff(attempts: int) -> timedelta:
    step = max(0, attempts - 1)
    seconds = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(step, 10)))
    return timedelta(seconds=seconds)


def _due(row: dict, now: datetime) -> bool:
    if row.get("status") not in _ACTIVE:
        return False
    nxt = parse_utc(row.get("next_attempt_at"))
    return nxt is None or nxt <= now


def _batch_is_fresh(batch: dict, now: datetime) -> bool:
    stamped = parse_utc(batch.get("timestamp") if isinstance(batch, dict) else None)
    if stamped is None:
        return False
    age = now - stamped
    return -FUTURE_SKEW <= age <= STALE_BATCH


def _index_by_id(events: list) -> dict:
    return {row.get("id"): row for row in events if isinstance(row, dict) and row.get("id")}


def _ingest(events: list, batch: dict, targets: dict, watermark: datetime, now: datetime) -> int:
    added = 0
    known = _index_by_id(events)
    releases = batch.get("newReleases") if isinstance(batch, dict) else None
    if not isinstance(releases, list):
        return 0
    for item in releases:
        if not is_qualified(item, targets):
            continue
        published = parse_utc(item.get("published"))
        if published is None or published <= watermark:
            continue
        event = build_event(item, targets)
        if event["id"] in known:
            continue
        row = {
            "id": event["id"],
            "event": event,
            "status": "queued",
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
            "updated_at": format_utc(now),
        }
        events.append(row)
        known[event["id"]] = row
        added += 1
    return added


def _deliver(events: list, now: datetime, timeout: float, max_per_run: int) -> tuple[str | None, bool]:
    """Attempt due rows once.

    Returns ``(failure outcome or None, whether any row changed)``. One 401
    stops the rest of the pass; those rows stay queued for a later run.
    """
    due = [row for row in events if _due(row, now)]
    due.sort(key=lambda row: row.get("updated_at") or "")
    failure: str | None = None
    changed = False
    for row in due[:max_per_run]:
        status, detail = send_release_event(row.get("event") or {}, timeout=timeout)
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["updated_at"] = format_utc(now)
        row["last_error"] = detail
        changed = True
        if status in {DELIVERED, DUPLICATE, REJECTED}:
            row["status"] = status
            row["next_attempt_at"] = None
            continue
        row["status"] = RETRYABLE
        row["next_attempt_at"] = format_utc(now + _backoff(row["attempts"]))
        if status == CONFIG_ERROR:
            row["last_error"] = CONFIG_ERROR
            return CONFIG_ERROR, True
        failure = RETRYABLE
    return failure, changed


def forward_release_events(
    *,
    now: datetime | None = None,
    memory: Path | None = None,
    targets: dict | None = None,
    timeout: float = 5,
    max_per_run: int = MAX_PER_RUN,
    max_outbox: int = MAX_OUTBOX,
) -> str:
    """One forwarding pass. Returns a single outcome token. Does not raise."""
    try:
        return _forward(
            now=now or datetime.now(timezone.utc),
            memory=memory_dir(memory),
            targets=targets,
            timeout=timeout,
            max_per_run=max_per_run,
            max_outbox=max_outbox,
        )
    except Exception as exc:  # noqa: BLE001 - collect must survive a forwarder bug
        log.exception("forward_release_events: error")
        print(f"[WARN] release-event forward failed: {type(exc).__name__}")
        return "error"


def _forward(*, now: datetime, memory: Path, targets: dict | None, timeout: float, max_per_run: int, max_outbox: int) -> str:
    if not forwarding_enabled():
        return "disabled"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    watermark_path = memory / WATERMARK_NAME
    outbox_path = memory / OUTBOX_NAME
    try:
        watermark = load_watermark(watermark_path)
    except ValueError:
        log.warning("forward_release_events: watermark unreadable")
        return "error"
    if watermark is None:
        atomic_write_json(watermark_path, {"watermark": format_utc(now)})
        return "baseline"

    try:
        events = load_outbox(outbox_path)
    except ValueError:
        log.warning("forward_release_events: outbox unreadable")
        return "error"

    fresh = False
    batch: dict = {}
    batch_path = memory / BATCH_NAME
    if batch_path.exists():
        try:
            loaded = json.loads(batch_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict) and _batch_is_fresh(loaded, now):
            fresh = True
            batch = loaded

    table = load_targets() if targets is None else targets
    if fresh and _ingest(events, batch, table, watermark, now):
        # Persist queued rows before any network call. Collect has already
        # recorded lastSeenReleases, so a crash during POST must not drop
        # the release on the floor.
        save_outbox(outbox_path, events, max_outbox)
    failure, changed = _deliver(events, now, timeout, max_per_run)
    if changed:
        save_outbox(outbox_path, events, max_outbox)
    if failure:
        return failure
    if not fresh and not changed:
        return "stale"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Always exits 0 so a wiring mistake cannot page collect."""
    del argv
    outcome = forward_release_events()
    print(f"forward_release_events: {outcome}")
    return 0
