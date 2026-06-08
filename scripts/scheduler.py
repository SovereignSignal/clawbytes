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

import logging
import os
import subprocess
import sys
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


def _run(label: str, args: list[str]) -> None:
    """Run a clawbytes_threads.py subcommand, logging start/finish; never raises."""
    cmd = [sys.executable, THREADS, *args]
    log.info("START %s: %s", label, " ".join(args))
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        log.info("DONE %s: exit=%s", label, result.returncode)
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR %s crashed", label)


def collect() -> None:
    _run("collect", ["collect", "--run-monitors", "--summary"])


def autopublish() -> None:
    args = ["autopublish"]
    if _publish_enabled():
        args.append("--send")
    _run("autopublish", args)


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

    log.info("Scheduled: collect=*:00,30  autopublish=*:05 (UTC). Waiting for triggers.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
