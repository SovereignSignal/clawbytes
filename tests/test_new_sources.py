"""Status, TestingCatalog, Kilo Blog, and Havoptic. Fixtures are recorded feeds."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clawbytes_threads as ct
import source_health as sh

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("claw_rss_monitor_sources", SCRIPTS / "claw-rss-monitor.py")
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)


def _xml(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _entries(name: str) -> list:
    return rss.parse_feed(_xml(name))


def _isolate(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "state.json")
    monkeypatch.setattr(ct, "now_utc", lambda: NOW)
    ct.save_json(ct.THREAD_STATE_FILE, {
        "seenSourceKeys": [],
        "postedBacklogIds": [],
        "postedUrls": [],
        "publishLog": [],
    })
    ct.save_json(ct.BACKLOG_FILE, {"items": []})
    return memory


def test_feed_list_registers_the_new_sources():
    names = {feed["name"]: feed["url"] for feed in rss.RSS_FEEDS}
    assert len(rss.RSS_FEEDS) == 88
    assert names["Claude Status"] == "https://status.claude.com/history.rss"
    assert names["Cursor Status"] == "https://status.cursor.com/history.rss"
    assert names["GitHub Status"] == "https://www.githubstatus.com/history.rss"
    assert names["TestingCatalog"] == "https://testingcatalog.com/rss/"
    assert names["Kilo Blog"] == "https://blog.kilo.ai/feed"
    assert names["Havoptic Releases"] == "https://havoptic.com/feed.xml"
    assert "kilo blog" in ct.CHANGELOG_SHIP_FEED_NAMES
    assert ct.repo_name_from_feed("Kilo Code Releases") == "kilo code"
    assert ct.repo_name_from_feed("Kilo Blog") == "kilo blog"
    assert "status" in sh.EMPTY_IS_HEALTHY


def test_status_updates_collapse_to_one_incident():
    entries = rss.collapse_status_entries(_entries("status-updates-grouped.xml"))
    assert len(entries) == 1
    assert entries[0]["link"] == "https://status.claude.com/incidents/4xvtc2gnq73l"
    assert entries[0]["id"] == entries[0]["link"]
    assert "Investigating -" not in entries[0]["title"]
    assert "Resolved -" not in entries[0]["title"]
    assert rss.status_incident_allowed("Claude Status", entries[0])
    assert ct.same_story_titles(entries[0]["title"], entries[0]["title"])


def test_recorded_claude_sep29_reaches_watch_and_short_noise_does_not():
    sep29 = _entries("status-claude-sep29.xml")[0]
    assert rss.is_relevant(sep29, "Claude Status")
    sep29["published"] = NOW.isoformat()
    candidate = ct.classify_rss({**sep29, "feed": "Claude Status"})
    assert candidate["primaryCategory"] == "watch"
    assert "claude.ai" in candidate["title"]
    assert "September" not in candidate["summary"]
    assert "resolved after" in candidate["summary"]

    opus = _entries("status-claude-opus-short.xml")[0]
    assert not rss.is_relevant(opus, "Claude Status")
    usage = _entries("status-claude-usage.xml")[0]
    assert not rss.is_relevant(usage, "Claude Status")
    maintenance = {
        "title": "Scheduled maintenance for the Claude API",
        "detail": "Oct 8 , 04:00 UTC Scheduled - maintenance window for the Claude API.",
        "summary": "",
        "published": "Wed, 08 Oct 2026 04:00:00 +0000",
    }
    assert not rss.status_incident_allowed("Claude Status", maintenance)


def test_recorded_cursor_oct6_keeps_real_incidents_distinct():
    kept = []
    for entry in _entries("status-cursor-oct6.xml"):
        if rss.status_incident_allowed("Cursor Status", entry):
            kept.append(ct.status_display_title("Cursor Status", entry["title"]))
    assert kept == [
        "Cursor: Cloud Agents and Grok Bot",
        "Cursor: Security Reviewer Agents",
    ]
    assert not ct.same_story_titles(kept[0], kept[1])
    assert not any("Anthropic models" in title for title in kept)
    assert not any(title.endswith("Cloud Agents") for title in kept)


def test_recorded_github_actions_kept_and_billing_dropped():
    actions = _entries("status-github-actions.xml")[0]
    billing = _entries("status-github-billing.xml")[0]
    assert rss.status_incident_allowed("GitHub Status", actions)
    assert not rss.status_incident_allowed("GitHub Status", billing)
    candidate = ct.classify_rss({**actions, "feed": "GitHub Status", "published": NOW.isoformat()})
    assert candidate["primaryCategory"] == "watch"
    assert "Actions" in candidate["title"] or "Actions" in candidate["summary"]


def test_status_health_line_on_empty_and_on_fetch_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rss, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(rss, "STATE_FILE", tmp_path / "claw-rss-state.json")
    monkeypatch.setattr(rss, "RSS_FEEDS", [
        {"name": "Claude Status", "url": "https://status.claude.com/history.rss", "tags": ["status"]},
    ])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: _xml("status-claude-sep29.xml"))
    assert rss.check_feeds(verbose=False)[0] == []
    assert "STATUS_HEALTH status=empty items=0 reason=-" in capsys.readouterr().out

    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: None)
    assert rss.check_feeds(verbose=False)[0] == []
    failed = capsys.readouterr().out
    assert "STATUS_HEALTH status=error" in failed
    assert "Claude Status fetch failed" in failed

    observed = ct._status_health_observation(
        "Found 2 new relevant items\nSTATUS_HEALTH status=empty items=0 reason=-\n"
    )
    assert observed == ("empty", 0, "-")
    # The word "error" in a health field used to mark a quiet RSS run as a failure.
    quiet, quiet_items, quiet_error = sh.outcome_from_process(
        returncode=0,
        stdout="Found 0 new relevant items\nSTATUS_HEALTH status=empty items=0 reason=-\n",
    )
    assert (quiet, quiet_items, quiet_error) == ("empty", 0, "-")
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    sent = []
    sh.record_source_health(
        "status", status="empty", items=0, error="-",
        memory_dir=tmp_path, now=NOW,
        alert_sender=lambda text: sent.append(text) or True,
    )
    assert sent == []


def test_testingcatalog_fixture_filters_and_marks_leaks():
    rows = {entry["title"]: entry for entry in _entries("testingcatalog-sample.xml")}
    launch = rows["Mistral launches Large 4 preview with 1 T parameters"]
    leak = rows["Google prepares Antigravity for Gemini 4 Argon and Concierge"]
    rumor = rows["Anthropic may release Claude Sonnet 5.5 within days"]
    assert rss.testingcatalog_relevant(launch)
    assert rss.testingcatalog_relevant(leak)
    assert rss.testingcatalog_relevant(rumor)
    assert not rss.is_reported_claim(launch["title"], launch.get("summary") or "")
    assert rss.is_reported_claim(leak["title"], leak.get("summary") or "")
    assert not rss.testingcatalog_relevant(rows["Maket 2.0 brings AI floor plans and 3D home renders"])
    assert not rss.testingcatalog_relevant(rows["Sesame updates voice agents with their own computers"])
    assert not rss.testingcatalog_relevant(rows["Claude adds editing in Google Docs, Sheets, and Slides"])

    leak["published"] = NOW.isoformat()
    candidate = ct.classify_rss({**leak, "feed": "TestingCatalog"})
    assert candidate["primaryCategory"] == "ship"
    assert candidate["reported"] is True
    assert candidate["summary"].startswith("Reportedly")

    launch["published"] = NOW.isoformat()
    launched = ct.classify_rss({**launch, "feed": "TestingCatalog"})
    assert launched["primaryCategory"] == "ship"
    assert not launched.get("reported")
    assert "coding" in launched["summary"].lower()


def test_kilo_blog_product_posts_ship_and_essays_do_not():
    rows = {entry["title"]: entry for entry in _entries("kilo-blog-sample.xml")}
    desktop = rows["Introducing Kilo Desktop"]
    agents = rows["Cloud Agents Can Now Take Inline Feedback"]
    essay = rows["Dots, Instinct, and Muse: The Same Category, Different Customers"]
    assert rss.is_relevant(desktop, "Kilo Blog", tags=["coding-agent"])
    assert rss.is_relevant(agents, "Kilo Blog", tags=["coding-agent"])
    assert not rss.is_relevant(essay, "Kilo Blog", tags=["coding-agent"])
    desktop["published"] = NOW.isoformat()
    candidate = ct.classify_rss({**desktop, "feed": "Kilo Blog"})
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= 58
    assert "Kilo Desktop" in candidate["title"]


def test_havoptic_fixture_prefers_vendor_url_and_dedupes():
    shaped = [rss.normalize_havoptic_entry(entry) for entry in _entries("havoptic-sample.xml")]
    by_title = {entry["title"]: entry for entry in shaped}
    claude = by_title["Claude Code v2.1.292"]
    assert claude["link"] == "https://github.com/anthropics/claude-code/releases/tag/v2.1.292"
    assert claude["aggregator"] == "havoptic"
    assert by_title["Antigravity CLI v1.3.0"]["link"].endswith("/releases/tag/1.3.0")
    assert by_title["Kiro CLI v2.27.0"]["link"] == "https://kiro.dev/changelog/cli/2-27"
    grok = by_title["Grok Build v1.0.46"]
    assert grok["link"] == "https://x.ai/build/changelog#1.0.46"
    assert not ct.urls_same_release(grok["link"], "https://x.ai/build/changelog#1.0.45")
    assert ct.urls_same_release(
        claude["link"],
        "https://github.com/anthropics/claude-code/releases/tag/v2.1.292",
    )

    fresh = NOW.isoformat()
    tag = claude["link"]
    antigravity = by_title["Antigravity CLI v1.3.0"]
    covered = ct.havoptic_already_covered(
        {"aggregator": "havoptic", "sourceName": claude["_feed"], "title": claude["title"], "url": tag},
        {tag},
        {("claude", "2.1.292")},
    )
    assert covered
    assert not ct.havoptic_already_covered(
        {
            "aggregator": "havoptic",
            "sourceName": antigravity["_feed"],
            "title": antigravity["title"],
            "url": antigravity["link"],
        },
        {tag},
        {("claude", "2.1.292")},
    )
    minor = ct.classify_rss({
        "feed": antigravity["_feed"],
        "title": antigravity["title"],
        "link": antigravity["link"],
        "published": fresh,
        "id": antigravity["id"],
        "aggregator": "havoptic",
    })
    assert minor["primaryCategory"] == "ship"
    assert minor["aggregator"] == "havoptic"
    assert ct.ship_low_signal_reason(minor) is None


def test_havoptic_collect_keeps_primary_and_posts_backstop(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    fresh = NOW.isoformat()
    tag = "https://github.com/anthropics/claude-code/releases/tag/v2.1.292"
    monkeypatch.setattr(ct, "collect_candidates", lambda: {
        "rss": [
            {
                "feed": "Claude Code Releases",
                "title": "v2.1.292",
                "link": tag,
                "published": fresh,
                "id": "primary-292",
            },
            {
                "feed": "Havoptic Claude Code Releases",
                "title": "Claude Code v2.1.292",
                "link": tag,
                "published": fresh,
                "id": "hav-292",
                "aggregator": "havoptic",
            },
            {
                "feed": "Havoptic Antigravity CLI Releases",
                "title": "Antigravity CLI v1.3.0",
                "link": "https://github.com/google-antigravity/antigravity-cli/releases/tag/1.3.0",
                "published": fresh,
                "id": "hav-ag",
                "aggregator": "havoptic",
            },
        ],
    })
    added = ct.collect_into_backlog()["items"]
    titles = [item["title"] for item in added]
    assert sum("2.1.292" in title for title in titles) == 1
    assert any(item["url"] == tag and item.get("aggregator") != "havoptic" for item in added)
    assert any("1.3.0" in item["title"] and item.get("aggregator") == "havoptic" for item in added)


def test_writer_and_curator_mark_reported_items():
    prompt, facts = ct._writer_inputs(
        [{
            "title": "Google prepares Antigravity for Gemini 4",
            "url": "https://www.testingcatalog.com/example",
            "summary": "Reportedly: Antigravity labels point to Argon.",
            "sourceType": "rss",
            "reported": True,
        }],
        "ship",
    )
    assert 'say "reportedly" or "spotted"' in prompt or "reportedly" in prompt
    assert "REPORTED" in facts
    curator = (Path(__file__).resolve().parent.parent / "docs" / "curator-prompt.md").read_text()
    assert '"reported": true' in curator
    assert "reportedly" in curator


def test_status_deterministic_post_line(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sep29 = _entries("status-claude-sep29.xml")[0]
    sep29["published"] = NOW.isoformat()
    candidate = ct.classify_rss({**sep29, "feed": "Claude Status"})
    ct.save_json(ct.BACKLOG_FILE, {"items": [ct.backlog_item(candidate)]})
    rendered = ct.format_category_bundle("watch", use_llm=False)
    assert "Elevated errors on claude.ai, Claude Code, Claude Cowork and the Claude API" in rendered
    assert "status.claude.com/incidents/4xvtc2gnq73l" in rendered
    assert "resolved after" in rendered
    assert "September" not in rendered
