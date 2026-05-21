from __future__ import annotations

import json
from pathlib import Path

from claw_content_engine.packet import build_weekly_packet
from claw_content_engine.renderers import render_linkedin_posts, render_spiral_packet


def test_build_weekly_packet_from_repo_like_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITORIAL_PROVIDER", "none")
    claw = tmp_path / "clawbytes"
    model = tmp_path / "modelbytes"
    (claw / "memory").mkdir(parents=True)
    (model / "pending").mkdir(parents=True)
    (claw / "memory" / "clawbytes-backlog.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "title": "Agent SDK ships safer tool approvals",
                        "url": "https://example.com/agent-sdk",
                        "summary": "Adds explicit approval hooks for tool calls.",
                        "primaryCategory": "ship",
                        "status": "posted",
                        "score": 120,
                    },
                    {
                        "title": "Framework advisory",
                        "url": "https://example.com/advisory",
                        "summary": "Auth bypass patched in a popular agent dashboard.",
                        "primaryCategory": "watch",
                        "status": "posted",
                        "score": 140,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (model / "pending" / "2026-05-21.txt").write_text(
        """
<b>ModelBytes Digest</b>

\u2501\u2501\u2501 <b>PREMIER OPEN</b>

<b>Command A+</b> \u2014 Apache 2.0 model with 128K context. <a href="https://example.com/model">\u2192 Source</a>

Total: 1 models tracked today
""".strip(),
        encoding="utf-8",
    )

    packet = build_weekly_packet(claw, model, days=3650)

    assert packet.sections["agent_ops"][0].title == "Agent SDK ships safer tool approvals"
    assert packet.sections["watchlist"][0].title == "Framework advisory"
    assert packet.sections["model_moves"][0].title == "Command A+"
    assert packet.consulting_takeaways


def test_renderers_include_manual_distribution_surfaces(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITORIAL_PROVIDER", "none")
    claw = tmp_path / "clawbytes"
    model = tmp_path / "modelbytes"
    (claw / "memory").mkdir(parents=True)
    (model / "pending").mkdir(parents=True)
    (claw / "memory" / "clawbytes-backlog.json").write_text('{"items":[]}', encoding="utf-8")
    (model / "pending" / "2026-05-21.txt").write_text(
        '<b>ModelBytes Digest</b>\n\n<b>Model X</b> \\u2014 Useful model. <a href="https://example.com/x">\\u2192 Source</a>',
        encoding="utf-8",
    )
    packet = build_weekly_packet(claw, model, days=3650)

    spiral = render_spiral_packet(packet)
    linkedin = render_linkedin_posts(packet)

    assert "Draft Instructions for Spiral" in spiral
    assert "LinkedIn Post 1" in linkedin
    assert "Substack" not in linkedin
