"""AI Wire outbox: a failed push survives a restart and the next run delivers it.

The publish path writes mapped rows to ``ai-wire-outbox.json`` on the memory
volume before the HTTP call. An ordinary autopublish run retries what is still
pending without reposting to Telegram. All network is mocked.
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import ai_wire
import clawbytes_threads as ct

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
RELEASE_URL = "https://github.com/openai/codex/releases/tag/v0.50.0"
RELEASE_KEY = "release:openai/codex@v0.50.0"


def _release():
    return {
        "id": "rel-1",
        "title": "Codex v0.50.0",
        "url": RELEASE_URL,
        "summary": "New Codex release",
        "sourceName": "Codex Releases",
        "sourceType": "rss",
        "primaryCategory": "ship",
        "categories": ["ship"],
        "score": 70,
        "tags": ["releases", "coding-agent"],
        "publishedAt": "2026-10-14T12:00:00+00:00",
        "status": "queued",
        "postedCategories": [],
    }


class _Body:
    status = 200

    def __init__(self, n=1):
        self._n = n

    def read(self, _n=-1):
        return json.dumps({"created": self._n, "upserted": 0}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _isolate(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "clawbytes-backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "clawbytes-thread-state.json")
    ct.save_json(ct.THREAD_STATE_FILE, {"postedUrls": [], "postedBacklogIds": [], "publishLog": []})
    monkeypatch.setenv("AI_WIRE_ENABLED", "1")
    monkeypatch.setenv("AI_WIRE_URL", "https://wire.example/")
    monkeypatch.setenv("AI_WIRE_INGEST_TOKEN", "test-token")
    return memory


# A fresh interpreter: new module state, everything reloaded from the memory
# volume. Telegram must not be called; AI Wire is mocked and recovered.
_RECOVERY_DRIVER = r"""
import json, sys, urllib.request
root = sys.argv[1]
sys.path.insert(0, root)
sys.path.insert(0, root + "/scripts")
import clawbytes_threads as ct

pushed = []

class Body:
    status = 200
    def read(self, _n=-1):
        return json.dumps({"created": 1, "upserted": 0}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return False

def _open(req, timeout=None):
    pushed.extend(json.loads(req.data.decode())["items"])
    return Body()

def _no_telegram(message):
    raise AssertionError("recovery run must not post to Telegram")

urllib.request.urlopen = _open
ct.collect_into_backlog = lambda: {}
ct.send_telegram = _no_telegram
ct.lane_ready = lambda category, state: {"ready": False, "reason": "test"}
ct.autopublish(send=True)
print("PUSHED=" + json.dumps(pushed))
"""


def test_failed_push_survives_restart_and_next_run_fills_the_registry(monkeypatch, tmp_path, capsys):
    memory = _isolate(monkeypatch, tmp_path)
    item = _release()
    ct.save_json(ct.BACKLOG_FILE, {"items": [item]})
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    html = f'📦 <b>Ship</b> — 1 item\n\n📦 <a href="{RELEASE_URL}">Codex v0.50.0</a> — It now adds a sandbox flag.'
    monkeypatch.setattr(ct, "format_category_bundle", lambda *_a, **_k: html)
    monkeypatch.setattr(ct, "bundle_for_category", lambda *_a, **_k: [dict(item)])
    sent = []
    monkeypatch.setattr(ct, "send_telegram", lambda message: sent.append(message) or True)
    calls = {"n": 0}

    def _down(*_a, **_k):
        calls["n"] += 1
        raise TimeoutError("down")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _down)

    # Publish while AI Wire is down: Telegram succeeds, the push fails twice.
    assert ct._publish_lane("ship", send=True) == (True, 1)
    assert len(sent) == 1
    assert calls["n"] == 2
    err = capsys.readouterr().err
    assert "ai_wire push failed: timeout" in err
    assert "ai_wire outbox pending=1" in err

    # A retry inside the same outage keeps the row pending.
    ct.retry_ai_wire_outbox()
    outbox_path = memory / ai_wire.OUTBOX_FILE_NAME
    box = json.loads(outbox_path.read_text())
    assert list(box["pending"]) == [RELEASE_KEY]
    assert box["pending"][RELEASE_KEY]["attempts"] == 2
    assert box["pending"][RELEASE_KEY]["lastError"] == "timeout"
    backlog_before = ct.load_json(ct.BACKLOG_FILE, {})
    assert backlog_before["items"][0]["status"] == "posted"

    # Restart: a new process with the endpoint recovered runs an ordinary
    # autopublish. Nothing is reposted; the registry gets the missing row.
    env = dict(os.environ)
    env["CLAWBYTES_MEMORY_DIR"] = str(memory)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("CLAWBYTES_PUBLISH", None)
    proc = subprocess.run(
        [sys.executable, "-c", _RECOVERY_DRIVER, str(ROOT)],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    pushed_line = [line for line in proc.stdout.splitlines() if line.startswith("PUSHED=")][-1]
    pushed = json.loads(pushed_line[len("PUSHED="):])
    assert [row["canonical_key"] for row in pushed] == [RELEASE_KEY]
    assert pushed[0]["lane"] == "Ship"
    assert "ai_wire push ok n=1" in proc.stderr

    box = json.loads(outbox_path.read_text())
    assert box["pending"] == {}
    assert [entry["key"] for entry in box["acked"]] == [RELEASE_KEY]
    assert len(sent) == 1
    state = ct.load_json(ct.THREAD_STATE_FILE, {})
    assert len(state["publishLog"]) == 1
    assert ct.load_json(ct.BACKLOG_FILE, {}) == backlog_before


def _queue_one(memory, now=NOW):
    path = memory / ai_wire.OUTBOX_FILE_NAME
    rows = ai_wire.map_clawbytes_items([_release()], lane="Ship", posted_at=now)
    keys = ai_wire.enqueue_outbox(path, rows, now=now)
    assert keys == [RELEASE_KEY]
    return path


def _http_error(code):
    def _open(req, timeout=None):
        raise HTTPError(req.full_url, code, "err", {}, None)
    return _open


def test_payload_rejection_is_a_permanent_failure(monkeypatch, tmp_path, capsys):
    memory = _isolate(monkeypatch, tmp_path)
    path = _queue_one(memory)
    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _http_error(422))
    result = ai_wire.deliver_outbox(path, now=NOW)
    assert result == {"acked": 0, "failed": 1, "pending": 0}
    box = ai_wire.load_outbox(path)
    assert box["pending"] == {}
    assert box["failed"][0]["key"] == RELEASE_KEY
    assert box["failed"][0]["error"] == "http_422"
    assert "ai_wire outbox dropped=1 error=http_422" in capsys.readouterr().err


def test_auth_error_stays_pending_and_attempts_are_capped(monkeypatch, tmp_path):
    memory = _isolate(monkeypatch, tmp_path)
    path = _queue_one(memory)
    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _http_error(401))
    assert ai_wire.deliver_outbox(path, now=NOW)["pending"] == 1
    box = ai_wire.load_outbox(path)
    box["pending"][RELEASE_KEY]["attempts"] = ai_wire.OUTBOX_MAX_ATTEMPTS - 1
    ai_wire.save_outbox(path, box)
    result = ai_wire.deliver_outbox(path, now=NOW)
    assert result == {"acked": 0, "failed": 1, "pending": 0}


def test_flag_off_leaves_the_outbox_alone(monkeypatch, tmp_path):
    memory = _isolate(monkeypatch, tmp_path)
    path = _queue_one(memory)
    monkeypatch.delenv("AI_WIRE_ENABLED", raising=False)

    def _open(*_a, **_k):
        raise AssertionError("flag off must not open a socket")

    monkeypatch.setattr(ai_wire.urllib.request, "urlopen", _open)
    ct.retry_ai_wire_outbox()
    assert list(ai_wire.load_outbox(path)["pending"]) == [RELEASE_KEY]


def test_oldest_pending_shows_in_status_and_health_log(monkeypatch, tmp_path, capsys, caplog):
    memory = _isolate(monkeypatch, tmp_path)
    _queue_one(memory, now=NOW - timedelta(hours=5))
    summary = ai_wire.outbox_summary(memory / ai_wire.OUTBOX_FILE_NAME, now=NOW)
    assert summary == {"pending": 1, "oldest": "2026-10-14T11:00:00Z", "oldestAgeHours": 5.0, "failed": 0}

    ct.save_json(ct.BACKLOG_FILE, {"items": []})
    ct.print_status()
    assert "AI Wire outbox: 1 pending | oldest 2026-10-14T11:00:00Z" in capsys.readouterr().out

    import scheduler
    with caplog.at_level(logging.INFO):
        scheduler._log_ai_wire_outbox(memory)
    assert "ai_wire outbox pending=1 oldest=2026-10-14T11:00:00Z" in caplog.text
