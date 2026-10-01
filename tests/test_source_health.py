"""Per-source health lines, the 24h alert, and a lazy JSON record."""
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clawbytes_threads as ct
import scheduler
import source_health as sh

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TOKEN = "ghp_" + "b" * 36
ENV_SECRET = "super-secret-token-value"


def _seed(memory: Path, source: str, *, empties=0, failures=0, since=None, last_ok=None, last_alert=None):
    path = memory / sh.HEALTH_FILENAME
    sh.save_health(path, {
        "sources": {
            source: {
                "lastOkAt": last_ok.isoformat() if last_ok else None,
                "consecutiveFailures": failures,
                "consecutiveEmpties": empties,
                "unhealthySince": since.isoformat() if since else None,
                "lastAlertAt": last_alert.isoformat() if last_alert else None,
            }
        }
    })
    return path


def _rec(memory: Path, source: str) -> dict:
    data = json.loads((memory / sh.HEALTH_FILENAME).read_text())
    return data["sources"][source]


def _lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("source_health ")]


def test_logs_one_line_and_redacts_truncated_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", ENV_SECRET)
    stderr = f"HTTP 403: Bearer {TOKEN} {ENV_SECRET} " + ("E" * 400)
    status, items, error = sh.outcome_from_process(
        returncode=1,
        stdout="Found 0 quality posts\n",
        stderr=stderr,
    )
    assert status == "error"
    assert items == 0
    assert TOKEN not in error
    assert ENV_SECRET not in error
    assert "<redacted>" in error
    assert len(error) <= sh.REASON_LIMIT

    monkeypatch.setattr(sh, "admin_channel_configured", lambda: False)
    sh.record_source_health(
        "leaderboard",
        status=status,
        items=items,
        error=error,
        memory_dir=tmp_path,
        now=NOW,
        alert_sender=lambda text: True,
    )
    lines = _lines(capsys)
    assert lines == [f"source_health source=leaderboard status=error items=0 error={error}"]
    assert TOKEN not in lines[0]
    assert ENV_SECRET not in lines[0]
    assert len(lines[0].split(" error=", 1)[1]) <= sh.REASON_LIMIT


def test_zero_items_with_http_error_is_empty_and_counts_successes():
    status, items, error = sh.outcome_from_process(
        returncode=0,
        stdout="  HTTP 403: https://www.reddit.com/r/openclaw/hot.json\nFound 0 quality posts\n",
        stderr="",
    )
    assert (status, items) == ("empty", 0)
    assert "HTTP 403" in error
    assert len(error) <= sh.REASON_LIMIT

    status, items, error = sh.outcome_from_process(
        returncode=0,
        stdout="Found 4 new relevant items\n",
        stderr="",
    )
    assert (status, items, error) == ("ok", 4, "-")

    status, items, error = sh.outcome_from_process(
        returncode=0,
        stdout="",
        stderr=(
            "   Found 2 new release(s)\n"
            "   Found 0 new story/stories\n"
            "   Found 1 new paper(s)\n"
            "   Found 0 new skill item(s)\n"
        ),
    )
    assert (status, items) == ("ok", 3)

    status, items, _ = sh.outcome_from_process(
        returncode=0,
        stdout="",
        stderr="   Found 2 new project(s)\n",
    )
    assert (status, items) == ("ok", 2)

    status, items, error = sh.outcome_from_process(
        returncode=None,
        stdout="",
        stderr="HTTP 504: upstream",
        timed_out=True,
    )
    assert status == "error"
    assert error.startswith("timed out after 300s")
    assert "HTTP 504" in error


def test_no_alert_before_24h(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    sent = []
    _seed(
        tmp_path,
        "leaderboard",
        empties=4,
        failures=2,
        since=NOW - timedelta(hours=23, minutes=59),
    )
    sh.record_source_health(
        "leaderboard",
        status="empty",
        items=0,
        error="HTTP 403: denied",
        memory_dir=tmp_path,
        now=NOW,
        alert_sender=lambda text: sent.append(text) or True,
    )
    assert sent == []
    rec = _rec(tmp_path, "leaderboard")
    assert rec["consecutiveEmpties"] == 5
    assert rec["consecutiveFailures"] == 0
    assert rec["lastAlertAt"] is None
    assert rec["lastOkAt"] is None
    assert _lines(capsys)[0].startswith("source_health source=leaderboard status=empty items=0 ")


def test_alert_at_24h_deduped_until_next_day(monkeypatch, tmp_path, capsys, caplog):
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    sent = []
    _seed(tmp_path, "leaderboard", empties=3, since=NOW - timedelta(hours=24))

    def _send(text):
        sent.append(text)
        return True

    with caplog.at_level(logging.WARNING, logger="clawbytes.source_health"):
        sh.record_source_health(
            "leaderboard", status="empty", items=0, error="HTTP 403: denied",
            memory_dir=tmp_path, now=NOW, alert_sender=_send,
        )
        sh.record_source_health(
            "leaderboard", status="error", items=0, error="exit 1: HTTP 403: denied",
            memory_dir=tmp_path, now=NOW + timedelta(hours=1), alert_sender=_send,
        )
        sh.record_source_health(
            "hn", status="error", items=0, error="timed out after 300s",
            memory_dir=tmp_path, now=NOW, alert_sender=_send,
        )
    assert len(sent) == 1
    assert "leaderboard" in sent[0]
    assert "24h" in sent[0]
    assert ENV_SECRET not in sent[0]
    assert not any(r.message.startswith("source_health ALERT") for r in caplog.records)
    rec = _rec(tmp_path, "leaderboard")
    assert rec["lastAlertAt"] == NOW.isoformat()
    assert rec["consecutiveFailures"] == 1
    assert rec["consecutiveEmpties"] == 0
    assert rec["unhealthySince"] == (NOW - timedelta(hours=24)).isoformat()

    capsys.readouterr()
    sh.record_source_health(
        "leaderboard", status="empty", items=0, error="HTTP 403: denied",
        memory_dir=tmp_path, now=NOW + timedelta(hours=24), alert_sender=_send,
    )
    assert len(sent) == 2


def test_undelivered_alert_is_not_deduped(monkeypatch, tmp_path, caplog, capsys):
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    calls = {"n": 0}

    def _fail(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("telegram down")
        return False

    _seed(tmp_path, "bsky", failures=1, since=NOW - timedelta(hours=30))
    with caplog.at_level(logging.WARNING, logger="clawbytes.source_health"):
        sh.record_source_health(
            "bsky", status="error", items=0, error="exit 1",
            memory_dir=tmp_path, now=NOW, alert_sender=_fail,
        )
        sh.record_source_health(
            "bsky", status="error", items=0, error="exit 1",
            memory_dir=tmp_path, now=NOW + timedelta(minutes=30), alert_sender=_fail,
        )
    assert calls["n"] == 2
    assert _rec(tmp_path, "bsky")["lastAlertAt"] is None
    warns = [r.message for r in caplog.records if r.message.startswith("source_health ALERT")]
    assert len(warns) == 2
    assert all("source=bsky" in msg for msg in warns)
    capsys.readouterr()


def test_no_admin_channel_logs_warn_once_per_24h(monkeypatch, tmp_path, caplog, capsys):
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: False)
    sent = []
    _seed(tmp_path, "leaderboard", empties=1, since=NOW - timedelta(hours=25))
    with caplog.at_level(logging.WARNING, logger="clawbytes.source_health"):
        sh.record_source_health(
            "leaderboard", status="empty", items=0, error="HTTP 403: denied",
            memory_dir=tmp_path, now=NOW, alert_sender=lambda text: sent.append(text) or True,
        )
        first = [r.message for r in caplog.records if r.message.startswith("source_health ALERT")]
        caplog.clear()
        sh.record_source_health(
            "leaderboard", status="empty", items=0, error="HTTP 403: denied",
            memory_dir=tmp_path, now=NOW + timedelta(hours=1),
            alert_sender=lambda text: sent.append(text) or True,
        )
        second = [r.message for r in caplog.records if r.message.startswith("source_health ALERT")]
    assert sent == []
    assert len(first) == 1
    assert "source=leaderboard" in first[0]
    assert "status=empty" in first[0]
    assert second == []
    assert _rec(tmp_path, "leaderboard")["lastAlertAt"] == NOW.isoformat()
    capsys.readouterr()


def test_missing_health_file_is_created_lazily(tmp_path, capsys):
    path = tmp_path / sh.HEALTH_FILENAME
    assert not path.exists()
    assert sh.load_health(path) == {"sources": {}}
    assert not path.exists()
    sh.record_source_health(
        "rss", status="ok", items=3, error="-",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: True,
    )
    rec = _rec(tmp_path, "rss")
    assert rec["lastOkAt"] == NOW.isoformat()
    assert rec["consecutiveFailures"] == 0
    assert rec["consecutiveEmpties"] == 0
    assert rec["unhealthySince"] is None
    assert _lines(capsys) == ["source_health source=rss status=ok items=3 error=-"]


def test_corrupt_health_file_is_tolerated(tmp_path, capsys):
    path = tmp_path / sh.HEALTH_FILENAME
    path.write_text("{this is not json")
    sh.record_source_health(
        "hn", status="error", items=0, error="exit 1: boom",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: False,
    )
    rec = _rec(tmp_path, "hn")
    assert rec["consecutiveFailures"] == 1
    assert rec["consecutiveEmpties"] == 0
    assert rec["lastOkAt"] is None

    path.write_text("[]")
    sh.record_source_health(
        "hn", status="empty", items=0, error="-",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: False,
    )
    rec = _rec(tmp_path, "hn")
    assert rec["consecutiveEmpties"] == 1
    assert rec["consecutiveFailures"] == 0

    path.write_text('{"sources": "nope"}')
    sh.record_source_health(
        "moltbook", status="ok", items=1, error="-",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: False,
    )
    assert _rec(tmp_path, "moltbook")["lastOkAt"] == NOW.isoformat()
    capsys.readouterr()


def test_ok_clears_empty_and_failure_streak(tmp_path, capsys):
    _seed(
        tmp_path,
        "leaderboard",
        empties=9,
        failures=4,
        since=NOW - timedelta(hours=48),
        last_ok=NOW - timedelta(days=3),
        last_alert=NOW - timedelta(hours=2),
    )
    sent = []
    sh.record_source_health(
        "leaderboard", status="ok", items=2, error="-",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: sent.append(text) or True,
    )
    rec = _rec(tmp_path, "leaderboard")
    assert sent == []
    assert rec["consecutiveEmpties"] == 0
    assert rec["consecutiveFailures"] == 0
    assert rec["unhealthySince"] is None
    assert rec["lastOkAt"] == NOW.isoformat()
    capsys.readouterr()


def test_clean_empty_on_diff_source_is_healthy_and_never_alerts(monkeypatch, tmp_path, capsys):
    """Zero new items is the resting state of a diff-style source."""
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    for source in sorted(sh.EMPTY_IS_HEALTHY):
        sent = []
        _seed(tmp_path, source, empties=48, failures=2, since=NOW - timedelta(hours=30))
        sh.record_source_health(
            source, status="empty", items=0, error="-",
            memory_dir=tmp_path, now=NOW, alert_sender=lambda text: sent.append(text) or True,
        )
        rec = _rec(tmp_path, source)
        assert sent == [], source
        assert rec["unhealthySince"] is None
        assert rec["consecutiveFailures"] == 0
        assert rec["consecutiveEmpties"] == 49
        assert rec["lastAlertAt"] is None
    assert {"leaderboard", "registry", "pagewatch", "advisory"} <= sh.EMPTY_IS_HEALTHY
    capsys.readouterr()


def test_clean_empty_on_feed_source_still_alerts_at_24h(monkeypatch, tmp_path, capsys):
    """A steady feed going silent for a day is still worth a page."""
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    sent = []
    _seed(tmp_path, "bsky", empties=48, since=NOW - timedelta(hours=24))
    sh.record_source_health(
        "bsky", status="empty", items=0, error="-",
        memory_dir=tmp_path, now=NOW, alert_sender=lambda text: sent.append(text) or True,
    )
    assert len(sent) == 1
    assert "bsky" in sent[0] and "no detail" in sent[0]
    assert _rec(tmp_path, "bsky")["unhealthySince"] == (NOW - timedelta(hours=24)).isoformat()
    capsys.readouterr()


def test_admin_channel_uses_existing_env_only(monkeypatch):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "CLAWBYTES_ADMIN_CHAT_ID",
        "SLACK_BOT_TOKEN",
        "CLAWBYTES_OPS_SLACK_CHANNEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    assert sh.admin_channel_configured() is False
    monkeypatch.setenv("CLAWBYTES_ADMIN_CHAT_ID", "123")
    assert sh.admin_channel_configured() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert sh.admin_channel_configured() is True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("CLAWBYTES_ADMIN_CHAT_ID")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("CLAWBYTES_OPS_SLACK_CHANNEL_ID", "C123")
    assert sh.admin_channel_configured() is True


def test_run_monitors_logs_one_line_per_source(monkeypatch, capsys):
    token = "ghp_" + "c" * 36
    scripts = {
        "claw-rss-monitor.py": (0, "Found 4 new relevant items\n", ""),
        "claw-hn-monitor.py": (0, "Found 3 new HN items\n", ""),
        "claw-moltbook-monitor.py": (0, "Found 1 new relevant items\n", ""),
        "claw-leaderboard-monitor.py": (0, "Leaderboards: 0 new movement item(s)\n", ""),
        "claw-registry-monitor.py": (0, "Registries: 2 new item(s)\n", ""),
        "claw-pagewatch-monitor.py": (0, "Pagewatch: 0 new item(s)\n", ""),
        "claw-bsky-monitor.py": (0, "Bluesky: 1 new item(s)\n", ""),
        "claw-advisory-monitor.py": (1, "", f"HTTP 500: Bearer {token} " + ("Z" * 400)),
        "claw-ecosystem-monitor.sh": (
            0,
            "",
            "   Found 2 new release(s)\n   Found 1 new story/stories\n"
            "   Found 0 new paper(s)\n   Found 0 new skill item(s)\n",
        ),
    }
    ran = []

    class _P:
        def __init__(self, code, stdout, stderr):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        assert kwargs.get("capture_output") is True
        script = Path(cmd[1]).name
        code, stdout, stderr = scripts[script]
        return _P(code, stdout, stderr)

    alerts = []
    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    monkeypatch.setattr(ct, "_source_health_alert", lambda text: alerts.append(text) or True)
    ct.run_monitors()
    assert len(ran) == 9
    assert not any("claw-reddit-monitor.py" in " ".join(cmd) for cmd in ran)
    lines = _lines(capsys)
    assert len(lines) == 9
    by_source = {}
    for line in lines:
        name = line.split()[1].split("=", 1)[1]
        assert name not in by_source
        by_source[name] = line
    assert set(by_source) == {
        "rss", "hn", "moltbook", "leaderboard",
        "registry", "pagewatch", "bsky", "advisory", "ecosystem",
    }
    assert "reddit" not in by_source
    assert by_source["rss"] == "source_health source=rss status=ok items=4 error=-"
    assert by_source["ecosystem"] == "source_health source=ecosystem status=ok items=3 error=-"
    assert "status=error" in by_source["advisory"] and "items=0" in by_source["advisory"]
    assert token not in "\n".join(lines)
    assert "<redacted>" in by_source["advisory"]
    assert len(by_source["advisory"].split(" error=", 1)[1]) <= sh.REASON_LIMIT
    assert alerts == []
    stored = json.loads((ct.MEMORY / sh.HEALTH_FILENAME).read_text())
    assert "reddit" not in stored["sources"]
    assert stored["sources"]["rss"]["lastOkAt"]
    assert stored["sources"]["rss"]["consecutiveFailures"] == 0


def test_discover_logs_one_line_per_source_and_captures_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))
    dms = []

    def _fake_run(cmd, **kwargs):
        assert kwargs.get("capture_output") is True
        joined = " ".join(cmd)
        if "claw-ecosystem-monitor.sh" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"summary": {"discoveryCount": 1}}',
                stderr="   Found 1 new project(s)\n",
            )
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="HTTP 403: blocked " + ("Q" * 300),
        )

    monkeypatch.setattr(scheduler.subprocess, "run", _fake_run)
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *args, **kwargs: dms.append(args) or True)
    scheduler.discover()

    lines = _lines(capsys)
    assert len(lines) == 2
    by_source = {ln.split()[1].split("=", 1)[1]: ln for ln in lines}
    assert by_source["ecosystem-discover"] == (
        "source_health source=ecosystem-discover status=ok items=1 error=-"
    )
    assert "source=source-discovery" in by_source["source-discovery"]
    assert "status=error" in by_source["source-discovery"]
    assert "HTTP 403" in by_source["source-discovery"]
    assert len(by_source["source-discovery"].split(" error=", 1)[1]) <= sh.REASON_LIMIT
    # Nonzero discovery still pages immediately; the 24h source alert is separate.
    assert len(dms) == 1
    assert dms[0][0] == "alert:discover_feeds"
    stored = json.loads((tmp_path / sh.HEALTH_FILENAME).read_text())
    assert stored["sources"]["ecosystem-discover"]["lastOkAt"]
    assert stored["sources"]["source-discovery"]["consecutiveFailures"] == 1
    assert stored["sources"]["source-discovery"]["lastAlertAt"] is None


def test_reddit_streak_is_not_tracked_or_alerted(monkeypatch, tmp_path, capsys, caplog):
    """An existing reddit entry stays byte-for-byte and never pages."""
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    assert sh.reddit_fetch_enabled() is False
    sent = []
    _seed(tmp_path, "reddit", empties=9, since=NOW - timedelta(hours=48))
    before = (tmp_path / sh.HEALTH_FILENAME).read_bytes()
    with caplog.at_level(logging.WARNING, logger="clawbytes.source_health"):
        returned = sh.record_source_health(
            "reddit",
            status="empty",
            items=0,
            error="HTTP 403: denied",
            memory_dir=tmp_path,
            now=NOW,
            alert_sender=lambda text: sent.append(text) or True,
        )
    assert sent == []
    assert (tmp_path / sh.HEALTH_FILENAME).read_bytes() == before
    assert _lines(capsys) == []
    assert not any(r.message.startswith("source_health ALERT") for r in caplog.records)
    assert returned["consecutiveEmpties"] == 9
    assert returned["lastAlertAt"] is None


def test_reddit_health_record_is_not_created_when_absent(tmp_path, capsys):
    sh.record_source_health(
        "reddit",
        status="empty",
        items=0,
        error="HTTP 403: denied",
        memory_dir=tmp_path,
        now=NOW,
        alert_sender=lambda text: True,
    )
    assert not (tmp_path / sh.HEALTH_FILENAME).exists()
    assert _lines(capsys) == []


def test_other_source_update_leaves_reddit_streak_untouched(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    sent = []
    _seed(tmp_path, "reddit", empties=12, since=NOW - timedelta(hours=72))
    original = _rec(tmp_path, "reddit")
    sh.record_source_health(
        "rss",
        status="ok",
        items=4,
        error="-",
        memory_dir=tmp_path,
        now=NOW,
        alert_sender=lambda text: sent.append(text) or True,
    )
    assert sent == []
    assert _rec(tmp_path, "reddit") == original
    assert not any("source=reddit" in line for line in _lines(capsys))


def test_run_monitors_does_not_alert_on_existing_reddit_streak(monkeypatch, tmp_path, capsys, caplog):
    monkeypatch.setattr(ct, "MEMORY", tmp_path)
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: True)
    alerts = []
    _seed(tmp_path, "reddit", empties=20, since=NOW - timedelta(hours=48))
    original = _rec(tmp_path, "reddit")

    class _P:
        def __init__(self):
            self.returncode = 0
            self.stdout = "Found 1 new relevant items\n"
            self.stderr = ""

    monkeypatch.setattr(ct.subprocess, "run", lambda cmd, **kwargs: _P())
    monkeypatch.setattr(ct, "_source_health_alert", lambda text: alerts.append(text) or True)
    with caplog.at_level(logging.WARNING, logger="clawbytes.source_health"):
        ct.run_monitors()
    assert alerts == []
    assert not any(r.message.startswith("source_health ALERT") for r in caplog.records)
    assert _rec(tmp_path, "reddit") == original
    assert not any("source=reddit" in line for line in _lines(capsys))


def test_reddit_returns_to_health_and_collect_when_enabled(monkeypatch, tmp_path, capsys):
    mod = sh._reddit_monitor()
    monkeypatch.setattr(mod, "REDDIT_FETCH_ENABLED", True)
    monkeypatch.setattr(sh, "admin_channel_configured", lambda: False)
    sh.record_source_health(
        "reddit",
        status="ok",
        items=2,
        error="-",
        memory_dir=tmp_path,
        now=NOW,
        alert_sender=lambda text: True,
    )
    assert _lines(capsys) == ["source_health source=reddit status=ok items=2 error=-"]

    monkeypatch.setattr(ct, "MEMORY", tmp_path)
    ran = []

    class _P:
        def __init__(self):
            self.returncode = 0
            self.stdout = "Found 1 quality posts\n"
            self.stderr = ""

    def _fake_run(cmd, **kwargs):
        ran.append(cmd)
        return _P()

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()
    assert any("claw-reddit-monitor.py" in " ".join(cmd) for cmd in ran)
    assert len(ran) == 10
