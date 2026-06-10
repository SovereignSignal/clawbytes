#!/usr/bin/env python3
"""In-process scheduler for the ClawBytes Railway deployment.

Replaces the VM's systemd timers with a single always-on container. Every job
runs in this one process against the shared volume (CLAWBYTES_MEMORY_DIR), so
there is no cross-container state problem. Mirrors the live VM cadence:

    collect      every 30 min        clawbytes_threads.py collect --run-monitors
    autopublish  hourly at minute 05 clawbytes_threads.py autopublish [--send]

Publishing is gated by CLAWBYTES_PUBLISH so the first deploy can soak without
posting to the live @clawbytes channel:

    CLAWBYTES_PUBLISH unset/0/false  -> autopublish runs WITHOUT --send (dry)
    CLAWBYTES_PUBLISH 1/true/yes/on  -> autopublish runs WITH --send (live)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

REPO_ROOT = Path(__file__).resolve().parent.parent
THREADS = str(REPO_ROOT / "clawbytes_threads.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [scheduler] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("clawbytes.scheduler")


def _publish_enabled() -> bool:
    return os.environ.get("CLAWBYTES_PUBLISH", "").strip().lower() in {"1", "true", "yes", "on"}


def _run_cmd(label: str, cmd: list[str]) -> None:
    """Run a subprocess job, logging start/finish; never raises."""
    log.info("START %s: %s", label, " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        log.info("DONE %s: exit=%s", label, result.returncode)
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR %s crashed", label)


def _run(label: str, args: list[str]) -> None:
    """Run a clawbytes_threads.py subcommand."""
    _run_cmd(label, [sys.executable, THREADS, *args])


def collect() -> None:
    _run("collect", ["collect", "--run-monitors", "--summary"])


def autopublish() -> None:
    args = ["autopublish"]
    if _publish_enabled():
        args.append("--send")
    _run("autopublish", args)


def discover() -> None:
    """Weekly source discovery. New repos land in claw-ecosystem-sources.json
    (merged into release checks by get_all_repos) and new feeds/subreddits in
    clawbytes-dynamic-feeds.json (merged by the rss/reddit monitors)."""
    _run_cmd(
        "discover_ecosystem",
        ["bash", str(REPO_ROOT / "scripts" / "claw-ecosystem-monitor.sh"), "--mode", "discover"],
    )
    _run_cmd(
        "discover_feeds",
        [sys.executable, str(REPO_ROOT / "scripts" / "claw-source-discovery.py")],
    )


def _send_admin_dm(label: str, text: str, *, html: bool = False) -> None:
    """Send an operational/status report to Sov as a Telegram DM.

    Status reports never go to audience surfaces (the Slack channel or the
    @clawbytes Telegram channel) — CLAWBYTES_ADMIN_CHAT_ID is the ops inbox.
    Falls back to a plain-text resend if Telegram rejects the HTML parse.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CLAWBYTES_ADMIN_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("SKIP %s (admin_chat_set=%s token_set=%s)", label, bool(chat_id), bool(token))
        return

    def _post(payload: dict) -> bool:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return bool(json.loads(resp.read().decode()).get("ok"))
        except Exception:  # noqa: BLE001 - report delivery must not kill the scheduler
            return False

    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    if html:
        ok = _post({**payload, "parse_mode": "HTML"})
        if not ok:
            ok = _post(payload)
    else:
        ok = _post(payload)
    log.info("DONE %s: telegram_ok=%s", label, ok)


def _capture(args: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, THREADS, *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    return (result.stdout or "").strip()


def lane_preview_report() -> None:
    """Daily lane preview, DMed to the admin (was a Slack channel post)."""
    log.info("START lane_preview_dm")
    parts = []
    for category in ("ship", "watch", "read", "community"):
        try:
            output = _capture(["preview", "--category", category])
        except Exception:  # noqa: BLE001
            log.exception("ERROR lane_preview_dm preview %s crashed", category)
            continue
        if output and "Nothing new" not in output:
            parts.append(output)
    body = "\n\n".join(parts) or "All lanes quiet."
    _send_admin_dm("lane_preview_dm", "ClawBytes — daily lane preview\n\n" + body, html=True)


def audit_report() -> None:
    """Weekly ingestion audit, DMed to the admin (was a Slack channel post)."""
    log.info("START audit_dm")
    ce_src = str(REPO_ROOT / "content-engine" / "src")
    if ce_src not in sys.path:
        sys.path.insert(0, ce_src)
    try:
        from claw_content_engine.feed_reports import clawbytes_audit_report

        text = clawbytes_audit_report(REPO_ROOT, python_bin=sys.executable)
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR audit_dm failed to build report")
        return
    # The builder emits Slack mrkdwn (*bold*/_italic_); send as plain text —
    # Telegram Markdown parse would choke on underscores in reason names.
    _send_admin_dm("audit_dm", text)


def main() -> int:
    memory_dir = os.environ.get("CLAWBYTES_MEMORY_DIR", "<unset - using repo default>")
    log.info("ClawBytes scheduler starting")
    log.info("  repo:        %s", REPO_ROOT)
    log.info("  memory dir:  %s", memory_dir)
    log.info("  publishing:  %s", "LIVE (--send)" if _publish_enabled() else "DRY (no --send)")

    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    # collect every 30 minutes (VM: OnCalendar=*:0/30)
    scheduler.add_job(collect, "cron", minute="0,30", id="collect")
    # autopublish hourly at :05 (VM: OnCalendar=*-*-* *:05:00)
    scheduler.add_job(autopublish, "cron", minute=5, id="autopublish")
    # daily lane preview at 15:30 UTC, DMed to the admin (~08:30 PT)
    scheduler.add_job(lane_preview_report, "cron", hour=15, minute=30, id="lane_preview")
    # weekly source discovery, Mondays 14:10 UTC (before the day's collects pick it up)
    scheduler.add_job(discover, "cron", day_of_week="mon", hour=14, minute=10, id="discover")
    # weekly ingestion audit DM, Mondays 15:45 UTC (after discovery + a collect cycle)
    scheduler.add_job(audit_report, "cron", day_of_week="mon", hour=15, minute=45, id="audit_report")

    log.info("Scheduled: collect=*:00,30  autopublish=*:05  lane_preview_dm=15:30  discover=Mon 14:10  audit_dm=Mon 15:45 (UTC). Waiting for triggers.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
