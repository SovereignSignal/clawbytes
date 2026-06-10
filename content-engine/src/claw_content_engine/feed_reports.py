from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from .slack import send_text


class _SlackMrkdwnConverter(HTMLParser):
    """Convert the small Telegram-HTML subset clawbytes/modelbytes emit
    (<b>, <i>, <a href>, <code>) into Slack mrkdwn, preserving line breaks.

    Slack link syntax is <url|label>; bold is *text*; italic is _text_.
    Display text must have &, <, > escaped, so we escape data runs but emit
    link/format markup literally.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_link = False
        self._href = ""
        self._link_text: list[str] = []

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def handle_starttag(self, tag: str, attrs) -> None:
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

    def handle_endtag(self, tag: str) -> None:
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
            else:
                self.parts.append(self._esc(label))
            self._in_link = False
            self._href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)
        else:
            self.parts.append(self._esc(data))

    def result(self) -> str:
        return "".join(self.parts)


def telegram_html_to_slack_mrkdwn(value: str) -> str:
    """Render Telegram-HTML message text as Slack mrkdwn.

    Unlike a flat text strip, this keeps bold/links/line structure: links
    become clickable <url|title>, <b> becomes *bold*, newlines are preserved.
    """
    parser = _SlackMrkdwnConverter()
    parser.feed(value)
    text = parser.result()
    # Tidy: trim trailing spaces per line, collapse 3+ blank lines to one gap.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def latest_modelbytes_report(modelbytes: Path) -> str:
    pending = modelbytes / "pending"
    files = sorted(pending.glob("*.txt"))
    if not files:
        return "*ModelBytes* — no published digests found yet."
    latest = files[-1]
    body = latest.read_text(encoding="utf-8")
    md = telegram_html_to_slack_mrkdwn(body)
    return f"*ModelBytes — latest digest* ({latest.stem})\n\n{md[:35000]}"


def clawbytes_preview_report(clawbytes: Path, *, python_bin: str = "python3") -> str:
    # Compact digest: a one-line queue summary (no monospace status dump) + the
    # enriched lanes; empty lanes collapse into a single trailing "Quiet" line.
    status = _run([python_bin, "clawbytes_threads.py", "status"], clawbytes)
    counts = {}
    for line in status.splitlines():
        m = re.match(r"\s*-\s*(\w+):\s*(\d+)\s*queued", line)
        if m:
            counts[m.group(1)] = m.group(2)
    parts = ["*ClawBytes — lane preview*"]
    if counts:
        summary = " · ".join(
            f"{lane} {counts[lane]}"
            for lane in ("Ship", "Watch", "Read", "Community")
            if lane in counts
        )
        parts.append(f"_Queue: {summary}_")
    quiet = []
    for category in ["ship", "watch", "read", "community"]:
        output = _run([python_bin, "clawbytes_threads.py", "preview", "--category", category], clawbytes)
        rendered = telegram_html_to_slack_mrkdwn(output).strip()
        if not rendered:
            continue
        if "Nothing new" in rendered:
            quiet.append(category.title())
            continue
        parts.append(rendered)
    if quiet:
        parts.append(f"_Quiet: {' · '.join(quiet)}_")
    return "\n\n".join(parts)[:35000]


def clawbytes_audit_report(clawbytes: Path, *, python_bin: str = "python3") -> str:
    """Weekly ingestion-audit summary: where raw items die in the pipeline.

    Surfaces classifier rejections (vocab gaps), zero-yield sources (dead
    feeds), and unconsumed state files (orphaned monitors).
    """
    raw = _run([python_bin, "clawbytes_threads.py", "audit", "--json"], clawbytes)
    try:
        report = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return f"*ClawBytes — ingestion audit*\n_audit failed:_\n```{raw[:1000]}```"

    parts = ["*ClawBytes — weekly ingestion audit*"]
    status = report.get("statusCounts", {})
    if status:
        joined = " · ".join(f"{key} {value}" for key, value in sorted(status.items()))
        parts.append(f"_Pipeline: {joined} (of {report.get('rawItems', 0)} raw items)_")
    reasons = report.get("reasonCounts", {})
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        parts.append("*Drop reasons:* " + " · ".join(f"{key} {value}" for key, value in top))
    sources = report.get("sourceCounts", {})
    if sources:
        ranked = sorted(sources.items(), key=lambda kv: -kv[1])
        parts.append("*Raw items by source:* " + " · ".join(f"{key} {value}" for key, value in ranked))
    lanes = report.get("laneCounts", {})
    if lanes:
        parts.append("*Lane flow:* " + " · ".join(f"{key} {value}" for key, value in lanes.items()))
    unconsumed = report.get("unconsumedStateFiles", [])
    if unconsumed:
        listed = ", ".join(f"{row['file']} ({row['items']})" for row in unconsumed)
        parts.append(f"*Unconsumed state files:* {listed}")
    return "\n\n".join(parts)[:35000]


def send_modelbytes_report(modelbytes: Path, channel_id: str) -> tuple[bool, str]:
    return send_text(latest_modelbytes_report(modelbytes), channel_id=channel_id)


def send_clawbytes_report(clawbytes: Path, channel_id: str, *, python_bin: str = "python3") -> tuple[bool, str]:
    return send_text(clawbytes_preview_report(clawbytes, python_bin=python_bin), channel_id=channel_id)


def send_clawbytes_audit(clawbytes: Path, channel_id: str, *, python_bin: str = "python3") -> tuple[bool, str]:
    return send_text(clawbytes_audit_report(clawbytes, python_bin=python_bin), channel_id=channel_id)


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
        env={
            **__import__("os").environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    )
    if result.returncode != 0:
        return (result.stdout + "\n" + result.stderr).strip()
    return result.stdout
