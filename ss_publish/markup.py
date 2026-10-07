"""HTML → Slack mrkdwn conversion, and the send-path sanitizer for the
Telegram-HTML subset both SovereignSignal channels emit.

Pure, no I/O, no deps. The most independently-reusable piece of the package:
both modelbytes and clawbytes ship Telegram-HTML to a Slack mirror and need
identical rendering, so they share one converter instead of drifting.

``sanitize_telegram_html`` runs on the audience send. Telegram autolinks bare
domains, IP addresses, and @handles even in HTML mode, and it does not render
markdown. The sanitizer turns inline `code` and **bold** into tags and wraps
the autolink shapes in ``<code>`` so they stay visible text. Existing
``<a href>`` links are left alone.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List


class _SlackMrkdwnConverter(HTMLParser):
    """Convert Telegram-HTML (<b>/<strong>, <i>/<em>, <code>/<pre>, <a href>,
    <br>) into Slack mrkdwn: *bold*, _italic_, `code`, <url|label>, newlines.

    Mirrors the converter both repos carried in-tree; consolidated here so the
    two Slack mirrors cannot render differently.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._in_link = False
        self._href = ""
        self._link_text: List[str] = []

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def handle_starttag(self, tag, attrs):
        if tag in ("b", "strong"):
            self.parts.append("*")
        elif tag in ("i", "em"):
            self.parts.append("_")
        elif tag in ("code", "pre"):
            self.parts.append("`")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self._in_link = True
            self._href = dict(attrs).get("href", "") or ""
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self.parts.append("*")
        elif tag in ("i", "em"):
            self.parts.append("_")
        elif tag in ("code", "pre"):
            self.parts.append("`")
        elif tag == "a":
            label = "".join(self._link_text).strip()
            href = self._href.strip()
            if href and label:
                self.parts.append(f"<{href}|{self._esc(label).replace('|', '/')}>")
            elif href:
                self.parts.append(f"<{href}>")
            self._in_link = False
            self._href = ""
            self._link_text = []

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        else:
            self.parts.append(self._esc(data))

    def get(self) -> str:
        return "".join(self.parts)


def telegram_html_to_mrkdwn(text: str) -> str:
    """Render the Telegram-HTML subset we emit into Slack mrkdwn."""
    if not text:
        return ""
    conv = _SlackMrkdwnConverter()
    conv.feed(text)
    conv.close()
    return conv.get()


# Telegram autolinks these in HTML messages unless they sit inside <code>,
# <pre>, or an existing <a>. Order: full URL, then IPv4, then bare host
# (optional path), then @handle. A host inside https:// is not matched again
# because the URL alternative consumes it first, and the host alternative
# refuses a preceding slash.
_AUTOLINK = re.compile(
    r"https?://[^\s<]+"
    r"|(?<![\w@./])(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|(?<![\w@./])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,}(?:/[^\s<)\]]*)?"
    r"|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{4,}",
)
_MARKDOWN = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>|<!--.*?-->", re.DOTALL)
_TRAIL_PUNCT = ".,;:)]"


def _esc_fragment(text: str) -> str:
    """Escape raw ``<>&`` without doubling entities already in the text."""
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _wrap_autolink(match: re.Match) -> str:
    raw = match.group(0)
    trail = ""
    while raw and raw[-1] in _TRAIL_PUNCT:
        trail = raw[-1] + trail
        raw = raw[:-1]
    if not raw or raw.startswith("<"):
        return match.group(0)
    return f"<code>{_esc_fragment(raw)}</code>{trail}"


def _defang_autolinks(text: str) -> str:
    if not text:
        return ""
    return _AUTOLINK.sub(_wrap_autolink, text)


def _apply_inline_markdown(text: str, defang: bool) -> str:
    """Turn `code` and **bold** into tags. Defang runs on the non-code parts."""
    if not text:
        return ""
    parts: List[str] = []
    pos = 0
    for match in _MARKDOWN.finditer(text):
        before = text[pos:match.start()]
        parts.append(_defang_autolinks(before) if defang else before)
        if match.group(1) is not None:
            parts.append(f"<code>{_esc_fragment(match.group(1))}</code>")
        else:
            inner = match.group(2)
            inner = _defang_autolinks(inner) if defang else _esc_fragment(inner)
            parts.append(f"<b>{inner}</b>")
        pos = match.end()
    tail = text[pos:]
    parts.append(_defang_autolinks(tail) if defang else tail)
    return "".join(parts)


def _tag_name(token: str) -> str:
    body = token.strip("<>").split(None, 1)[0]
    return body[1:] if body.startswith("/") else body


def sanitize_telegram_html(text: str) -> str:
    """Make one audience HTML message safe to send.

    Existing tags stay, including ``<a href>`` item titles. Inline markdown
    becomes ``<code>`` / ``<b>``. Bare domains, IPs, and @handles outside
    links and code are wrapped in ``<code>`` so Telegram does not autolink
    them. Idempotent: a second pass does not wrap twice.
    """
    if not text:
        return ""
    suppress = {"a", "code", "pre"}
    stack: List[str] = []
    parts: List[str] = []
    pos = 0
    for match in _HTML_TAG.finditer(text):
        chunk = text[pos:match.start()]
        hidden = any(name in suppress for name in stack)
        parts.append(_apply_inline_markdown(chunk, defang=not hidden))
        token = match.group(0)
        parts.append(token)
        name = _tag_name(token).lower()
        if token.startswith("</"):
            if name in stack:
                # Pop the matching open, not an earlier unrelated tag.
                while stack:
                    opened = stack.pop()
                    if opened == name:
                        break
        elif token.endswith("/>") or name in {"br"}:
            pass
        else:
            stack.append(name)
        pos = match.end()
    hidden = any(name in suppress for name in stack)
    parts.append(_apply_inline_markdown(text[pos:], defang=not hidden))
    return "".join(parts)
