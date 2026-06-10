from datetime import datetime, timezone

import clawbytes_threads as ct


def test_new_harness_repos_have_priorities():
    for repo in ["claude agent sdk", "openhands", "aider", "cline",
                 "roo code", "goose", "qwen code", "smolagents", "crush"]:
        assert repo in ct.REPO_PRIORITY, f"{repo} missing from REPO_PRIORITY"


def test_claude_agent_sdk_precedes_claude_in_matching():
    # repo_name_from_feed matches REPO_PRIORITY keys as substrings in dict
    # order: "claude agent sdk" must win over "claude" for SDK feeds.
    assert ct.repo_name_from_feed("Claude Agent SDK Python Releases") == "claude agent sdk"
    assert ct.repo_name_from_feed("Claude Code Releases") == "claude"


def test_aider_release_routes_to_ship():
    item = {
        "feed": "Aider Releases",
        "title": "v0.85.0",
        "link": "https://github.com/Aider-AI/aider/releases/tag/v0.85.0",
        "published": datetime.now(timezone.utc).isoformat(),
    }
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= ct.REPO_PRIORITY["aider"]
