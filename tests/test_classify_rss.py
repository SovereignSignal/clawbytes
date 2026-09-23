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


def test_coding_agent_tagged_blog_does_not_ship():
    # A coding-agent tag plus the word "blog" is not a Ship signal.
    item = _rss_item("Acme Blog", "Acme agent CLI adds parallel subagents")
    item["tags"] = ["coding-agent"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


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


def test_acp_schema_v1_ships():
    item = _rss_item("Agent Client Protocol Releases", "Schema v1.20.0")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 58
    assert ct.repo_name_from_feed(item["feed"]) == "agent client protocol"


def test_acp_rust_crate_is_dropped():
    # Monorepo ships Schema + crate in lockstep; crate bumps are not operator news.
    item = _rss_item("Agent Client Protocol Releases", "Rust Crate v1.6.0")
    assert ct.classify_rss(item) is None


def test_acp_schema_v2_alpha_is_dropped():
    item = _rss_item("Agent Client Protocol Releases", "Schema v2.0.0-alpha.2")
    assert ct.classify_rss(item) is None


def test_deepagents_code_ships_and_sidecars_drop():
    code = _rss_item("Deep Agents Releases", "deepagents-code==0.2.0")
    sdk = _rss_item("Deep Agents Releases", "deepagents==0.8.0")
    acp = _rss_item("Deep Agents Releases", "deepagents-acp==0.0.12")
    talon = _rss_item("Deep Agents Releases", "deepagents-talon==0.0.8")
    for item in (code, sdk):
        candidate = ct.classify_rss(item)
        assert candidate is not None
        assert candidate["primaryCategory"] == "ship"
        assert ct.repo_name_from_feed(item["feed"]) == "deep agents"
    assert ct.classify_rss(acp) is None
    assert ct.classify_rss(talon) is None


def test_kilo_prerelease_is_dropped():
    item = _rss_item("Kilo Code Releases", "v7.7.0 (pre-release)")
    assert ct.classify_rss(item) is None


def test_warp_blog_keyword_hit_is_read_not_ship():
    item = _rss_item("Warp Blog", "Agent mode now runs in the background")
    item["tags"] = ["coding-agent"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


def test_langchain_blog_stays_read_even_when_tagged_coding_agent():
    item = _rss_item("LangChain Blog", "How we built a new agent runtime")
    item["tags"] = ["coding-agent"]
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


def test_developer_in_stable_release_title_still_ships():
    item = _rss_item("Claude Code Releases", "v2.0.0 for developers")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 58


def test_preview_word_in_stable_release_still_ships():
    item = _rss_item("Claude Code Releases", "v2.0.0 Adds preview of background agents")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 58


def test_fixed_and_prefix_do_not_demote_dot_zero():
    fixed = ct.classify_rss(_rss_item("Claude Code Releases", "v2.0.0 fixed streaming tool calls"))
    prefix = ct.classify_rss(_rss_item("Aider Releases", "v0.80.0 prefix matching for tools"))
    assert fixed is not None and fixed["score"] >= 58
    assert prefix is not None and prefix["score"] >= 58
    assert not ct.is_minor_release("v2.0.0 device support")
    assert not ct.is_minor_release("v2.0.0 dispatch notes")
    assert ct.is_minor_release("v2.1.3")


def test_patch_release_stays_demoted():
    candidate = ct.classify_rss(_rss_item("Claude Code Releases", "v2.1.3"))
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] < 58


def test_alphabetical_release_is_not_dropped_as_alpha():
    candidate = ct.classify_rss(_rss_item("Cline Releases", "Alphabetical tool index v2.0.0"))
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"


def test_real_prerelease_tags_still_drop():
    assert ct.classify_rss(_rss_item("Cline Releases", "v2.0.0-alpha.1")) is None
    assert ct.classify_rss(_rss_item("Kilo Code Releases", "v7.7.0 (pre-release)")) is None
    assert ct.classify_rss(_rss_item("Herdr Releases", "Preview build 2026-09-16")) is None


def test_rfc822_pubdate_is_aware_and_zed_clears_ship_bar():
    parsed = ct.parse_dt("Fri, 18 Sep 2026 13:42:27 +0000")
    assert parsed is not None and parsed.tzinfo is not None
    gmt = ct.parse_dt("Wed, 23 Sep 2026 12:00:00 GMT")
    assert gmt is not None and gmt.tzinfo is not None
    naive = ct.parse_dt("2026-09-23T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    ct.age_score(naive, 96)  # must not raise
    assert "zed" in ct.REPO_PRIORITY
    assert ct.REPO_PRIORITY["zed"] >= 58
    assert ct.repo_name_from_feed("Zed Blog") == "zed"
    item = _rss_item("Cline Releases", "v2.0.0")
    item["published"] = "Wed, 23 Sep 2026 12:00:00 GMT"
    candidate = ct.classify_rss(item)
    assert candidate["publishedAt"] is not None
    assert candidate["publishedAt"].tzinfo is not None


def test_arxiv_bare_agent_is_not_read():
    bare = _rss_item("ArXiv cs.AI", "Tool-use agents with longer context")
    # "tool-use" is a harness compound, so this one stays.
    kept = ct.classify_rss(bare)
    assert kept is not None and kept["primaryCategory"] == "read"
    dropped = ct.classify_rss(_rss_item("ArXiv cs.CL", "Early agent world models"))
    assert dropped is None
    survey = ct.classify_rss(_rss_item("ArXiv cs.AI", "A survey of diffusion models"))
    assert survey is None


def test_precursor_and_raider_are_not_read():
    precursor = ct.classify_rss(_rss_item("Simon Willison", "Precursor chemistry results from the lab", high_signal=False))
    raider = ct.classify_rss(_rss_item("Hugging Face Blog", "A raider's guide to storage layouts", high_signal=False))
    assert precursor is None
    assert raider is None
    cursor = ct.classify_rss(_rss_item("Simon Willison", "Using Cursor for refactors", high_signal=False))
    assert cursor is not None and cursor["primaryCategory"] == "read"


def test_kimi_code_minor_release_ships():
    item = _rss_item("Kimi Code Releases", "@moonshot-ai/kimi-code@2.0.0")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert ct.repo_name_from_feed(item["feed"]) == "kimi code"
