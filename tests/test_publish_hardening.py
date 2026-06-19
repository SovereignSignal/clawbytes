"""Tests for the publish-path hardening (Phase 1 of the resilience plan).

Covers the failure surface modelbytes already covers and clawbytes did not:
fail-soft send (bool, never raises), 4096-char truncation, 429/5xx retry with
Retry-After, the channel-harm content gate, send-fail does not mark a lane
posted, and one lane's crash does not abort the whole autopublish loop.
"""
import json
from urllib.parse import parse_qs

import pytest

import clawbytes_threads as ct


class _FakeResp:
    """Stand-in for the value returned by urlopen(): a readable context manager."""
    def __init__(self, body_bytes):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _no_send(monkeypatch):
    """Pin the parts of send_telegram that reach outside the process."""
    monkeypatch.setattr(ct, "cred", lambda *a, **k: "fake-token")
    monkeypatch.setattr(ct, "mirror_to_slack", lambda *a, **k: None)
    monkeypatch.setattr(ct.time, "sleep", lambda *a, **k: None)


# --- truncation --------------------------------------------------------------

def test_truncate_passthrough_short():
    assert ct._truncate_for_telegram("short") == "short"


def test_truncate_caps_at_limit_and_marks():
    big = "line\n" * 4000  # well over 4096
    out = ct._truncate_for_telegram(big)
    assert len(out) <= ct.TELEGRAM_MAX_CHARS
    assert out.endswith("…[truncated]")


# --- content gate ------------------------------------------------------------

def test_gate_rejects_empty():
    ok, errs = ct.validate_lane_for_publish("")
    assert ok is False and errs


def test_gate_rejects_whitespace_only():
    ok, errs = ct.validate_lane_for_publish("   \n   ")
    assert ok is False and errs


def test_gate_accepts_balanced_tags():
    ok, _ = ct.validate_lane_for_publish('<b>x</b> and <a href="https://e.x/t">t</a> <i>y</i> <code>c</code>')
    assert ok is True


def test_gate_accepts_tagfree_text():
    assert ct.validate_lane_for_publish("just a plain headline")[0] is True


def test_gate_rejects_unclosed_tag():
    ok, errs = ct.validate_lane_for_publish("<b>Ship — never closed")
    assert ok is False and errs


def test_gate_rejects_mismatched_close():
    ok, errs = ct.validate_lane_for_publish("<b>x</i>")
    assert ok is False and errs


# --- send_telegram is fail-soft ----------------------------------------------

def test_send_returns_false_on_network_error(monkeypatch):
    _no_send(monkeypatch)

    def _neterr(*a, **k):
        raise ct.URLError("connection refused")

    monkeypatch.setattr(ct, "urlopen", _neterr)
    assert ct.send_telegram("hi") is False  # retries 3×, then gives up — no raise


def test_send_returns_false_on_ok_false(monkeypatch):
    _no_send(monkeypatch)
    calls = {"n": 0}

    def _okfalse(req, timeout=20):
        calls["n"] += 1
        return _FakeResp(b'{"ok":false,"description":"Bad Request: chat not found"}')

    monkeypatch.setattr(ct, "urlopen", _okfalse)
    assert ct.send_telegram("hi") is False
    assert calls["n"] == 1  # ok:false is a content problem — no retry


def test_send_retries_429_then_succeeds(monkeypatch):
    _no_send(monkeypatch)
    calls = {"n": 0}

    def _flaky(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ct.HTTPError("https://x", 429, "Too Many Requests",
                               {"Retry-After": "0"}, None)
        return _FakeResp(b'{"ok":true,"result":{"message_id":1}}')

    monkeypatch.setattr(ct, "urlopen", _flaky)
    assert ct.send_telegram("hi") is True
    assert calls["n"] == 2


def test_send_returns_false_on_persistent_500(monkeypatch):
    _no_send(monkeypatch)
    calls = {"n": 0}

    def _always_500(req, timeout=20):
        calls["n"] += 1
        raise ct.HTTPError("https://x", 500, "Server Error", {}, None)

    monkeypatch.setattr(ct, "urlopen", _always_500)
    assert ct.send_telegram("hi") is False
    assert calls["n"] == ct.TELEGRAM_SEND_ATTEMPTS  # retried up to the cap


def test_send_truncates_oversize_before_posting(monkeypatch):
    _no_send(monkeypatch)
    sent = {}

    def _cap(req, timeout=20):
        sent["text"] = parse_qs(req.data.decode())["text"][0]
        return _FakeResp(b'{"ok":true,"result":{}}')

    monkeypatch.setattr(ct, "urlopen", _cap)
    big = "word\n" * 3000
    assert ct.send_telegram(big) is True
    assert len(sent["text"]) <= ct.TELEGRAM_MAX_CHARS
    assert sent["text"].endswith("…[truncated]")


def test_send_mirrors_to_slack_only_on_success(monkeypatch):
    monkeypatch.setattr(ct, "cred", lambda *a, **k: "fake-token")
    monkeypatch.setattr(ct.time, "sleep", lambda *a, **k: None)
    mirrored = {"n": 0}
    monkeypatch.setattr(ct, "mirror_to_slack",
                        lambda *a, **k: mirrored.__setitem__("n", mirrored["n"] + 1))

    monkeypatch.setattr(ct, "urlopen",
                        lambda *a, **k: _FakeResp(b'{"ok":true,"result":{}}'))
    assert ct.send_telegram("hi") is True
    assert mirrored["n"] == 1

    monkeypatch.setattr(ct, "urlopen",
                        lambda *a, **k: _FakeResp(b'{"ok":false,"description":"x"}'))
    assert ct.send_telegram("hi") is False
    assert mirrored["n"] == 1  # a message Telegram rejected is not mirrored


# --- _publish_lane fail-soft -------------------------------------------------

def _det_lane(monkeypatch, send_succeeds, message="<b>Ship</b> bundle"):
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: message)
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    monkeypatch.setattr(ct, "send_telegram", lambda m: send_succeeds)
    marked = {"n": 0}
    monkeypatch.setattr(ct, "mark_posted",
                        lambda *a, **k: marked.__setitem__("n", marked["n"] + 1))
    return marked


def test_publish_lane_marks_posted_on_success(monkeypatch):
    marked = _det_lane(monkeypatch, send_succeeds=True)
    sent, count = ct._publish_lane("ship", send=True)
    assert (sent, count) == (True, 1)
    assert marked["n"] == 1


def test_publish_lane_does_not_mark_posted_on_send_fail(monkeypatch):
    marked = _det_lane(monkeypatch, send_succeeds=False)
    sent, count = ct._publish_lane("ship", send=True)
    assert (sent, count) == (False, 1)  # not sent, but the count is real
    assert marked["n"] == 0  # must not be marked posted → retries next cycle


def test_publish_lane_blocked_by_gate_does_not_send(monkeypatch):
    marked = _det_lane(monkeypatch, send_succeeds=True, message="<b>oops")  # unclosed
    monkeypatch.setattr(ct, "send_telegram",
                        lambda m: pytest.fail("must not send when the gate rejects"))
    sent, count = ct._publish_lane("ship", send=True)
    assert sent is False and marked["n"] == 0


def test_publish_lane_curator_fail_falls_back_to_deterministic(monkeypatch):
    # Existing invariant, now under the hardened path: a curator failure must
    # not silence the lane.
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: None)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "<b>Det</b>")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    monkeypatch.setattr(ct, "send_telegram", lambda m: True)
    marked = {"n": 0}
    monkeypatch.setattr(ct, "mark_posted",
                        lambda *a, **k: marked.__setitem__("n", marked["n"] + 1))
    sent, count = ct._publish_lane("ship", send=True)
    assert (sent, count) == (True, 1) and marked["n"] == 1


# --- autopublish lane isolation ---------------------------------------------

def test_autopublish_continues_after_one_lane_crashes(monkeypatch):
    monkeypatch.setattr(ct, "collect_into_backlog", lambda *a, **k: None)
    monkeypatch.setattr(ct, "load_json", lambda *a, **k: {})
    monkeypatch.setattr(ct, "lane_ready", lambda c, s=None, dt_local=None: {"ready": True})

    def _boom(category, send):
        if category == "ship":
            raise RuntimeError("boom")
        return (True, 1)

    monkeypatch.setattr(ct, "_publish_lane", _boom)
    results = ct.autopublish(send=False)
    assert [r["category"] for r in results] == ["ship", "watch", "read", "community"]
    assert next(r for r in results if r["category"] == "ship")["sent"] is False
    assert all(r["sent"] is True for r in results if r["category"] != "ship")


def test_autopublish_continues_after_one_lane_send_fails(monkeypatch):
    # A send failure (not a crash) must likewise not abort later lanes.
    monkeypatch.setattr(ct, "collect_into_backlog", lambda *a, **k: None)
    monkeypatch.setattr(ct, "load_json", lambda *a, **k: {})
    monkeypatch.setattr(ct, "lane_ready", lambda c, s=None, dt_local=None: {"ready": True})

    def _flaky(category, send):
        return (False, 1) if category == "ship" else (True, 1)

    monkeypatch.setattr(ct, "_publish_lane", _flaky)
    results = ct.autopublish(send=True)
    assert len(results) == 4
    assert next(r for r in results if r["category"] == "ship")["sent"] is False
    assert sum(1 for r in results if r["sent"]) == 3
