from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SignalItem:
    title: str
    url: str = ""
    summary: str = ""
    source: str = ""
    category: str = ""
    published_at: str = ""
    score: float | int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "source": self.source,
            "category": self.category,
            "published_at": self.published_at,
            "score": self.score,
            "metadata": self.metadata,
        }


@dataclass
class WeeklyPacket:
    generated_at: str
    week_start: str
    week_end: str
    source_repos: dict[str, str]
    sections: dict[str, list[SignalItem]]
    consulting_takeaways: list[str]
    editorial: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        week_start: str,
        week_end: str,
        source_repos: dict[str, str],
        sections: dict[str, list[SignalItem]],
        consulting_takeaways: list[str],
        editorial: dict[str, Any] | None = None,
    ) -> "WeeklyPacket":
        return cls(
            generated_at=datetime.now(timezone.utc).isoformat(),
            week_start=week_start,
            week_end=week_end,
            source_repos=source_repos,
            sections=sections,
            consulting_takeaways=consulting_takeaways,
            editorial=editorial or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "week_start": self.week_start,
            "week_end": self.week_end,
            "source_repos": self.source_repos,
            "sections": {
                key: [item.as_dict() for item in items]
                for key, items in self.sections.items()
            },
            "consulting_takeaways": self.consulting_takeaways,
            "editorial": self.editorial,
        }

