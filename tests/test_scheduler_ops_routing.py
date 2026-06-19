"""Tests for scheduler ops-alert routing (Phase 2, Task 2.1).

The admin DM was Telegram-only: if Telegram was the thing that broke, the
alert had nowhere to go. modelbytes routes Telegram-first then Slack in
isolated try-blocks. These tests pin that parity: a failed Telegram DM must
fall through to a Slack ops channel when configured, and Slack failure must
never raise.
"""
import json

import pytest

# scheduler.py lives in scripts/; conftest puts the repo root on sys.path.
import scheduler


class _Resp:
    def __init__(self, body_bytes):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_net(monkeypatch, tg_ok=True, slack_calls=None, tg_calls=None):
    """Replace urlopen so tests never touch the network. Returns call counters."""
    tg_calls = tg_calls if tg_calls is not None else {"n": 0}
    slack_calls = slack_calls if slack_calls is not None else {"n": 0}

    def _fake_urlopen(req, timeout=30):
        url = getattr(req, "full_url", str(req))
        body = (req.data or b"").decode() if getattr(req, "data", None) else ""
        if "api.telegram.org" in url:
            tg_calls["n"] += 1
            if tg_ok:
                return _Resp(b'{"ok":true,"result":{}}')
            # Simulate a Telegram outage: network error
            raise OSError("telegram unreachable")
        if "slack.com" in url:
            slack_calls["n"] += 1
            return _Resp(b'{"ok":true}')
        return _Resp(b'{"ok":true}')

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", _fake_urlopen)
    return tg_calls, slack_calls


def test_dm_skipped_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWBYTES_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    tg, slack = _patch_net(monkeypatch)
    scheduler._send_admin_dm("t", "hi")  # must not raise, must not send
    assert tg["n"] == 0 and slack["n"] == 0


def test_dm_sends_telegram_when_healthy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    monkeypatch.delenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", raising=False)
    tg, slack = _patch_net(monkeypatch, tg_ok=True)
    scheduler._send_admin_dm("t", "hi")
    assert tg["n"] == 1
    assert slack["n"] == 0  # Telegram worked; Slack not needed (and not configured)


def test_dm_falls_back_to_slack_on_telegram_outage(monkeypatch):
    # THE BUG: if Telegram is down, the alert had nowhere to go.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    monkeypatch.setenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    tg, slack = _patch_net(monkeypatch, tg_ok=False)
    scheduler._send_admin_dm("t", "hi")
    assert tg["n"] >= 1      # tried Telegram
    assert slack["n"] == 1   # fell through to Slack ops channel


def test_dm_does_not_use_slack_when_slack_unconfigured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    monkeypatch.delenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    tg, slack = _patch_net(monkeypatch, tg_ok=False)
    scheduler._send_admin_dm("t", "hi")
    assert tg["n"] >= 1
    assert slack["n"] == 0  # nothing to fall back to — but must not raise


def test_dm_never_raises_even_if_both_paths_fail(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    monkeypatch.setenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    def _all_fail(req, timeout=30):
        raise OSError("everything is broken")

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", _all_fail)
    scheduler._send_admin_dm("t", "hi")  # must not raise


def test_dm_carries_ops_banner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    sent = {}

    def _capture(req, timeout=30):
        sent["body"] = (req.data or b"").decode()
        return _Resp(b'{"ok":true,"result":{}}')

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", _capture)
    scheduler._send_admin_dm("t", "something broke")
    import urllib.parse as _up
    decoded = _up.parse_qs(sent["body"])["text"][0]
    assert scheduler.OPS_BANNER.strip() in decoded
