from datetime import datetime, timezone

import clawbytes_threads as ct


def _rss_item(feed, title, high_signal=True):
    return {
        "feed": feed,
        "title": title,
        "link": "https://example.com/post",
        "published": datetime.now(timezone.utc).isoformat(),
        "high_signal": high_signal,
    }


def test_harness_vocab_routes_to_read():
    # Title deliberately avoids "agent"/"mcp" substrings so it does NOT match
    # the pre-widening READ_TERMS (note: "subagent" would match "agent").
    item = _rss_item("Simon Willison", "Skills and hooks: harness context engineering patterns")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


def test_copilot_changelog_routes_to_ship():
    # Feed name is "Changelog" not "Releases"/"Release Notes" — the 2026-08
    # routing fix Ships coding-agent changelogs instead of dumping them in Read.
    item = _rss_item("GitHub Copilot Changelog", "GitHub Copilot CLI adds slash commands")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 58  # clears Ship window 1 without leaning on age


def test_cursor_changelog_routes_to_ship():
    item = _rss_item(
        "Cursor Changelog",
        "Cloud Agents and Cursor Harness Improvements",
        high_signal=True,
    )
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= ct.REPO_PRIORITY["cursor"]


def test_amp_news_routes_to_ship():
    item = _rss_item("Amp News", "MCP in Orbs")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert ct.repo_name_from_feed("Amp News") == "amp news"


def test_coding_agent_tagged_blog_ships_without_allowlist_name():
    item = _rss_item("Acme Blog", "Acme agent CLI adds parallel subagents")
    item["tags"] = ["coding-agent"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"


def test_research_blog_stays_read_even_with_blog_in_the_name():
    # LangChain/Mistral/DeepMind blogs must NOT ride the changelog→Ship path.
    item = _rss_item("LangChain Blog", "LangGraph agent workflows")
    item["tags"] = ["frameworks", "agents"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


def test_github_changelog_does_not_auto_ship():
    # The generic GitHub changelog is not a coding-agent feed. A Copilot-flavored
    # title can still reach Read via READ_TERMS; it must not become Ship just
    # because the feed name contains "changelog".
    item = _rss_item("GitHub Changelog", "Copilot coding agent: usage-based billing")
    item["tags"] = ["developer-tools", "official"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"
