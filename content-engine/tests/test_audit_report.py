import json
from pathlib import Path

from claw_content_engine import feed_reports


FIXTURE = {
    "memoryDir": "/data/memory",
    "lastCollectedAt": "2026-06-10T14:00:00+00:00",
    "rawItems": 120,
    "statusCounts": {"would_add": 12, "skipped": 80, "rejected": 28},
    "reasonCounts": {"classifier_rejected": 28, "seen_source_key": 70, "posted_url": 10},
    "sourceCounts": {"rss": 60, "reddit": 30, "ecosystem_hn": 20, "hf_papers": 10},
    "laneCounts": {"ship": 5, "watch": 2, "read": 4, "community": 8},
    "unconsumedStateFiles": [{"file": "claw-people-state.json", "items": 7}],
    "currentBundles": {},
    "items": [],
}


def test_audit_report_formats_counts(monkeypatch):
    monkeypatch.setattr(feed_reports, "_run", lambda cmd, cwd: json.dumps(FIXTURE))
    out = feed_reports.clawbytes_audit_report(Path("."))
    assert "ingestion audit" in out
    assert "would_add 12" in out
    assert "classifier_rejected 28" in out
    assert "rss 60" in out
    assert "claw-people-state.json (7)" in out


def test_audit_report_survives_bad_json(monkeypatch):
    monkeypatch.setattr(feed_reports, "_run", lambda cmd, cwd: "Traceback: boom")
    out = feed_reports.clawbytes_audit_report(Path("."))
    assert "audit failed" in out
