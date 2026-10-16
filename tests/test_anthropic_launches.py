"""Anthropic launches the news index and platform notes missed.

Claude apps release notes (help center), the Claude Code weekly
"What's new" digest, and @ClaudeDevs on the X watchlist.
"""
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import clawbytes_threads as ct
from title_text import first_stated_date

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load("claw_pagewatch_anthropic", "claw-pagewatch-monitor.py")
xmon = _load("claw_x_anthropic", "claw-x-monitor.py")

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
APPS_MD = (FIXTURES / "claude_apps_release_notes.md").read_text()
WHATS_NEW_MD = (FIXTURES / "claude_code_whats_new.md").read_text()
APPS_PAGE = "https://support.claude.com/en/articles/12138966-release-notes"
WHATS_NEW_PAGE = "https://code.claude.com/docs/en/whats-new"

# The newest entry on each page, cut out to stand in for the earlier page.
APPS_NEWEST = re.search(
    r"\*\*Claude Haiku 5\.5 launch\*\*.*?(?=\*\*Monthly API credits)", APPS_MD, re.DOTALL
).group(0)
APPS_OLDER = APPS_MD.replace(APPS_NEWEST, "", 1)
WHATS_NEW_OLDER = re.sub(r'<Update label="Week 37".*?</Update>\s*', "", WHATS_NEW_MD, count=1, flags=re.DOTALL)


def _pagewatch_only_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    monkeypatch.setattr(pw, "NEWS_INDEX_WATCHES", [])
    monkeypatch.setattr(pw, "RELEASE_NOTE_WATCHES", [])


def test_entry_watches_fetch_the_markdown_pages():
    by_key = {watch["key"]: watch for watch in pw.ENTRY_WATCHES}
    assert by_key["claude-apps-release-notes"]["md"] == APPS_PAGE + ".md"
    assert by_key["claude-code-whats-new"]["md"] == WHATS_NEW_PAGE + "/index.md"
    assert {watch["lane"] for watch in pw.ENTRY_WATCHES} == {"ship"}


def test_claude_apps_entries_parse_bold_titles_under_dates():
    entries = pw.claude_apps_entries(APPS_MD, APPS_PAGE)
    assert entries[0]["title"] == "Claude Haiku 5.5 launch"
    assert entries[0]["url"] == APPS_PAGE + "#october-7-2026-claude-haiku-5-5-launch"
    assert entries[0]["summary"].startswith("October 7, 2026 — We just launched Claude Haiku 5.5")
    assert entries[1]["title"] == "Monthly API credits for Max and Team plans"
    assert entries[1]["summary"].startswith("October 7, 2026 —")
    titles = [entry["title"] for entry in entries]
    assert not any(title.startswith("Note") for title in titles)
    # Titles reused on other days ("Project Sharing") stay separate entries.
    urls = [entry["url"] for entry in entries]
    assert len(urls) == len(set(urls))
    assert len(titles) > len(set(titles))
    assert all(entry["url"].startswith(APPS_PAGE + "#") for entry in entries)


def test_whats_new_entries_one_per_week_with_week_url():
    entries = pw.whats_new_entries(WHATS_NEW_MD, WHATS_NEW_PAGE)
    assert len(entries) == WHATS_NEW_MD.count("<Update ")
    assert entries[0]["title"] == "Week 37: claude plugin eval"
    assert entries[0]["url"] == "https://code.claude.com/docs/en/whats-new/2026-w37"
    assert "Desktop pane" in entries[0]["summary"]
    assert "digest →" not in entries[0]["summary"]
    urls = [entry["url"] for entry in entries]
    assert len(urls) == len(set(urls))
    # The week range stays out so a late digest is not stale-dropped.
    assert all(first_stated_date(entry["title"] + " " + entry["summary"]) is None for entry in entries[:5])


def test_entry_watches_baseline_silently_then_emit_the_new_entry(tmp_path, monkeypatch):
    _pagewatch_only_entries(monkeypatch, tmp_path)
    pages = {"apps": APPS_OLDER, "code": WHATS_NEW_OLDER}

    def fetch(url, timeout=30):
        return pages["apps"] if "support.claude.com" in url else pages["code"]

    monkeypatch.setattr(pw, "fetch_text", fetch)
    assert pw.check_pages(verbose=False) == []
    assert pw.FAILURES == []
    slugs = json.loads((tmp_path / "pw.json").read_text())["slugs"]
    assert len(slugs["claude-apps-release-notes"]) == len(pw.claude_apps_entries(APPS_OLDER, APPS_PAGE))
    assert len(slugs["claude-code-whats-new"]) == len(pw.whats_new_entries(WHATS_NEW_OLDER, WHATS_NEW_PAGE))

    assert pw.check_pages(verbose=False) == []

    pages.update(apps=APPS_MD, code=WHATS_NEW_MD)
    fresh = pw.check_pages(verbose=False)
    assert [item["title"] for item in fresh] == [
        "Claude apps — Claude Haiku 5.5 launch",
        "Claude Code — Week 37: claude plugin eval",
    ]
    assert [item["url"] for item in fresh] == [
        APPS_PAGE + "#october-7-2026-claude-haiku-5-5-launch",
        "https://code.claude.com/docs/en/whats-new/2026-w37",
    ]
    assert fresh[0]["id"] == "pagewatch:claude-apps-release-notes:" + fresh[0]["url"]
    for item in fresh:
        classified = ct.classify_pagewatch({**item, "found_at": NOW.isoformat()})
        assert classified["primaryCategory"] == "ship"
        assert classified["url"] == item["url"]
    assert not ct.is_stale_dated_item(fresh[0], datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert not ct.is_stale_dated_item(fresh[1], NOW)

    assert pw.check_pages(verbose=False) == []


def test_failed_first_fetch_is_not_a_baseline(tmp_path, monkeypatch):
    _pagewatch_only_entries(monkeypatch, tmp_path)
    monkeypatch.setattr(pw, "ENTRY_WATCHES", pw.ENTRY_WATCHES[:1])

    def _boom(url, timeout=30):
        raise OSError("down")

    monkeypatch.setattr(pw, "fetch_text", _boom)
    assert pw.check_pages(verbose=False) == []
    assert pw.FAILURES
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: "<html>no entries</html>")
    assert pw.check_pages(verbose=False) == []
    assert "claude-apps-release-notes" not in json.loads((tmp_path / "pw.json").read_text())["slugs"]
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: APPS_MD)
    assert pw.check_pages(verbose=False) == []


def test_entry_urls_ground_on_their_markdown_section():
    assert ct._looks_like_changelog(APPS_PAGE + "#october-7-2026-claude-haiku-5-5-launch")
    assert ct._looks_like_changelog("https://code.claude.com/docs/en/whats-new/2026-w37")
    section = ct.focus_changelog_section(APPS_MD[:1800], "Claude apps — Monthly API credits for Max and Team plans")
    assert section.startswith("**Monthly API credits for Max and Team plans**")
    assert "Haiku" not in section
    assert "Sonnet 5.5" not in section


def test_claudedevs_is_official_and_replies_are_dropped(tmp_path, monkeypatch):
    payload = json.loads((FIXTURES / "x_claudedevs_posts.json").read_text())
    posts = payload["data"]
    launch_id = "2110866210573386065"
    launch = next(post for post in posts if post["id"] == launch_id)
    assert launch["text"].startswith("Code Review is now generally available")
    assert not xmon.is_reply(launch)

    accounts = {account["handle"]: account["id"] for account in xmon.ACCOUNTS}
    assert accounts["ClaudeDevs"] == "2024518793679294464"
    monkeypatch.setattr(xmon, "ACCOUNTS", tuple(a for a in xmon.ACCOUNTS if a["handle"] == "ClaudeDevs"))
    monkeypatch.setenv("CLAWBYTES_X_WATCH", "1")
    monkeypatch.setenv("CLAWBYTES_X_BEARER_TOKEN", "x-bearer-test")
    monkeypatch.setattr(xmon, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(xmon, "STATE_FILE", tmp_path / "x.json")
    monkeypatch.setattr(xmon, "FAILURES", [])

    def fetch(user_id, token, since_id=""):
        # The API applies since_id; the baseline is everything before the launch thread.
        if not since_id:
            return {"data": [post for post in posts if int(post["id"]) < int(launch_id)]}
        return {"data": [post for post in posts if int(post["id"]) > int(since_id)]}

    monkeypatch.setattr(xmon, "fetch_posts", fetch)
    assert xmon.check_x(verbose=False) == []
    items = xmon.check_x(verbose=False)
    assert xmon.FAILURES == []
    assert [item["id"] for item in items] == [launch_id]
    assert items[0]["url"] == f"https://x.com/ClaudeDevs/status/{launch_id}"
    assert xmon.check_x(verbose=False) == []

    candidate = ct.classify_x({**items[0], "found_at": NOW.isoformat()})
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 64
    assert "reported" not in candidate


def test_claudedevs_desktop_parallel_sessions_post_ships(tmp_path, monkeypatch):
    """The Oct 14 2026 Claude Code desktop launch (parallel sessions, /btw tabs)."""
    posts = json.loads((FIXTURES / "x_claudedevs_desktop_posts.json").read_text())["data"]
    launch_id = "2110445844675838148"
    monkeypatch.setattr(xmon, "ACCOUNTS", tuple(a for a in xmon.ACCOUNTS if a["handle"] == "ClaudeDevs"))
    monkeypatch.setenv("CLAWBYTES_X_WATCH", "1")
    monkeypatch.setenv("CLAWBYTES_X_BEARER_TOKEN", "x-bearer-test")
    monkeypatch.setattr(xmon, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(xmon, "STATE_FILE", tmp_path / "x.json")
    monkeypatch.setattr(xmon, "FAILURES", [])

    def fetch(user_id, token, since_id=""):
        if not since_id:
            return {"data": [post for post in posts if int(post["id"]) < int(launch_id)]}
        return {"data": [post for post in posts if int(post["id"]) > int(since_id)]}

    monkeypatch.setattr(xmon, "fetch_posts", fetch)
    assert xmon.check_x(verbose=False) == []
    items = xmon.check_x(verbose=False)
    assert [item["id"] for item in items] == [launch_id]
    candidate = ct.classify_x({**items[0], "found_at": NOW.isoformat()})
    assert candidate["primaryCategory"] == "ship"
    assert "reported" not in candidate
