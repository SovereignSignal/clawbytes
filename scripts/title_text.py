"""Visible text from titles that contain links or inline formatting."""

from __future__ import annotations

import html
import re

_INLINE_TAG = re.compile(
    r"<(a|strong|em|b|i|code|span|mark)\b[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_ANY_TAG = re.compile(r"<[^>]+>")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_MD_ITALIC = re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)")
_MD_CODE = re.compile(r"`([^`]+)`")


def flatten_inline_markup(value: str) -> str:
    """Keep words that sit inside links, emphasis, and inline tags.

    A tag strip that drops the inner text, or a product-name scrub that runs
    on the raw markup, turns "in Copilot CLI and the Copilot app" into
    "in  CLI and the  app".
    """
    text = value or ""
    previous = None
    while previous != text:
        previous = text
        text = _INLINE_TAG.sub(lambda match: match.group(2), text)
    text = _ANY_TAG.sub("", text)
    text = _MD_IMAGE.sub(lambda match: match.group(1), text)
    text = _MD_LINK.sub(lambda match: match.group(1), text)

    def _marked(match: re.Match) -> str:
        return next((group for group in match.groups() if group), "")

    text = _MD_BOLD.sub(_marked, text)
    text = _MD_ITALIC.sub(_marked, text)
    text = _MD_CODE.sub(lambda match: match.group(1), text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
