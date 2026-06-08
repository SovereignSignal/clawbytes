from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from .models import SignalItem


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _within_days(value: str | None, days: int) -> bool:
    dt = _parse_dt(value)
    if dt is None:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=days)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_clawbytes_items(repo: Path, *, days: int = 7, limit: int = 40) -> dict[str, list[SignalItem]]:
    """Load recent ClawBytes backlog items and group them into buyer-facing lanes."""
    backlog_path = repo / "memory" / "clawbytes-backlog.json"
    backlog = _load_json(backlog_path, {"items": []})
    grouped = {
        "agent_ops": [],
        "watchlist": [],
        "reading": [],
        "community": [],
    }

    for raw in backlog.get("items", []):
        published_at = raw.get("publishedAt") or raw.get("discoveredAt") or raw.get("postedAt")
        if not _within_days(published_at, days):
            continue
        status = (raw.get("status") or "").lower()
        if status and status not in {"posted", "queued", "published"}:
            continue

        categories = raw.get("categories") or [raw.get("primaryCategory") or raw.get("category") or ""]
        primary = (raw.get("primaryCategory") or categories[0] or "").lower()
        item = SignalItem(
            title=(raw.get("title") or raw.get("name") or "Untitled signal").strip(),
            url=raw.get("url") or raw.get("link") or "",
            summary=(raw.get("summary") or raw.get("blurb") or raw.get("why") or "").strip(),
            source=raw.get("sourceName") or raw.get("sourceType") or "clawbytes",
            category=primary,
            published_at=published_at or "",
            score=raw.get("score"),
            metadata={
                "status": raw.get("status"),
                "source_type": raw.get("sourceType"),
                "raw_category": raw.get("category"),
            },
        )

        if primary == "watch":
            grouped["watchlist"].append(item)
        elif primary == "read":
            grouped["reading"].append(item)
        elif primary == "community":
            grouped["community"].append(item)
        else:
            grouped["agent_ops"].append(item)

    for key, items in grouped.items():
        grouped[key] = sorted(
            items,
            key=lambda item: (item.score or 0, item.published_at),
            reverse=True,
        )[:limit]
    return grouped


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return unescape(parser.text())


def load_modelbytes_items(repo: Path, *, days: int = 7, limit: int = 30) -> list[SignalItem]:
    """Load recent ModelBytes pending digest entries as model-move signals."""
    pending_dir = repo / "pending"
    if not pending_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    items: list[SignalItem] = []
    for path in sorted(pending_dir.glob("*.txt")):
        try:
            digest_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if digest_date < cutoff:
            continue
        body = path.read_text(encoding="utf-8")
        items.extend(_parse_modelbytes_digest(body, published_at=path.stem))

    return items[:limit]


_MB_BAR = "\u2501"  # \u2501 heavy horizontal \u2014 old-format section bar
_MB_BULLET = "\u2022"  # \u2022
_MB_EMDASH = "\u2014"  # \u2014
_MB_ARROW = "\u2192"  # \u2192


def _strip_section_label(label: str) -> str:
    """Reduce a section header's bold text to its words (drop bars/emoji)."""
    label = label.replace(_MB_BAR, " ")
    label = re.sub(r"^\W+", "", label)
    label = re.sub(r"\W+$", "", label)
    return label.strip()


def _modelbytes_model_item(line: str, section: str, published_at: str) -> SignalItem | None:
    """Parse a single model line: `[\u2022] <b>Name</b> \u2014 desc <a href>\u2192 Source</a>`."""
    stripped = re.sub(rf"^{_MB_BULLET}\s*", "", line.strip())
    title_match = re.search(r"<b>([^<]+)</b>", stripped)
    if not title_match:
        return None
    title = unescape(title_match.group(1).strip())
    link_match = re.search(r'<a\s+href="([^"]+)"', stripped)
    text = _html_to_text(stripped)
    text = re.sub(rf"^{re.escape(title)}\s*", "", text).strip()
    text = re.sub(rf"^[{_MB_EMDASH}\-]\s*", "", text).strip()
    text = re.sub(rf"\s*{_MB_ARROW}\s*[\w ]+$", "", text).strip()
    return SignalItem(
        title=title,
        url=unescape(link_match.group(1)) if link_match else "",
        summary=text,
        source="modelbytes",
        category=section.lower() if section else "model release",
        published_at=published_at,
    )


def _parse_modelbytes_digest(body: str, *, published_at: str) -> list[SignalItem]:
    """Parse a ModelBytes digest into one SignalItem per model.

    Handles both digest layouts: the older `\u2501\u2501\u2501 <b>SECTION</b>` headers with
    blank-line-delimited model blocks, and the current `<b>{emoji} Section</b>`
    headers with models listed one-per-line. Parsing line-by-line keeps a section
    header from being mistaken for a model (the bug that collapsed an entire
    section into a single run-on entry).
    """
    entries: list[SignalItem] = []
    current_section = ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "ModelBytes Digest" in line or line.startswith("<i>") or line.startswith("Total:"):
            continue

        # Section header: a lone <b>\u2026</b> label (optionally bar/emoji wrapped)
        # with no model description (em-dash) or link.
        bold = re.search(r"<b>([^<]+)</b>", line)
        if bold and _MB_ARROW not in line and _MB_EMDASH not in line and "<a " not in line:
            remainder = line.replace(bold.group(0), "").replace(_MB_BAR, "")
            if not re.search(r"[0-9A-Za-z]", remainder):  # only emoji/space remain
                current_section = _strip_section_label(bold.group(1))
                continue

        item = _modelbytes_model_item(line, current_section, published_at)
        if item is not None:
            entries.append(item)
    return entries
