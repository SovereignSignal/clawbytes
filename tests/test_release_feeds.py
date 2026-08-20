from datetime import datetime, timezone

import clawbytes_threads as ct


def test_new_harness_repos_have_priorities():
    for repo in ["claude agent sdk", "openhands", "aider", "cline",
                 "roo code", "goose", "qwen code", "smolagents", "crush",
                 "devin desktop", "devin", "antigravity", "amp news",
                 "factory", "copilot", "warp blog", "windsurf", "replit",
                 "augment code", "junie", "jetbrains", "agent client protocol"]:
        assert repo in ct.REPO_PRIORITY, f"{repo} missing from REPO_PRIORITY"
    # Substring traps: bare tokens that live inside common words stay out.
    for trap in ("amp", "acp", "opus", "augment"):
        assert trap not in ct.REPO_PRIORITY, f"bare {trap!r} is a substring trap"


def test_claude_agent_sdk_precedes_claude_in_matching():
    # repo_name_from_feed matches REPO_PRIORITY keys as substrings in dict
    # order: "claude agent sdk" must win over "claude" for SDK feeds.
    assert ct.repo_name_from_feed("Claude Agent SDK Python Releases") == "claude agent sdk"
    assert ct.repo_name_from_feed("Claude Code Releases") == "claude"


def test_devin_desktop_precedes_devin_in_matching():
    assert ct.repo_name_from_feed("Devin Desktop Changelog") == "devin desktop"
    assert ct.repo_name_from_feed("Devin Release Notes") == "devin"


def test_junie_precedes_jetbrains_in_matching():
    assert ct.repo_name_from_feed("JetBrains Junie Blog") == "junie"
    assert ct.repo_name_from_feed("JetBrains AI Blog") == "jetbrains"


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
