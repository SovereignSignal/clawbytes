"""HTML → Slack mrkdwn conversion for the small Telegram-HTML subset both
SovereignSignal channels emit.

Pure, no I/O, no deps. The most independently-reusable piece of the package:
both modelbytes and clawbytes ship Telegram-HTML to a Slack mirror and need
identical rendering, so they share one converter instead of drifting.
"""
from __future__ import annotations

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
