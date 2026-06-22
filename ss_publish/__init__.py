"""ss-publish: the shared Telegram/Slack/ops publish core for SovereignSignal
channels (modelbytes, clawbytes, and future siblings).

See :mod:`ss_publish.publisher` for the ``Publisher`` class and the contract.
"""
from .markup import telegram_html_to_mrkdwn
from .publisher import (
    RETRYABLE_STATUS,
    TELEGRAM_MAX_CHARS,
    Publisher,
    TelegramResult,
    redact_secrets,
    retry_delay,
    truncate_for_telegram,
)

__version__ = "0.1.0"

__all__ = [
    "Publisher",
    "TelegramResult",
    "truncate_for_telegram",
    "redact_secrets",
    "retry_delay",
    "telegram_html_to_mrkdwn",
    "RETRYABLE_STATUS",
    "TELEGRAM_MAX_CHARS",
    "__version__",
]
