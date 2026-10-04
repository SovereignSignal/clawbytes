"""Scheduler job table: yield snapshot is weekly, file-only, no success DM."""
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler

import release_forwarding
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


def _fake_collect(monkeypatch, returncode):
    commands = []

    def _run(cmd, cwd=None, check=False, **kwargs):
        commands.append(list(cmd))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(scheduler.subprocess, "run", _run)
    return commands


def test_collect_invokes_forwarding_once_after_exit_zero(monkeypatch, caplog):
    commands = _fake_collect(monkeypatch, 0)
    forwarded = []
    monkeypatch.setattr(
        release_forwarding,
        "forward_release_events",
        lambda: forwarded.append("once") or "disabled",
    )
    dms = []
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *args, **kwargs: dms.append(args))
    with caplog.at_level("INFO", logger="clawbytes.scheduler"):
        scheduler.collect()
    assert forwarded == ["once"]
    assert len(commands) == 1
    assert commands[0][1].endswith("clawbytes_threads.py")
    assert commands[0][2:] == ["collect", "--run-monitors", "--summary"]
    assert all("forward-release-events.py" not in part for part in commands[0])
    assert dms == []
    assert "forward_release_events: disabled" in caplog.text


def test_collect_skips_forwarding_when_collect_exits_nonzero(monkeypatch):
    _fake_collect(monkeypatch, 1)
    forwarded = []
    monkeypatch.setattr(
        release_forwarding,
        "forward_release_events",
        lambda: forwarded.append("once"),
    )
    dms = []
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *args, **kwargs: dms.append(args))
    scheduler.collect()
    assert forwarded == []
    assert [item[0] for item in dms] == ["alert:collect"]


def test_forward_failure_does_not_page_collect(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))
    _fake_collect(monkeypatch, 0)

    def _boom():
        raise RuntimeError("receiver down")

    monkeypatch.setattr(release_forwarding, "forward_release_events", _boom)
    dms = []
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *args, **kwargs: dms.append(args) or False)
    scheduler.collect()
    assert dms == []
    assert (tmp_path / scheduler.FORWARD_FAILURE_STATE).exists()


def test_sustained_forward_failures_use_a_separate_ops_note(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "FORWARD_FAILURES_BEFORE_NOTE", 2)
    _fake_collect(monkeypatch, 0)
    monkeypatch.setattr(release_forwarding, "forward_release_events", lambda: "retryable")
    dms = []
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *args, **kwargs: dms.append(args) or True)
    scheduler.collect()
    assert dms == []
    scheduler.collect()
    assert [item[0] for item in dms] == ["forward_release_events"]
    assert all(item[0] != "alert:collect" for item in dms)


def test_autopublish_waits_for_a_running_collect(monkeypatch):
    """A collect that runs past :05 must not overlap autopublish's backlog write."""
    import threading

    collect_started = threading.Event()
    release_collect = threading.Event()
    order = []

    def _run(cmd, cwd=None, check=False, **kwargs):
        sub = cmd[2]
        order.append(f"start {sub}")
        if sub == "collect":
            collect_started.set()
            release_collect.wait(5)
        order.append(f"end {sub}")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(scheduler.subprocess, "run", _run)
    monkeypatch.setattr(release_forwarding, "forward_release_events", lambda: "disabled")
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *a, **k: None)

    collect = threading.Thread(target=scheduler.collect)
    collect.start()
    assert collect_started.wait(5)
    publish = threading.Thread(target=scheduler.autopublish)
    publish.start()
    publish.join(0.2)
    assert publish.is_alive()
    release_collect.set()
    collect.join(5)
    publish.join(5)
    assert order == ["start collect", "end collect", "start autopublish", "end autopublish"]
