"""Release-event forwarding: gating, watermark, queue, policy, import path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import release_events
import release_forwarding

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 3, 18, 5, tzinfo=timezone.utc)
WATERMARK = datetime(2026, 10, 3, 17, 0, tzinfo=timezone.utc)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "path": self.path,
            "body": raw,
            "authorization": self.headers.get("Authorization"),
        })
        script = self.server.script
        if script:
            code, payload, delay = script.pop(0)
        else:
            code, payload, delay = 500, b'{"status":"unexpected"}', 0
        if delay:
            time.sleep(delay)
        body = payload if isinstance(payload, bytes) else str(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            return

    def log_message(self, fmt, *args):
        return


class LocalReceiver:
    """127.0.0.1 only. Tests must not call a real Release Bot host."""

    def __init__(self, script):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.script = list(script)
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        port = self.httpd.server_address[1]
        return f"http://127.0.0.1:{port}/v1/releases"

    @property
    def requests(self):
        return self.httpd.requests

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _enable(monkeypatch, url: str):
    monkeypatch.setenv("RELEASE_EVENTS_URL", url)
    monkeypatch.setenv("RELEASE_EVENTS_TOKEN", "test-token")
    monkeypatch.setenv("CLAWBYTES_RELEASE_FORWARDING", "1")


def _clear_gates(monkeypatch):
    monkeypatch.delenv("RELEASE_EVENTS_URL", raising=False)
    monkeypatch.delenv("RELEASE_EVENTS_TOKEN", raising=False)
    monkeypatch.delenv("CLAWBYTES_RELEASE_FORWARDING", raising=False)
    monkeypatch.delenv("CLAWBYTES_PUBLISH", raising=False)


def _write_watermark(memory: Path, when: datetime) -> None:
    (memory / release_forwarding.WATERMARK_NAME).write_text(
        json.dumps({"watermark": release_forwarding.format_utc(when)})
    )


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _install_batch(memory: Path, payload: dict) -> None:
    (memory / release_forwarding.BATCH_NAME).write_text(json.dumps(payload))


def _outbox(memory: Path) -> list:
    path = memory / release_forwarding.OUTBOX_NAME
    return json.loads(path.read_text())["events"]


def _bodies(receiver: LocalReceiver) -> list:
    return [json.loads(item["body"].decode()) for item in receiver.requests]


# --- response classification -------------------------------------------------


def test_classify_receiver_statuses():
    assert release_events.classify_release_response(202, b"{}") == "delivered"
    assert release_events.classify_release_response(200, b'{"status":"ok"}') == "delivered"
    assert release_events.classify_release_response(200, b'{"status":"duplicate"}') == "duplicate"
    assert release_events.classify_release_response(200, b'{"status":"owned_by_poller"}') == "duplicate"
    assert release_events.classify_release_response(200, b'{"duplicate": true}') == "duplicate"
    assert release_events.classify_release_response(400, b'{"status":"bad"}') == "rejected"
    assert release_events.classify_release_response(401, b"{}") == "config_error"
    assert release_events.classify_release_response(503, b"{}") == "retryable"
    assert release_events.classify_release_response(429, b"{}") == "retryable"


def test_emit_boolean_matches_delivered_and_duplicate(monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0), (200, b'{"status":"duplicate"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        event = {
            "id": "software:github:qwibitai/nanoclaw:v1.2.3",
            "kind": "software",
            "name": "Nanoclaw",
            "version": "1.2.3",
            "source": "clawbytes",
            "url": "https://github.com/qwibitai/nanoclaw/releases/tag/v1.2.3",
        }
        assert release_events.emit_release_event(event, timeout=2) is True
        assert release_events.emit_release_event(event, timeout=2) is True
        assert server.requests[0]["authorization"] == "Bearer test-token"
        assert "127.0.0.1" in server.url
    finally:
        server.close()


def test_send_timeout_is_retryable(monkeypatch):
    server = LocalReceiver([(503, b"{}", 1.0)])
    try:
        _enable(monkeypatch, server.url)
        status, detail = release_events.send_release_event(
            {
                "id": "software:github:qwibitai/nanoclaw:v9.9.9",
                "kind": "software",
                "name": "Nanoclaw",
                "version": "9.9.9",
                "source": "clawbytes",
                "url": "https://github.com/qwibitai/nanoclaw/releases/tag/v9.9.9",
            },
            timeout=0.15,
        )
        assert status == "retryable"
        assert detail
        assert "test-token" not in detail
    finally:
        server.close()


def test_send_without_url_does_not_open_a_socket(monkeypatch):
    _clear_gates(monkeypatch)
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("urlopen")

    monkeypatch.setattr(release_events.urllib.request, "urlopen", _boom)
    status, detail = release_events.send_release_event({"id": "x", "kind": "software", "name": "n", "version": "1", "source": "s", "url": "https://example.test"})
    assert (status, detail) == ("skipped", "unconfigured")
    assert called["n"] == 0


# --- policy ------------------------------------------------------------------


def test_titles_containing_rc_still_qualify():
    batch = _load_fixture("release-batch-qualify.json")
    targets = release_forwarding.load_targets()
    wanted = {
        "Source tracking": "qwibitai/nanoclaw",
        "Architecture overhaul": "nearai/ironclaw",
        "March release": "RightNow-AI/openfang",
    }
    for title, repo in wanted.items():
        item = next(row for row in batch["newReleases"] if row["name"] == title and row["repo"] == repo)
        assert release_forwarding.is_prerelease_tag(title) is False
        assert release_forwarding.is_qualified(item, targets) is True
        event = release_forwarding.build_event(item, targets)
        assert event["name"] != title
        assert event["name"] in {"Nanoclaw", "IronClaw", "OpenFang"}
        assert " " not in event["version"]
        assert event["version"] == release_forwarding.normalize_tag(item["tag"])


def test_prerelease_draft_baseline_legacy_and_unknown_do_not_qualify():
    batch = _load_fixture("release-batch-qualify.json")
    targets = release_forwarding.load_targets()
    rejected_tags = {
        "v1.2.0-rc.1",
        "v2.0.0-beta",
        "nightly-2026-10-03",
        "inputs-abc",
        "v9.9.9",
        "v8.8.8",
        "rust-v0.161.0",
        "v4.0.0",
        "v2026.10.1",
    }
    for item in batch["newReleases"]:
        if item["tag"] in rejected_tags or item["repo"] == "acme/not-reviewed":
            assert release_forwarding.is_qualified(item, targets) is False


def test_prerelease_opt_in_allows_rc_and_beta_only():
    batch = _load_fixture("release-batch-qualify.json")
    targets = release_forwarding.load_targets()
    targets["qwibitai/nanoclaw"] = {**targets["qwibitai/nanoclaw"], "prereleases": True}
    targets["nearai/ironclaw"] = {**targets["nearai/ironclaw"], "prereleases": True}
    by_tag = {(item["repo"], item["tag"]): item for item in batch["newReleases"]}
    assert release_forwarding.is_qualified(by_tag[("qwibitai/nanoclaw", "v1.2.0-rc.1")], targets)
    assert release_forwarding.is_qualified(by_tag[("nearai/ironclaw", "v2.0.0-beta")], targets)
    assert release_forwarding.is_qualified(by_tag[("sipeed/picoclaw", "nightly-2026-10-03")], targets) is False
    assert release_forwarding.is_qualified(by_tag[("moltis-org/moltis", "inputs-abc")], targets) is False
    assert release_forwarding.is_qualified(by_tag[("qwibitai/nanoclaw", "v9.9.9")], targets) is False


def test_legacy_owned_stays_off_even_if_the_map_says_forward():
    item = _load_fixture("release-batch-legacy-codex.json")["newReleases"][0]
    targets = release_forwarding.load_targets()
    targets["openai/codex"] = {**targets["openai/codex"], "forward": True, "legacy_owned": False}
    assert release_forwarding.is_qualified(item, targets) is False
    event = release_forwarding.build_event(item, targets)
    assert event["id"] == "software:github:openai/codex:rust-v0.161.0"
    assert event["version"] == "0.161.0"
    assert event["name"] == "Codex"


def test_production_map_partitions_legacy_and_defaults_closed():
    raw = json.loads((ROOT / "release_targets.json").read_text())
    assert raw["policy"] == "forward-only-listed-targets"
    targets = raw["targets"]
    for repo in release_forwarding.LEGACY_OWNED:
        row = next(value for key, value in targets.items() if key.lower() == repo)
        assert row["forward"] is False
        assert row["legacy_owned"] is True
        assert row["name"]
        assert row["tag_pattern"]
    assert targets["openclaw/openclaw"]["prereleases"] is True
    forwarded = [key for key, row in targets.items() if row["forward"]]
    assert forwarded
    assert "acme/not-reviewed" not in {key.lower() for key in targets}
    assert release_forwarding.is_qualified(
        {
            "repo": "acme/not-reviewed",
            "tag": "v1.2.3",
            "name": "Source tracking",
            "url": "https://github.com/acme/not-reviewed/releases/tag/v1.2.3",
        },
        release_forwarding.load_targets(),
    ) is False


def test_missing_display_name_uses_repo_not_release_title():
    item = {
        "repo": "acme/widget",
        "tag": "v1.2.3",
        "name": "Source tracking",
        "url": "https://github.com/acme/widget/releases/tag/v1.2.3",
        "published": "2026-10-03T18:00:00Z",
    }
    targets = {"acme/widget": {"forward": True, "prereleases": False}}
    event = release_forwarding.build_event(item, targets)
    assert event["name"] == "acme/widget"
    assert event["id"] == "software:github:acme/widget:v1.2.3"
    assert event["version"] == "1.2.3"


# --- gating, watermark, queue ------------------------------------------------


def test_gating_skips_http_and_writes_nothing(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b"{}", 0)])
    batch = _load_fixture("release-batch-qualify.json")
    try:
        _install_batch(tmp_path, batch)
        cases = [
            {"RELEASE_EVENTS_TOKEN": "test-token", "CLAWBYTES_RELEASE_FORWARDING": "1"},
            {"RELEASE_EVENTS_URL": server.url, "CLAWBYTES_RELEASE_FORWARDING": "1"},
            {"RELEASE_EVENTS_URL": server.url, "RELEASE_EVENTS_TOKEN": "test-token"},
            {"RELEASE_EVENTS_URL": server.url, "RELEASE_EVENTS_TOKEN": "test-token", "CLAWBYTES_RELEASE_FORWARDING": "0"},
        ]
        for case in cases:
            _clear_gates(monkeypatch)
            for key, value in case.items():
                monkeypatch.setenv(key, value)
            outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
            assert outcome == "disabled"
        assert server.requests == []
        assert list(tmp_path.iterdir()) == [tmp_path / release_forwarding.BATCH_NAME]
    finally:
        server.close()


def test_first_enabled_run_writes_watermark_and_forwards_nothing(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, _load_fixture("release-batch-qualify.json"))
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
        assert outcome == "baseline"
        assert server.requests == []
        stored = json.loads((tmp_path / release_forwarding.WATERMARK_NAME).read_text())
        assert stored["watermark"] == "2026-10-03T18:05:00Z"
        assert not (tmp_path / release_forwarding.OUTBOX_NAME).exists()
    finally:
        server.close()


def test_second_run_forwards_only_releases_newer_than_the_watermark(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0)] * 6)
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, _load_fixture("release-batch-qualify.json"))
        _write_watermark(tmp_path, WATERMARK)
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
        assert outcome == "ok"
        bodies = _bodies(server)
        assert sorted(item["id"] for item in bodies) == [
            "software:github:nearai/ironclaw:v2.0.0",
            "software:github:qwibitai/nanoclaw:v1.2.3",
            "software:github:rightnow-ai/openfang:v3.0.0",
        ]
        names = {item["name"] for item in bodies}
        assert names == {"Nanoclaw", "IronClaw", "OpenFang"}
        assert "Source tracking" not in names
        assert "Architecture overhaul" not in names
        assert "March release" not in names
        versions = {item["version"] for item in bodies}
        assert versions == {"1.2.3", "2.0.0", "3.0.0"}
        for item in bodies:
            assert item["kind"] == "software"
            assert item["schema"] == "release-event/v1"
        # Same batch again does not resend.
        again = release_forwarding.forward_release_events(
            now=NOW + timedelta(minutes=5), memory=tmp_path
        )
        assert again == "ok"
        assert len(server.requests) == 3
        watermark = json.loads((tmp_path / release_forwarding.WATERMARK_NAME).read_text())
        assert watermark["watermark"] == "2026-10-03T17:00:00Z"
    finally:
        server.close()


def test_stale_batch_forwards_nothing(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        payload = _load_fixture("release-batch-stale.json")
        _install_batch(tmp_path, payload)
        stamped = release_forwarding.parse_utc(payload["timestamp"])
        _write_watermark(tmp_path, stamped - timedelta(days=1))
        outcome = release_forwarding.forward_release_events(
            now=stamped + timedelta(hours=3), memory=tmp_path
        )
        assert outcome == "stale"
        assert server.requests == []
        assert not (tmp_path / release_forwarding.OUTBOX_NAME).exists()
    finally:
        server.close()


def test_legacy_codex_fixture_is_not_forwarded(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, _load_fixture("release-batch-legacy-codex.json"))
        _write_watermark(tmp_path, WATERMARK)
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
        assert outcome == "ok"
        assert server.requests == []
        assert not (tmp_path / release_forwarding.OUTBOX_NAME).exists()
    finally:
        server.close()


def test_503_stays_queued_and_202_marks_delivered(tmp_path, monkeypatch):
    server = LocalReceiver([
        (503, b'{"status":"unavailable"}', 0),
        (202, b'{"status":"accepted"}', 0),
    ])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, _load_fixture("release-batch-legacy-codex.json"))
        # One forwardable row, not the legacy fixture.
        _install_batch(tmp_path, {
            "timestamp": "2026-10-03T18:00:00Z",
            "newReleases": [_load_fixture("release-batch-qualify.json")["newReleases"][0]],
        })
        _write_watermark(tmp_path, WATERMARK)
        first = release_forwarding.forward_release_events(now=NOW, memory=tmp_path, timeout=2)
        assert first == "retryable"
        rows = _outbox(tmp_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "retryable"
        assert rows[0]["attempts"] == 1
        assert rows[0]["next_attempt_at"]
        held = release_forwarding.forward_release_events(
            now=NOW + timedelta(seconds=10), memory=tmp_path, timeout=2
        )
        assert held == "ok"
        assert len(server.requests) == 1
        second = release_forwarding.forward_release_events(
            now=NOW + timedelta(seconds=40), memory=tmp_path, timeout=2
        )
        assert second == "ok"
        assert len(server.requests) == 2
        assert _outbox(tmp_path)[0]["status"] == "delivered"
        third = release_forwarding.forward_release_events(
            now=NOW + timedelta(minutes=20), memory=tmp_path, timeout=2
        )
        assert third == "ok"
        assert len(server.requests) == 2
    finally:
        server.close()


def test_timeout_stays_retryable(tmp_path, monkeypatch):
    server = LocalReceiver([(200, b'{"status":"ok"}', 1.0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, {
            "timestamp": "2026-10-03T18:00:00Z",
            "newReleases": [_load_fixture("release-batch-qualify.json")["newReleases"][0]],
        })
        _write_watermark(tmp_path, WATERMARK)
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path, timeout=0.15)
        assert outcome == "retryable"
        assert _outbox(tmp_path)[0]["status"] == "retryable"
        assert _outbox(tmp_path)[0]["attempts"] == 1
    finally:
        server.close()


def test_400_is_rejected_and_not_retried(tmp_path, monkeypatch):
    server = LocalReceiver([(400, b'{"status":"bad"}', 0), (202, b"{}", 0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, {
            "timestamp": "2026-10-03T18:00:00Z",
            "newReleases": [_load_fixture("release-batch-qualify.json")["newReleases"][0]],
        })
        _write_watermark(tmp_path, WATERMARK)
        assert release_forwarding.forward_release_events(now=NOW, memory=tmp_path) == "ok"
        assert _outbox(tmp_path)[0]["status"] == "rejected"
        assert release_forwarding.forward_release_events(
            now=NOW + timedelta(hours=1), memory=tmp_path
        ) == "stale"
        assert len(server.requests) == 1
    finally:
        server.close()


def test_200_duplicate_is_done(tmp_path, monkeypatch):
    server = LocalReceiver([(200, b'{"status":"duplicate"}', 0), (202, b"{}", 0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, {
            "timestamp": "2026-10-03T18:00:00Z",
            "newReleases": [_load_fixture("release-batch-qualify.json")["newReleases"][1]],
        })
        _write_watermark(tmp_path, WATERMARK)
        assert release_forwarding.forward_release_events(now=NOW, memory=tmp_path) == "ok"
        assert _outbox(tmp_path)[0]["status"] == "duplicate"
        release_forwarding.forward_release_events(now=NOW + timedelta(minutes=10), memory=tmp_path)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_401_is_config_error_and_does_not_raise(tmp_path, monkeypatch):
    server = LocalReceiver([(401, b'{"status":"unauthorized"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        _install_batch(tmp_path, {
            "timestamp": "2026-10-03T18:00:00Z",
            "newReleases": [_load_fixture("release-batch-qualify.json")["newReleases"][0]],
        })
        _write_watermark(tmp_path, WATERMARK)
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
        assert outcome == "config_error"
        row = _outbox(tmp_path)[0]
        assert row["status"] == "retryable"
        assert row["last_error"] == "config_error"
    finally:
        server.close()


def test_corrupt_outbox_is_not_overwritten(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b"{}", 0)])
    try:
        _enable(monkeypatch, server.url)
        _write_watermark(tmp_path, WATERMARK)
        _install_batch(tmp_path, _load_fixture("release-batch-qualify.json"))
        path = tmp_path / release_forwarding.OUTBOX_NAME
        path.write_text("{")
        outcome = release_forwarding.forward_release_events(now=NOW, memory=tmp_path)
        assert outcome == "error"
        assert path.read_text() == "{"
        assert server.requests == []
    finally:
        server.close()


def test_corrupt_watermark_is_not_reset(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b"{}", 0)])
    try:
        _enable(monkeypatch, server.url)
        path = tmp_path / release_forwarding.WATERMARK_NAME
        path.write_text("{")
        _install_batch(tmp_path, _load_fixture("release-batch-qualify.json"))
        assert release_forwarding.forward_release_events(now=NOW, memory=tmp_path) == "error"
        assert path.read_text() == "{"
        assert server.requests == []
    finally:
        server.close()


def test_outbox_drops_oldest_terminal_rows(tmp_path, monkeypatch):
    server = LocalReceiver([(202, b'{"status":"accepted"}', 0)])
    try:
        _enable(monkeypatch, server.url)
        _write_watermark(tmp_path, WATERMARK)
        item = _load_fixture("release-batch-qualify.json")["newReleases"][0]
        _install_batch(tmp_path, {"timestamp": "2026-10-03T18:00:00Z", "newReleases": [item]})
        seeded = []
        for index in range(5):
            seeded.append({
                "id": f"software:github:qwibitai/nanoclaw:v0.0.{index}",
                "event": {"id": f"software:github:qwibitai/nanoclaw:v0.0.{index}"},
                "status": "delivered",
                "attempts": 1,
                "next_attempt_at": None,
                "last_error": None,
                "updated_at": f"2026-10-01T00:00:0{index}Z",
            })
        release_forwarding.save_outbox(tmp_path / release_forwarding.OUTBOX_NAME, seeded, limit=50)
        release_forwarding.forward_release_events(now=NOW, memory=tmp_path, max_outbox=3)
        rows = _outbox(tmp_path)
        assert len(rows) == 3
        assert any(row["id"] == "software:github:qwibitai/nanoclaw:v1.2.3" for row in rows)
        names = list(tmp_path.iterdir())
        assert not any(path.name.endswith(".tmp") for path in names)
    finally:
        server.close()


def test_forwarder_exception_returns_error_and_does_not_raise(tmp_path, monkeypatch):
    _enable(monkeypatch, "http://127.0.0.1:9/v1/releases")
    _write_watermark(tmp_path, WATERMARK)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(release_forwarding, "load_outbox", _boom)
    assert release_forwarding.forward_release_events(now=NOW, memory=tmp_path) == "error"


def test_cli_without_pythonpath_is_a_disabled_noop(tmp_path, monkeypatch):
    env = os.environ.copy()
    for key in (
        "PYTHONPATH",
        "RELEASE_EVENTS_URL",
        "RELEASE_EVENTS_TOKEN",
        "CLAWBYTES_RELEASE_FORWARDING",
        "CLAWBYTES_PUBLISH",
    ):
        env.pop(key, None)
    env["CLAWBYTES_MEMORY_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "scripts/forward-release-events.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "forward_release_events: disabled"
    assert "ModuleNotFoundError" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_cli_receiver_failure_still_exits_zero(tmp_path):
    server = LocalReceiver([(503, b"{}", 0)])
    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("CLAWBYTES_PUBLISH", None)
        env["CLAWBYTES_MEMORY_DIR"] = str(tmp_path)
        env["RELEASE_EVENTS_URL"] = server.url
        env["RELEASE_EVENTS_TOKEN"] = "test-token"
        env["CLAWBYTES_RELEASE_FORWARDING"] = "1"
        current = datetime.now(timezone.utc).replace(microsecond=0)
        _write_watermark(tmp_path, current - timedelta(hours=1))
        item = dict(_load_fixture("release-batch-qualify.json")["newReleases"][0])
        item["published"] = release_forwarding.format_utc(current)
        _install_batch(tmp_path, {
            "timestamp": release_forwarding.format_utc(current),
            "newReleases": [item],
        })
        result = subprocess.run(
            [sys.executable, "scripts/forward-release-events.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "forward_release_events: retryable"
        assert "127.0.0.1" in server.url
    finally:
        server.close()
