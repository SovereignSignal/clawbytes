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
from datetime import datetime, timedelta, timezone
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
    """Run a subprocess job, logging start/finish; never raises.

    A nonzero exit is "things are not going well" — the one case ops DMs
    exist for — so it alerts the admin immediately.
    """
    log.info("START %s: %s", label, " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        log.info("DONE %s: exit=%s", label, result.returncode)
        if result.returncode != 0:
            _send_admin_dm(f"alert:{label}", f"⚠️ ClawBytes job '{label}' exited {result.returncode}. Check Railway logs.")
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR %s crashed", label)
        _send_admin_dm(f"alert:{label}", f"⚠️ ClawBytes job '{label}' crashed before completing. Check Railway logs.")


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


OPS_BANNER = "🔧 OPS REPORT — visible only to you, never posted to the channel.\n\n"


def _send_admin_dm(label: str, text: str, *, html: bool = False) -> None:
    """Send an operational/status report to Sov as a Telegram DM.

    Status reports never go to audience surfaces (the Slack channel or the
    @clawbytes Telegram channel) — CLAWBYTES_ADMIN_CHAT_ID is the ops inbox.
    Every DM is prefixed with OPS_BANNER so an ops report can never be
    mistaken for channel content (same bot sends both). Falls back to a
    plain-text resend if Telegram rejects the HTML parse.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CLAWBYTES_ADMIN_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("SKIP %s (admin_chat_set=%s token_set=%s)", label, bool(chat_id), bool(token))
        return
    text = OPS_BANNER + text

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


_PARSE_ISO_FAIL = object()

COLLECT_STALL_HOURS = 2
CHANNEL_SILENT_HOURS = 30
ALERT_REPEAT_HOURS = 6


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def health_issues(memory_dir: Path, now: datetime | None = None) -> list[str]:
    """Things that are NOT going well. Empty list = healthy = no DM.

    Ops DMs are exception-only by contract: routine status never reaches the
    admin; only breakage does.
    """
    now = now or datetime.now(timezone.utc)
    state_path = Path(memory_dir) / "clawbytes-thread-state.json"
    try:
        state = json.loads(state_path.read_text())
    except Exception:  # noqa: BLE001
        return [f"thread state unreadable at {state_path}"]

    issues = []
    last_collect = _parse_iso(state.get("lastCollectedAt"))
    if not last_collect or now - last_collect > timedelta(hours=COLLECT_STALL_HOURS):
        issues.append(f"collect stalled — lastCollectedAt={state.get('lastCollectedAt')}")

    last_post = None
    for event in reversed(state.get("publishLog", [])):
        last_post = _parse_iso(event.get("at"))
        if last_post:
            break
    if not last_post or now - last_post > timedelta(hours=CHANNEL_SILENT_HOURS):
        issues.append(f"channel silent — no post since {last_post.isoformat() if last_post else 'ever'}")
    return issues


def health_check() -> None:
    """Hourly: DM the admin ONLY when something is wrong. An unchanged issue
    set re-alerts at most every ALERT_REPEAT_HOURS."""
    memory_dir = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(REPO_ROOT / "memory")))
    issues = health_issues(memory_dir)
    if not issues:
        log.info("health_check: healthy, no DM")
        return
    text = "⚠️ ClawBytes health check found problems:\n- " + "\n- ".join(issues)
    marker_path = memory_dir / "claw-health-alert-state.json"
    try:
        marker = json.loads(marker_path.read_text())
    except Exception:  # noqa: BLE001
        marker = {}
    sent_at = _parse_iso(marker.get("sentAt"))
    if (
        marker.get("text") == text
        and sent_at
        and datetime.now(timezone.utc) - sent_at < timedelta(hours=ALERT_REPEAT_HOURS)
    ):
        log.info("health_check: issues unchanged, alert already sent recently")
        return
    _send_admin_dm("health_alert", text)
    try:
        marker_path.write_text(json.dumps({"sentAt": datetime.now(timezone.utc).isoformat(), "text": text}))
    except Exception:  # noqa: BLE001
        log.exception("health_check: could not write alert marker")


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
    # hourly health check at :20 — silent when healthy, DMs the admin only on breakage
    scheduler.add_job(health_check, "cron", minute=20, id="health_check")
    # weekly source discovery, Mondays 14:10 UTC (before the day's collects pick it up)
    scheduler.add_job(discover, "cron", day_of_week="mon", hour=14, minute=10, id="discover")

    log.info("Scheduled: collect=*:00,30  autopublish=*:05  health_check=*:20 (alert-only)  discover=Mon 14:10 (UTC). Waiting for triggers.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
