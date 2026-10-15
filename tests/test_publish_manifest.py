"""The polished message and its item manifest travel together.

polish_lane_post can drop filler lines and the Telegram limit can cut lines
off. Only items still in the sent message are marked posted and pushed to AI
Wire; the rest stay queued. Both publish paths (curated and deterministic).
"""
import json

import ai_wire
import clawbytes_threads as ct


GOOD_URL = "https://github.com/runmaestro/maestro/releases/tag/v0.17.8"
FILLER_URL = "https://mistral.ai/news/mistral-large-4"


def _items():
    good = {
        "id": "good-1",
        "title": "Runmaestro/Maestro 0.17.8",
        "url": GOOD_URL,
        "summary": "It now listens on 127.0.0.1 only until Live is turned on.",
        "sourceName": "Maestro Releases",
        "sourceType": "rss",
        "status": "queued",
        "categories": ["ship"],
        "postedCategories": [],
    }
    filler = {
        "id": "filler-1",
        "title": "Introducing Mistral Large 4",
        "url": FILLER_URL,
        "summary": "Mistral announces Mistral Large 4.",
        "sourceName": "Mistral News",
        "sourceType": "rss",
        "status": "queued",
        "categories": ["ship"],
        "postedCategories": [],
    }
    return good, filler


def _setup(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "state.json")
    ct.save_json(ct.THREAD_STATE_FILE, {"postedUrls": [], "postedBacklogIds": [], "publishLog": []})
    good, filler = _items()
    ct.save_json(ct.BACKLOG_FILE, {"items": [good, filler]})
    monkeypatch.setenv("AI_WIRE_ENABLED", "1")
    monkeypatch.setenv("AI_WIRE_URL", "https://wire.example/")
    monkeypatch.setenv("AI_WIRE_INGEST_TOKEN", "test-token")
    sent = []
    monkeypatch.setattr(ct, "send_telegram", lambda message: sent.append(message) or True)
    pushed = []

    class _Body:
        status = 200

        def read(self, _n=-1):
            return b'{"upserted": 1}'

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _open(req, timeout=None):
        pushed.extend(json.loads(req.data.decode())["items"])
        return _Body()

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    return good, filler, sent, pushed


def _assert_one_delivered(sent, pushed):
    assert len(sent) == 1
    assert GOOD_URL in sent[0]
    assert FILLER_URL not in sent[0]
    assert sent[0].count("<a href=") == 1

    backlog = {row["id"]: row for row in ct.load_json(ct.BACKLOG_FILE, {})["items"]}
    assert backlog["good-1"]["status"] == "posted"
    assert backlog["filler-1"]["status"] == "queued"
    state = ct.load_json(ct.THREAD_STATE_FILE, {})
    assert state["postedBacklogIds"] == ["good-1"]
    assert FILLER_URL not in state["postedUrls"]
    assert state["publishLog"][-1]["count"] == 1

    assert [row["url"] for row in pushed] == [GOOD_URL]


def test_deterministic_path_records_only_the_surviving_item(monkeypatch, tmp_path, capsys):
    good, filler, sent, pushed = _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    html = (
        "📦 <b>Ship</b> — 2 items\n\n"
        f'📦 <a href="{GOOD_URL}">Runmaestro/Maestro 0.17.8</a>'
        " — It now listens on 127.0.0.1 only until Live is turned on.\n\n"
        f'📦 <a href="{FILLER_URL}">Introducing Mistral Large 4</a>'
        " — Mistral announces Mistral Large 4."
    )
    monkeypatch.setattr(ct, "format_category_bundle", lambda *_a, **_k: html)
    monkeypatch.setattr(ct, "bundle_for_category", lambda *_a, **_k: [dict(good), dict(filler)])

    assert ct._publish_lane("ship", send=True) == (True, 1)
    _assert_one_delivered(sent, pushed)
    assert "lane_item_dropped lane=ship reason=filler id=filler-1" in capsys.readouterr().err


def test_curated_path_records_only_the_surviving_item(monkeypatch, tmp_path, capsys):
    good, filler, sent, pushed = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    curated = {
        "_curator": {"approved": True},
        "items": [
            {"id": "good-1", "title": good["title"], "url": GOOD_URL, "blurb": good["summary"]},
            {"id": "filler-1", "title": filler["title"], "url": FILLER_URL, "blurb": filler["summary"]},
        ],
    }
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: curated)
    monkeypatch.setattr(ct, "format_category_bundle",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("deterministic path ran")))

    assert ct._publish_lane("ship", send=True) == (True, 1)
    _assert_one_delivered(sent, pushed)
    assert "lane_item_dropped lane=ship reason=filler id=filler-1" in capsys.readouterr().err


def test_items_cut_by_the_length_limit_stay_queued():
    blurb = "It now adds " + "x" * 1200
    items = [
        {"id": "a", "url": "https://example.com/a?x=1&y=2"},
        {"id": "b", "url": "https://example.com/b"},
        {"id": "c", "url": "https://example.com/c"},
        {"id": "d", "url": "https://example.com/d"},
    ]
    lines = [f'📦 <a href="{ct.html_escape(item["url"])}">{item["id"].upper()} 1.0</a> — {blurb}' for item in items]
    raw = "📦 <b>Ship</b> — 4 items\n\n" + "\n\n".join(lines)
    message, manifest = ct.finalize_lane_post("ship", raw, items)
    assert len(message) <= ct.TELEGRAM_MAX_CHARS
    assert [item["id"] for item in manifest] == ["a", "b", "c"]
    assert "example.com/d" not in message


def test_unlinked_bundle_members_still_ride_with_the_post():
    items = [{"id": "a", "url": "https://example.com/a"}, {"id": "b", "url": "https://example.com/b2"}]
    raw = '📦 <b>Ship</b> — 1 item\n\n📦 <a href="https://example.com/a">A 1.0</a> — It now adds a flag.'
    message, manifest = ct.finalize_lane_post("ship", raw, items)
    assert [item["id"] for item in manifest] == ["a", "b"]
    assert message == raw
