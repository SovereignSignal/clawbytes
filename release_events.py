"""Best-effort Release Events v1 producer shared by Sov's signal services.

`send_release_event` reports a queue status so callers can retry transport
failures without retrying a rejected payload. A missing URL or token returns
`skipped` and does not open a socket. This module does not know about the
ClawBytes enable flag; the forwarder checks that before calling in.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SCHEMA = "release-event/v1"

DELIVERED = "delivered"
DUPLICATE = "duplicate"
RETRYABLE = "retryable"
REJECTED = "rejected"
CONFIG_ERROR = "config_error"
SKIPPED = "skipped"

_DUPLICATE_STATUSES = frozenset({
    "duplicate",
    "owned_by_poller",
    "already_seen",
})


def classify_release_response(status: int, body: bytes = b"") -> str:
    """Map an HTTP result onto a queue status.

    202 and other 2xx are delivered. 200 is delivered unless the body says
    the receiver already has the event (`duplicate`, `owned_by_poller`).
    400 is a permanent reject. 401 is a local configuration error. 408, 429,
    and 5xx are retryable.
    """
    if status == 401:
        return CONFIG_ERROR
    if status == 400:
        return REJECTED
    if status in {408, 429} or 500 <= status <= 599:
        return RETRYABLE
    if status == 200 and _body_says_duplicate(body):
        return DUPLICATE
    if 200 <= status < 300:
        return DELIVERED
    if 400 <= status < 500:
        return REJECTED
    return RETRYABLE


def _body_says_duplicate(body: bytes) -> bool:
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("duplicate") is True:
        return True
    status = str(data.get("status") or "").strip().lower().replace("-", "_")
    return status in _DUPLICATE_STATUSES


def send_release_event(event: dict, timeout: float = 5) -> tuple[str, str]:
    """POST one event. Returns `(status, detail)`. Never raises.

    `detail` is a short token (`http_202`, `timeout`, `unconfigured`). It
    does not include the request URL, the token, or the response body.
    """
    url = os.environ.get("RELEASE_EVENTS_URL", "").strip()
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "").strip()
    if not url or not token:
        return SKIPPED, "unconfigured"
    payload = dict(event)
    payload["schema"] = SCHEMA
    required = ("id", "kind", "name", "version", "source", "url")
    if any(not payload.get(key) for key in required):
        return REJECTED, "invalid_event"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536)
            status = classify_release_response(getattr(resp, "status", 200), body)
            return status, f"http_{getattr(resp, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:  # noqa: BLE001 - a broken error body is still an HTTP result
            body = b""
        return classify_release_response(exc.code, body), f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - timeout and DNS must be retryable, not fatal
        print(f"[WARN] release-event forward failed: {type(exc).__name__}")
        return RETRYABLE, type(exc).__name__


def emit_release_event(event: dict, timeout: int = 5) -> bool:
    """True when the receiver accepted the event (delivered or duplicate)."""
    status, _detail = send_release_event(event, timeout=timeout)
    return status in {DELIVERED, DUPLICATE}
