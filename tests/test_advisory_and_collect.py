"""Advisory monitor, ecosystem-release handoff, and collect isolation."""

import importlib.util
import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adv = _load("claw_advisory_monitor", "claw-advisory-monitor.py")
hf = _load("claw_hf_papers", "claw-hf-papers.py")

ROOT = Path(__file__).resolve().parent.parent


def _adv(gid, package, summary="auth bypass"):
    return {
        "ghsa_id": gid,
        "summary": summary,
        "html_url": f"https://github.com/advisories/{gid}",
        "severity": "high",
        "published_at": "2026-09-23T00:00:00Z",
        "vulnerabilities": [{"package": {"ecosystem": "pip", "name": package}}],
    }


def test_advisory_baselines_then_emits_allowlisted_only():
    state = {"baselined": False, "seenGhsaIds": []}
    seeded = [
        _adv("GHSA-aaaa-bbbb-cccc", "langchain"),
        _adv("GHSA-dddd-eeee-ffff", "numpy"),
    ]
    assert adv.select_advisories(seeded, state, "2026-09-23T00:00:00+00:00") == []
    assert state["baselined"] is True
    fresh = [
        _adv("GHSA-1111-2222-3333", "langchain", "langchain auth bypass"),
        _adv("GHSA-4444-5555-6666", "numpy", "unrelated"),
        _adv("GHSA-7777-8888-9999", "@scope/claude-code", "claude-code sandbox escape"),
    ]
    items = adv.select_advisories(seeded + fresh, state, "2026-09-23T01:00:00+00:00")
    assert [item["id"] for item in items] == ["GHSA-1111-2222-3333", "GHSA-7777-8888-9999"]
    assert all(item["url"].startswith("https://github.com/advisories/GHSA-") for item in items)
    assert "GHSA-4444-5555-6666" in state["seenGhsaIds"]
    # Third matching advisory the same day is held, not swallowed.
    held = _adv("GHSA-aaaa-bbbb-dddd", "mcp", "mcp token leak")
    assert adv.select_advisories([held], state, "2026-09-23T02:00:00+00:00") == []
    assert "GHSA-aaaa-bbbb-dddd" not in state["seenGhsaIds"]
    next_day = adv.select_advisories([held], state, "2026-09-24T00:00:00+00:00")
    assert [item["id"] for item in next_day] == ["GHSA-aaaa-bbbb-dddd"]


def test_classify_advisory_is_watch_above_morning_bar():
    candidate = ct.classify_advisory({
        "id": "GHSA-1111-2222-3333",
        "package": "langchain",
        "title": "langchain auth bypass",
        "url": "https://github.com/advisories/GHSA-1111-2222-3333",
        "summary": "high advisory affecting langchain",
        "found_at": datetime.now(timezone.utc).isoformat(),
    })
    assert candidate is not None
    assert candidate["primaryCategory"] == "watch"
    assert candidate["score"] >= 25
    assert "ecosystem=pip" not in adv.ADVISORY_URL


def test_hn_how_to_stays_community():
    candidate = ct.classify_hackernews({
        "url": "https://news.ycombinator.com/item?id=1",
        "title": "How to build your first MCP server",
        "score": 40,
        "comments": 20,
        "found_at": datetime.now(timezone.utc).isoformat(),
    })
    assert candidate is not None
    assert candidate["primaryCategory"] == "community"


def test_paper_rl_keyword_is_a_word():
    early, _text = hf.score_paper({
        "title": "Early World Models for Video",
        "summary": "see the url",
        "ai_keywords": [],
    })
    assert early == 0
    rlhf, _text = hf.score_paper({
        "title": "RLHF for tool-using agents",
        "summary": "",
        "ai_keywords": [],
    })
    assert rlhf >= 2


def test_load_json_treats_torn_file_as_default(tmp_path):
    path = tmp_path / "torn.json"
    path.write_text('{"items": [')
    assert ct.load_json(path, {"items": []}) == {"items": []}


def test_collect_skips_one_bad_item(monkeypatch):
    def classify(kind, item):
        if item.get("title") == "bad":
            raise TypeError("can't subtract offset-naive and offset-aware datetimes")
        now = datetime.now(timezone.utc)
        return {
            "primaryCategory": "read",
            "categories": ["read"],
            "score": 40,
            "summary": "ok",
            "expiresAt": now + timedelta(hours=10),
            "publishedAt": now,
            "sourceType": "rss",
            "sourceName": "t",
            "sourceId": item["id"],
            "url": item["url"],
            "title": item["title"],
        }

    monkeypatch.setattr(ct, "collect_candidates", lambda: {
        "rss": [
            {"title": "bad", "id": "1", "url": "https://example.com/bad"},
            {"title": "good", "id": "2", "url": "https://example.com/good"},
        ]
    })
    monkeypatch.setattr(ct, "classify_source_candidate", classify)
    result = ct.collect_into_backlog()
    assert result["added"] == 1
    assert result["items"][0]["title"] == "good"


def test_ecosystem_release_classifies_and_baseline_is_silent(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "claw-ecosystem-sources.json").write_text(json.dumps({
        "curated": [{"repo": "acme/widget"}],
        "dynamic": [],
        "_meta": {},
    }))
    (memory / "claw-ecosystem-state.json").write_text(json.dumps({
        "lastCheck": None,
        "lastSeenReleases": {},
        "lastSeenHNStories": [],
        "lastSeenSkills": [],
    }))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = tmp_path / "release.json"
    body.write_text(json.dumps([{
        "tag_name": "v1.0.0",
        "name": "v1.0.0",
        "html_url": "https://github.com/acme/widget/releases/tag/v1.0.0",
        "published_at": "2026-09-23T00:00:00Z",
        "body": "first",
    }]))
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "url=\"\"\n"
        "for arg in \"$@\"; do url=\"$arg\"; done\n"
        "case \"$url\" in\n"
        "  *api.github.com/repos/acme/widget/releases*) cat \"$RELEASE_BODY_FILE\" ;;\n"
        "  *) echo '{\"hits\":[]}' ;;\n"
        "esac\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update({
        "CLAWBYTES_MEMORY_DIR": str(memory),
        "CLAWBYTES_ECOSYSTEM_RELEASES_ONLY": "1",
        "RELEASE_BODY_FILE": str(body),
        "GITHUB_TOKEN": "",
        "PATH": f"{bindir}:{env.get('PATH', '')}",
    })
    script = str(ROOT / "scripts" / "claw-ecosystem-monitor.sh")

    def run():
        proc = subprocess.run(
            ["bash", script, "--mode", "check"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads((memory / "claw-ecosystem-new-items.json").read_text())

    first = run()
    assert first["newReleases"] == []
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    assert state["lastSeenReleases"]["acme/widget"] == "v1.0.0"

    body.write_text(json.dumps([{
        "tag_name": "v2.0.0",
        "name": "v2.0.0 for developers",
        "html_url": "https://github.com/acme/widget/releases/tag/v2.0.0",
        "published_at": "2026-09-23T12:00:00Z",
        "body": "second",
    }]))
    second = run()
    assert len(second["newReleases"]) == 1
    assert second["newReleases"][0]["tag"] == "v2.0.0"
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    assert state["lastSeenReleases"]["acme/widget"] == "v1.0.0"

    candidate = ct.classify_ecosystem_release(second["newReleases"][0])
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["url"].endswith("/v2.0.0")

    monkeypatch.setattr(ct, "MEMORY", memory)
    ct._mark_ecosystem_releases_seen(second["newReleases"])
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    assert state["lastSeenReleases"]["acme/widget"] == "v2.0.0"
    assert "lastSeenHNStories" in state

    third = run()
    assert third["newReleases"] == []
