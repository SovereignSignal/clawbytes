from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


def send_slack_summary(summary_path: Path, webhook_url: str | None = None) -> tuple[bool, str]:
    webhook = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not summary_path.exists():
        return False, f"summary file does not exist: {summary_path}"

    text = summary_path.read_text(encoding="utf-8")
    if webhook:
        return _send_webhook(webhook, text)

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel_id = os.environ.get("SLACK_CHANNEL_ID", "")
    user_id = os.environ.get("SLACK_USER_ID", "")
    if bot_token and (channel_id or user_id):
        return _send_bot_message(bot_token, text, channel_id=channel_id, user_id=user_id)

    return False, "Set SLACK_WEBHOOK_URL, or SLACK_BOT_TOKEN plus SLACK_CHANNEL_ID/SLACK_USER_ID"


def send_text(text: str, *, channel_id: str = "", user_id: str = "") -> tuple[bool, str]:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if webhook and not channel_id and not user_id:
        return _send_webhook(webhook, text)
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    target_channel = channel_id or os.environ.get("SLACK_CHANNEL_ID", "")
    target_user = user_id or os.environ.get("SLACK_USER_ID", "")
    if not bot_token or not (target_channel or target_user):
        return False, "Set SLACK_BOT_TOKEN plus SLACK_CHANNEL_ID/SLACK_USER_ID"
    return _send_bot_message(bot_token, text, channel_id=target_channel, user_id=target_user)


def _send_webhook(webhook: str, text: str) -> tuple[bool, str]:
    payload = {"text": text[:39000]}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return 200 <= resp.status < 300, body
    except Exception as exc:
        return False, str(exc)


def _send_bot_message(
    bot_token: str,
    text: str,
    *,
    channel_id: str = "",
    user_id: str = "",
) -> tuple[bool, str]:
    channel = channel_id
    if not channel:
        ok, result = _slack_api(
            "conversations.open",
            bot_token,
            {"users": user_id},
        )
        if not ok:
            return False, result
        channel = result.get("channel", {}).get("id", "")
        if not channel:
            return False, "Slack conversations.open returned no channel id"

    ok, result = _slack_api(
        "chat.postMessage",
        bot_token,
        {
            "channel": channel,
            "text": text[:39000],
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )
    if not ok:
        return False, result
    return True, f"sent via Slack Web API to {channel}"


def _slack_api(method: str, bot_token: str, payload: dict) -> tuple[bool, dict | str]:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return False, str(exc)
    if not body.get("ok"):
        return False, f"{method} failed: {body.get('error', body)}"
    return True, body
