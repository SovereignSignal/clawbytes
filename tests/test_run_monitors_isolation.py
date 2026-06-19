"""Tests for run_monitors isolation (Phase 2, Task 2.2).

Today run_monitors runs ~10 monitor subprocesses sequentially with a 300s
timeout each but NO per-monitor try/except: a single monitor timing out
(subprocess.TimeoutExpired) raises out and the rest of the batch in that
collect cycle never runs. One bad source starves all the others. These tests
pin the isolation fix (and that shell=True is dropped for the safer arg-list
form scheduler.py already uses).
"""
import subprocess

import pytest

import clawbytes_threads as ct


def _fake_proc(returncode=0, stderr=""):
    """A stand-in matching the attributes run_monitors reads off the result."""
    class _P:
        def __init__(self):
            self.returncode = returncode
            self.stderr = stderr
    return _P()


def test_run_monitors_runs_all_when_all_succeed(monkeypatch):
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        return _fake_proc(returncode=0)

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()
    assert len(ran) == 10  # every monitor in the batch ran


def test_run_monitors_continues_after_one_fails(monkeypatch):
    # THE BUG: a nonzero monitor used to print but continue; the real danger
    # is the timeout/exception path. This test pins the nonzero-isolation too.
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        # Fail the first one; succeed the rest.
        return _fake_proc(returncode=1 if len(ran) == 1 else 0, stderr="boom")

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()
    assert len(ran) == 10  # later monitors still ran


def test_run_monitors_survives_timeout(monkeypatch):
    # THE CORE BUG: a TimeoutExpired used to abort the whole batch.
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        if len(ran) == 3:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)
        return _fake_proc(returncode=0)

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()  # must not raise
    assert len(ran) == 10  # the timed-out monitor didn't starve the rest


def test_run_monitors_survives_unexpected_exception(monkeypatch):
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        if len(ran) == 5:
            raise RuntimeError("unexpected monitor crash")
        return _fake_proc(returncode=0)

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()  # must not raise
    assert len(ran) == 10


def test_run_monitors_does_not_use_shell(monkeypatch):
    """shell=True with interpolated cwd is a command-injection footgun and
    unnecessary; scheduler.py already uses cwd= + arg list. Pin that pattern."""
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["shell"] = kwargs.get("shell", "MISSING")
        seen["cwd"] = kwargs.get("cwd", "MISSING")
        return _fake_proc(returncode=0)

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()
    assert seen["shell"] in (False, None, "MISSING") and seen["shell"] is not True
