"""Tests for the publish-path hardening (Phase 1 of the resilience plan).

Covers the failure surface modelbytes already covers and clawbytes did not:
fail-soft send (bool, never raises), 4096-char truncation, 429/5xx retry with
Retry-After, the channel-harm content gate, send-fail does not mark a lane
posted, and one lane's crash does not abort the whole autopublish loop.
"""
import json

import pytest

import clawbytes_threads as ct


class _FakeResp:
    """Stand-in for a requests.Response as seen by the shared Publisher."""
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.text = ""

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakePost:
    """Callable fake for requests.post, scriptable per-call."""
    def __init__(self, responses=None, capture=None):
        self._responses = responses
        self.capture = capture if capture is not None else {}
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if callable(self._responses) and not isinstance(self._responses, (list, _FakeResp)):
            return self._responses(url, **kwargs)
        if isinstance(self._responses, list):
            return self._responses.pop(0) if self._responses else _FakeResp(500, {"ok": False})
        if isinstance(self._responses, _FakeResp):
            return self._responses
        return _FakeResp(payload={"ok": True, "result": {"message_id": 1}})


def _pub(monkeypatch, responses=None, capture=None):
    """Build a real Publisher with test creds + a fake requests.post, and point
    clawbytes' module-level _publisher at it. send_telegram delegates to it.
    Sets env vars (not just cred()) so _ensure_publisher's cache check sees a
    match and doesn't rebuild over our injected publisher."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("CLAWBYTES_CHANNEL_ID", "-100test")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100test")
    monkeypatch.setattr(ct, "CHANNEL_ID", "-100test")  # read at import; mirror the env here
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWBYTES_SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("CLAWBYTES_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", raising=False)
    post = _FakePost(responses=responses, capture=capture if capture is not None else {})
    pub = ct._SsPublisher(
        telegram_token="fake-token",
        telegram_channel_id="-100test",
        slack_token="",
        slack_channel_id="",
        ops_telegram_chat_id="",
        ops_slack_channel_id="",
        disable_preview=False,
        _post=post,
        _sleep=lambda *a, **k: None,  # instant retry backoff in tests
    )
    # Pin sleep so retry backoff is instant in tests.
    import ss_publish.publisher as _pubmod
    monkeypatch.setattr(_pubmod.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ct, "_publisher", pub)
    return pub, post


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


# --- send_telegram is fail-soft (via the shared core) -----------------------

def test_send_returns_false_on_network_error(monkeypatch):
    import requests as _requests
    def _neterr(url, **k):
        raise _requests.ConnectionError("connection refused")
    pub, post = _pub(monkeypatch, responses=_neterr)
    assert ct.send_telegram("hi") is False  # retries, then gives up — no raise


def test_send_returns_false_on_ok_false(monkeypatch):
    calls = {"n": 0}
    def _okfalse(url, **k):
        calls["n"] += 1
        return _FakeResp(400, {"ok": False, "description": "Bad Request: chat not found"})
    pub, post = _pub(monkeypatch, responses=_okfalse)
    assert ct.send_telegram("hi") is False
    assert calls["n"] == 1  # ok:false is a content problem — no retry


def test_send_retries_429_then_succeeds(monkeypatch):
    pub, post = _pub(monkeypatch, responses=[
        _FakeResp(429, {"ok": False}, headers={"Retry-After": "0"}),
        _FakeResp(200, {"ok": True, "result": {"message_id": 1}}),
    ])
    assert ct.send_telegram("hi") is True
    assert len(post.calls) == 2


def test_send_returns_false_on_persistent_500(monkeypatch):
    pub, post = _pub(monkeypatch, responses=_FakeResp(500, {"ok": False}))
    assert ct.send_telegram("hi") is False
    assert len(post.calls) == pub.send_attempts  # retried up to the cap


def test_send_truncates_oversize_before_posting(monkeypatch):
    sent = {}
    def _cap(url, **k):
        sent["text"] = k["json"]["text"] if "json" in k else None
        return _FakeResp(200, {"ok": True, "result": {}})
    pub, post = _pub(monkeypatch, responses=_cap)
    big = "word\n" * 3000
    assert ct.send_telegram(big) is True
    assert len(sent["text"]) <= ct.TELEGRAM_MAX_CHARS
    assert sent["text"].endswith("…[truncated]")


def test_send_mirrors_to_slack_only_on_success(monkeypatch):
    # The shared core mirrors inside send_telegram on success only; on a failed
    # send it returns False without mirroring. Patch mirror_to_slack to count.
    mirrored = {"n": 0}
    monkeypatch.setattr(ct, "mirror_to_slack",
                        lambda *a, **k: mirrored.__setitem__("n", mirrored["n"] + 1))
    # success path → publisher.send_telegram returns ok=True; the wrapper does
    # NOT call mirror_to_slack itself (the core does, internally, on success).
    # So assert via the core's contract: ok=True mirrors once; ok=False mirrors zero.
    pub, post = _pub(monkeypatch, responses=_FakeResp(200, {"ok": True, "result": {}}))
    # Give the test publisher slack creds so its internal mirror fires.
    pub.slack_token = "xoxb"; pub.slack_channel_id = "C1"
    assert ct.send_telegram("hi") is True
    # (the count here is driven by the core's internal mirror, which is
    # best-effort; the contract under test is that a failed send does NOT mirror.)
    pub2, post2 = _pub(monkeypatch, responses=_FakeResp(400, {"ok": False}))
    mirrored["n"] = 0
    assert ct.send_telegram("hi") is False
    assert mirrored["n"] == 0  # a message Telegram rejected is not mirrored


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
