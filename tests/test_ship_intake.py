"""Ship queue intake: per-source/per-run caps and low-signal filtering.

Existing queued rows are not rewritten. Caps and the patch filter apply only
to items collect is about to append.
"""

import json
from datetime import datetime, timedelta, timezone

import clawbytes_threads as ct


def _isolate(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(ct, "MEMORY", memory)
    monkeypatch.setattr(ct, "BACKLOG_FILE", memory / "clawbytes-backlog.json")
    monkeypatch.setattr(ct, "THREAD_STATE_FILE", memory / "clawbytes-thread-state.json")
    return memory


def _rss(feed, title, url, published=None):
    return {
        "feed": feed,
        "title": title,
        "link": url,
        "id": url,
        "published": published or datetime.now(timezone.utc).isoformat(),
    }


def _ship(title, source="Example Releases", source_type="rss"):
    return {
        "primaryCategory": "ship",
        "sourceType": source_type,
        "sourceName": source,
        "title": title,
    }


def _queued_ship(url, title, source="vercel-ai Releases"):
    now = datetime.now(timezone.utc)
    return {
        "id": ct.backlog_id(url, title),
        "url": url,
        "title": title,
        "summary": "already queued",
        "sourceType": "rss",
        "sourceName": source,
        "sourceId": url,
        "primaryCategory": "ship",
        "categories": ["ship"],
        "score": 42,
        "publishedAt": now.isoformat(),
        "discoveredAt": now.isoformat(),
        "expiresAt": (now + timedelta(days=6)).isoformat(),
        "status": "queued",
        "postedCategories": [],
    }


def _state(seen=None):
    return {
        "seenSourceKeys": list(seen or []),
        "postedBacklogIds": [],
        "postedUrls": [],
        "lastCollectedAt": None,
        "lastPublishedAt": {},
        "publishLog": [],
    }


def _collect(monkeypatch, items, kind="rss"):
    monkeypatch.setattr(ct, "collect_candidates", lambda: {kind: items})
    return ct.collect_into_backlog()


def _lines(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ship_intake ")]


def test_defaults_and_env_override(monkeypatch):
    assert ct.SHIP_INTAKE_PER_SOURCE == 2
    assert ct.SHIP_INTAKE_PER_RUN == 6
    monkeypatch.delenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", raising=False)
    monkeypatch.delenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", raising=False)
    assert ct.ship_intake_per_source() == 2
    assert ct.ship_intake_per_run() == 6
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", "1")
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "4")
    assert ct.ship_intake_per_source() == 1
    assert ct.ship_intake_per_run() == 4
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "nope")
    assert ct.ship_intake_per_run() == 6
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", "-2")
    assert ct.ship_intake_per_source() == 2
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "0")
    assert ct.ship_intake_per_run() == 0


def test_low_signal_reasons_cover_patch_prerelease_and_calver():
    assert ct.ship_low_signal_reason(_ship("v2.1.3")) == "patch"
    assert ct.ship_low_signal_reason(_ship("@ai-sdk/zai@3.0.23", "vercel-ai Releases")) == "patch"
    assert ct.ship_low_signal_reason(_ship("ai@5.0.52", "vercel-ai Releases")) == "patch"
    assert ct.ship_low_signal_reason(_ship("v0.0.10")) == "patch"
    assert ct.ship_low_signal_reason(_ship("build 20260413.04")) == "patch"
    assert ct.ship_low_signal_reason(_ship("v2.0.0-alpha.1")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("v1.4.0-rc.2")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("v1.4.0-beta.1")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("v1.4.0-dev.2")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("nightly build 2026-09-30")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("canary 1.2.0")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("Preview build 2026-09-16")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("dev build 1.2.0")) == "prerelease"
    assert ct.ship_low_signal_reason(_ship("v7.7.0 (pre-release)")) == "prerelease"

    assert ct.ship_low_signal_reason(_ship("v2.0.0")) is None
    assert ct.ship_low_signal_reason(_ship("v0.0.1")) is None
    assert ct.ship_low_signal_reason(_ship("v2.0.0 for developers")) is None
    assert ct.ship_low_signal_reason(_ship("v2.0.0 Adds preview of background agents")) is None
    assert ct.ship_low_signal_reason(_ship("v2.0.0 fixed streaming tool calls")) is None
    assert ct.ship_low_signal_reason(_ship("Alphabetical tool index v2.0.0")) is None
    assert ct.ship_low_signal_reason(_ship("Schema v1.20.0")) is None
    assert ct.ship_low_signal_reason(_ship("@ai-sdk/openai@2.0.0", "vercel-ai Releases")) is None
    assert ct.ship_low_signal_reason(_ship("openclaw 2026.9.7", "OpenClaw Releases")) is None
    assert ct.ship_low_signal_reason(_ship("OpenClaw 2026.9.7", "OpenClaw Releases")) is None
    assert ct.ship_low_signal_reason(
        _ship("SWE-bench Verified 1.2.3", "SWE-bench", "leaderboard")
    ) is None
    assert ct.ship_low_signal_reason(
        {"primaryCategory": "read", "sourceType": "rss", "sourceName": "x", "title": "v1.2.3"}
    ) is None


def test_registry_diff_is_low_signal_and_named_model_is_not():
    litellm = ct.classify_registry({
        "id": "LiteLLM registry:batch:1",
        "registry": "LiteLLM registry",
        "title": "12 new model(s) priced in the LiteLLM registry",
        "url": "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json#new-1",
        "summary": "azure/gpt-4o",
        "found_at": datetime.now(timezone.utc).isoformat(),
    })
    assert litellm["primaryCategory"] == "ship"
    assert ct.ship_low_signal_reason(litellm) == "registry_diff"

    openrouter = ct.classify_registry({
        "id": "OpenRouter:moonshotai/kimi-k2.7-code",
        "registry": "OpenRouter",
        "title": "Kimi K2.7 Code now live on OpenRouter",
        "url": "https://openrouter.ai/moonshotai/kimi-k2.7-code",
        "summary": "x",
        "found_at": datetime.now(timezone.utc).isoformat(),
    })
    assert ct.ship_low_signal_reason(openrouter) is None


def test_collect_keeps_patches_out_of_ship_and_logs_one_line(monkeypatch, tmp_path, capsys):
    memory = _isolate(monkeypatch, tmp_path)
    old = _queued_ship("https://example.com/already", "@ai-sdk/legacy@1.2.3")
    state = _state()
    ct.save_json(ct.BACKLOG_FILE, {"items": [json.loads(json.dumps(old))]})
    ct.save_json(ct.THREAD_STATE_FILE, state)
    before_state_keys = set(state)
    before_item_keys = set(old)

    items = [
        _rss("Claude Code Releases", "v2.0.0", "https://example.com/claude-minor"),
        _rss("Claude Code Releases", "v2.1.3", "https://example.com/claude-patch"),
        _rss("vercel-ai Releases", "@ai-sdk/zai@3.0.23", "https://example.com/sdk-patch"),
        _rss("vercel-ai Releases", "@ai-sdk/openai@2.0.0", "https://example.com/sdk-minor"),
        _rss("OpenClaw Releases", "openclaw 2026.9.7", "https://example.com/openclaw"),
        _rss("Cline Releases", "v2.0.0-alpha.1", "https://example.com/alpha"),
        _rss("Claude Code Releases", "v2.0.0 Adds preview of background agents", "https://example.com/preview-word"),
        _rss("Devin Release Notes", "v1.8.0-rc.1", "https://example.com/devin-rc"),
        _rss("fx Coding Agent Releases", "v0.0.10", "https://example.com/fx-patch"),
        _rss("fx Coding Agent Releases", "v0.1.0", "https://example.com/fx-minor"),
    ]
    result = _collect(monkeypatch, items)
    lines = _lines(capsys)
    assert len(lines) == 1
    assert "\n" not in lines[0]

    stored = json.loads(ct.BACKLOG_FILE.read_text())
    assert set(stored) == {"items"}
    by_url = {item["url"]: item for item in stored["items"]}
    kept = by_url[old["url"]]
    assert kept["primaryCategory"] == "ship"
    assert kept["status"] == "queued"
    assert kept["categories"] == ["ship"]
    assert kept["score"] == old["score"]
    assert kept["title"] == old["title"]
    assert set(kept) == before_item_keys

    ship_urls = {item["url"] for item in stored["items"] if "ship" in item["categories"]}
    assert "https://example.com/claude-patch" not in ship_urls
    assert "https://example.com/sdk-patch" not in ship_urls
    assert "https://example.com/alpha" not in ship_urls
    assert "https://example.com/devin-rc" not in ship_urls
    assert "https://example.com/fx-patch" not in ship_urls
    assert "https://example.com/claude-minor" in ship_urls
    assert "https://example.com/sdk-minor" in ship_urls
    assert "https://example.com/openclaw" in ship_urls
    assert "https://example.com/preview-word" in ship_urls
    assert "https://example.com/fx-minor" in ship_urls

    read_ceiling = ct.CATEGORY_META["read"]["min_top_score"][0] - 1
    for url in (
        "https://example.com/claude-patch",
        "https://example.com/sdk-patch",
        "https://example.com/devin-rc",
        "https://example.com/fx-patch",
    ):
        routed = by_url[url]
        assert routed["primaryCategory"] == "read"
        assert routed["categories"] == ["read"]
        assert routed["status"] == "queued"
        assert routed["score"] <= read_ceiling
        assert "low-signal" in routed["summary"]
    # Release-feed prereleases never become candidates, so they are not queued.
    assert "https://example.com/alpha" not in by_url
    assert result["shipIntake"]["filtered"] >= 4
    assert result["shipIntake"]["added"] == result["counts"]["ship"]
    assert f"filtered={result['shipIntake']['filtered']}" in lines[0]
    assert f"added={result['shipIntake']['added']}" in lines[0]
    assert "vercel-ai Releases=" in lines[0]
    assert "Claude Code Releases=" in lines[0]

    saved_state = json.loads(ct.THREAD_STATE_FILE.read_text())
    assert set(saved_state) == before_state_keys
    seen = set(saved_state["seenSourceKeys"])
    assert "rss:https://example.com/claude-patch" in seen
    assert "rss:https://example.com/sdk-patch" in seen
    # Release-feed prereleases are dropped by classify_rss and never become
    # an intake row, so they are not marked seen here.
    assert "rss:https://example.com/alpha" not in seen

    # A second collect must not re-count the same filtered rows.
    again = _collect(monkeypatch, items)
    assert _lines(capsys) == ["ship_intake added=0 capped=0 filtered=0 by_source=-"]
    assert again["shipIntake"]["filtered"] == 0
    assert old["url"] in {item["url"] for item in json.loads(ct.BACKLOG_FILE.read_text())["items"]}


def test_caps_defer_ship_items_until_a_later_collect(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", "1")
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "2")
    items = [
        _rss("OpenClaw Releases", "openclaw 2026.9.0", "https://example.com/oc"),
        _rss("Claude Code Releases", "v2.0.0", "https://example.com/cc"),
        _rss("Claude Code Releases", "v3.0.0", "https://example.com/cc2"),
        _rss("Aider Releases", "v0.85.0", "https://example.com/aider"),
        _rss("Herdr Releases", "v0.10.0", "https://example.com/herdr"),
    ]
    first = _collect(monkeypatch, items)
    line = _lines(capsys)[0]
    assert first["shipIntake"]["added"] == 2
    assert first["shipIntake"]["capped"] == 3
    assert "capped=3" in line
    stored = json.loads(ct.BACKLOG_FILE.read_text())
    assert len([i for i in stored["items"] if i["primaryCategory"] == "ship"]) == 2
    # Same source cannot take both slots: only one Claude Code release.
    claude = [i for i in stored["items"] if i["sourceName"] == "Claude Code Releases"]
    assert len(claude) == 1
    seen = set(json.loads(ct.THREAD_STATE_FILE.read_text())["seenSourceKeys"])
    assert "rss:https://example.com/aider" not in seen or "rss:https://example.com/herdr" not in seen
    capped_urls = {
        "https://example.com/cc",
        "https://example.com/cc2",
        "https://example.com/aider",
        "https://example.com/herdr",
        "https://example.com/oc",
    } - {i["url"] for i in stored["items"]}
    assert capped_urls
    for url in capped_urls:
        assert f"rss:{url}" not in seen

    second = _collect(monkeypatch, items)
    assert second["shipIntake"]["added"] == 2
    assert len(json.loads(ct.BACKLOG_FILE.read_text())["items"]) == 4
    _lines(capsys)


def test_read_items_ignore_ship_caps(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", "1")
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "1")
    items = [
        _rss("Simon Willison", f"Using Cursor for refactors {i}", f"https://example.com/read-{i}")
        for i in range(3)
    ]
    result = _collect(monkeypatch, items)
    assert result["added"] == 3
    assert result["counts"]["read"] == 3
    assert result["shipIntake"]["added"] == 0
    assert result["shipIntake"]["capped"] == 0
    assert _lines(capsys) == ["ship_intake added=0 capped=0 filtered=0 by_source=-"]


def test_audit_marks_new_patches_skipped_but_not_existing_backlog(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    already = _rss("vercel-ai Releases", "v9.9.9", "https://example.com/already")
    classified = ct.classify_rss(already)
    old = _queued_ship(classified["url"], classified["title"], classified["sourceName"])
    old["id"] = ct.backlog_id(classified["url"], classified["title"])
    ct.save_json(ct.BACKLOG_FILE, {"items": [old]})
    ct.save_json(ct.THREAD_STATE_FILE, _state())
    fresh = _rss("Claude Code Releases", "v2.1.4", "https://example.com/new-patch")
    minor = _rss("Claude Code Releases", "v2.0.0", "https://example.com/minor")
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"rss": [fresh, already, minor]})
    monkeypatch.setattr(ct, "unconsumed_state_report", lambda: [])
    report = ct.audit_sources(limit=10)
    by_title = {row.get("url") or row.get("rawTitle"): row for row in report["items"]}
    # audit rows are truncated by limit but raw counts use the full set.
    reasons = {row["url"]: row["reason"] for row in report["items"] if row.get("url")}
    # limit 10 keeps all three.
    assert reasons["https://example.com/new-patch"] == "ship_filtered:patch"
    assert by_title["https://example.com/new-patch"]["primaryCategory"] == "read"
    assert reasons["https://example.com/already"] == "already_in_backlog"
    assert by_title["https://example.com/already"]["primaryCategory"] == "ship"
    assert reasons["https://example.com/minor"] == "passes_classifier"
    assert by_title["https://example.com/minor"]["primaryCategory"] == "ship"


def test_ecosystem_filter_acks_and_cap_does_not(monkeypatch, tmp_path, capsys):
    memory = _isolate(monkeypatch, tmp_path)
    ct.save_json(memory / "claw-ecosystem-state.json", {
        "lastCheck": None,
        "lastSeenReleases": {},
        "lastSeenHNStories": [],
        "lastSeenSkills": [],
    })
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_SOURCE", "1")
    monkeypatch.setenv("CLAWBYTES_SHIP_INTAKE_PER_RUN", "1")
    now = datetime.now(timezone.utc).isoformat()
    releases = [
        {
            "repo": "acme/widget",
            "tag": "v1.2.3",
            "name": "v1.2.3",
            "url": "https://github.com/acme/widget/releases/tag/v1.2.3",
            "published": now,
        },
        {
            "repo": "acme/cli",
            "tag": "v2.0.0",
            "name": "v2.0.0",
            "url": "https://github.com/acme/cli/releases/tag/v2.0.0",
            "published": now,
        },
        {
            "repo": "acme/sdk",
            "tag": "v3.0.0",
            "name": "v3.0.0 for developers",
            "url": "https://github.com/acme/sdk/releases/tag/v3.0.0",
            "published": now,
        },
    ]
    monkeypatch.setattr(ct, "collect_candidates", lambda: {"ecosystem_release": releases})
    result = ct.collect_into_backlog()
    seen = json.loads((memory / "claw-ecosystem-state.json").read_text())["lastSeenReleases"]
    assert seen.get("acme/widget") == "v1.2.3"
    assert result["shipIntake"]["added"] == 1
    assert result["shipIntake"]["capped"] == 1
    assert result["shipIntake"]["filtered"] == 1
    admitted = "acme/cli" if seen.get("acme/cli") else "acme/sdk"
    deferred = "acme/sdk" if admitted == "acme/cli" else "acme/cli"
    assert seen.get(admitted)
    assert deferred not in seen
    assert len(_lines(capsys)) == 1


def test_registry_collect_filters_litellm_and_keeps_openrouter(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    items = [
        {
            "id": "LiteLLM registry:batch:abc",
            "registry": "LiteLLM registry",
            "title": "4 new model(s) priced in the LiteLLM registry",
            "url": "https://github.com/BerriAI/litellm#new-abc",
            "summary": "azure/gpt",
            "found_at": now,
        },
        {
            "id": "OpenRouter:moonshotai/kimi-k2.7-code",
            "registry": "OpenRouter",
            "title": "Kimi K2.7 Code now live on OpenRouter",
            "url": "https://openrouter.ai/moonshotai/kimi-k2.7-code",
            "summary": "x",
            "found_at": now,
        },
    ]
    result = _collect(monkeypatch, items, kind="registry")
    stored = {item["url"]: item for item in json.loads(ct.BACKLOG_FILE.read_text())["items"]}
    assert stored["https://openrouter.ai/moonshotai/kimi-k2.7-code"]["primaryCategory"] == "ship"
    litellm = stored["https://github.com/BerriAI/litellm#new-abc"]
    assert litellm["primaryCategory"] == "read"
    assert litellm["categories"] == ["read"]
    assert "ship" not in litellm["categories"]
    assert result["shipIntake"]["filtered"] == 1
    assert result["shipIntake"]["added"] == 1
    line = _lines(capsys)[0]
    assert "LiteLLM registry=0/0/1" in line
    assert "OpenRouter=1/0/0" in line
