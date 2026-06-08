from __future__ import annotations

import json
from pathlib import Path

from claw_content_engine.loaders import _parse_modelbytes_digest
from claw_content_engine.packet import build_weekly_packet
from claw_content_engine.renderers import render_linkedin_posts, render_spiral_packet


# Real June 2026 ModelBytes format: section headers are `<b>{emoji} Name</b>`
# (no `━` bar, emoji inside the bold) and models are newline-separated inside
# one section rather than blank-line-delimited blocks.
NEW_FORMAT_DIGEST = """
\U0001f916 <b>ModelBytes Digest</b>
<i>Thursday, June 04, 2026</i>

<b>\U0001f513 Premier Open</b>
<b>MiniCPM-V-4.6</b> — Released Apr 13. Multimodal image-text-to-text model. <a href="https://huggingface.co/openbmb/MiniCPM-V-4.6">→ Source</a>
<b>Nemotron-3-Ultra-550B</b> — Released Jun 3. Text-generation model. <a href="https://huggingface.co/nvidia/Nemotron-3-Ultra-550B">→ Source</a>

<b>\U0001f512 Closed Giants</b>
<b>magenta-realtime-2</b> — Released May 28. ML model. <a href="https://huggingface.co/google/magenta-realtime-2">→ OpenRouter</a>

Total: 3 models tracked today
""".strip()


def test_parse_modelbytes_new_section_format_yields_one_item_per_model() -> None:
    items = _parse_modelbytes_digest(NEW_FORMAT_DIGEST, published_at="2026-06-04")

    titles = [item.title for item in items]
    assert titles == [
        "MiniCPM-V-4.6",
        "Nemotron-3-Ultra-550B",
        "magenta-realtime-2",
    ]
    # The section label must never leak in as a model title.
    assert not any("Premier Open" in t or "Closed Giants" in t for t in titles)
    # Each summary holds only its own model, not the whole section blob.
    assert items[0].summary == "Released Apr 13. Multimodal image-text-to-text model."
    assert "→" not in items[0].summary
    assert "Nemotron" not in items[0].summary
    assert items[0].url == "https://huggingface.co/openbmb/MiniCPM-V-4.6"
    # Section label (emoji stripped) is carried through as the category.
    assert items[0].category == "premier open"
    assert items[2].category == "closed giants"


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
