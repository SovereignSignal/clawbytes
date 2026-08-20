"""Scheduler job table: yield snapshot is weekly, file-only, no success DM."""
from apscheduler.schedulers.blocking import BlockingScheduler

import scheduler


def test_schedule_includes_silent_monday_yield_snapshot():
    sched = BlockingScheduler(timezone="UTC")
    scheduler.schedule_jobs(sched)
    ids = {job.id for job in sched.get_jobs()}
    assert ids == {"collect", "autopublish", "health_check", "discover", "yield_snapshot"}
    job = sched.get_job("yield_snapshot")
    trigger = str(job.trigger)
    assert "day_of_week='mon'" in trigger
    assert "hour='15'" in trigger
    assert "minute='45'" in trigger


def test_yield_snapshot_job_runs_cli_and_does_not_notify_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "_run", lambda label, args: calls.append((label, args)))
    monkeypatch.setattr(
        scheduler,
        "_send_admin_dm",
        lambda *a, **k: calls.append(("DM", a)),
    )
    scheduler.yield_snapshot()
    assert calls == [("yield_snapshot", ["yield-snapshot"])]
