from __future__ import annotations

import re
import subprocess
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from .slack import send_text


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


def telegram_html_to_text(value: str) -> str:
    value = re.sub(r'<a\s+href="([^"]+)">([^<]+)</a>', r'\2 \1', value)
    parser = _TextExtractor()
    parser.feed(value)
    text = unescape(parser.text())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def latest_modelbytes_report(modelbytes: Path) -> str:
    pending = modelbytes / "pending"
    files = sorted(pending.glob("*.txt"))
    if not files:
        return "*ModelBytes* - no pending curated digests found."
    latest = files[-1]
    body = latest.read_text(encoding="utf-8")
    plain = telegram_html_to_text(body)
    plain = plain.replace("━━━", "\n\n")
    return f"*ModelBytes latest digest* ({latest.stem})\n\n{plain[:35000]}"


def clawbytes_preview_report(clawbytes: Path, *, python_bin: str = "python3") -> str:
    status = _run([python_bin, "clawbytes_threads.py", "status"], clawbytes)
    sections = [f"*ClawBytes lane preview*\n\n```{status.strip()}```"]
    for category in ["ship", "watch", "read", "community"]:
        output = _run([python_bin, "clawbytes_threads.py", "preview", "--category", category], clawbytes)
        sections.append(f"*{category.title()}*\n{telegram_html_to_text(output) or 'No output'}")
    return "\n\n".join(sections)[:35000]


def send_modelbytes_report(modelbytes: Path, channel_id: str) -> tuple[bool, str]:
    return send_text(latest_modelbytes_report(modelbytes), channel_id=channel_id)


def send_clawbytes_report(clawbytes: Path, channel_id: str, *, python_bin: str = "python3") -> tuple[bool, str]:
    return send_text(clawbytes_preview_report(clawbytes, python_bin=python_bin), channel_id=channel_id)


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

