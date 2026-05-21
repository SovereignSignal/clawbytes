from __future__ import annotations

import json
from pathlib import Path

from .models import SignalItem, WeeklyPacket


SECTION_LABELS = {
    "agent_ops": "What changed in agent operations",
    "model_moves": "Model moves that matter",
    "watchlist": "Operational risk / watchlist",
    "reading": "Longer reads worth a skim",
    "community": "Builder community pulse",
}


def write_outputs(packet: WeeklyPacket, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / "weekly_signal_packet.json",
        "spiral": out_dir / "spiral_packet.md",
        "substack": out_dir / "substack_draft.md",
        "linkedin": out_dir / "linkedin_posts.md",
        "slack": out_dir / "slack_summary.md",
    }
    paths["json"].write_text(json.dumps(packet.as_dict(), indent=2), encoding="utf-8")
    paths["spiral"].write_text(render_spiral_packet(packet), encoding="utf-8")
    paths["substack"].write_text(render_substack_draft(packet), encoding="utf-8")
    paths["linkedin"].write_text(render_linkedin_posts(packet), encoding="utf-8")
    paths["slack"].write_text(render_slack_summary(packet), encoding="utf-8")
    return paths


def render_spiral_packet(packet: WeeklyPacket) -> str:
    lines = [
        "# Claw Consulting Weekly Brief - Source Packet",
        "",
        f"Week: {packet.week_start} to {packet.week_end}",
        "",
        "Audience: CTOs, founders, and operators evaluating governed AI agents.",
        "Goal: Turn the week's technical movement into buyer-useful judgment.",
        "",
        "## Editorial Angle",
        "",
    ]
    for takeaway in packet.consulting_takeaways:
        lines.append(f"- {takeaway}")
    lines.append("")

    for key in ["agent_ops", "model_moves", "watchlist", "reading", "community"]:
        lines.extend(_render_items_section(key, packet.sections.get(key, [])))

    lines.extend(
        [
            "## Draft Instructions for Spiral",
            "",
            "Write one polished Substack issue for Claw Consulting. Keep it practical, specific, and sober. Make the consulting angle clear without sounding like an ad. End with a soft CTA to talk about governed agent operations.",
            "",
        ]
    )
    return "\n".join(lines)


def render_substack_draft(packet: WeeklyPacket) -> str:
    title = "This Week in Governed Agent Operations"
    lines = [
        f"# {title}",
        "",
        "_A Claw Consulting brief on the agent and model signals worth turning into operating decisions._",
        "",
        "## The Short Version",
        "",
    ]
    for takeaway in packet.consulting_takeaways[:4]:
        lines.append(f"- {takeaway}")
    lines.append("")

    for key in ["agent_ops", "model_moves", "watchlist"]:
        lines.extend(_render_items_section(key, packet.sections.get(key, [])[:6]))

    lines.extend(
        [
            "## What Claw Would Tell a Client",
            "",
            "Do not respond to the week by adopting every new tool. Pick one workflow, one risk surface, and one model assumption to test under the governance constraints you already have.",
            "",
            "If your team is moving from experiments to operated agents, Claw Consulting helps define the roadmap, pilot, governance model, and context layer before scale turns messy.",
            "",
        ]
    )
    return "\n".join(lines)


def render_linkedin_posts(packet: WeeklyPacket) -> str:
    top_agent = _first(packet.sections.get("agent_ops", []))
    top_model = _first(packet.sections.get("model_moves", []))
    watch = _first(packet.sections.get("watchlist", []))
    model_fallback = "the week's strongest model move"

    posts = [
        [
            "## LinkedIn Post 1 - Flagship",
            "",
            'The useful question is not, "What shipped this week?"',
            "",
            'It is: "What changed that should alter how we operate AI agents?"',
            "",
            *[f"- {takeaway}" for takeaway in packet.consulting_takeaways[:3]],
            "",
            "For teams moving from demos to deployed agents, the work is governance, context, evals, ownership, and rollback paths.",
            "",
            "#AIAgents #AIGovernance #AgentOperations",
        ],
        [
            "## LinkedIn Post 2 - Model Strategy",
            "",
            f"Model release to watch: {top_model.title if top_model else model_fallback}.",
            "",
            (top_model.summary if top_model and top_model.summary else "The model landscape keeps improving, but production teams still need to test cost, latency, licensing, and fit before swapping defaults."),
            "",
            "Better models matter most when they make a governed workflow cheaper, faster, or more reliable.",
            "",
            "#LLMOps #AIModels #AIOperations",
        ],
        [
            "## LinkedIn Post 3 - Operator Risk",
            "",
            f"Operational watch item: {watch.title if watch else 'agent risk is becoming an operating discipline, not a side note'}.",
            "",
            (watch.summary if watch and watch.summary else "Every new agent capability expands the surface area: tools, credentials, auth, packages, logs, and human approval paths."),
            "",
            "The teams that win will keep shipping while making risk visible enough to manage.",
            "",
            "#AIGovernance #CyberSecurity #AgentOps",
        ],
    ]
    return "\n\n---\n\n".join("\n".join(post) for post in posts)


def render_slack_summary(packet: WeeklyPacket) -> str:
    lines = [
        f"*Claw weekly brief packet* ({packet.week_start} to {packet.week_end})",
        "",
        "*Consulting takeaways*",
    ]
    for takeaway in packet.consulting_takeaways[:4]:
        lines.append(f"- {takeaway}")
    lines.append("")
    for key in ["agent_ops", "model_moves", "watchlist"]:
        label = SECTION_LABELS[key]
        lines.append(f"*{label}*")
        items = packet.sections.get(key, [])[:3]
        if not items:
            lines.append("- No strong signals captured.")
        for item in items:
            link = f" <{item.url}>" if item.url else ""
            summary = f" - {item.summary}" if item.summary else ""
            lines.append(f"- {item.title}{link}{summary}")
        lines.append("")
    lines.append("Ready for Spiral/Substack review.")
    return "\n".join(lines)


def _render_items_section(key: str, items: list[SignalItem]) -> list[str]:
    lines = [f"## {SECTION_LABELS.get(key, key.replace('_', ' ').title())}", ""]
    if not items:
        lines.extend(["No strong signals captured this week.", ""])
        return lines
    for item in items:
        title = f"[{item.title}]({item.url})" if item.url else item.title
        lines.append(f"- **{title}**")
        if item.summary:
            lines.append(f"  {item.summary}")
    lines.append("")
    return lines


def _first(items: list[SignalItem] | None) -> SignalItem | None:
    return items[0] if items else None
