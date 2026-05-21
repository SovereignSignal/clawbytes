from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .editorial import EditorialClient
from .loaders import load_clawbytes_items, load_modelbytes_items
from .models import WeeklyPacket


def build_weekly_packet(clawbytes: Path, modelbytes: Path, *, days: int = 7) -> WeeklyPacket:
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=days)).date().isoformat()
    week_end = now.date().isoformat()

    claw_sections = load_clawbytes_items(clawbytes, days=days)
    model_moves = load_modelbytes_items(modelbytes, days=days)
    sections = {
        "agent_ops": claw_sections["agent_ops"],
        "model_moves": model_moves,
        "watchlist": claw_sections["watchlist"],
        "reading": claw_sections["reading"],
        "community": claw_sections["community"],
    }
    editorial = EditorialClient().generate_takeaways(sections)
    return WeeklyPacket.new(
        week_start=week_start,
        week_end=week_end,
        source_repos={
            "clawbytes": str(clawbytes.resolve()),
            "modelbytes": str(modelbytes.resolve()),
        },
        sections=sections,
        consulting_takeaways=editorial.takeaways,
        editorial=editorial.metadata,
    )

