"""Cross-lane story window (7 days) and stated-date staleness (3 days).

Fixtures are the headlines from posts #938, #950, #958, #964, #971, #972, #976.
"""
from datetime import datetime, timedelta, timezone

import clawbytes_threads as ct


NOW = datetime(2026, 10, 6, 23, tzinfo=timezone.utc)


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
    return memory


def _item(url, title, status="queued", category="read", discovered=None, summary=""):
    stamp = discovered or NOW
    return {
        "id": url,
        "url": url,
        "title": title,
        "summary": summary,
        "sourceType": "rss",
        "sourceName": "Example",
        "sourceId": url,
        "primaryCategory": category,
        "categories": [category],
        "score": 70,
        "publishedAt": stamp.isoformat(),
        "discoveredAt": stamp.isoformat(),
        "expiresAt": (NOW + timedelta(days=5)).isoformat(),
        "status": status,
        "postedCategories": [category] if status == "posted" else [],
    }


def test_same_story_titles_from_the_channel():
    assert ct.same_story_titles("Introducing Mistral Large 4", "Mistral Large 4")
    assert ct.same_story_titles(
        "Introducing Mistral Large 4",
        "Introducing Mistral Large 4: Le chonk",
    )
    assert ct.same_story_titles(
        "Kiro — Introducing Workflows in Web",
        "Kiro — Workflow Delegation, Live Steering Context, and Saved Prompt Commands",
    )
    assert ct.same_story_titles(
        "Kiro — Introducing Workflows in Web",
        "Kiro — Workflows, Safer Untrusted Workspaces, and Enterprise Sign-In Controls",
    )
    # Different versions and different products stay distinct.
    assert not ct.same_story_titles("Claude Code 2.1.290", "Claude Code 2.1.291")
    assert not ct.same_story_titles("Codex 0.158.0", "Codex 0.161.0")
    assert not ct.same_story_titles("Mistral Large 3", "Mistral Large 4")
    assert not ct.same_story_titles("OpenClaw 2026.9.7", "OpenClaw 2026.9.8")
    assert not ct.same_story_titles(
        "Kiro — Claude Sonnet 5.5 Now Available",
        "Kiro — Claude Opus 5.5 Now Available",
    )
    assert not ct.same_story_titles(
        "Devin CLI v3000.11.1 / v3000.11.3",
        "Devin: October 5, 2026",
    )


def test_mistral_does_not_queue_twice_inside_seven_days(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    posted = _item(
        "https://news.ycombinator.com/item?id=49977979",
        "Mistral Large 4",
        status="posted",
        category="community",
        discovered=NOW - timedelta(days=1),
    )
    again = _item(
        "https://simonwillison.net/2026/Oct/6/le-chonk/",
        "Introducing Mistral Large 4: Le chonk",
        category="read",
    )
    other = _item(
        "https://example.com/codex",
        "Codex 0.161.0",
        category="read",
    )
    ct.save_json(ct.BACKLOG_FILE, {"items": [posted, again, other]})
    urls = [item["url"] for item in ct.queue_for_category("read")]
    assert again["url"] not in urls
    assert other["url"] in urls


def test_story_window_expires(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    posted = _item(
        "https://example.com/old",
        "Mistral Large 4",
        status="posted",
        discovered=NOW - timedelta(days=8),
    )
    again = _item("https://example.com/new", "Introducing Mistral Large 4")
    ct.save_json(ct.BACKLOG_FILE, {"items": [posted, again]})
    urls = [item["url"] for item in ct.queue_for_category("read")]
    assert again["url"] in urls


def test_stale_devin_changelog_is_skipped(monkeypatch, tmp_path):
    """#964 posted September 21/22 notes on October 5."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(ct, "now_utc", lambda: datetime(2026, 10, 5, 16, tzinfo=timezone.utc))
    stale = _item(
        "https://docs.devin.ai/cli/changelog/stable#updated-db37b8f3",
        "Devin CLI updated",
        category="ship",
        summary="Changelog page updated — September 22, 2026",
    )
    fresh = _item(
        "https://docs.devin.ai/release-notes/overview#october-5-2026",
        "Devin: October 5, 2026",
        category="ship",
        summary="Changelog page updated",
    )
    # September 25 seen on September 28 is three days old, still inside the window.
    borderline = _item(
        "https://docs.devin.ai/release-notes/overview#september-25-2026",
        "Devin September 25, 2026",
        category="ship",
    )
    ct.save_json(ct.BACKLOG_FILE, {"items": [stale, fresh, borderline]})
    # Re-bind now after the helper captured NOW. The items' expiry is in October.
    monkeypatch.setattr(ct, "now_utc", lambda: datetime(2026, 10, 5, 16, tzinfo=timezone.utc))
    urls = [item["url"] for item in ct.queue_for_category("ship")]
    assert stale["url"] not in urls
    assert fresh["url"] in urls
    # Borderline uses the module now. September 25 vs October 5 is more than 3 days,
    # so check the predicate directly against September 28.
    seen = datetime(2026, 9, 28, 16, tzinfo=timezone.utc)
    assert ct.is_stale_dated_item(
        {"title": "Devin September 25, 2026", "summary": ""},
        seen,
    ) is False
    assert ct.is_stale_dated_item(stale, datetime(2026, 10, 5, tzinfo=timezone.utc)) is True
    assert ct.is_stale_dated_item(
        {"title": "retires November 30, 2026", "summary": ""},
        datetime(2026, 10, 5, tzinfo=timezone.utc),
    ) is False


def test_collect_does_not_queue_a_stale_pagewatch_item(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(ct, "now_utc", lambda: datetime(2026, 10, 5, 16, tzinfo=timezone.utc))
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"pagewatch": [
        {
            "id": "pagewatch:devin:old",
            "watch": "Devin CLI",
            "lane": "ship",
            "title": "Devin CLI updated",
            "url": "https://docs.devin.ai/cli/changelog/stable#updated-old",
            "summary": "Changelog page updated — September 22, 2026",
            "found_at": "2026-10-05T16:00:00+00:00",
        },
        {
            "id": "pagewatch:devin:new",
            "watch": "Devin CLI",
            "lane": "ship",
            "title": "Devin: October 5, 2026",
            "url": "https://docs.devin.ai/release-notes/overview#october-5-2026",
            "summary": "Changelog page updated",
            "found_at": "2026-10-05T16:00:00+00:00",
        },
    ]})
    ct.collect_into_backlog()
    stored = {item["url"] for item in ct.load_json(ct.BACKLOG_FILE, {"items": []})["items"]}
    assert "https://docs.devin.ai/cli/changelog/stable#updated-old" not in stored
    assert "https://docs.devin.ai/release-notes/overview#october-5-2026" in stored


# --- conflicting events stay distinct (2026-10-14 review, finding 3) --------

DISTINCT_PAIRS = [
    ("Claude Code adds remote agents", "Claude Code removes remote agents"),
    ("Cursor launches background agents", "Cursor background agents security vulnerability"),
    ("Claude Code enables sandbox isolation", "Claude Code disables sandbox isolation"),
]


def test_conflicting_event_pairs_are_not_the_same_story():
    for left, right in DISTINCT_PAIRS:
        assert not ct.same_story_titles(left, right), (left, right)
        assert not ct.same_story_titles(right, left), (right, left)
        assert ct.story_repeat_match(right, [left]) is None


def test_paraphrases_of_one_release_still_dedupe():
    assert ct.same_story_titles(
        "Claude Code 2.1.300 adds remote agents",
        "Remote agents arrive in Claude Code 2.1.300",
    )
    assert ct.story_repeat_match(
        "Remote agents arrive in Claude Code 2.1.300",
        ["Claude Code 2.1.300 adds remote agents"],
    ) == "identified"
    assert ct.same_story_titles("Cursor launches background agents", "Cursor introduces background agents")
    assert ct.story_repeat_match(
        "Cursor introduces background agents", ["Cursor launches background agents"]
    ) == "fuzzy"
    assert ct.story_repeat_match(
        "Cursor background agents vulnerability CVE-2026-12345",
        ["Security advisory: CVE-2026-12345 in Cursor background agents"],
    ) == "identified"


def _rss_candidate(url, title):
    return {
        "id": url,
        "title": title,
        "url": url,
        "summary": "",
        "sourceType": "rss",
        "sourceName": "Example",
        "sourceId": url,
        "primaryCategory": "read",
        "categories": ["read"],
        "score": 70,
        "publishedAt": NOW,
        "expiresAt": NOW + timedelta(days=3),
    }


def _collect_once(monkeypatch, posted, candidates):
    ct.save_json(ct.BACKLOG_FILE, {"items": posted})
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": candidates})
    monkeypatch.setattr(ct, "classify_source_candidate", lambda _kind, item: dict(item))
    ct.collect_into_backlog()
    stored = {row["url"] for row in ct.load_json(ct.BACKLOG_FILE, {"items": []})["items"]}
    seen = set(ct.load_json(ct.THREAD_STATE_FILE, {}).get("seenSourceKeys", []))
    return stored, seen


def test_collect_queues_the_conflicting_pairs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    posted = [
        _item(f"https://example.com/posted-{i}", left, status="posted", discovered=NOW - timedelta(days=1))
        for i, (left, _right) in enumerate(DISTINCT_PAIRS)
    ]
    candidates = [_rss_candidate(f"https://example.com/new-{i}", right) for i, (_l, right) in enumerate(DISTINCT_PAIRS)]
    stored, _seen = _collect_once(monkeypatch, posted, candidates)
    for candidate in candidates:
        assert candidate["url"] in stored, candidate["title"]


def test_collect_fuzzy_repeat_is_skipped_but_not_marked_seen(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    posted = [_item("https://example.com/p", "Cursor launches background agents",
                    status="posted", discovered=NOW - timedelta(days=1))]
    fuzzy = _rss_candidate("https://example.com/fuzzy", "Cursor introduces background agents")
    stored, seen = _collect_once(monkeypatch, posted, [fuzzy])
    assert fuzzy["url"] not in stored
    assert ct.source_key("rss", fuzzy["sourceId"], fuzzy["url"]) not in seen


def test_collect_identified_repeat_is_marked_seen(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    posted = [_item("https://example.com/p", "Claude Code 2.1.300 adds remote agents",
                    status="posted", discovered=NOW - timedelta(days=1))]
    same = _rss_candidate("https://example.com/same", "Remote agents arrive in Claude Code 2.1.300")
    stored, seen = _collect_once(monkeypatch, posted, [same])
    assert same["url"] not in stored
    assert ct.source_key("rss", same["sourceId"], same["url"]) in seen
