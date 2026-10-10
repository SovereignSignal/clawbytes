"""AI Wire ingest: mapping, flag-off no-op, and a push that never fails a post."""
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import ai_wire
import clawbytes_threads as ct


NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
RELEASE_URL = "https://github.com/openai/codex/releases/tag/v0.50.0"


def _release(**extra):
    item = {
        "title": "Codex v0.50.0",
        "url": RELEASE_URL,
        "summary": "New Codex release",
        "sourceName": "Codex Releases",
        "sourceType": "rss",
        "primaryCategory": "ship",
        "score": 70,
        "tags": ["releases", "coding-agent"],
        "publishedAt": "2026-10-09T12:00:00+00:00",
    }
    item.update(extra)
    return item


class _Body:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {"upserted": 1, "created": 1, "keys": []}

    def read(self, _n=-1):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _enable(monkeypatch, token="test-token"):
    monkeypatch.setenv("AI_WIRE_ENABLED", "1")
    monkeypatch.setenv("AI_WIRE_URL", "https://wire.example/")
    monkeypatch.setenv("AI_WIRE_INGEST_TOKEN", token)


def _disable(monkeypatch):
    monkeypatch.delenv("AI_WIRE_ENABLED", raising=False)
    monkeypatch.delenv("AI_WIRE_URL", raising=False)
    monkeypatch.delenv("AI_WIRE_INGEST_TOKEN", raising=False)


# --- mapping -----------------------------------------------------------------


def test_map_github_release_status_and_news():
    release = ai_wire.map_item(
        _release(),
        lane="ship",
        channel_post_url="https://t.me/clawbytes/979",
        posted_at=NOW,
    )
    assert release == {
        "source_bot": "clawbytes",
        "kind": "tool_release",
        "canonical_key": "release:openai/codex@v0.50.0",
        "title": "Codex v0.50.0",
        "url": RELEASE_URL,
        "summary": "New Codex release",
        "published_at": "2026-10-09T12:00:00Z",
        "posted_at": "2026-10-10T16:00:00Z",
        "channel": "clawbytes",
        "channel_post_url": "https://t.me/clawbytes/979",
        "lane": "Ship",
        "org": "openai",
        "tags": ["releases", "coding-agent"],
        "score": 70,
    }

    status = ai_wire.map_item(
        {
            "title": "Elevated errors on the Claude API",
            "url": "https://status.claude.com/incidents/abc123/?utm=1",
            "sourceName": "Claude Status",
            "summary": "<b>Elevated errors</b> on the API",
            "score": 64.5,
            "primaryCategory": "watch",
        },
        lane="watch",
    )
    assert status["kind"] == "status"
    assert status["lane"] == "Watch"
    assert status["canonical_key"] == "url:https://status.claude.com/incidents/abc123"
    assert status["summary"] == "Elevated errors on the API"
    assert status["score"] == 64.5
    assert "channel_post_url" not in status

    news = ai_wire.map_item(
        {
            "title": "The Claude release went badly",
            "url": "https://Example.com/Foo/Bar/?q=1#frag",
            "sourceName": "Simon Willison",
            "summary": "Worth reading",
            "primaryCategory": "read",
        },
        lane="Read",
    )
    assert news["kind"] == "news"
    assert news["lane"] == "Read"
    assert news["canonical_key"] == "url:https://example.com/foo/bar"
    assert news["url"] == "https://Example.com/Foo/Bar/?q=1#frag"
    assert "org" not in news


def test_release_notes_without_a_github_tag_use_a_url_key():
    row = ai_wire.map_item(
        {
            "title": "Cursor: June 10, 2026",
            "url": "https://cursor.com/changelog/june-10/",
            "sourceName": "Cursor Release Notes",
            "summary": "Cursor release notes update",
        },
        lane="ship",
    )
    assert row["kind"] == "tool_release"
    assert row["canonical_key"] == "url:https://cursor.com/changelog/june-10"
    assert "org" not in row


def test_github_release_key_is_lowercase_and_keeps_the_tag():
    row = ai_wire.map_item(
        {
            "title": "Codex",
            "url": "https://GitHub.com/OpenAI/Codex/releases/tag/V0.50.0?utm=newsletter",
            "sourceName": "Hacker News",
        },
        lane="community",
    )
    assert row["kind"] == "tool_release"
    assert row["canonical_key"] == "release:openai/codex@v0.50.0"
    assert row["org"] == "OpenAI"
    assert row["lane"] == "Community"
    assert row["url"].endswith("?utm=newsletter")


def test_explicit_org_and_blurb_win_and_summary_caps_at_600():
    row = ai_wire.map_item(
        {
            "title": "Codex v0.50.0",
            "url": RELEASE_URL,
            "sourceName": "Codex Releases",
            "summary": "old summary",
            "blurb": "adds " + ("x" * 700),
            "org": "OpenAI",
            "score": True,
            "tags": ["releases", "", 3, "  agent  "],
        },
        lane="ship",
    )
    assert row["org"] == "OpenAI"
    assert row["summary"].startswith("adds ")
    assert len(row["summary"]) == 600
    assert "score" not in row
    assert row["tags"] == ["releases", "agent"]


def test_map_skips_rows_without_a_title_or_http_url():
    assert ai_wire.map_item({"title": " ", "url": RELEASE_URL}, lane="ship") is None
    assert ai_wire.map_item({"title": "Nope", "url": "ftp://example.com/a"}, lane="ship") is None


def test_channel_post_url_rejects_anything_except_the_public_channel():
    assert ai_wire.telegram_post_url(979) == "https://t.me/clawbytes/979"
    assert ai_wire.telegram_post_url("979") == "https://t.me/clawbytes/979"
    assert ai_wire.telegram_post_url(0) is None
    assert ai_wire.telegram_post_url(None) is None
    row = ai_wire.map_item(
        _release(channelPostUrl="https://evil.example/clawbytes/1"),
        lane="ship",
        channel_post_url="https://t.me/other/4",
    )
    assert "channel_post_url" not in row


# --- flag and transport ------------------------------------------------------


def test_flag_off_is_a_noop(monkeypatch, capsys):
    _disable(monkeypatch)
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("flag off must not open a socket")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ai_wire.push_clawbytes_items([_release()], lane="ship", posted_at=NOW)
    assert calls["n"] == 0
    assert "ai_wire" not in capsys.readouterr().err
    assert ai_wire.enabled() is False


def test_flag_on_without_credentials_does_not_open_a_socket(monkeypatch, capsys):
    monkeypatch.setenv("AI_WIRE_ENABLED", "true")
    monkeypatch.delenv("AI_WIRE_URL", raising=False)
    monkeypatch.delenv("AI_WIRE_INGEST_TOKEN", raising=False)
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("missing credentials must not open a socket")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ai_wire.push_clawbytes_items([_release()], lane="Ship")
    err = capsys.readouterr().err
    assert calls["n"] == 0
    assert "ai_wire push failed: unconfigured" in err


def test_push_posts_the_batch_and_logs_ok(monkeypatch, capsys):
    _enable(monkeypatch)
    seen = {}

    def _open(req, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return _Body(payload={"upserted": 1, "created": 1, "keys": ["release:openai/codex@v0.50.0"]})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ai_wire.push_clawbytes_items(
        [_release()],
        lane="ship",
        channel_post_url="https://t.me/clawbytes/979",
        posted_at=NOW,
    )
    err = capsys.readouterr().err
    assert "ai_wire push ok n=1" in err
    assert seen["timeout"] == 5
    assert seen["url"] == "https://wire.example/api/ingest/items"
    assert seen["auth"] == "Bearer test-token"
    assert seen["body"]["items"][0]["canonical_key"] == "release:openai/codex@v0.50.0"
    assert "test-token" not in err


def test_timeout_retries_once_then_success_logs_ok(monkeypatch, capsys):
    _enable(monkeypatch)
    calls = {"n": 0}

    def _open(_req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return _Body(payload={"upserted": 2})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    items = [
        _release(),
        {
            "title": "A note",
            "url": "https://example.com/note",
            "sourceName": "Simon Willison",
            "primaryCategory": "read",
        },
    ]
    ai_wire.push_clawbytes_items(items, lane="read", posted_at=NOW)
    assert calls["n"] == 2
    assert "ai_wire push ok n=2" in capsys.readouterr().err


def test_push_failure_never_raises_and_does_not_log_the_token(monkeypatch, capsys):
    _enable(monkeypatch, token="super-secret-token")
    calls = {"n": 0}

    def _open(req, timeout=None):
        calls["n"] += 1
        raise TimeoutError("super-secret-token")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ai_wire.push_clawbytes_items([_release()], lane="ship")  # must not raise
    err = capsys.readouterr().err
    assert calls["n"] == 2
    assert "ai_wire push failed: timeout" in err
    assert "super-secret-token" not in err


def test_http_401_and_400_are_not_retried(monkeypatch, capsys):
    _enable(monkeypatch)
    calls = {"n": 0}

    def _open(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 401, "nope", hdrs=None, fp=None)

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ai_wire.push_mapped([{"canonical_key": "url:https://example.com/a", "title": "A", "url": "https://example.com/a"}])
    assert calls["n"] == 1
    assert "ai_wire push failed: http_401" in capsys.readouterr().err

    calls["n"] = 0

    def _bad(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 400, "bad", hdrs=None, fp=None)

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _bad)
    ai_wire.push_mapped([{"canonical_key": "url:https://example.com/a", "title": "A", "url": "https://example.com/a"}])
    assert calls["n"] == 1
    assert "ai_wire push failed: http_400" in capsys.readouterr().err


def test_mapping_exception_never_raises(monkeypatch, capsys):
    _enable(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_wire, "map_item", _boom)
    ai_wire.push_clawbytes_items([_release()], lane="ship")
    err = capsys.readouterr().err
    assert "ai_wire push failed: RuntimeError" in err
    assert "boom" not in err


def test_batches_above_100_are_split(monkeypatch, capsys):
    _enable(monkeypatch)
    sizes = []

    def _open(req, timeout=None):
        body = json.loads(req.data.decode())
        sizes.append(len(body["items"]))
        return _Body(payload={"upserted": len(body["items"])})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    items = [
        {"title": f"Item {i}", "url": f"https://example.com/p/{i}", "sourceName": "Example"}
        for i in range(101)
    ]
    ai_wire.push_clawbytes_items(items, lane="read")
    assert sizes == [100, 1]
    assert "ai_wire push ok n=101" in capsys.readouterr().err


# --- backfill ----------------------------------------------------------------


def _posted(url, title, *, status="posted", age_days=1, source="Simon Willison", category="read"):
    stamp = (NOW - timedelta(days=age_days)).isoformat()
    return {
        "id": url,
        "title": title,
        "url": url,
        "summary": "Worth reading",
        "sourceName": source,
        "sourceType": "rss",
        "primaryCategory": category,
        "categories": [category],
        "score": 40,
        "status": status,
        "postedCategories": [category] if status == "posted" else [],
        "discoveredAt": stamp,
        "publishedAt": "2026-09-01T00:00:00+00:00",
    }


def test_backfill_selects_posted_rows_inside_the_window():
    items = [
        _posted("https://example.com/new", "New", age_days=1),
        _posted(RELEASE_URL, "Old release", age_days=2, source="Codex Releases", category="ship"),
        _posted("https://example.com/old", "Old", age_days=8),
        _posted("https://example.com/queued", "Queued", status="queued", age_days=0),
        _posted(
            "https://example.com/edge",
            "Edge",
            age_days=7,
        ),
    ]
    # Exactly 7 days old is inside a 7-day window. One second older is not.
    edge = items[-1]
    edge["discoveredAt"] = (NOW - timedelta(days=7)).isoformat()
    outside = dict(edge)
    outside["url"] = "https://example.com/outside"
    outside["title"] = "Outside"
    outside["discoveredAt"] = (NOW - timedelta(days=7, seconds=1)).isoformat()
    items.append(outside)

    rows = ai_wire.payloads_for_backfill(items, days=7, now=NOW)
    keys = [row["canonical_key"] for row in rows]
    assert "url:https://example.com/old" not in keys
    assert "url:https://example.com/queued" not in keys
    assert "url:https://example.com/outside" not in keys
    assert "url:https://example.com/edge" in keys
    assert "release:openai/codex@v0.50.0" in keys
    release = next(row for row in rows if row["kind"] == "tool_release")
    assert release["lane"] == "Ship"
    assert release["posted_at"] == (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    news = next(row for row in rows if row["title"] == "New")
    assert news["kind"] == "news"
    assert news["lane"] == "Read"


def test_backfill_keeps_the_newest_copy_of_a_release_key():
    older = _posted(RELEASE_URL, "Older title", age_days=3, source="Codex Releases", category="ship")
    newer = _posted(RELEASE_URL, "Newer title", age_days=1, source="Codex Releases", category="ship")
    newer["channelPostUrl"] = "https://t.me/clawbytes/980"
    rows = ai_wire.payloads_for_backfill([older, newer], days=7, now=NOW)
    assert len(rows) == 1
    assert rows[0]["title"] == "Newer title"
    assert rows[0]["channel_post_url"] == "https://t.me/clawbytes/980"


def _isolate(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "state.json")
    ct.save_json(ct.THREAD_STATE_FILE, {"postedUrls": [], "postedBacklogIds": [], "publishLog": []})
    return memory


def test_backfill_command_is_dry_run_by_default(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _disable(monkeypatch)
    item = _posted("https://example.com/new", "New", age_days=1)
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("dry run must not post")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    monkeypatch.setattr(ct, "now_utc", lambda: NOW)
    monkeypatch.setattr(sys, "argv", ["clawbytes_threads.py", "ai-wire-backfill", "--days", "7"])
    assert ct.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["count"] == 1
    assert report["items"][0]["canonical_key"] == "url:https://example.com/new"
    assert calls["n"] == 0


def test_backfill_send_respects_the_flag(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _disable(monkeypatch)
    ct.save_json(ct.BACKLOG_FILE, {"items": [_posted("https://example.com/new", "New")]})
    monkeypatch.setattr(ct, "now_utc", lambda: NOW)
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("flag off must not post")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    report = ct.ai_wire_backfill(days=7, send=True, now=NOW)
    assert report["pushed"] is False
    assert report["reason"] == "AI_WIRE_ENABLED is off"
    assert calls["n"] == 0

    _enable(monkeypatch)

    def _ok(req, timeout=None):
        calls["n"] += 1
        return _Body(payload={"upserted": 1})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _ok)
    report = ct.ai_wire_backfill(days=7, send=True, now=NOW)
    assert report["pushed"] is True
    assert report["count"] == 1
    assert calls["n"] == 1
    assert "ai_wire push ok n=1" in capsys.readouterr().err


# --- publish path ------------------------------------------------------------


def test_publish_lane_flag_off_does_not_push(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _disable(monkeypatch)
    item = _release(id="rel-1", status="queued", postedCategories=[])
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    monkeypatch.setattr(ct, "polish_lane_post", lambda message: message)
    monkeypatch.setattr(ct, "format_category_bundle", lambda *_a, **_k: "<b>Ship</b>")
    monkeypatch.setattr(ct, "bundle_for_category", lambda *_a, **_k: [item])
    monkeypatch.setattr(ct, "send_telegram", lambda _message: True)
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("flag off must not push")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    assert ct._publish_lane("ship", send=True) == (True, 1)
    assert calls["n"] == 0
    assert "ai_wire" not in capsys.readouterr().err
    stored = ct.load_json(ct.BACKLOG_FILE, {})["items"][0]
    assert stored["status"] == "posted"


def test_publish_lane_push_failure_still_marks_posted(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _enable(monkeypatch, token="super-secret-token")
    item = _release(id="rel-1", status="queued", postedCategories=[])
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    monkeypatch.setattr(ct, "polish_lane_post", lambda message: message)
    monkeypatch.setattr(ct, "format_category_bundle", lambda *_a, **_k: "<b>Ship</b>")
    monkeypatch.setattr(ct, "bundle_for_category", lambda *_a, **_k: [item])
    monkeypatch.setattr(ct, "send_telegram", lambda _message: True)
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise TimeoutError("super-secret-token")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    assert ct._publish_lane("ship", send=True) == (True, 1)
    assert calls["n"] == 2
    err = capsys.readouterr().err
    assert "ai_wire push failed: timeout" in err
    assert "super-secret-token" not in err
    stored = ct.load_json(ct.BACKLOG_FILE, {})["items"][0]
    assert stored["status"] == "posted"
    assert stored["postedAt"]


def test_publish_lane_sends_the_telegram_message_url(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _enable(monkeypatch)
    item = _release(id="rel-1", status="queued", postedCategories=[], publishedAt="2026-10-09T12:00:00+00:00")
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100test")
    monkeypatch.setattr(ct, "CHANNEL_ID", "-100test")
    monkeypatch.setattr(ct, "now_utc", lambda: NOW)
    monkeypatch.setattr(ct, "polish_lane_post", lambda message: message)
    monkeypatch.setattr(ct, "format_category_bundle", lambda *_a, **_k: "<b>Ship</b>")
    monkeypatch.setattr(ct, "bundle_for_category", lambda *_a, **_k: [item])

    class _Resp:
        status_code = 200
        ok = True
        headers = {}
        text = ""

        def json(self):
            return {"ok": True, "result": {"message_id": 979}}

    pub = ct._SsPublisher(
        telegram_token="fake-token",
        telegram_channel_id="-100test",
        disable_preview=False,
        _post=lambda *_a, **_k: _Resp(),
        _sleep=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(ct, "_publisher", pub)
    seen = {}

    def _open(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return _Body(payload={"upserted": 1})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    assert ct._publish_lane("ship", send=True) == (True, 1)
    row = seen["body"]["items"][0]
    assert row["channel_post_url"] == "https://t.me/clawbytes/979"
    assert row["kind"] == "tool_release"
    assert row["canonical_key"] == "release:openai/codex@v0.50.0"
    assert row["lane"] == "Ship"
    assert row["posted_at"] == "2026-10-10T16:00:00Z"
    assert seen["timeout"] == 5
    assert "ai_wire push ok n=1" in capsys.readouterr().err
    stored = ct.load_json(ct.BACKLOG_FILE, {})["items"][0]
    assert stored["channelPostUrl"] == "https://t.me/clawbytes/979"
    assert stored["postedAt"].startswith("2026-10-10T16:00:00")
    log = ct.load_json(ct.THREAD_STATE_FILE, {})["publishLog"][-1]
    assert log["channelPostUrl"] == "https://t.me/clawbytes/979"


def test_curated_row_keeps_its_blurb_and_the_backlog_source(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _enable(monkeypatch)
    item = {
        "id": "inc-1",
        "title": "Claude API errors",
        "url": "https://status.claude.com/incidents/abc123/",
        "summary": "stored summary",
        "sourceName": "Claude Status",
        "sourceType": "rss",
        "primaryCategory": "watch",
        "status": "queued",
        "postedCategories": [],
        "score": 64,
    }
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    seen = {}

    def _open(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Body(payload={"upserted": 1})

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    slim = {
        "id": "inc-1",
        "title": "Claude API errors",
        "url": item["url"],
        "blurb": "API error rate is elevated",
    }
    ct.push_lane_to_ai_wire("watch", [slim], channel_post_url="https://t.me/clawbytes/12")
    row = seen["body"]["items"][0]
    assert row["kind"] == "status"
    assert row["lane"] == "Watch"
    assert row["summary"] == "API error rate is elevated"
    assert row["canonical_key"] == "url:https://status.claude.com/incidents/abc123"
    assert row["channel_post_url"] == "https://t.me/clawbytes/12"
    assert row["score"] == 64
    assert "ai_wire push ok n=1" in capsys.readouterr().err


def test_mocked_send_does_not_reuse_a_stale_channel_url(monkeypatch):
    monkeypatch.setattr(ct, "_send_generation", 4)
    monkeypatch.setattr(ct, "_last_channel_post_url", "https://t.me/clawbytes/1")
    captured = {}
    monkeypatch.setattr(ct, "send_telegram", lambda _message: True)

    def _mark(*_a, **kwargs):
        captured["mark"] = kwargs
        return [{"id": "1", "title": "T", "url": "https://example.com/a", "sourceName": "Example"}]

    monkeypatch.setattr(ct, "mark_posted", _mark)
    monkeypatch.setattr(ct, "push_lane_to_ai_wire", lambda *_a, **kwargs: captured.setdefault("push", kwargs))
    assert ct._send_and_record("ship", "hi") is True
    assert captured["mark"]["channel_post_url"] is None
    assert captured["push"]["channel_post_url"] is None


def test_send_failure_does_not_push(monkeypatch, capsys):
    _disable(monkeypatch)
    monkeypatch.setattr(ct, "send_telegram", lambda _message: False)
    monkeypatch.setattr(ct, "mark_posted", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("marked")))
    calls = {"n": 0}

    def _open(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("failed send must not push")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    assert ct._send_and_record("ship", "hi") is False
    assert calls["n"] == 0
