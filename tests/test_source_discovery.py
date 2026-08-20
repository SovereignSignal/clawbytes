"""Discovery queries must stay inside editorial scope.

Generic ML search terms re-widen the channel (homelab / MachineLearning /
diffusion). Keep current allowlisted subs known so they aren't re-proposed,
and keep dropped subs excluded so they aren't re-added.
"""
import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


disc = _load("claw_source_discovery", "claw-source-discovery.py")
hn = _load("claw_hn_monitor", "claw-hn-monitor.py")


def test_subreddit_queries_are_harness_scoped():
    blob = " ".join(disc.SUBREDDIT_QUERIES).lower()
    assert "machine learning news" not in blob
    for phrase in ("coding agent", "claude code", "mcp server", "agent harness"):
        assert phrase in blob, f"discovery queries missing {phrase!r}"


def test_dropped_subs_are_excluded_and_live_subs_are_known():
    for sub in ("homelab", "singularity", "machinelearning", "artificial"):
        assert sub in disc.SUBREDDIT_EXCLUSIONS, f"{sub} would be re-added"
    for sub in (
        "openclaw", "selfhosted", "localllama",
        "claudeai", "claudecode", "cursor", "chatgptcoding", "ai_agents", "mcp",
        "codex", "anthropic", "githubcopilot", "windsurf",
    ):
        assert sub in disc.KNOWN_SUBREDDITS, f"live sub {sub} not marked known"


def test_hn_topic_keywords_drop_generic_ml_and_cover_harnesses():
    keys = {k.lower() for k in disc.HN_TOPIC_KEYWORDS}
    assert "diffusion" not in keys
    assert "transformer" not in keys
    for phrase in ("antigravity", "cursor", "coding agent", "mcp", "devin desktop", "agent client protocol"):
        assert phrase in keys, f"HN topic keywords missing {phrase!r}"
    # Substring traps — never add bare tokens that live inside common words.
    for trap in ("amp", "opus", "acp", "augment", "droid"):
        assert trap not in keys


def test_existing_hn_queries_cover_live_monitor_queries():
    live = {q["query"] for q in hn.HN_QUERIES}
    assert live <= disc.EXISTING_HN_QUERIES
    assert any("antigravity" in q and "devin desktop" in q for q in disc.EXISTING_HN_QUERIES)
