"""Log fields for an empty OpenAI-compatible completion.

Reasoning models can spend the whole max_tokens budget on thinking and
return content="". finish_reason, completion tokens, reasoning tokens, and
the reasoning length say whether the cap was the cause.
"""

from __future__ import annotations


def _reasoning_text(message: dict) -> str:
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list) and value:
            return "".join(str(part) for part in value)
    return ""


def completion_diagnostics(payload: dict) -> str:
    """finish_reason, completion/reasoning token counts, and reasoning length."""
    if not isinstance(payload, dict):
        payload = {}
    choice = {}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    reasoning_tokens = usage.get("reasoning_tokens")
    details = usage.get("completion_tokens_details")
    if reasoning_tokens is None and isinstance(details, dict):
        reasoning_tokens = details.get("reasoning_tokens")
    if reasoning_tokens is None:
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = output_details.get("reasoning_tokens")
    reasoning = _reasoning_text(message)
    return (
        f"finish_reason={choice.get('finish_reason')} "
        f"completion_tokens={usage.get('completion_tokens')} "
        f"reasoning_tokens={reasoning_tokens} "
        f"reasoning_len={len(reasoning)}"
    )


def empty_content_message(payload: dict) -> str:
    return f"empty content (len=0) {completion_diagnostics(payload)}"
