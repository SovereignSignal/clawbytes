"""Filler lines, prompt guards, and a closing sentence that repeats one bullet.

#972, #975, and #976 are the fixtures. #971's closing line names two threads
and stays. An all-filler lane is skipped, not sent.
"""
from pathlib import Path

import clawbytes_threads as ct

ROOT = Path(__file__).resolve().parent.parent


def test_filler_blurbs_from_the_channel_drop_and_a_real_change_stays():
    assert ct.blurb_is_filler(
        "Advancing computer use with Ironclad",
        "OpenAI post on advancing computer use with Ironclad.",
    )
    assert ct.blurb_is_filler(
        "Introducing Mistral Large 4",
        "Mistral announces Mistral Large 4.",
    )
    assert ct.blurb_is_filler(
        "Codex 0.161.0",
        "Codex 0.161.0, tagged rust-v0.161.0 in openai/codex and published 2026-10-06 — release notes at the link.",
    )
    assert ct.blurb_is_filler(
        "Copilot HydraFusion in VS Code and the GitHub app",
        "changelog details what's improved.",
    )
    assert not ct.blurb_is_filler(
        "Runmaestro/Maestro 0.17.8",
        "It now listens on 127.0.0.1 only until Live is turned on.",
    )


def test_closing_line_that_repeats_one_bullet_is_removed():
    # #976
    post = (
        "📚 <b>Read</b> — 2 items\n\n"
        '📚 <a href="https://simonwillison.net/2026/Oct/6/le-chonk/">Introducing Mistral Large 4: Le chonk</a>'
        " — Mistral ships a preview of Mistral Large 4 — a 1 trillion parameter, 49 billion active parameter model"
        " trained on their own cluster.\n\n"
        '📚 <a href="https://claude.dev/blog/claude-code-in-the-cloud/">Claude Code in the cloud</a>'
        " — Addy Osmani's field guide to running Claude Code on its own machine: what changes about cloud sessions.\n\n"
        "Mistral Large 4's preview is a 1 trillion parameter, 49 billion active parameter model."
    )
    cleaned = ct.polish_lane_post(post)
    assert "1 trillion parameter" in cleaned  # still in the bullet
    assert not cleaned.strip().endswith("parameter model.")
    assert cleaned.count("<a href=") == 2


def test_closing_line_that_connects_two_items_stays():
    # #971
    post = (
        "💬 <b>Community</b> — 2 items\n\n"
        '💬 <a href="https://news.ycombinator.com/item?id=49977979">Mistral Large 4</a>'
        " — Mistral Large 4 discussion — 1037 points / 680 comments on HN.\n\n"
        '💬 <a href="https://news.ycombinator.com/item?id=49942865">ColonistOne</a>'
        " — An agent emailed researchers — 50 points / 80 comments on HN.\n\n"
        "Mistral Large 4 drew 680 comments on HN; the ColonistOne agent story drew 80."
    )
    cleaned = ct.polish_lane_post(post)
    assert "drew 680 comments" in cleaned
    assert "ColonistOne agent story drew 80" in cleaned


def test_filler_only_lane_is_not_sent(monkeypatch):
    html = (
        "📚 <b>Read</b> — 1 item\n\n"
        '📚 <a href="https://example.com/m">Introducing Mistral Large 4</a>'
        " — Mistral announces Mistral Large 4."
    )
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    monkeypatch.setattr(ct, "format_category_bundle", lambda *a, **k: html)
    monkeypatch.setattr(ct, "bundle_for_category", lambda *a, **k: [{"id": "1"}])

    def _no_send(message):
        raise AssertionError(message)

    monkeypatch.setattr(ct, "send_telegram", _no_send)
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: (_ for _ in ()).throw(AssertionError("marked")))
    sent, count = ct._publish_lane("read", send=True)
    assert (sent, count) == (False, 0)


def test_writer_and_curator_prompts_carry_the_new_rules(monkeypatch):
    monkeypatch.setattr(ct, "grounding_for_item", lambda category, item: "")
    prompt, _facts = ct._writer_inputs(
        [{"title": "Example", "url": "", "summary": "s", "sourceType": "rss"}],
        "ship",
    )
    assert "bare domains" in prompt
    assert "@handles" in prompt
    assert "Do not add a closing paragraph" in prompt
    assert "one concrete change" in prompt
    assert "Not a funding announcement" in prompt
    curator = (ROOT / "docs" / "curator-prompt.md").read_text()
    assert "Lane definitions" in curator
    assert "bare domains" in curator
    assert "leave `take` as an empty string" in curator
    assert "nothing substantive left" in curator
