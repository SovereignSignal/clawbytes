"""Dedupe keys ignore changelog fragments and still match legacy state.

postedUrls / seenSourceKeys are exact strings. Changelog watches append a
changing ``#updated-<hash>`` fragment, so the same release used to look new.
Comparison normalizes; the stored strings are not rewritten.
"""
from datetime import datetime, timedelta, timezone

import clawbytes_threads as ct


KIRO_A = "https://kiro.dev/changelog#updated-aaa111"
KIRO_B = "HTTPS://Kiro.dev/changelog/#updated-bbb222"
CLAUDE_OLD = "https://docs.claude.com/en/release-notes/overview#updated-abc123"
CLAUDE_NEW = "https://docs.claude.com/en/release-notes/overview#updated-def456"
CLAUDE_OTHER = "https://docs.claude.com/en/release-notes/claude-code#updated-abc123"
RELEASE_170 = "https://github.com/kiro-project/kiro/releases/tag/v1.1.70"
RELEASE_171 = "https://github.com/kiro-project/kiro/releases/tag/v1.1.71"


def test_updated_fragments_count_as_duplicates():
    assert ct.normalize_url_for_dedupe(KIRO_A) == ct.normalize_url_for_dedupe(KIRO_B)
    assert ct.normalize_url_for_dedupe(CLAUDE_OLD) == ct.normalize_url_for_dedupe(CLAUDE_NEW)
    # Tracking params are not part of the identity; other query params are.
    tracked = "https://kiro.dev/changelog?utm_source=telegram&utm_medium=social#updated-ccc"
    assert ct.normalize_url_for_dedupe(tracked) == ct.normalize_url_for_dedupe(KIRO_A)
    kept = "https://example.com/rel?id=170&utm_source=tg"
    assert ct.normalize_url_for_dedupe(kept) == "https://example.com/rel?id=170"


def test_distinct_releases_stay_distinct():
    assert ct.normalize_url_for_dedupe(RELEASE_170) != ct.normalize_url_for_dedupe(RELEASE_171)
    assert ct.normalize_url_for_dedupe(CLAUDE_OLD) != ct.normalize_url_for_dedupe(CLAUDE_OTHER)
    assert ct.normalize_url_for_dedupe("https://example.com/rel?id=170") != ct.normalize_url_for_dedupe(
        "https://example.com/rel?id=171"
    )
    # A fragment on a versioned release URL does not merge it with another version.
    assert ct.normalize_url_for_dedupe(RELEASE_170 + "#updated-aaa") != ct.normalize_url_for_dedupe(
        RELEASE_171 + "#updated-bbb"
    )


def test_legacy_stored_url_matches_without_rewriting_state():
    stored = [CLAUDE_OLD]
    assert ct.normalize_url_for_dedupe(CLAUDE_NEW) in ct.dedupe_url_set(stored)
    assert ct.normalize_url_for_dedupe(KIRO_B) in ct.dedupe_url_set([KIRO_A])
    # Normalization is comparison-only. The legacy string is still the legacy string.
    assert stored == [CLAUDE_OLD]

    legacy_key = "rss:" + CLAUDE_OLD
    fresh_key = "rss:" + CLAUDE_NEW
    assert ct.normalize_seen_key(legacy_key) == ct.normalize_seen_key(fresh_key)
    assert ct.normalize_seen_key(fresh_key) in ct.seen_key_set([legacy_key])
    assert ct.normalize_seen_key("rss:" + RELEASE_171) != ct.normalize_seen_key(legacy_key)


def _queue(monkeypatch, items, posted_urls):
    backlog = {"items": items}
    state = {"postedUrls": posted_urls, "seenSourceKeys": []}
    monkeypatch.setattr(ct, "ensure_files", lambda: None)

    def fake_load(path, default):
        if path == ct.BACKLOG_FILE:
            return backlog
        if path == ct.THREAD_STATE_FILE:
            return state
        return default

    monkeypatch.setattr(ct, "load_json", fake_load)
    return state


def _queued(url, title, score=60):
    return {
        "status": "queued",
        "categories": ["ship"],
        "primaryCategory": "ship",
        "url": url,
        "title": title,
        "score": score,
        "sourceType": "pagewatch",
        "sourceName": "changelog",
    }


def test_queue_treats_legacy_fragment_url_as_already_posted(monkeypatch):
    state = _queue(
        monkeypatch,
        [
            _queued(CLAUDE_NEW, "Claude platform release notes — September 2026", score=80),
            _queued(RELEASE_171, "kiro v1.1.71", score=70),
        ],
        [CLAUDE_OLD],
    )
    urls = [item["url"] for item in ct.queue_for_category("ship")]
    assert urls == [RELEASE_171]
    # Reading the queue must not rewrite the legacy postedUrls entry.
    assert state["postedUrls"] == [CLAUDE_OLD]


def test_queue_collapses_unposted_fragment_variants(monkeypatch):
    _queue(
        monkeypatch,
        [
            _queued(KIRO_A, "Kiro — 1.1.70", score=80),
            _queued(KIRO_B, "Kiro — 1.1.70", score=50),
            _queued(RELEASE_171, "kiro v1.1.71", score=70),
        ],
        [],
    )
    urls = [item["url"] for item in ct.queue_for_category("ship")]
    # Higher score wins; the stored URL (fragment and all) is what remains.
    assert urls == [KIRO_A, RELEASE_171]


def test_collect_skips_fragment_duplicate_and_keeps_legacy_state(monkeypatch):
    legacy_url = KIRO_A
    legacy_key = "pagewatch:pagewatch:kiro-changelog:olddigest"
    now = datetime.now(timezone.utc)
    before_backlog = ct.load_json(ct.BACKLOG_FILE, {"items": []})
    before_state = ct.load_json(ct.THREAD_STATE_FILE, {})
    ct.save_json(ct.THREAD_STATE_FILE, {
        "seenSourceKeys": [legacy_key],
        "postedBacklogIds": [],
        "postedUrls": [legacy_url],
        "lastCollectedAt": None,
        "lastPublishedAt": {},
        "publishLog": [],
    })
    ct.save_json(ct.BACKLOG_FILE, {"items": []})

    def classify(kind, item):
        return {
            "primaryCategory": "ship",
            "categories": ["ship"],
            "score": 70,
            "summary": "ok",
            "expiresAt": now + timedelta(hours=10),
            "publishedAt": now,
            "sourceType": kind,
            "sourceName": "t",
            "sourceId": item["id"],
            "url": item["url"],
            "title": item["title"],
        }

    monkeypatch.setattr(ct, "collect_candidates", lambda: {
        "pagewatch": [
            {"title": "Kiro — 1.1.70", "id": "pagewatch:kiro-changelog:newdigest", "url": KIRO_B},
        ],
        "rss": [
            {"title": "kiro v1.1.71", "id": "kiro-project/kiro:v1.1.71", "url": RELEASE_171},
        ],
    })
    monkeypatch.setattr(ct, "classify_source_candidate", classify)
    try:
        result = ct.collect_into_backlog()
        assert result["added"] == 1
        assert result["items"][0]["url"] == RELEASE_171
        state = ct.load_json(ct.THREAD_STATE_FILE, {})
        assert state["postedUrls"] == [legacy_url]
        assert legacy_key in state["seenSourceKeys"]
        assert "pagewatch:pagewatch:kiro-changelog:newdigest" not in state["seenSourceKeys"]
    finally:
        ct.save_json(ct.BACKLOG_FILE, before_backlog)
        ct.save_json(ct.THREAD_STATE_FILE, before_state)


def test_audit_recognizes_legacy_fragment_as_posted():
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": "pagewatch:claude-release-notes:new",
        "watch": "Claude platform release notes",
        "lane": "ship",
        "title": "Claude platform release notes — September 2026",
        "url": CLAUDE_NEW,
        "summary": "Changelog page updated",
        "found_at": now,
    }
    state = {"postedUrls": [CLAUDE_OLD], "seenSourceKeys": []}
    row = ct.audit_candidate("pagewatch", item, state, {"items": []})
    assert row["status"] == "skipped"
    assert row["reason"] == "posted_url"
    assert state["postedUrls"] == [CLAUDE_OLD]

    distinct = dict(item, id="rel-171", title="kiro v1.1.71", url=RELEASE_171)
    other = ct.audit_candidate("pagewatch", distinct, state, {"items": []})
    assert other["status"] == "would_add"


def test_mark_posted_records_original_urls(monkeypatch):
    legacy = CLAUDE_OLD
    fresh = KIRO_A
    sibling = "https://kiro.dev/changelog#updated-sibling"
    before_backlog = ct.load_json(ct.BACKLOG_FILE, {"items": []})
    before_state = ct.load_json(ct.THREAD_STATE_FILE, {})
    fresh_item = {
        "id": "fresh",
        "url": fresh,
        "title": "Kiro — 1.1.70",
        "categories": ["ship"],
        "status": "queued",
        "postedCategories": [],
    }
    sibling_item = {
        "id": "sibling",
        "url": sibling,
        "title": "Kiro — 1.1.70",
        "categories": ["ship"],
        "status": "queued",
        "postedCategories": [],
    }
    ct.save_json(ct.BACKLOG_FILE, {"items": [fresh_item, sibling_item]})
    ct.save_json(ct.THREAD_STATE_FILE, {
        "seenSourceKeys": [],
        "postedBacklogIds": [],
        "postedUrls": [legacy],
        "lastCollectedAt": None,
        "lastPublishedAt": {},
        "publishLog": [],
    })
    try:
        ct.mark_posted("ship", posted_items=[fresh_item])
        state = ct.load_json(ct.THREAD_STATE_FILE, {})
        assert set(state["postedUrls"]) == {legacy, fresh, sibling}
        backlog = ct.load_json(ct.BACKLOG_FILE, {"items": []})
        assert {item["id"]: item["status"] for item in backlog["items"]} == {
            "fresh": "posted",
            "sibling": "posted",
        }
    finally:
        ct.save_json(ct.BACKLOG_FILE, before_backlog)
        ct.save_json(ct.THREAD_STATE_FILE, before_state)
