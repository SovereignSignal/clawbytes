from datetime import datetime, timezone

import clawbytes_threads as ct


def _item(subreddit, title="Claude Code 2.0 subagent orchestration deep dive",
          score=120, comments=45):
    return {
        "id": "t3_test1",
        "subreddit": subreddit,
        "title": title,
        "url": f"https://www.reddit.com/r/{subreddit}/comments/test1/",
        "score": score,
        "comments": comments,
        "found_at": datetime.now(timezone.utc).isoformat(),
    }


def test_new_harness_subreddits_are_allowed():
    for sub in ["claudeai", "claudecode", "cursor", "chatgptcoding", "ai_agents", "mcp"]:
        assert sub in ct.ALLOWED_SUBREDDITS, f"{sub} missing from ALLOWED_SUBREDDITS"


def test_dropped_subreddits_are_rejected():
    for sub in ["homelab", "singularity", "artificial", "machinelearning"]:
        assert ct.classify_reddit(_item(sub)) is None


def test_classify_reddit_accepts_claudecode_post():
    candidate = ct.classify_reddit(_item("ClaudeCode"))
    assert candidate is not None
    assert "community" in candidate["categories"] or "read" in candidate["categories"]
