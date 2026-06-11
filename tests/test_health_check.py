import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scheduler  # noqa: E402


NOW = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)


def _write_state(tmp_path, collected_at, posted_at):
    state = {
        "lastCollectedAt": collected_at,
        "publishLog": [{"category": "ship", "at": posted_at, "day": "2026-06-11", "count": 4}] if posted_at else [],
    }
    (tmp_path / "clawbytes-thread-state.json").write_text(json.dumps(state))


def test_healthy_state_reports_no_issues(tmp_path):
    _write_state(tmp_path,
                 (NOW - timedelta(minutes=20)).isoformat(),
                 (NOW - timedelta(hours=3)).isoformat())
    assert scheduler.health_issues(tmp_path, now=NOW) == []


def test_stalled_collect_is_an_issue(tmp_path):
    _write_state(tmp_path,
                 (NOW - timedelta(hours=5)).isoformat(),
                 (NOW - timedelta(hours=3)).isoformat())
    issues = scheduler.health_issues(tmp_path, now=NOW)
    assert len(issues) == 1 and "collect" in issues[0]


def test_silent_channel_is_an_issue(tmp_path):
    _write_state(tmp_path,
                 (NOW - timedelta(minutes=20)).isoformat(),
                 (NOW - timedelta(hours=40)).isoformat())
    issues = scheduler.health_issues(tmp_path, now=NOW)
    assert len(issues) == 1 and "silent" in issues[0]


def test_no_posts_ever_is_an_issue(tmp_path):
    _write_state(tmp_path, (NOW - timedelta(minutes=20)).isoformat(), None)
    issues = scheduler.health_issues(tmp_path, now=NOW)
    assert any("silent" in issue for issue in issues)


def test_unreadable_state_is_an_issue(tmp_path):
    issues = scheduler.health_issues(tmp_path, now=NOW)
    assert issues and "state" in issues[0]
