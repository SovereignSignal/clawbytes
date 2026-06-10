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


def test_copilot_blog_routes_to_read():
    item = _rss_item("GitHub Copilot Changelog", "GitHub Copilot CLI adds slash commands")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"
