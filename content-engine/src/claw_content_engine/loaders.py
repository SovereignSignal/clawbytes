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


def _parse_modelbytes_digest(body: str, *, published_at: str) -> list[SignalItem]:
    entries: list[SignalItem] = []
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    current_section = ""
    for block in blocks:
        section_match = re.search(r"<b>([^<]+)</b>", block)
        plain = _html_to_text(block)
        if "\u2501\u2501\u2501" in block and section_match:
            current_section = section_match.group(1).strip()
            continue
        if "Total:" in plain or "ModelBytes Digest" in plain:
            continue

        if "\u2022" in block and block.count("<b>") > 1:
            entries.extend(_parse_modelbytes_bullets(block, current_section, published_at))
            continue

        title_match = re.search(r"(?:^|[\u2022]\s*)<b>([^<]+)</b>", block)
        link_match = re.search(r'<a\s+href="([^"]+)"', block)
        if not title_match:
            continue

        title = unescape(title_match.group(1).strip())
        text = _html_to_text(block)
        text = re.sub(rf"^{re.escape(title)}\s*[\u2014-]?\s*", "", text).strip()
        text = re.sub(r"\s*\u2192\s*(Source|HF)\s*$", "", text).strip()
        entries.append(
            SignalItem(
                title=title,
                url=unescape(link_match.group(1)) if link_match else "",
                summary=text,
                source="modelbytes",
                category=current_section.lower() if current_section else "model release",
                published_at=published_at,
            )
        )
    return entries


def _parse_modelbytes_bullets(block: str, current_section: str, published_at: str) -> list[SignalItem]:
    items: list[SignalItem] = []
    segments = re.split(r"(?=\u2022\s*<b>)", block)
    for segment in segments:
        if not segment.strip():
            continue
        title_match = re.search(r"\u2022\s*<b>([^<]+)</b>", segment)
        if not title_match:
            continue
        title = unescape(title_match.group(1).strip())
        link_match = re.search(r'<a\s+href="([^"]+)"', segment)
        text = _html_to_text(segment)
        text = re.sub(rf"^\u2022\s*{re.escape(title)}\s*[\u2014-]?\s*", "", text).strip()
        text = re.sub(r"\s*\u2192\s*(Source|HF)\s*$", "", text).strip()
        items.append(
            SignalItem(
                title=title,
                url=unescape(link_match.group(1)) if link_match else "",
                summary=text,
                source="modelbytes",
                category=current_section.lower() if current_section else "model release",
                published_at=published_at,
            )
        )
    return items
