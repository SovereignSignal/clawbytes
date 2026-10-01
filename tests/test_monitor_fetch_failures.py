"""Diff-style monitors report a failed fetch on stderr and exit 1.

source_health treats a clean empty run from these sources as healthy, so a
failure has to be distinguishable from a quiet day: stderr even under
--quiet, and a nonzero exit. A run where every fetch works still exits 0.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import source_health as sh

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load("claw_registry_failures", "claw-registry-monitor.py")
lb = _load("claw_leaderboard_failures", "claw-leaderboard-monitor.py")
pw = _load("claw_pagewatch_failures", "claw-pagewatch-monitor.py")
adv = _load("claw_advisory_failures", "claw-advisory-monitor.py")
hn = _load("claw_hn_failures", "claw-hn-monitor.py")


def _bind(monkeypatch, tmp_path, mod):
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "FAILURES", [])
    monkeypatch.setattr(sys, "argv", ["monitor", "--quiet"])


def _boom(*args, **kwargs):
    raise OSError("HTTP Error 503: Service Unavailable")


def _assert_reported(capsys, code, needle):
    captured = capsys.readouterr()
    assert code == 1
    assert needle in captured.err
    status, _, error = sh.outcome_from_process(
        returncode=code, stdout=captured.out, stderr=captured.err,
    )
    assert status == "error"
    assert needle in error


def test_registry_failed_fetch_exits_1_and_keeps_other_registries(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, reg)

    def fake_fetch(url, headers=None, timeout=30):
        if "openrouter.ai" in url:
            raise OSError("HTTP Error 503: Service Unavailable")
        if "api.github.com" in url:
            return {"sha": "abc"}
        if "huggingface.co" in url:
            return []
        return {"model-a": {}}

    monkeypatch.setattr(reg, "_fetch_json", fake_fetch)
    code = reg.main()
    _assert_reported(capsys, code, "OpenRouter fetch failed")
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["litellmKeys"] == ["model-a"]


def test_registry_clean_run_exits_0(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, reg)

    def fake_fetch(url, headers=None, timeout=30):
        if "openrouter.ai" in url:
            return {"data": [{"id": "org/model"}]}
        if "api.github.com" in url:
            return {"sha": "abc"}
        if "huggingface.co" in url:
            return []
        return {"model-a": {}}

    monkeypatch.setattr(reg, "_fetch_json", fake_fetch)
    assert reg.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert sh.outcome_from_process(returncode=0, stdout=captured.out, stderr="") == ("empty", 0, "-")


def test_leaderboard_failed_fetch_exits_1(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, lb)
    board = next(b for b in lb.BOARDS if b.get("parser") == "terminal_bench")
    monkeypatch.setattr(lb, "BOARDS", [board])
    monkeypatch.setattr(lb, "github_contents", lambda *a, **k: None)
    _assert_reported(capsys, lb.main(), "could not list submissions")


def test_leaderboard_unparseable_board_exits_1(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, lb)
    board = next(b for b in lb.BOARDS if b.get("parser") == "terminal_bench")
    monkeypatch.setattr(lb, "BOARDS", [board])
    monkeypatch.setattr(lb, "github_contents", lambda *a, **k: [{"name": "a.json", "sha": "1"}])
    monkeypatch.setattr(lb, "fetch_raw", lambda *a, **k: "{}")
    _assert_reported(capsys, lb.main(), "no entries parsed")


def test_pagewatch_failed_fetch_exits_1(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, pw)
    monkeypatch.setattr(pw, "MD_WATCHES", pw.MD_WATCHES[:1])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", pw.SITEMAP_WATCHES[:1])
    monkeypatch.setattr(pw, "fetch_text", _boom)
    code = pw.main()
    err = capsys.readouterr().err
    assert code == 1
    assert err.count("fetch failed") == 2


def test_pagewatch_empty_sitemap_exits_1(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, pw)
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", pw.SITEMAP_WATCHES[:1])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: "<urlset></urlset>")
    _assert_reported(capsys, pw.main(), "no matching URLs parsed")


def test_advisory_failed_fetch_exits_1(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, adv)
    monkeypatch.setattr(adv, "fetch_advisories", _boom)
    _assert_reported(capsys, adv.main(), "advisories fetch failed")


def test_hn_failed_fetch_is_not_a_clean_empty_and_still_alerts(monkeypatch, tmp_path, capsys):
    """HN is in EMPTY_IS_HEALTHY because it matches a handful of stories a
    day. That is safe only while a failed search leaves a reason behind."""
    assert "hn" in sh.EMPTY_IS_HEALTHY
    monkeypatch.setattr(hn, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(hn, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(hn, "urlopen", _boom)
    monkeypatch.setattr(hn.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["monitor", "--quiet"])
    hn.main()
    captured = capsys.readouterr()
    status, items, error = sh.outcome_from_process(
        returncode=0, stdout=captured.out, stderr=captured.err,
    )
    assert (status, items) == ("empty", 0)
    assert "HN fetch error" in error

    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    sh.save_health(tmp_path / sh.HEALTH_FILENAME, {"sources": {"hn": {
        "lastOkAt": None, "consecutiveFailures": 0, "consecutiveEmpties": 48,
        "unhealthySince": (now - timedelta(hours=24)).isoformat(), "lastAlertAt": None,
    }}})
    sent = []
    sh.record_source_health(
        "hn", status=status, items=items, error=error,
        memory_dir=tmp_path, now=now, alert_sender=lambda text: sent.append(text) or True,
    )
    assert len(sent) == 1
    assert "HN fetch error" in sent[0]
    capsys.readouterr()


def test_advisory_clean_run_exits_0(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path, adv)
    monkeypatch.setattr(adv, "fetch_advisories", lambda: [])
    assert adv.main() == 0
    assert capsys.readouterr().err == ""
