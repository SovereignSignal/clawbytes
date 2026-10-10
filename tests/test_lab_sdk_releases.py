"""Official lab SDK atoms: notable notes Ship, routine bumps do not.

The Oct 7 2026 Anthropic release (Python v1.12.0 / TypeScript sdk-v0.132.0)
is the fixture. Its notes say "typed computer and browser toolset". A later
minor that only adds analytics types is the routine bump.
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "claw_rss_monitor_lab_sdk", SCRIPTS / "claw-rss-monitor.py"
)
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)

BROWSER_NOTES = (
    "1.12.0 (2026-10-07) Full Changelog: v1.11.0...v1.12.0 Features "
    "api: add claude-haiku-5-5 and typed computer and browser toolset tool calls "
    "f6346e63b7507a596e6e8acc9425cae2dfc7bf24 "
    "api: add disabled to thinking types in model capabilities"
)
ROUTINE_NOTES = (
    "Features api: add types for the Chat and Cowork unified analytics metrics "
    "api: add workflows, multiagent configuration and thread status filtering "
    "to Managed Agents"
)
NOT_FORWARDABLE = (
    "anthropics/anthropic-sdk-python",
    "anthropics/anthropic-sdk-typescript",
    "anthropics/claude-agent-sdk-python",
    "anthropics/claude-agent-sdk-typescript",
    "openai/openai-python",
    "openai/openai-node",
    "openai/openai-agents-python",
    "googleapis/python-genai",
    "googleapis/js-genai",
)


def _item(feed, title, summary="", link="https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.12.0"):
    return {
        "feed": feed,
        "title": title,
        "link": link,
        "id": link,
        "summary": summary,
        "published": datetime.now(timezone.utc).isoformat(),
    }


def test_lab_sdk_feeds_match_the_monitor_and_are_not_forwarded():
    names = {feed["name"]: feed for feed in rss.RSS_FEEDS}
    assert len(rss.RSS_FEEDS) == 90
    expected = {
        "anthropic-sdk-python Releases": "https://github.com/anthropics/anthropic-sdk-python/releases.atom",
        "anthropic-sdk-typescript Releases": "https://github.com/anthropics/anthropic-sdk-typescript/releases.atom",
        "Claude Agent SDK Python Releases": "https://github.com/anthropics/claude-agent-sdk-python/releases.atom",
        "Claude Agent SDK TypeScript Releases": "https://github.com/anthropics/claude-agent-sdk-typescript/releases.atom",
        "openai-python Releases": "https://github.com/openai/openai-python/releases.atom",
        "openai-node Releases": "https://github.com/openai/openai-node/releases.atom",
        "openai-agents Releases": "https://github.com/openai/openai-agents-python/releases.atom",
        "python-genai Releases": "https://github.com/googleapis/python-genai/releases.atom",
        "js-genai Releases": "https://github.com/googleapis/js-genai/releases.atom",
    }
    for name, url in expected.items():
        assert names[name]["url"] == url
        assert "lab-sdk" in names[name]["tags"]
    tagged = {feed["name"].lower() for feed in rss.RSS_FEEDS if "lab-sdk" in feed.get("tags", [])}
    assert tagged == ct.LAB_SDK_FEED_NAMES
    targets = json.loads((ROOT / "release_targets.json").read_text())["targets"]
    for repo in NOT_FORWARDABLE:
        assert repo not in targets


def test_new_sdk_keys_do_not_steal_sibling_feeds():
    assert ct.repo_name_from_feed("openai-node Releases") == "openai-node"
    assert ct.repo_name_from_feed("openai-python Releases") == "openai-python"
    assert ct.repo_name_from_feed("js-genai Releases") == "js-genai"
    assert ct.repo_name_from_feed("python-genai Releases") == "python-genai"
    assert ct.repo_name_from_feed("anthropic-sdk-typescript Releases") == "anthropic-sdk"
    assert ct.repo_name_from_feed("Claude Agent SDK Python Releases") == "claude agent sdk"
    assert "openai-node" in ct.REPO_PRIORITY
    assert "js-genai" in ct.REPO_PRIORITY


def test_oct7_browser_toolset_ships_and_routine_minor_does_not():
    kept = ct.classify_rss(_item("anthropic-sdk-python Releases", "v1.12.0", BROWSER_NOTES))
    assert kept is not None
    assert kept["primaryCategory"] == "ship"
    assert kept["labSdkNotable"] is True
    assert kept["score"] >= ct.NOTABLE_LAB_SDK_SCORE
    assert "computer and browser toolset" in kept["summary"].lower()
    assert "f6346e6" not in kept["summary"]
    assert ct.ship_low_signal_reason(kept) is None

    routine = ct.classify_rss(_item(
        "anthropic-sdk-python Releases",
        "v1.13.0",
        ROUTINE_NOTES,
        "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.13.0",
    ))
    assert routine is None


def test_notable_patch_stays_on_ship():
    item = _item(
        "anthropic-sdk-python Releases",
        "v1.12.1",
        "Bug fixes. api: add the computer and browser toolset to the tool runner.",
        "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.12.1",
    )
    kept = ct.classify_rss(item)
    assert kept is not None
    assert kept["primaryCategory"] == "ship"
    assert kept["labSdkNotable"] is True
    assert kept["score"] >= ct.NOTABLE_LAB_SDK_SCORE
    assert ct.ship_low_signal_reason(kept) is None
    assert ct.is_minor_release("v1.12.1")


def test_typescript_main_package_ships_and_adapter_tags_do_not():
    kept = ct.classify_rss(_item(
        "anthropic-sdk-typescript Releases",
        "sdk: v0.132.0",
        BROWSER_NOTES,
        "https://github.com/anthropics/anthropic-sdk-typescript/releases/tag/sdk-v0.132.0",
    ))
    assert kept is not None
    assert kept["title"].startswith("Anthropic SDK")
    assert "computer and browser" in kept["summary"].lower()

    for title in (
        "vertex-sdk: v0.20.3",
        "vertex-sdk-v0.20.4",
        "bedrock-sdk-v0.34.4",
        "foundry-sdk-v0.5.4",
        "aws-sdk-v0.7.9",
        "google-cloud-sdk: v0.0.18",
    ):
        assert ct.classify_rss(_item(
            "anthropic-sdk-typescript Releases",
            title,
            BROWSER_NOTES,
            f"https://github.com/anthropics/anthropic-sdk-typescript/releases/tag/{title}",
        )) is None


def test_negated_breaking_change_and_toolchain_are_not_notable():
    negated = ct.classify_rss(_item(
        "openai-python Releases",
        "v3.28.0",
        "This minor release does not introduce a breaking change. "
        "api: add types for analytics metrics only.",
        "https://github.com/openai/openai-python/releases/tag/v3.28.0",
    ))
    assert negated is None
    toolchain = ct.classify_rss(_item(
        "js-genai Releases",
        "v2.28.0",
        "api: add a new toolchain for internal release builds and nothing else.",
        "https://github.com/googleapis/js-genai/releases/tag/v2.28.0",
    ))
    assert toolchain is None


def test_explicit_new_tool_and_breaking_change_ship():
    tool = ct.classify_rss(_item(
        "openai-node Releases",
        "v7.32.0",
        "api: add a new tool for desktop clicks in the agent runner.",
        "https://github.com/openai/openai-node/releases/tag/v7.32.0",
    ))
    assert tool is not None
    assert tool["title"].startswith("OpenAI Node SDK")
    assert tool["labSdkNotable"] is True
    breaking = ct.classify_rss(_item(
        "openai-agents Releases",
        "v0.23.1",
        "Breaking change: the tool runner no longer accepts the legacy session shape.",
        "https://github.com/openai/openai-agents-python/releases/tag/v0.23.1",
    ))
    assert breaking is not None
    assert breaking["primaryCategory"] == "ship"
    assert ct.ship_low_signal_reason(breaking) is None
    assert "breaking change" in breaking["summary"].lower()


def test_missing_notes_keep_the_version_gate():
    minor = ct.classify_rss(_item("anthropic-sdk-python Releases", "v1.12.0", ""))
    assert minor is not None
    assert minor["primaryCategory"] == "ship"
    assert "labSdkNotable" not in minor
    assert ct.ship_low_signal_reason(minor) is None

    patch = ct.classify_rss(_item(
        "anthropic-sdk-python Releases",
        "v1.12.1",
        "",
        "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.12.1",
    ))
    assert patch is not None
    assert ct.ship_low_signal_reason(patch) == "patch"

    claude = ct.classify_rss(_item(
        "Claude Code Releases",
        "v2.1.3",
        "api: add a new tool",
        "https://github.com/anthropics/claude-code/releases/tag/v2.1.3",
    ))
    assert claude is not None
    assert "labSdkNotable" not in claude
    assert ct.ship_low_signal_reason(claude) == "patch"


def test_collect_queues_the_notable_release_and_drops_the_bump(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "state.json")
    ct.save_json(ct.THREAD_STATE_FILE, {
        "seenSourceKeys": [],
        "postedBacklogIds": [],
        "postedUrls": [],
        "lastCollectedAt": None,
        "lastPublishedAt": {},
        "publishLog": [],
    })
    ct.save_json(ct.BACKLOG_FILE, {"items": []})
    items = [
        _item("anthropic-sdk-python Releases", "v1.12.0", BROWSER_NOTES),
        _item(
            "anthropic-sdk-python Releases",
            "v1.13.0",
            ROUTINE_NOTES,
            "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.13.0",
        ),
    ]
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": items})
    ct.collect_into_backlog()
    stored = json.loads(ct.BACKLOG_FILE.read_text())["items"]
    urls = {item["url"] for item in stored}
    assert "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.12.0" in urls
    assert "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.13.0" not in urls
    kept = next(item for item in stored if item["url"].endswith("v1.12.0"))
    assert kept["primaryCategory"] == "ship"
    assert "computer and browser" in kept["summary"].lower()


def test_new_lab_sdk_feed_baselines_then_keeps_the_notes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rss, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(rss, "STATE_FILE", tmp_path / "claw-rss-state.json")
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "openai-node Releases",
        "url": "https://github.com/openai/openai-node/releases.atom",
        "tags": ["releases", "agent-sdk", "lab-sdk"],
    }])
    first = (
        "<rss><channel><item><title>v7.31.0</title>"
        "<link>https://github.com/openai/openai-node/releases/tag/v7.31.0</link>"
        "<guid>id-1</guid>"
        "<description>api: add types for analytics metrics</description>"
        "</item></channel></rss>"
    )
    second = (
        "<rss><channel>"
        "<item><title>v7.32.0</title>"
        "<link>https://github.com/openai/openai-node/releases/tag/v7.32.0</link>"
        "<guid>id-2</guid>"
        "<description>&lt;p&gt;add typed computer and browser toolset tool calls&lt;/p&gt;</description>"
        "</item>"
        "<item><title>v7.31.0</title>"
        "<link>https://github.com/openai/openai-node/releases/tag/v7.31.0</link>"
        "<guid>id-1</guid>"
        "<description>api: add types for analytics metrics</description>"
        "</item>"
        "</channel></rss>"
    )
    bodies = iter([first, second])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: next(bodies))

    assert rss.check_feeds(verbose=False)[0] == []
    state = json.loads((tmp_path / "claw-rss-state.json").read_text())
    assert state["feedBaseline"]["openai-node Releases"]["url"].endswith("openai-node/releases.atom")
    assert "id-1" in state["lastSeenByFeed"]["openai-node Releases"]

    emitted, _status = rss.check_feeds(verbose=False)
    assert [item["title"] for item in emitted] == ["v7.32.0"]
    assert "computer and browser toolset" in emitted[0]["summary"]
    assert "<p>" not in emitted[0]["summary"]
    capsys.readouterr()
