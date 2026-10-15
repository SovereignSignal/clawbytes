"""Lab launches the sitemap and GitHub atoms were missing.

Anthropic news index, Claude article launches, the Codex changelog RSS,
Codex rows on the OpenAI release notes, and the X watchlist.
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load("claw_pagewatch_launches", "claw-pagewatch-monitor.py")
rss = _load("claw_rss_launches", "claw-rss-monitor.py")
xmon = _load("claw_x_launches", "claw-x-monitor.py")

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
RELEASE_NOTES = """
Markdown Content:
Codex

Oct 9, 2026

Beta

## Composer predictions in Codex

Composer predictions suggest the next message in the Codex thread.

[View source](https://help.openai.com/en/articles/6825453-chatgpt-release-notes#composer-predictions-in-codex)

API

Oct 8, 2026

GA

## Ultrafast mode for GPT-6.1 Sol

Added Ultrafast mode for the Responses API.

Codex

Oct 8, 2026

GA

## Faster steering in Codex

Follow-ups steer a running Codex task sooner.

[View source](https://help.openai.com/en/articles/6825453-chatgpt-release-notes#faster-steering-in-codex)
"""
NEWS_HTML = """
<a href="/news">Newsroom</a>
<a href="/news/introducing-dynamic-workflows">Dynamic workflows</a>
<a href="https://www.anthropic.com/news/projects-redesigned">Projects</a>
<a href="/news/introducing-dynamic-workflows">dup</a>
<a href="/engineering/managed-agents">Engineering</a>
"""


def test_codex_changelog_feed_is_the_official_rss_and_drops_ios():
    names = {feed["name"]: feed for feed in rss.RSS_FEEDS}
    assert names["Codex Changelog"]["url"] == "https://developers.openai.com/codex/changelog/rss.xml"
    assert "codex changelog" in ct.CHANGELOG_SHIP_FEED_NAMES
    assert ct.repo_name_from_feed("Codex Changelog") == "codex"
    mobile = {
        "title": "ChatGPT for iOS",
        "link": "https://developers.openai.com/codex/changelog/#codex-2026-10-07-mobile",
        "id": "https://developers.openai.com/codex/changelog/#codex-2026-10-07-mobile",
    }
    note = {
        "title": "Composer predictions in Codex",
        "link": "https://developers.openai.com/codex/changelog/#codex-2026-10-09-composer",
        "id": "https://developers.openai.com/codex/changelog/#codex-2026-10-09-composer",
        "summary": "Suggestions in the composer.",
    }
    assert not rss.is_relevant(mobile, "Codex Changelog", ["coding-agent"])
    assert rss.is_relevant(note, "Codex Changelog", ["coding-agent"])
    assert ct.classify_rss({**mobile, "feed": "Codex Changelog", "tags": ["coding-agent"]}) is None
    candidate = ct.classify_rss({
        **note,
        "feed": "Codex Changelog",
        "tags": ["coding-agent", "official"],
        "published": NOW.isoformat(),
    })
    assert candidate["primaryCategory"] == "ship"
    assert candidate["url"].endswith("#codex-2026-10-09-composer")
    assert candidate["score"] >= 68


def test_release_notes_keep_codex_anchors_and_baseline_silently(tmp_path, monkeypatch):
    entries = pw.codex_release_note_entries(RELEASE_NOTES)
    assert [entry["title"] for entry in entries] == [
        "Composer predictions in Codex",
        "Faster steering in Codex",
    ]
    assert entries[0]["url"].endswith("#composer-predictions-in-codex")
    assert entries[0]["summary"].startswith("October 9, 2026 —")
    assert ct.is_stale_dated_item(
        {"title": entries[0]["title"], "summary": entries[0]["summary"]},
        datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    assert "Ultrafast" not in " ".join(entry["title"] for entry in entries)
    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    monkeypatch.setattr(pw, "NEWS_INDEX_WATCHES", [])
    bodies = iter([RELEASE_NOTES, RELEASE_NOTES + "\nCodex\n\nOct 15, 2026\n\nGA\n\n## Plugins in Codex\n\nInstall plugins from the composer.\n\n[View source](https://help.openai.com/en/articles/6825453-chatgpt-release-notes#plugins-in-codex)\n"])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: next(bodies))
    assert pw.check_pages(verbose=False) == []
    state = json.loads((tmp_path / "pw.json").read_text())
    assert state["slugs"]["codex-release-notes"] == [entry["url"] for entry in entries]
    fresh = pw.check_pages(verbose=False)
    assert len(fresh) == 1
    assert fresh[0]["title"] == "Plugins in Codex"
    assert fresh[0]["url"].endswith("#plugins-in-codex")
    assert fresh[0]["lane"] == "ship"
    classified = ct.classify_pagewatch({**fresh[0], "found_at": NOW.isoformat()})
    assert classified["url"] == fresh[0]["url"]
    assert classified["primaryCategory"] == "ship"


def test_news_index_uses_the_sitemap_source_id(tmp_path, monkeypatch):
    urls = pw.news_index_urls(NEWS_HTML)
    assert urls == [
        "https://www.anthropic.com/news/introducing-dynamic-workflows",
        "https://www.anthropic.com/news/projects-redesigned",
    ]
    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    monkeypatch.setattr(pw, "RELEASE_NOTE_WATCHES", [])
    pages = iter([
        NEWS_HTML,
        NEWS_HTML + '<a href="/news/claude-code-projects-opening">Opening</a>',
    ])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: next(pages))
    assert pw.check_pages(verbose=False) == []
    fresh = pw.check_pages(verbose=False)
    assert len(fresh) == 1
    assert fresh[0]["url"] == "https://www.anthropic.com/news/claude-code-projects-opening"
    assert fresh[0]["id"] == "pagewatch:anthropic:" + fresh[0]["url"]
    classified = ct.classify_pagewatch({**fresh[0], "found_at": NOW.isoformat()})
    assert ct.source_key("pagewatch", classified["sourceId"], classified["url"]) == (
        "pagewatch:" + fresh[0]["id"]
    )


def test_claude_article_launches_skip_customer_stories():
    assert pw.claude_article_launch(
        "https://claude.com/resources/articles/introducing-dynamic-workflows-in-claude-code"
    )
    assert pw.claude_article_launch(
        "https://claude.com/resources/articles/projects-redesigned"
    )
    assert pw.claude_article_launch(
        "https://claude.com/resources/articles/claude-managed-agents-updates"
    )
    assert not pw.claude_article_launch(
        "https://claude.com/resources/articles/how-block-orchestrates-claude-fable-across-thousands-of-pull-requests"
    )
    assert not pw.claude_article_launch(
        "https://claude.com/resources/articles/how-abc-legal-turned-every-employee-into-a-builder-with-claude-managed-agents"
    )
    by_key = {watch["key"]: watch for watch in pw.SITEMAP_WATCHES}
    assert by_key["claude-articles"]["prefixes"] == ("https://claude.com/resources/articles/",)


def test_claude_article_sitemap_remembers_non_launches(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "NEWS_INDEX_WATCHES", [])
    monkeypatch.setattr(pw, "RELEASE_NOTE_WATCHES", [])
    watch = next(item for item in pw.SITEMAP_WATCHES if item["key"] == "claude-articles")
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [watch])
    base = (
        "<urlset>"
        "<loc>https://claude.com/resources/articles/how-block-orchestrates</loc>"
        "<loc>https://claude.com/resources/articles/projects-redesigned</loc>"
        "</urlset>"
    )
    nxt = base.replace(
        "</urlset>",
        "<loc>https://claude.com/resources/articles/how-a-new-customer-uses-claude</loc>"
        "<loc>https://claude.com/resources/articles/introducing-dynamic-workflows-in-claude-code</loc>"
        "</urlset>",
    )
    pages = iter([base, nxt])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: next(pages))
    assert pw.check_pages(verbose=False) == []
    fresh = pw.check_pages(verbose=False)
    assert [item["url"] for item in fresh] == [
        "https://claude.com/resources/articles/introducing-dynamic-workflows-in-claude-code",
    ]
    slugs = json.loads((tmp_path / "pw.json").read_text())["slugs"]["claude-articles"]
    assert "https://claude.com/resources/articles/how-a-new-customer-uses-claude" in slugs
    assert fresh[0]["url"] in slugs


def test_x_watch_is_off_without_the_flag_and_baselines_before_emitting(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAWBYTES_X_WATCH", raising=False)
    monkeypatch.delenv("CLAWBYTES_X_BEARER_TOKEN", raising=False)
    assert xmon.watch_enabled() is False
    monkeypatch.setattr(xmon, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(xmon, "STATE_FILE", tmp_path / "x.json")
    monkeypatch.setattr(xmon, "FAILURES", [])
    called = []
    monkeypatch.setattr(xmon, "fetch_posts", lambda *args, **kwargs: called.append(args))
    assert xmon.check_x(verbose=False) == []
    assert called == []
    assert not (tmp_path / "x.json").exists()

    monkeypatch.setenv("CLAWBYTES_X_WATCH", "1")
    monkeypatch.setattr(xmon.sys, "argv", ["claw-x-monitor.py", "--quiet"])
    assert xmon.main() == 1
    assert any("CLAWBYTES_X_BEARER_TOKEN" in message for message in xmon.FAILURES)

    monkeypatch.setattr(xmon, "FAILURES", [])
    monkeypatch.setenv("CLAWBYTES_X_BEARER_TOKEN", "x-bearer-test")
    payloads = {
        "338443084": {"data": [{
            "id": "100",
            "text": "Sora might come back",
            "referenced_tweets": [{"type": "quoted", "id": "9"}],
        }, {
            "id": "101",
            "text": "During the beta, composer predictions are included at no additional cost",
        }]},
        "1695890961094909952": {"data": [{
            "id": "200",
            "text": "Gemini 4 Argon is now available in Cursor",
        }, {
            "id": "201",
            "text": "Read the publishing guide",
            "referenced_tweets": [{"type": "replied_to", "id": "200"}],
        }]},
    }

    def fetch(user_id, token, since_id=""):
        assert token == "x-bearer-test"
        assert since_id == ""
        return payloads[user_id]

    monkeypatch.setattr(xmon, "fetch_posts", fetch)
    assert xmon.check_x(verbose=False) == []
    state = json.loads((tmp_path / "x.json").read_text())
    assert state["sinceId"]["1695890961094909952"] == "201"
    assert "100" not in json.dumps(state.get("foundItems"))

    def fetch_next(user_id, token, since_id=""):
        assert since_id in {"101", "201"}
        if user_id == "1695890961094909952":
            return {"data": [{
                "id": "202",
                "text": "Cursor is now part of The X Subscription",
            }]}
        return {"data": [{
            "id": "102",
            "text": "there is a chance Sora is coming back",
        }, {
            "id": "103",
            "text": "Composer predictions are in beta for Codex Pro users",
        }]}

    monkeypatch.setattr(xmon, "fetch_posts", fetch_next)
    items = xmon.check_x(verbose=False)
    by_handle = {item["handle"]: item for item in items}
    assert set(by_handle) == {"cursor_ai", "btibor91"}
    assert by_handle["cursor_ai"]["url"] == "https://x.com/cursor_ai/status/202"
    assert "Sora" not in by_handle["btibor91"]["title"]
    assert by_handle["btibor91"]["url"] == "https://x.com/btibor91/status/103"
    official = ct.classify_x({**by_handle["cursor_ai"], "found_at": NOW.isoformat()})
    reporter = ct.classify_x({**by_handle["btibor91"], "found_at": NOW.isoformat()})
    assert official["primaryCategory"] == "ship"
    assert official["score"] >= 64
    assert "reported" not in official
    assert reporter["reported"] is True
    assert reporter["primaryCategory"] == "ship"
    assert ct.classify_x({
        "id": "9",
        "handle": "btibor91",
        "title": "Sora might come back from the dead",
        "url": "https://x.com/btibor91/status/9",
        "found_at": NOW.isoformat(),
    }) is None
