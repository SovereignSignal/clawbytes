"""Status feeds baseline per URL, and startup drops incidents from before that."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "claw_rss_monitor_baseline", SCRIPTS / "claw-rss-monitor.py"
)
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)

NOW = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
BASELINE_AT = "2026-10-07T16:30:00+00:00"

CLAUDE_URL = "https://status.claude.com/history.rss"
CURSOR_URL = "https://status.cursor.com/history.rss"
GITHUB_URL = "https://www.githubstatus.com/history.rss"
STATUS_FEEDS = [
    {"name": "Claude Status", "url": CLAUDE_URL, "tags": ["status"]},
    {"name": "Cursor Status", "url": CURSOR_URL, "tags": ["status"]},
    {"name": "GitHub Status", "url": GITHUB_URL, "tags": ["status"]},
]


def _xml(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _isolate_rss(monkeypatch, tmp_path):
    monkeypatch.setattr(rss, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(rss, "STATE_FILE", tmp_path / "claw-rss-state.json")
    monkeypatch.setattr(rss, "RSS_FEEDS", list(STATUS_FEEDS))


def _state(tmp_path) -> dict:
    return json.loads((tmp_path / "claw-rss-state.json").read_text())


def test_three_status_feeds_baseline_together(monkeypatch, tmp_path, capsys):
    _isolate_rss(monkeypatch, tmp_path)
    bodies = {
        CLAUDE_URL: _xml("status-claude-sep29.xml"),
        CURSOR_URL: _xml("status-cursor-oct6.xml"),
        GITHUB_URL: _xml("status-github-actions.xml"),
    }
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: bodies[url])
    assert rss.check_feeds(verbose=False)[0] == []
    out = capsys.readouterr().out
    assert out.count("baseline written") == 3
    assert "STATUS_HEALTH status=empty items=0 reason=-" in out
    state = _state(tmp_path)
    assert "https://status.claude.com/incidents/4xvtc2gnq73l" in state["lastSeenByFeed"]["Claude Status"]
    assert "https://status.cursor.com/incidents/bccntkmymws0" in state["lastSeenByFeed"]["Cursor Status"]
    assert "https://www.githubstatus.com/incidents/3q1yb5m7ltvb" in state["lastSeenByFeed"]["GitHub Status"]
    for name, url in (
        ("Claude Status", CLAUDE_URL),
        ("Cursor Status", CURSOR_URL),
        ("GitHub Status", GITHUB_URL),
    ):
        assert state["feedBaseline"][name]["url"] == url
        assert state["feedBaseline"][name].get("pending") is not True
    assert rss.check_feeds(verbose=False)[0] == []
    assert "baseline written" not in capsys.readouterr().out


def test_retired_feed_names_do_not_count_as_a_baseline(monkeypatch, tmp_path, capsys):
    """Cursor and GitHub kept June's atom ids. Those lists are not a baseline."""
    _isolate_rss(monkeypatch, tmp_path)
    (tmp_path / "claw-rss-state.json").write_text(json.dumps({
        "lastSeenByFeed": {
            "Cursor Status": ["https://status.cursor.com/history.atom#old"],
            "GitHub Status": ["https://www.githubstatus.com/history.atom#old"],
        },
        "foundItems": [],
    }))
    bodies = {
        CLAUDE_URL: _xml("status-claude-sep29.xml"),
        CURSOR_URL: _xml("status-cursor-oct6.xml"),
        GITHUB_URL: _xml("status-github-actions.xml"),
    }
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: bodies[url])
    assert rss.check_feeds(verbose=False)[0] == []
    out = capsys.readouterr().out
    assert out.count("baseline written") == 3
    state = _state(tmp_path)
    assert "https://status.cursor.com/history.atom#old" not in state["lastSeenByFeed"]["Cursor Status"]
    assert "https://www.githubstatus.com/history.atom#old" not in state["lastSeenByFeed"]["GitHub Status"]
    assert state["feedBaseline"]["Cursor Status"]["url"] == CURSOR_URL
    assert state["feedBaseline"]["GitHub Status"]["url"] == GITHUB_URL
    assert rss.check_feeds(verbose=False)[0] == []


def test_failed_then_successful_fetch_baselines_on_the_success(monkeypatch, tmp_path, capsys):
    _isolate_rss(monkeypatch, tmp_path)
    monkeypatch.setattr(rss, "RSS_FEEDS", [STATUS_FEEDS[1]])
    first = _xml("status-cursor-oct6.xml")
    extra = first.replace(
        "</channel>",
        "<item><title>Incident with Actions</title>"
        "<link>https://status.cursor.com/incidents/new1</link>"
        "<guid>https://status.cursor.com/incidents/new1</guid>"
        "<pubDate>Wed, 07 Oct 2026 19:00:00 +0000</pubDate>"
        "<description>Oct 7, 18:00 UTC Investigating - service degradation affecting Cloud Agents. "
        "Oct 7, 19:00 UTC Resolved - Cloud Agents recovered.</description>"
        "</item></channel>",
    )
    bodies = iter([None, first, extra])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: next(bodies))

    assert rss.check_feeds(verbose=False)[0] == []
    failed = capsys.readouterr().out
    assert "baseline written" not in failed
    assert "Cursor Status fetch failed" in failed
    state = _state(tmp_path) if (tmp_path / "claw-rss-state.json").exists() else {}
    assert "Cursor Status" not in (state.get("lastSeenByFeed") or {})
    assert not ((state.get("feedBaseline") or {}).get("Cursor Status") or {}).get("url")

    assert rss.check_feeds(verbose=False)[0] == []
    assert "baseline written" in capsys.readouterr().out
    seen = _state(tmp_path)["lastSeenByFeed"]["Cursor Status"]
    assert "https://status.cursor.com/incidents/bccntkmymws0" in seen
    assert _state(tmp_path)["feedBaseline"]["Cursor Status"]["url"] == CURSOR_URL

    again, _status = rss.check_feeds(verbose=False)
    assert [item["link"] for item in again] == ["https://status.cursor.com/incidents/new1"]


def test_unparseable_first_fetch_does_not_baseline(monkeypatch, tmp_path, capsys):
    _isolate_rss(monkeypatch, tmp_path)
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "Brand New Releases",
        "url": "https://example.com/releases.atom",
        "tags": ["releases"],
    }])
    good = (
        "<rss><channel><item><title>v1.0.0</title>"
        "<link>https://ex/1</link><guid>id-1</guid></item></channel></rss>"
    )
    newer = (
        "<rss><channel>"
        "<item><title>v2.0.0</title><link>https://ex/2</link><guid>id-2</guid></item>"
        "<item><title>v1.0.0</title><link>https://ex/1</link><guid>id-1</guid></item>"
        "</channel></rss>"
    )
    bodies = iter(["<html>down</html>", "<rss><channel><item>", good, newer])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: next(bodies))

    assert rss.check_feeds(verbose=False)[0] == []
    assert "baseline written" not in capsys.readouterr().out
    assert rss.check_feeds(verbose=False)[0] == []
    assert "baseline written" not in capsys.readouterr().out
    assert "Brand New Releases" not in _state(tmp_path).get("lastSeenByFeed", {})

    assert rss.check_feeds(verbose=False)[0] == []
    assert "id-1" in _state(tmp_path)["lastSeenByFeed"]["Brand New Releases"]
    third, _status = rss.check_feeds(verbose=False)
    assert [item["title"] for item in third] == ["v2.0.0"]


def test_legacy_non_status_ids_still_diff(monkeypatch, tmp_path):
    _isolate_rss(monkeypatch, tmp_path)
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "Brand New Releases",
        "url": "https://example.com/releases.atom",
        "tags": ["releases"],
    }])
    (tmp_path / "claw-rss-state.json").write_text(json.dumps({
        "lastSeenByFeed": {"Brand New Releases": ["id-1"]},
        "foundItems": [],
    }))
    body = (
        "<rss><channel>"
        "<item><title>v2.0.0</title><link>https://ex/2</link><guid>id-2</guid></item>"
        "<item><title>v1.0.0</title><link>https://ex/1</link><guid>id-1</guid></item>"
        "</channel></rss>"
    )
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: body)
    items, _status = rss.check_feeds(verbose=False)
    assert [item["title"] for item in items] == ["v2.0.0"]
    assert _state(tmp_path)["feedBaseline"]["Brand New Releases"]["url"] == "https://example.com/releases.atom"


def test_changed_feed_url_baselines_again(monkeypatch, tmp_path):
    _isolate_rss(monkeypatch, tmp_path)
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "Brand New Releases",
        "url": "https://example.com/releases.atom",
        "tags": ["releases"],
    }])
    (tmp_path / "claw-rss-state.json").write_text(json.dumps({
        "lastSeenByFeed": {"Brand New Releases": ["old-id"]},
        "feedBaseline": {
            "Brand New Releases": {
                "url": "https://example.com/old.atom",
                "at": "2026-10-01T00:00:00+00:00",
            },
        },
    }))
    body = (
        "<rss><channel><item><title>v9.0.0</title>"
        "<link>https://ex/9</link><guid>id-9</guid></item></channel></rss>"
    )
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: body)
    assert rss.check_feeds(verbose=False)[0] == []
    rec = _state(tmp_path)["feedBaseline"]["Brand New Releases"]
    assert rec["url"] == "https://example.com/releases.atom"
    assert rec["at"] == "2026-10-01T00:00:00+00:00"
    assert "id-9" in _state(tmp_path)["lastSeenByFeed"]["Brand New Releases"]


def test_empty_later_fetch_does_not_wipe_seen_ids(monkeypatch, tmp_path):
    _isolate_rss(monkeypatch, tmp_path)
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "Brand New Releases",
        "url": "https://example.com/releases.atom",
        "tags": ["releases"],
    }])
    first = (
        "<rss><channel><item><title>v1.0.0</title>"
        "<link>https://ex/1</link><guid>id-1</guid></item></channel></rss>"
    )
    empty = "<rss><channel></channel></rss>"
    newer = (
        "<rss><channel>"
        "<item><title>v2.0.0</title><link>https://ex/2</link><guid>id-2</guid></item>"
        "<item><title>v1.0.0</title><link>https://ex/1</link><guid>id-1</guid></item>"
        "</channel></rss>"
    )
    bodies = iter([first, empty, newer])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: next(bodies))
    assert rss.check_feeds(verbose=False)[0] == []
    assert rss.check_feeds(verbose=False)[0] == []
    assert "id-1" in _state(tmp_path)["lastSeenByFeed"]["Brand New Releases"]
    third, _status = rss.check_feeds(verbose=False)
    assert [item["title"] for item in third] == ["v2.0.0"]


def _bind_threads(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "state.json")
    monkeypatch.setattr(ct, "now_utc", lambda: NOW)
    return memory


def _row(source, url, published, title, status="queued", summary="Affects Actions, resolved after 71 minutes"):
    return {
        "id": url.rsplit("/", 1)[-1],
        "url": url,
        "title": title,
        "summary": summary,
        "sourceType": "rss",
        "sourceName": source,
        "sourceId": url,
        "primaryCategory": "watch",
        "categories": ["watch"],
        "score": 70,
        "publishedAt": published,
        "discoveredAt": "2026-10-07T16:32:00+00:00",
        "expiresAt": "2026-10-14T16:32:00+00:00",
        "status": status,
        "postedCategories": [],
    }


def test_cleanup_drops_status_items_before_baseline_or_older_than_a_day(monkeypatch, tmp_path, capsys):
    memory = _bind_threads(monkeypatch, tmp_path)
    security = "https://status.cursor.com/incidents/xgj4x58k55dx"
    github_1625 = "https://www.githubstatus.com/incidents/djlmxz2zd0j7"
    started_earlier = "https://status.cursor.com/incidents/startedearlier"
    too_old = "https://status.claude.com/incidents/tooold"
    keeper = "https://status.claude.com/incidents/keeper"
    changelog = "https://github.blog/changelog/not-a-status-incident"
    posted = "https://status.cursor.com/incidents/bccntkmymws0"
    ct.save_json(memory / "claw-rss-state.json", {
        "lastSeenByFeed": {},
        "foundItems": [{
            "feed": "Cursor Status",
            "link": started_earlier,
            "id": started_earlier,
            "published": "2026-10-07T19:00:00+00:00",
            "detail": (
                "Oct 6, 19:27 UTC Investigating - service degradation affecting Cloud Agents. "
                "Oct 7, 19:00 UTC Resolved - Cloud Agents recovered."
            ),
        }],
        "feedBaseline": {
            "Claude Status": {
                "url": CLAUDE_URL,
                "at": "2026-09-01T00:00:00+00:00",
            },
            "Cursor Status": {"url": CURSOR_URL, "at": BASELINE_AT},
            "GitHub Status": {"url": GITHUB_URL, "at": BASELINE_AT},
        },
    })
    ct.save_json(ct.THREAD_STATE_FILE, {"seenSourceKeys": [], "postedUrls": []})
    ct.save_json(ct.BACKLOG_FILE, {"items": [
        _row("Cursor Status", security, "2026-10-06T18:56:35+00:00", "Cursor: Security Reviewer Agents"),
        _row("GitHub Status", github_1625, "2026-10-07T16:25:14+00:00", "GitHub: Incident with Git Operations, Pull Requests and Actions"),
        _row("Cursor Status", started_earlier, "2026-10-07T19:00:00+00:00", "Cursor: Cloud Agents"),
        _row("Claude Status", too_old, "2026-10-06T12:00:00+00:00", "Claude: old incident", summary="still open"),
        _row("Claude Status", keeper, "2026-10-07T18:00:00+00:00", "Claude: live incident", summary="still open"),
        _row("GitHub Changelog", changelog, "2026-09-01T00:00:00+00:00", "A normal changelog post"),
        _row("Cursor Status", posted, "2026-10-06T22:06:44+00:00", "Cursor: Cloud Agents and Grok Bot", status="posted"),
    ]})

    dropped = ct.drop_prebaseline_status_items(now=NOW)
    assert set(dropped) == {security, github_1625, started_earlier, too_old}
    log = capsys.readouterr().out.strip()
    assert log.startswith("INFO status-queue-cleanup dropped=4 urls=")
    for url in (security, github_1625, started_earlier, too_old):
        assert url in log
    assert keeper not in log
    assert changelog not in log

    items = {item["url"]: item for item in ct.load_json(ct.BACKLOG_FILE, {})["items"]}
    for url in (security, github_1625, started_earlier, too_old):
        assert items[url]["status"] == "retired"
        assert items[url]["retiredReason"] == "status_before_baseline"
    assert items[keeper]["status"] == "queued"
    assert items[changelog]["status"] == "queued"
    assert items[posted]["status"] == "posted"
    seen = set(ct.load_json(ct.THREAD_STATE_FILE, {})["seenSourceKeys"])
    assert f"rss:{security}" in seen
    assert f"rss:{keeper}" not in seen
    baselines = ct.load_json(memory / "claw-rss-state.json", {})["feedBaseline"]
    assert baselines["Claude Status"]["at"] == "2026-09-01T00:00:00+00:00"
    assert baselines["Cursor Status"]["at"] == BASELINE_AT

    again = ct.drop_prebaseline_status_items(now=NOW)
    assert again == []
    assert capsys.readouterr().out.strip() == "INFO status-queue-cleanup dropped=0 urls=-"
    items = {item["url"]: item for item in ct.load_json(ct.BACKLOG_FILE, {})["items"]}
    assert items[keeper]["status"] == "queued"
    assert items[changelog]["status"] == "queued"
    assert items[security]["status"] == "retired"


def test_collect_retires_stale_status_and_keeps_a_later_incident(monkeypatch, tmp_path):
    memory = _bind_threads(monkeypatch, tmp_path)
    stale = "https://status.cursor.com/incidents/xgj4x58k55dx"
    fresh = "https://www.githubstatus.com/incidents/fresh1"
    ct.save_json(memory / "claw-rss-state.json", {
        "lastSeenByFeed": {},
        "feedBaseline": {
            "Claude Status": {"url": CLAUDE_URL, "at": BASELINE_AT},
            "Cursor Status": {"url": CURSOR_URL, "at": BASELINE_AT},
            "GitHub Status": {"url": GITHUB_URL, "at": BASELINE_AT},
        },
        "foundItems": [
            {
                "feed": "Cursor Status",
                "title": "Security Reviewer Agents",
                "link": stale,
                "id": stale,
                "published": "2026-10-06T18:56:35+00:00",
                "detail": "Oct 6, 17:49 UTC Investigating - Security Reviewer agents. Oct 6, 18:56 UTC Resolved - recovered.",
            },
            {
                "feed": "GitHub Status",
                "title": "Incident with Actions",
                "link": fresh,
                "id": fresh,
                "published": "2026-10-07T18:30:00+00:00",
                "detail": (
                    "Oct 7, 18:00 UTC Investigating - GitHub Actions outage. "
                    "Oct 7, 18:30 UTC Resolved - Actions recovered."
                ),
            },
        ],
    })
    ct.save_json(ct.THREAD_STATE_FILE, {"seenSourceKeys": [], "postedUrls": []})
    ct.save_json(ct.BACKLOG_FILE, {"items": [
        _row("Cursor Status", stale, "2026-10-06T18:56:35+00:00", "Cursor: Security Reviewer Agents"),
    ]})
    added = ct.collect_into_backlog()["items"]
    assert [item["url"] for item in added] == [fresh]
    rows = {item["url"]: item for item in ct.load_json(ct.BACKLOG_FILE, {})["items"]}
    assert rows[stale]["status"] == "retired"
    assert rows[fresh]["status"] == "queued"


def test_scheduler_cleans_the_status_queue_before_it_starts(monkeypatch):
    import scheduler

    order = []

    def clean():
        order.append("clean")

    class _Stop(Exception):
        pass

    class _Scheduler:
        def start(self):
            order.append("start")
            raise _Stop()

    monkeypatch.setattr(scheduler, "run_startup_maintenance", clean)
    monkeypatch.setattr(scheduler, "BlockingScheduler", lambda **_kwargs: _Scheduler())
    monkeypatch.setattr(scheduler, "schedule_jobs", lambda _sched: None)
    try:
        scheduler.main()
    except _Stop:
        pass
    assert order == ["clean", "start"]
