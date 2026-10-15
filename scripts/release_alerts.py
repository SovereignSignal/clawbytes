"""Immediate per-release Telegram alerts (replaces the separate Release Bot).

Inert unless all are set:

* ``CLAWBYTES_RELEASE_ALERTS=1`` (also true/yes/on)
* ``CLAWBYTES_RELEASE_ALERTS_CHAT_ID`` — never falls back to the @clawbytes
  audience channel
* ``TELEGRAM_BOT_TOKEN``

Targets are ``release_targets.json`` rows with ``alert: true`` (the four core
tools, which may also carry ``npm``, ``docker``, ``tag_prefixes``) or
``forward: true`` (GitHub only). ``legacy_owned`` is ignored here.

Per target and source (GitHub newest qualifying release, npm ``latest``), the
first successful fetch records a baseline and sends nothing. Later a new
version alerts once: npm and GitHub share a normalized version (``rust-v`` /
``v`` stripped), so the same release from both sources is one message. A
version is recorded only after Telegram accepts the send. Alerts are never
mirrored to Slack. Nothing here raises into collect.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
for _p in (str(SCRIPTS), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from release_forwarding import (  # noqa: E402
    NON_PRODUCT_PREFIX,
    VERSION,
    _flag,
    atomic_write_json,
    is_prerelease_tag,
    load_targets,
    memory_dir,
    normalize_tag,
)

log = logging.getLogger("clawbytes.release_alerts")

STATE_NAME = "release-alerts-state.json"
MAX_ALERTS_PER_RUN = 10
MAX_BULLETS = 4
BULLET_CHARS = 200
TELEGRAM_LIMIT = 4096
GH_PAGE_SIZE = 15
HTTP_TIMEOUT = 15
KEEP_ALERTED = 50

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Version tokens in a release title (Hermes: "v0.20.6 (v2026.8.27)").
_TITLE_VERSION = re.compile(r"(?<![\w.])(?:rust-v|v)?(\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)(?![\w])")
_CALENDAR = re.compile(r"^\d{4}\.\d{1,2}\.\d")


def alerts_chat_id() -> str:
    return os.environ.get("CLAWBYTES_RELEASE_ALERTS_CHAT_ID", "").strip()


def alerts_enabled() -> bool:
    flag = os.environ.get("CLAWBYTES_RELEASE_ALERTS", "").strip().lower()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return bool(flag in _TRUTHY and alerts_chat_id() and token)


def alert_targets(targets: dict) -> dict:
    return {repo: row for repo, row in targets.items() if _flag(row.get("alert")) or _flag(row.get("forward"))}


# ── fetch ───────────────────────────────────────────────────────────

def fetch_json(url: str, timeout: float = HTTP_TIMEOUT):
    """Parsed JSON, or None on any failure."""
    headers = {"User-Agent": "ClawBytes/1.0"}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
        log.info("release_alerts: fetch failed %s (%s)", url.split("?")[0], type(exc).__name__)
        return None


def qualifies(release: dict, row: dict) -> bool:
    if not isinstance(release, dict) or release.get("draft"):
        return False
    tag = str(release.get("tag_name") or "").strip()
    if not tag or NON_PRODUCT_PREFIX.search(tag) or not VERSION.match(tag):
        return False
    pattern = str(row.get("tag_pattern") or "")
    if pattern and re.search(pattern, tag) is None:
        return False
    prefixes = [str(p).lower() for p in row.get("tag_prefixes") or []]
    if prefixes and not tag.lower().startswith(tuple(prefixes)):
        return False
    if (release.get("prerelease") or is_prerelease_tag(tag)) and not _flag(row.get("prereleases")):
        return False
    return True


def github_releases(repo: str, row: dict) -> list | None:
    """Qualifying releases, newest first. None when the fetch failed."""
    data = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page={GH_PAGE_SIZE}")
    if not isinstance(data, list):
        return None
    return [rel for rel in data if qualifies(rel, row)]


def npm_latest(package: str) -> str | None:
    quoted = urllib.parse.quote(package, safe="@/")
    data = fetch_json(f"https://registry.npmjs.org/{quoted}/latest")
    version = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
    return version or None


def npm_published(package: str, version: str) -> str:
    quoted = urllib.parse.quote(package, safe="@/")
    data = fetch_json(f"https://registry.npmjs.org/{quoted}")
    times = data.get("time") if isinstance(data, dict) else None
    return str(times.get(version) or "") if isinstance(times, dict) else ""


def release_aliases(release: dict) -> list[str]:
    """Normalized tag plus versions named in the title, for npm/GitHub dedupe."""
    tag = str(release.get("tag_name") or "")
    out = [normalize_tag(tag)]
    for match in _TITLE_VERSION.finditer(str(release.get("name") or "")):
        if match.group(1) not in out:
            out.append(match.group(1))
    return [a for a in out if a]


def display_version(release: dict) -> str:
    """Normalized tag; a semver in the title wins over a calendar tag (Hermes)."""
    version = normalize_tag(str(release.get("tag_name") or ""))
    if _CALENDAR.match(version):
        for alias in release_aliases(release)[1:]:
            if not _CALENDAR.match(alias):
                return alias
    return version


# ── message ─────────────────────────────────────────────────────────

_FENCE = re.compile(r"^(```|~~~)")
_RULE = re.compile(r"^[-*_=]{3,}$")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_COMMIT_PREFIX = re.compile(r"^(?:feat|fix|chore|docs|refactor|perf|test|ci|build|style)(?:\([^)]*\))?!?:\s*", re.I)
_PR_SUFFIX = re.compile(r"\s+by\s+@\S+\s+in\s+https?://\S+", re.I)


def strip_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[`?(?:#\d+|[0-9a-fA-F]{7,40})`?\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    # Only real HTML tags; "<command>" placeholders in notes stay.
    text = re.sub(r"</?(?:a|b|br|code|details|div|em|hr|i|img|kbd|p|picture|source|span|strong|sub|summary|sup|video)\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*|__([^_]*)__", lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = text.replace("**", "")
    text = _PR_SUFFIX.sub(" ", text)
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _COMMIT_PREFIX.sub("", text).strip()


def _clip(text: str, limit: int = BULLET_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    if cut < limit // 2:
        cut = limit - 1
    return text[:cut].rstrip(" ,;:-–—") + "…"


def _is_noise(text: str, title: str) -> bool:
    folded = text.casefold().replace("’", "'")
    if folded.startswith("full changelog") or "made their first contribution" in folded:
        return True
    if re.fullmatch(r"https?://\S+", text):
        return True
    if folded.rstrip(":") in {"what's changed", "what's new", "whats new", "changelog", "new contributors"}:
        return True
    # Section labels ("Bug fixes:", "**Features**").
    if len(text) < 40 and text.endswith(":"):
        return True
    return bool(title) and folded == title.casefold()


def note_bullets(body: str, title: str = "", limit: int = MAX_BULLETS) -> list[str]:
    """Up to ``limit`` plain, cleaned lines from release notes."""
    out: list[str] = []
    fenced = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or not line or line.startswith("#") or _RULE.match(line):
            continue
        if re.fullmatch(r"(\*\*|__)[^*_]+(\*\*|__):?", line) and not line.rstrip(":*_").endswith("."):
            continue
        line = re.sub(r"^>\s*", "", line)
        line = _BULLET.sub("", line)
        text = strip_markdown(line)
        if not text or _is_noise(text, title):
            continue
        out.append(_clip(text))
        if len(out) >= limit:
            break
    return out


def format_published(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local, zone = parsed.astimezone(ZoneInfo("America/New_York")), "ET"
    except Exception:  # noqa: BLE001 - no tzdata in the image
        local, zone = parsed.astimezone(timezone.utc), "UTC"
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{_MONTHS[local.month - 1]} {local.day}, {local.year} {hour}:{local.minute:02d} {ampm} {zone}"


def build_message(name: str, version: str, *, body: str = "", url: str = "",
                  published: str = "", docker: str = "", prerelease: bool = False) -> str:
    title = f"{name} {version}".strip()
    header = f"<b>{html.escape(title)}</b>"
    if prerelease:
        header += " <i>(pre-release)</i>"
    tail: list[str] = []
    meta = []
    when = format_published(published)
    if when:
        meta.append(html.escape(when))
    if docker:
        meta.append(f"🐳 <code>{html.escape(docker)}</code>")
    if meta:
        tail.append("\n".join(meta))
    if url:
        tail.append(f'🔗 <a href="{html.escape(url, quote=True)}">Release notes</a>')

    bullets = note_bullets(body, title)
    while True:
        parts = [header]
        if bullets:
            parts.append("\n".join(f"• {html.escape(b)}" for b in bullets))
        message = "\n\n".join(parts + tail)
        if len(message) <= TELEGRAM_LIMIT or not bullets:
            return message[:TELEGRAM_LIMIT]
        bullets.pop()


# ── send ────────────────────────────────────────────────────────────

def send_alert(message: str) -> bool:
    """Shared publish core (sanitize, retry, never raise) to the alerts chat. No Slack mirror."""
    from ss_publish import Publisher, redact_secrets

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    pub = Publisher(
        telegram_token=token,
        telegram_channel_id=alerts_chat_id(),
        disable_preview=True,
        secret_values=(token,),
    )
    result = pub.send_telegram(message)
    if not result.ok:
        log.warning("release_alerts: send failed: %s", redact_secrets(result.error, (token,)))
    return result.ok


# ── run ─────────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    """Missing file → empty. Corrupt file raises ValueError (do not overwrite)."""
    if not path.exists():
        return {"version": 1, "targets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(path.name) from exc
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise ValueError(path.name)
    return data


def _pending_for(repo: str, row: dict, entry: dict) -> tuple[list[dict], bool]:
    """Fetch both sources. Baseline new sources in ``entry``; return (alerts, entry changed)."""
    alerted = entry.setdefault("alerted", [])
    changed = False
    pending: list[dict] = []

    releases = github_releases(repo, row)
    newest = releases[0] if releases else None
    if releases is not None:
        tag = str((newest or {}).get("tag_name") or "")
        if "github" not in entry:
            entry["github"] = tag
            alerted.extend(a for a in release_aliases(newest or {}) if a not in alerted)
            changed = True
        elif tag and tag != entry["github"]:
            if {normalize_tag(tag), display_version(newest)} & set(alerted):
                entry["github"] = tag
                changed = True
            else:
                pending.append({"release": newest, "version": display_version(newest),
                                "record": {"github": tag}, "aliases": release_aliases(newest)})

    package = str(row.get("npm") or "").strip()
    version = npm_latest(package) if package else None
    if version:
        if "npm" not in entry:
            entry["npm"] = version
            if normalize_tag(version) not in alerted:
                alerted.append(normalize_tag(version))
            changed = True
        elif version != entry["npm"]:
            norm = normalize_tag(version)
            if norm in alerted:
                entry["npm"] = version
                changed = True
            else:
                merged = next((p for p in pending if norm in p["aliases"]), None)
                if merged:
                    merged["record"]["npm"] = version
                else:
                    match = next((r for r in releases or [] if norm in release_aliases(r)), None)
                    pending.append({"release": match, "version": norm, "npm": package,
                                    "record": {"npm": version},
                                    "aliases": release_aliases(match) if match else [norm]})
                    if norm not in pending[-1]["aliases"]:
                        pending[-1]["aliases"].append(norm)
    return pending, changed


def _message_for(row: dict, repo: str, alert: dict) -> str:
    release = alert.get("release") or {}
    version = alert["version"]
    url = str(release.get("html_url") or "")
    published = str(release.get("published_at") or "")
    if not release and alert.get("npm"):
        package = alert["npm"]
        url = f"https://www.npmjs.com/package/{urllib.parse.quote(package, safe='@/')}/v/{version}"
        published = npm_published(package, version)
    docker_tmpl = str(row.get("docker") or "")
    return build_message(
        str(row.get("name") or repo),
        version,
        body=str(release.get("body") or ""),
        url=url,
        published=published,
        docker=docker_tmpl.format(version=version) if docker_tmpl else "",
        prerelease=bool(release.get("prerelease")) or is_prerelease_tag(version),
    )


def _run(*, memory: Path, targets: dict | None, max_alerts: int) -> str:
    if not alerts_enabled():
        return "disabled"
    path = memory / STATE_NAME
    try:
        state = load_state(path)
    except ValueError:
        log.warning("release_alerts: state unreadable, not overwriting")
        return "error"
    table = alert_targets(load_targets() if targets is None else targets)
    sent = baselined = 0
    for repo, row in table.items():
        entry = state["targets"].setdefault(repo.lower(), {})
        had_sources = {"github", "npm"} & set(entry)
        pending, changed = _pending_for(repo, row, entry)
        if changed:
            if not had_sources:
                baselined += 1
            atomic_write_json(path, state)
        for alert in pending:
            if sent >= max_alerts:
                log.info("release_alerts: cap %d reached, rest wait for next run", max_alerts)
                return f"capped sent={sent}"
            if not send_alert(_message_for(row, repo, alert)):
                return f"send_failed sent={sent}"
            sent += 1
            entry.update(alert["record"])
            alerted = entry.setdefault("alerted", [])
            alerted.extend(a for a in alert["aliases"] if a not in alerted)
            del alerted[:-KEEP_ALERTED]
            atomic_write_json(path, state)
            log.info("release_alerts: sent %s %s", repo, alert["version"])
    return f"ok sent={sent} baselined={baselined}"


def run_release_alerts(*, memory: Path | None = None, targets: dict | None = None,
                       max_alerts: int = MAX_ALERTS_PER_RUN) -> str:
    """One pass. Returns an outcome token. Never raises."""
    try:
        return _run(memory=memory_dir(memory), targets=targets, max_alerts=max_alerts)
    except Exception:  # noqa: BLE001 - collect must survive an alerts bug
        log.exception("release_alerts: error")
        return "error"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"release_alerts: {run_release_alerts()}")
