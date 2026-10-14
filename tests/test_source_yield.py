"""Weekly source-yield snapshot: compact JSON, no notify.

Ops DMs stay exception-only. This job writes memory/claw-source-yield.json
so the next coverage round has per-source would_add/rejected counts.
"""
from datetime import datetime, timezone

import clawbytes_threads as ct


def test_rollup_by_source_name_counts_status_and_lanes():
    rows = [
        {"sourceName": "Cursor Changelog", "sourceType": "rss", "status": "would_add", "primaryCategory": "ship"},
        {"sourceName": "Cursor Changelog", "sourceType": "rss", "status": "rejected", "primaryCategory": None},
        {"sourceName": "r/cursor", "sourceType": "reddit", "status": "skipped", "primaryCategory": "community"},
    ]
    rollup = ct.rollup_by_source_name(rows)
    assert rollup["Cursor Changelog"]["total"] == 2
    assert rollup["Cursor Changelog"]["would_add"] == 1
    assert rollup["Cursor Changelog"]["rejected"] == 1
    assert rollup["Cursor Changelog"]["lanes"]["ship"] == 1
    assert rollup["r/cursor"]["skipped"] == 1
    assert rollup["r/cursor"]["sourceType"] == "reddit"


def test_source_yield_snapshot_drops_item_payload():
    report = {
        "lastCollectedAt": "2026-08-20T15:00:00+00:00",
        "rawItems": 3,
        "statusCounts": {"would_add": 2, "rejected": 1},
        "reasonCounts": {"passes_classifier": 2},
        "sourceCounts": {"rss": 3},
        "laneCounts": {"ship": 2, "watch": 0, "read": 0, "community": 0},
        "bySourceName": {"Cursor Changelog": {"total": 3, "would_add": 2, "skipped": 0, "rejected": 1}},
        "unconsumedStateFiles": [],
        "currentBundles": {"ship": [{"title": "secret payload"}]},
        "items": [{"title": "do not persist me"}],
        "memoryDir": "/tmp/unused",
    }
    snap = ct.source_yield_snapshot(report, written_at="2026-08-20T15:45:00+00:00")
    assert snap["writtenAt"] == "2026-08-20T15:45:00+00:00"
    assert snap["rawItems"] == 3
    assert snap["bySourceName"]["Cursor Changelog"]["would_add"] == 2
    assert "items" not in snap
    assert "currentBundles" not in snap
    assert "memoryDir" not in snap


def test_audit_sources_rolls_up_by_source_name_before_limit(monkeypatch):
    items = []
    now = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        items.append({
            "feed": "Cursor Changelog",
            "title": f"Cursor harness change {i}",
            "link": f"https://cursor.com/changelog/yield-test-{i}",
            "published": now,
            "high_signal": True,
            "tags": ["coding-agent", "official"],
        })
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": items})
    monkeypatch.setattr(ct, "unconsumed_state_report", lambda: [])
    report = ct.audit_sources(limit=2)
    assert len(report["items"]) == 2
    assert report["rawItems"] == 5
    assert report["bySourceName"]["Cursor Changelog"]["total"] == 5
    assert report["bySourceName"]["Cursor Changelog"]["would_add"] == 5


def test_write_source_yield_keeps_eight_weeks_and_does_not_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(ct, "send_telegram", lambda *a, **k: sent.append("telegram"))
    monkeypatch.setattr(ct, "mirror_to_slack", lambda *a, **k: sent.append("slack"))

    def _report(i):
        return {
            "lastCollectedAt": f"2026-08-{i:02d}T15:00:00+00:00",
            "rawItems": i,
            "statusCounts": {"would_add": i},
            "reasonCounts": {},
            "sourceCounts": {"rss": i},
            "laneCounts": {"ship": i, "watch": 0, "read": 0, "community": 0},
            "bySourceName": {"Cursor Changelog": {"total": i, "would_add": i, "skipped": 0, "rejected": 0}},
            "unconsumedStateFiles": [],
            "items": [{"title": "payload"}],
            "currentBundles": {},
        }

    # First write: latest only, empty history.
    payload = ct.write_source_yield(_report(1), written_at="2026-08-01T15:45:00+00:00")
    assert payload["latest"]["rawItems"] == 1
    assert payload["history"] == []
    assert "items" not in payload["latest"]

    for i in range(2, 11):
        payload = ct.write_source_yield(_report(i), written_at=f"2026-08-{i:02d}T15:45:00+00:00")

    assert payload["latest"]["rawItems"] == 10
    assert len(payload["history"]) == 8
    assert [h["rawItems"] for h in payload["history"]] == list(range(2, 10))
    assert sent == []

    on_disk = ct.load_json(ct.SOURCE_YIELD_FILE, {})
    assert on_disk["latest"]["rawItems"] == 10
    assert len(on_disk["history"]) == 8


def test_yield_snapshot_prints_start_and_one_summary(capsys):
    def _write():
        return {
            "latest": {
                "rawItems": 10,
                "statusCounts": {"would_add": 2, "skipped": 5, "rejected": 3},
                "bySourceName": {
                    "Cursor Changelog": {"total": 6, "would_add": 2},
                    "r/cursor": {"total": 4, "skipped": 4},
                },
            }
        }

    ticks = iter([10.0, 11.0])
    code = ct.run_yield_snapshot(write=_write, clock=lambda: next(ticks))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert code == 0
    assert lines == [
        "yield-snapshot start",
        "yield-snapshot done raw=10 sources=2 would_add=2 skipped=5 rejected=3 duration=1.0s ok",
    ]


def test_yield_snapshot_failure_is_one_line(capsys):
    def _write():
        raise OSError("disk full")

    ticks = iter([0.0, 0.4])
    code = ct.run_yield_snapshot(write=_write, clock=lambda: next(ticks))
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines == [
        "yield-snapshot start",
        "yield-snapshot done duration=0.4s failed error=OSError",
    ]
    assert "disk full" not in lines[1]
