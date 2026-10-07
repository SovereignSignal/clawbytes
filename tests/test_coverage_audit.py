"""Coverage audit 2026-10-04: HN queries, Ship headlines, watchlist feeds.

RSS gzip stays in tests/test_rss_fetch.py (#36). Scheduler locking stays in
tests/test_scheduler_jobs.py (#33). This file does not change either.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hn = _load("claw_hn_coverage", "claw-hn-monitor.py")
rss = _load("claw_rss_coverage", "claw-rss-monitor.py")
pw = _load("claw_pagewatch_coverage", "claw-pagewatch-monitor.py")


def _isolate(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "clawbytes-backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "clawbytes-thread-state.json")
    return memory


def _rss(feed, title, url, published=None):
    return {
        "feed": feed,
        "title": title,
        "link": url,
        "id": url,
        "published": published or datetime.now(timezone.utc).isoformat(),
    }


def _state():
    return {
        "seenSourceKeys": [],
        "postedBacklogIds": [],
        "postedUrls": [],
        "lastCollectedAt": None,
        "lastPublishedAt": {},
        "publishLog": [],
    }


def _queued(url, title, source, score, **extra):
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    item = {
        "id": url,
        "url": url,
        "title": title,
        "summary": "queued",
        "sourceType": "rss",
        "sourceName": source,
        "sourceId": url,
        "primaryCategory": "ship",
        "categories": ["ship"],
        "score": score,
        "publishedAt": now.isoformat(),
        "discoveredAt": now.isoformat(),
        "expiresAt": (now + timedelta(days=6)).isoformat(),
        "status": "queued",
        "postedCategories": [],
    }
    item.update(extra)
    return item


def _hit(object_id, title, points, url):
    return {
        "objectID": object_id,
        "title": title,
        "points": points,
        "num_comments": 8,
        "created_at": "2026-10-01T00:00:00Z",
        "url": url,
    }


def test_or_queries_are_split_deduped_and_front_page_is_one_pass(monkeypatch, tmp_path):
    assert all(" OR " not in q["query"] for q in hn.HN_QUERIES)
    expanded = hn.expand_hn_queries([
        {"query": "claude code OR cursor", "tags": "story", "min_points": 15},
        {"query": "cursor", "tags": "story", "min_points": 15},
        {"query": "widgets OR gadgets", "tags": "story", "min_points": 4},
    ])
    assert [q["query"] for q in expanded] == ["claude code", "cursor", "widgets", "gadgets"]
    assert expanded[0]["min_points"] == 15
    assert expanded[-1]["min_points"] == 4

    monkeypatch.setattr(hn, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(hn, "STATE_FILE", tmp_path / "hn.json")
    monkeypatch.setattr(hn.time, "sleep", lambda *_a, **_k: None)
    (tmp_path / "clawbytes-dynamic-feeds.json").write_text(json.dumps({
        "hn_queries": [{"query": "widgets OR gadgets", "tags": "story", "min_points": 4}],
    }))
    calls = []

    def fake(query, tags="story", **_kwargs):
        calls.append((query, tags))
        if tags == "front_page":
            return {"hits": []}
        return {"hits": [_hit("same", "Claude Code tips", 200, "https://example.com/cc")]}

    monkeypatch.setattr(hn, "fetch_hn", fake)
    items = hn.check_hn(verbose=False)
    assert len(items) == 1
    assert items[0]["id"] == "same"
    assert all(" OR " not in query for query, _tags in calls)
    assert ("claude code", "story") in calls
    assert ("cursor", "story") in calls
    assert ("widgets", "story") in calls
    assert ("gadgets", "story") in calls
    assert ("", "front_page") in calls


def test_front_page_keeps_relevant_stories_and_a_failed_fetch(monkeypatch, tmp_path):
    assert hn.front_page_relevant("Pi 1.0", "https://earendil.com/posts/pi-1-0/")
    assert hn.front_page_relevant("Pi Durable", "")
    assert hn.front_page_relevant("You said no MCP", "")
    assert hn.front_page_relevant("Coding is not solved", "")
    assert hn.front_page_relevant("Has Opus 5.5 been nerfed yet?", "")
    assert not hn.front_page_relevant("Raspberry Pi 5", "https://example.com/pi")
    assert not hn.front_page_relevant("Video encoding tips", "")
    assert not hn.front_page_relevant("A recipe for sourdough", "")

    monkeypatch.setattr(hn, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(hn, "STATE_FILE", tmp_path / "hn.json")
    monkeypatch.setattr(hn.time, "sleep", lambda *_a, **_k: None)

    def fake(_query, tags="story", **_kwargs):
        if tags != "front_page":
            return {"hits": []}
        return {"hits": [
            _hit("1", "Pi 1.0", 1678, "https://earendil.com/posts/pi-1-0/"),
            _hit("2", "A recipe for sourdough", 400, "https://example.com/bread"),
            _hit("3", "Coding agents got cheaper", 40, "https://example.com/cheap"),
            _hit("4", "You said no MCP", 680, "https://earendil.com/posts/you-said-no-mcp/"),
        ]}

    monkeypatch.setattr(hn, "fetch_hn", fake)
    items = hn.check_hn(verbose=False)
    titles = {item["title"] for item in items}
    assert titles == {"Pi 1.0", "You said no MCP"}
    state = json.loads((tmp_path / "hn.json").read_text())
    urls = {row["url"] for row in state["frontPage"]}
    assert urls == {
        "https://earendil.com/posts/pi-1-0/",
        "https://earendil.com/posts/you-said-no-mcp/",
    }

    def fail(_query, tags="story", **_kwargs):
        return None

    monkeypatch.setattr(hn, "fetch_hn", fail)
    hn.check_hn(verbose=False)
    kept = json.loads((tmp_path / "hn.json").read_text())
    assert {row["url"] for row in kept["frontPage"]} == urls


def test_ship_caps_and_windows_stay_put():
    assert ct.CATEGORY_META["ship"]["windows"] == [9, 18]
    assert ct.SHIP_INTAKE_PER_SOURCE == 2
    assert ct.SHIP_INTAKE_PER_RUN == 6


def test_junk_release_titles_are_dropped_and_real_ones_stay():
    assert ct.is_junk_release_title("release-publish/004549970362-1790888386")
    assert ct.is_junk_release_title("Pinned inputs 9")
    assert ct.is_junk_release_title("Pinned inputs a/b/9")
    assert ct.is_junk_release_title("Hermes Agent: Pinned inputs b")
    assert ct.is_junk_release_title("Hermes Agent — Pinned inputs a")
    assert not ct.is_junk_release_title("Pinned inputs for the new runtime")
    assert not ct.is_junk_release_title("openclaw 2026.9.7")
    # Post #968: the atom title was the tag `inputs-8`, which the
    # "Pinned inputs N" fullmatch does not cover. The release URL carries it.
    inputs8 = "https://github.com/NousResearch/hermes-agent/releases/tag/inputs-8"
    assert ct.is_junk_release_title("inputs-8", inputs8)
    assert ct.is_junk_release_title("New Hermes release", inputs8)
    assert ct.classify_rss(_rss("Hermes Agent Releases", "inputs-8", inputs8)) is None
    assert ct.classify_ecosystem_release({
        "repo": "NousResearch/hermes-agent",
        "tag": "inputs-8",
        "name": "inputs-8",
        "url": inputs8,
        "published": "2026-10-05T20:00:00Z",
    }) is None

    junk = [
        ("OpenClaw Releases", "release-publish/004549970362-1790888386"),
        ("Hermes Agent Releases", "Pinned inputs 9"),
        ("Hermes Agent Releases", "Pinned inputs a/b/9"),
    ]
    for feed, title in junk:
        assert ct.classify_rss(_rss(feed, title, f"https://example.com/{title}")) is None
    kept = ct.classify_rss(_rss("OpenClaw Releases", "openclaw 2026.9.7", "https://example.com/oc"))
    assert kept["primaryCategory"] == "ship"
    hermes = ct.classify_rss(_rss("Hermes Agent Releases", "v0.9.0", "https://example.com/hermes"))
    assert hermes["primaryCategory"] == "ship"


def test_junk_tags_are_not_queued(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    ct.save_json(ct.THREAD_STATE_FILE, _state())
    items = [
        _rss("OpenClaw Releases", "release-publish/004549970362-1790888386", "https://example.com/oc-junk"),
        _rss("Hermes Agent Releases", "Pinned inputs 9", "https://example.com/hermes-n"),
        _rss("Hermes Agent Releases", "Pinned inputs a/b/9", "https://example.com/hermes-asset"),
        _rss("OpenClaw Releases", "openclaw 2026.9.7", "https://example.com/oc-real"),
    ]
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": items})
    ct.collect_into_backlog()
    stored = {item["url"]: item for item in json.loads(ct.BACKLOG_FILE.read_text())["items"]}
    assert "https://example.com/oc-junk" not in stored
    assert "https://example.com/hermes-n" not in stored
    assert "https://example.com/hermes-asset" not in stored
    assert stored["https://example.com/oc-real"]["primaryCategory"] == "ship"
    capsys.readouterr()


def test_queued_inputs_tag_does_not_publish(monkeypatch, tmp_path):
    """#968 was already a backlog row titled like the channel post.

    Classify-time filtering does not revisit rows queued earlier. The ship
    queue has to drop the tag on the way out.
    """
    _isolate(monkeypatch, tmp_path)
    ct.save_json(ct.THREAD_STATE_FILE, _state())
    url = "https://github.com/NousResearch/hermes-agent/releases/tag/inputs-8"
    ct.save_json(ct.BACKLOG_FILE, {"items": [
        _queued(url, "inputs-8", "Hermes Agent Releases", 90),
        _queued("https://example.com/real", "Hermes Agent 0.9.0", "Hermes Agent Releases", 80),
    ]})
    queued = [item["url"] for item in ct.queue_for_category("ship")]
    assert url not in queued
    assert "https://example.com/real" in queued


def test_major_and_front_page_items_lead_the_ship_queue(monkeypatch, tmp_path):
    memory = _isolate(monkeypatch, tmp_path)
    ct.save_json(ct.THREAD_STATE_FILE, _state())
    dots = "https://www.openai.com/index/introducing-dots/?utm=hn"
    items = [
        _queued("https://example.com/oc", "OpenClaw 2026.9.7", "OpenClaw Releases", 100),
        _queued("https://example.com/minor", "Pi 1.2.0", "Pi Coding Agent Releases", 99),
        _queued("https://example.com/pi", "v1.0.0", "Pi Coding Agent Releases", 60),
        _queued(dots, "Introducing dots", "OpenAI News", 40),
        _queued(
            "https://example.com/untracked",
            "v3.0.0",
            "Example Releases",
            70,
        ),
    ]
    ct.save_json(ct.BACKLOG_FILE, {"items": items})
    monkeypatch.setattr(
        ct,
        "load_hn_front_page_urls",
        lambda: {ct.canonical_story_url(dots)},
    )
    order = [item["url"] for item in ct.queue_for_category("ship")]
    assert order[:2] == ["https://example.com/pi", dots]
    assert order[2] == "https://example.com/oc"
    assert ct.bundle_for_category("ship", limit=1)[0]["url"] == "https://example.com/pi"
    assert ct.canonical_story_url(dots) == "openai.com/index/introducing-dots"
    assert not ct.is_major_x0_release("v1.2.0")
    assert not ct.is_major_x0_release("openclaw 2026.9.7")
    assert ct.is_major_x0_release("v1.0.0")
    assert ct.is_major_x0_release("Pi 1.0")
    assert ct.ship_bypass_rank(items[2], set()) == 2
    assert ct.ship_bypass_rank(items[1], set()) == 0
    assert memory == ct.MEMORY


def test_claude_code_patches_roll_up_once_a_week(monkeypatch, tmp_path, capsys):
    memory = _isolate(monkeypatch, tmp_path)
    ct.save_json(ct.THREAD_STATE_FILE, _state())
    clock = {"now": datetime(2026, 9, 30, 15, tzinfo=timezone.utc)}
    monkeypatch.setattr(ct, "now_utc", lambda: clock["now"])
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "0")
    items = [
        _rss("Claude Code Releases", "v2.1.289", "https://example.com/289"),
        _rss("Claude Code Releases", "v2.1.284", "https://example.com/284"),
        _rss("Claude Code Releases", "v2.0.0", "https://example.com/major"),
        _rss("Claude Code Action Releases", "v2.1.50", "https://example.com/action"),
    ]
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": items})
    ct.collect_into_backlog()
    stored = json.loads(ct.BACKLOG_FILE.read_text())["items"]
    assert not any(item.get("weeklyRollup") for item in stored)
    assert all(item["primaryCategory"] != "ship" for item in stored)
    book = json.loads((memory / "claw-claude-code-patches.json").read_text())
    assert [row["version"] for row in book["patches"]] == ["2.1.289", "2.1.284"]
    assert "pending" not in book or not book["pending"]

    clock["now"] = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": []})
    ct.collect_into_backlog()
    rollups = [item for item in json.loads(ct.BACKLOG_FILE.read_text())["items"] if item.get("weeklyRollup")]
    assert len(rollups) == 1
    assert rollups[0]["title"] == "Claude Code 2.1.284–2.1.289"
    assert rollups[0]["primaryCategory"] == "ship"
    assert "2.1.284" in rollups[0]["summary"] and "2.1.289" in rollups[0]["summary"]
    assert ct.display_title(rollups[0]) == rollups[0]["title"]
    assert ct.ship_bypass_rank(rollups[0], set()) == 1
    assert ct.ship_intake_per_run() == 0
    assert ct.CATEGORY_META["ship"]["windows"] == [9, 18]

    ct.collect_into_backlog()
    again = [item for item in json.loads(ct.BACKLOG_FILE.read_text())["items"] if item.get("weeklyRollup")]
    assert len(again) == 1
    capsys.readouterr()


def test_watchlist_feeds_and_research_baseline(monkeypatch, tmp_path):
    names = {feed["name"]: feed for feed in rss.RSS_FEEDS}
    assert names["DeepSeek Harness Releases"]["url"].endswith(
        "deepseek-ai/deepseek-harness/releases.atom"
    )
    assert rss.is_relevant({"title": "v0.2.0", "summary": ""}, "DeepSeek Harness Releases")
    assert ct.repo_name_from_feed("DeepSeek Harness Releases") == "deepseek harness"
    stable = ct.classify_rss(_rss(
        "DeepSeek Harness Releases",
        "v0.2.0",
        "https://github.com/deepseek-ai/deepseek-harness/releases/tag/v0.2.0",
    ))
    assert stable["primaryCategory"] == "ship"
    assert ct.classify_rss(_rss(
        "DeepSeek Harness Releases",
        "v0.2.1-alpha.1",
        "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-alpha.1",
    )) is None

    assert names["claude.dev Blog"]["url"] == "https://claude.dev/rss.xml"
    assert rss.is_relevant(
        {"title": "Building with Claude Sonnet 5.5", "summary": ""},
        "claude.dev Blog",
    )
    guide = ct.classify_rss(_rss(
        "claude.dev Blog",
        "Getting the most out of Opus 5.5 in Claude and Claude Code",
        "https://claude.dev/blog/getting-the-most-out-of-opus-5-5/",
    ))
    plain = ct.classify_rss(_rss(
        "claude.dev Blog",
        "Building with Claude Sonnet 5.5",
        "https://claude.dev/blog/building-with-claude-sonnet-5-5/",
    ))
    assert guide["primaryCategory"] == "read"
    assert plain["primaryCategory"] == "read"

    by_key = {watch["key"]: watch for watch in pw.SITEMAP_WATCHES}
    assert by_key["anthropic"]["prefixes"] == (
        "https://www.anthropic.com/news/",
        "https://www.anthropic.com/engineering/",
    )
    assert by_key["anthropic-research"]["prefixes"] == ("https://www.anthropic.com/research/",)
    glm = "https://www.anthropic.com/research/glm-5-3-and-the-spread-of-advanced-cyber-capabilities"
    assert pw.lane_for_slug(glm) == "watch"
    assert pw.lane_for_slug("https://www.anthropic.com/research/agents-in-biology") == "read"
    assert pw.lane_for_slug("https://www.anthropic.com/news/zoom-partnership") == "ship"
    assert pw.lane_for_slug("https://www.anthropic.com/engineering/advanced-tool-use") == "read"

    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [by_key["anthropic-research"]])
    pages = {"xml": (
        "<urlset>"
        f"<loc>{glm}</loc>"
        "<loc>https://www.anthropic.com/research/agents-in-biology</loc>"
        "<loc>https://www.anthropic.com/news/not-research</loc>"
        "</urlset>"
    )}
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: pages["xml"])
    assert pw.check_pages(verbose=False) == []
    slugs = json.loads((tmp_path / "pw.json").read_text())["slugs"]["anthropic-research"]
    assert slugs == sorted(u for u in (
        glm,
        "https://www.anthropic.com/research/agents-in-biology",
    ))
    pages["xml"] = pages["xml"].replace(
        "</urlset>",
        "<loc>https://www.anthropic.com/research/new-report</loc></urlset>",
    )
    fresh = pw.check_pages(verbose=False)
    assert [item["url"] for item in fresh] == ["https://www.anthropic.com/research/new-report"]
    assert fresh[0]["lane"] == "read"
