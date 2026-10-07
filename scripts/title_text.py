"""Visible text from titles that contain links or inline formatting.

Also the calendar-date reader the staleness gate and the page watcher share.
"""

from __future__ import annotations

import html
import re
from datetime import date

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


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def first_stated_date(text: str):
    """The first calendar date written in ``text``, or None.

    This is the date the item claims ("September 22, 2026"), not the moment
    we discovered it. The earliest match wins so a changelog's top entry
    beats an older date further down the page.
    """
    if not text:
        return None
    found = None
    month_match = _MONTH_DATE.search(text)
    iso_match = _ISO_DATE.search(text)
    if month_match and (iso_match is None or month_match.start() <= iso_match.start()):
        month = _MONTHS[month_match.group(1).lower()]
        day = int(month_match.group(2))
        year = int(month_match.group(3))
        try:
            found = date(year, month, day)
        except ValueError:
            found = None
    elif iso_match:
        try:
            found = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            found = None
    return found


def format_stated_date(value: date) -> str:
    """English month name, independent of the process locale."""
    return f"{MONTH_NAMES[value.month - 1]} {value.day}, {value.year}"
