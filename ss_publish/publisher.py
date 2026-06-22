"""The shared publish core for SovereignSignal channels.

One place owns the Telegram send, Slack mirror, ops-alert routing, and the
text helpers (truncation, secret redaction, retry backoff) that modelbytes and
clawbytes each carried by hand — and that had drifted apart. Both repos
construct a ``Publisher`` with their own credentials and call methods; the
core is config-driven, not env-coupled, so it stays testable and the two repos
keep their own env-var names and prefixes.

Design rules (the contract both repos depend on):

- **Never raise on the publish path.** Send/ops methods return a result or
  bool; a transient outage, a malformed payload, or a missing credential
  degrades to a False return, never an exception. (A raising send aborts the
  caller's whole loop — the original clawbytes bug.)
- **Telegram is primary, Slack is the mirror.** ``mirror_to_slack`` is
  best-effort and never blocks; the channel never goes dark because Slack is
  down.
- **Ops alerts are Telegram-then-Slack in isolated try-blocks.** A Telegram
  outage is exactly when the Slack fallback must fire.
- **Idempotency is the caller's job.** This core sends; it does not remember
  what was sent. (modelbytes' ``posted_digests`` ledger and clawbytes'
  ``postedUrls`` state live in the repos.)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import requests

from .markup import telegram_html_to_mrkdwn

__all__ = [
    "Publisher",
    "TelegramResult",
    "truncate_for_telegram",
    "redact_secrets",
    "retry_delay",
    "telegram_html_to_mrkdwn",
    "RETRYABLE_STATUS",
    "TELEGRAM_MAX_CHARS",
]

TELEGRAM_MAX_CHARS = 4096  # sendMessage hard limit (char count is a safe lower bound)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def truncate_for_telegram(message: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Truncate at the last newline before Telegram's 4096-char limit, with a
    marker. Without this an oversize bundle 400-fails and never lands."""
    if len(message) <= limit:
        return message
    marker = "\n\n…[truncated]"
    headroom = limit - len(marker)
    cut = message.rfind("\n", 0, headroom)
    if cut < headroom * 0.7:  # no good newline boundary; fall back to a char cut
        cut = headroom
    return message[:cut].rstrip() + marker


def redact_secrets(text: str, secrets: Tuple[str, ...] = ()) -> str:
    """Scrub each known secret value from ``text`` (logs, alerts, exceptions).
    Empty/whitespace secrets are ignored so an unset env var can't blank a log
    line by replacing '' with a placeholder."""
    out = str(text)
    for secret in secrets:
        s = secret or ""
        if s:
            out = out.replace(s, "<redacted>")
    return out


def retry_delay(response: Any, attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Backoff for a send retry. Honors ``Retry-After`` when the response
    carries it (Telegram 429s do), else linear ``base × attempt``. Capped.

    ``response`` may be a ``requests.Response`` (has ``.headers``), an exception
    with a ``.response``, or ``None`` (network error with no response)."""
    retry_after = None
    headers = None
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is None:
            resp = getattr(response, "response", None)
            headers = getattr(resp, "headers", None) if resp is not None else None
    if headers is not None:
        try:
            retry_after = headers.get("Retry-After")
        except Exception:
            retry_after = None
    if retry_after:
        try:
            return min(float(retry_after), cap)
        except (TypeError, ValueError):
            pass
    return min(base * attempt, cap)


@dataclass
class TelegramResult:
    """Outcome of a single Telegram send. Carries the message_id (the one
    durable proof of publication Telegram returns) so the caller can record it
    in its own audit ledger without a module global."""
    ok: bool
    message_id: Optional[int] = None
    error: str = ""
    truncated: bool = False


@dataclass
class Publisher:
    """A configured channel + ops inbox. Construct one per service with that
    service's own env vars; call methods. Never raises on the publish path.

    The two services set different values: modelbytes disables link previews
    (clean digest channel), clawbytes enables them (link cards are the point).
    modelbytes ops banner is ``🚨 ModelBytes ops:``; clawbytes is
    ``🔧 OPS REPORT — …``. Both are just constructor args here.
    """

    # Audience Telegram channel
    telegram_token: str = ""
    telegram_channel_id: str = ""
    # Audience Slack mirror (best-effort)
    slack_token: str = ""
    slack_channel_id: str = ""
    # Ops alerts: Telegram DM first, Slack fallback
    ops_telegram_chat_id: str = ""
    ops_slack_channel_id: str = ""
    # Presentation / tuning
    disable_preview: bool = True
    ops_banner: str = "🚨 ops"
    secret_values: Tuple[str, ...] = ()
    max_chars: int = TELEGRAM_MAX_CHARS
    send_attempts: int = 3
    backoff_base: float = 1.0
    timeout: float = 30.0
    # Injected for testing; production leaves the default and uses requests.
    _sleep: Any = field(default=time.sleep, repr=False)
    _post: Any = field(default=requests.post, repr=False)

    # --- internals -----------------------------------------------------

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self.secret_values)

    # --- audience send -------------------------------------------------

    def send_telegram(self, message: str) -> TelegramResult:
        """Send one message to the audience Telegram channel.

        Returns a ``TelegramResult``; never raises. Truncates to the char
        limit, retries transient 429/5xx honoring ``Retry-After``, and treats
        ``ok:false`` as a non-retryable content problem.
        """
        if not self.telegram_token or not self.telegram_channel_id:
            return TelegramResult(ok=False, error="telegram not configured")
        original_len = len(message)
        body = message
        truncated = False
        if original_len > self.max_chars:
            body = truncate_for_telegram(message, self.max_chars)
            truncated = True
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        last_err = ""
        for attempt in range(1, self.send_attempts + 1):
            try:
                resp = self._post(
                    url,
                    json={
                        "chat_id": self.telegram_channel_id,
                        "text": body,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": self.disable_preview,
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = f"network: {e!r}"
                if attempt < self.send_attempts:
                    self._sleep(retry_delay(None, attempt, self.backoff_base))
                    continue
                return TelegramResult(ok=False, error=last_err, truncated=truncated)
            except Exception as e:  # noqa: BLE001 - publish path never raises
                # A non-network error (bug, unexpected raise) is not retryable;
                # surface it and degrade to ok=False rather than aborting the
                # caller's whole publish loop.
                last_err = f"unexpected: {e!r}"
                return TelegramResult(ok=False, error=last_err, truncated=truncated)
            status = getattr(resp, "status_code", None)
            if status in RETRYABLE_STATUS and attempt < self.send_attempts:
                self._sleep(retry_delay(resp, attempt, self.backoff_base))
                continue
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if status not in (None, 200) or not payload.get("ok"):
                # ok:false is usually a malformed-HTML content problem; retrying
                # won't fix it. Surface Telegram's description for the caller.
                desc = payload.get("description") or getattr(resp, "text", "")[:200]
                return TelegramResult(
                    ok=False, error=f"HTTP {status}: {desc}", truncated=truncated
                )
            return TelegramResult(
                ok=True,
                message_id=payload.get("result", {}).get("message_id"),
                truncated=truncated,
            )
        return TelegramResult(ok=False, error=last_err or "exhausted retries",
                              truncated=truncated)

    def mirror_to_slack(self, message: str) -> bool:
        """Best-effort audience-channel mirror. Returns False when unconfigured
        or on any failure; never raises. Slack being down must never block the
        Telegram publish."""
        if not self.slack_token or not self.slack_channel_id:
            return False
        try:
            resp = self._post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.slack_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "channel": self.slack_channel_id,
                    "text": telegram_html_to_mrkdwn(message)[:39000],
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
                timeout=self.timeout,
            )
            data = resp.json()
            return bool(data.get("ok"))
        except Exception:
            return False

    # --- ops alerts ----------------------------------------------------

    def send_ops_alert(self, text: str) -> bool:
        """Tell the operator something broke or degraded. Telegram DM first;
        if that can't be delivered, a Slack ops channel fallback. Returns True
        if either path landed; never raises. Both paths are isolated so a
        Telegram outage is exactly when Slack must fire.

        Body is prefixed with ``ops_banner`` and secret-scrubbed so a token or
        DB url leaking into an exception never reaches the operator's inbox.
        """
        body = f"{self.ops_banner} {self._redact(text)}"[:3900]
        if self.ops_telegram_chat_id and self.telegram_token:
            try:
                resp = self._post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={
                        "chat_id": self.ops_telegram_chat_id,
                        "text": body,
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
                if resp.ok:
                    return True
            except Exception:
                pass
        if self.ops_slack_channel_id and self.slack_token:
            try:
                resp = self._post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.slack_token}"},
                    json={"channel": self.ops_slack_channel_id, "text": body,
                          "unfurl_links": False},
                    timeout=10,
                )
                if resp.ok and resp.json().get("ok"):
                    return True
            except Exception:
                pass
        return False
