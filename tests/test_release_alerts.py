"""Immediate per-release alerts: gating, baseline, dedupe, retry, message."""
import json

import pytest

import release_alerts as ra

PATTERN = r"^(?:rust-)?v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$"
TARGETS = {
    "openai/codex": {"name": "Codex", "alert": True, "npm": "@openai/codex",
                     "tag_prefixes": ["rust-v", "v"], "prereleases": False, "tag_pattern": PATTERN},
    "openclaw/openclaw": {"name": "OpenClaw", "alert": True, "npm": "openclaw", "tag_prefixes": ["v"],
                          "prereleases": True, "tag_pattern": PATTERN,
                          "docker": "ghcr.io/openclaw/openclaw:{version}"},
    "cline/cline": {"name": "Cline", "forward": True, "prereleases": False, "tag_pattern": PATTERN},
    "off/repo": {"name": "Off", "forward": False, "legacy_owned": False},
}


def rel(tag, *, prerelease=False, name="", body="- Added a thing.", draft=False):
    return {"tag_name": tag, "name": name or tag, "prerelease": prerelease, "draft": draft, "body": body,
            "html_url": f"https://github.com/x/y/releases/tag/{tag}", "published_at": "2026-10-15T14:03:00Z"}


class World:
    """Fake GitHub/npm responses plus a recording Telegram."""

    def __init__(self, monkeypatch):
        self.gh = {}
        self.npm = {}
        self.urls = []
        self.sent = []
        self.send_ok = True
        monkeypatch.setattr(ra, "fetch_json", self.fetch)
        monkeypatch.setattr(ra, "send_alert", self.send)

    def fetch(self, url, timeout=ra.HTTP_TIMEOUT):
        self.urls.append(url)
        if url.startswith("https://api.github.com/repos/"):
            repo = url.split("/repos/")[1].split("/releases")[0]
            return self.gh.get(repo)
        if url.startswith("https://registry.npmjs.org/"):
            name = url.split(".org/")[1]
            if name.endswith("/latest"):
                version = self.npm.get(name[: -len("/latest")])
                return {"version": version} if version else None
            return {"time": {}}
        raise AssertionError(url)

    def send(self, message):
        self.sent.append(message)
        return self.send_ok


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_RELEASE_ALERTS", "1")
    monkeypatch.setenv("CLAWBYTES_RELEASE_ALERTS_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    w = World(monkeypatch)
    w.memory = tmp_path
    w.run = lambda **kw: ra.run_release_alerts(memory=tmp_path, targets=TARGETS, **kw)
    return w


def baseline(world):
    world.gh = {"openai/codex": [rel("rust-v0.47.0")], "openclaw/openclaw": [rel("v2026.8.1")],
                "cline/cline": [rel("v3.0.0")]}
    world.npm = {"@openai/codex": "0.47.0", "openclaw": "2026.7.9"}
    assert world.run().startswith("ok sent=0")
    assert world.sent == []


def state(world):
    return json.loads((world.memory / ra.STATE_NAME).read_text())["targets"]


def test_flag_off_makes_no_network_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAWBYTES_RELEASE_ALERTS", raising=False)
    monkeypatch.setenv("CLAWBYTES_RELEASE_ALERTS_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    w = World(monkeypatch)
    assert ra.run_release_alerts(memory=tmp_path, targets=TARGETS) == "disabled"
    assert w.urls == [] and w.sent == []
    assert not (tmp_path / ra.STATE_NAME).exists()


def test_missing_chat_id_is_off_and_never_uses_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_RELEASE_ALERTS", "yes")
    monkeypatch.delenv("CLAWBYTES_RELEASE_ALERTS_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@clawbytes")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    w = World(monkeypatch)
    assert ra.run_release_alerts(memory=tmp_path, targets=TARGETS) == "disabled"
    assert w.urls == []


def test_send_alert_targets_alerts_chat_without_slack(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_RELEASE_ALERTS_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@clawbytes")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("CLAWBYTES_SLACK_CHANNEL_ID", "C1")
    import ss_publish
    from ss_publish import TelegramResult

    made = []

    class FakePublisher:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def send_telegram(self, message):
            return TelegramResult(ok=True, message_id=1)

        def mirror_to_slack(self, message):
            raise AssertionError("alerts must not mirror to Slack")

    monkeypatch.setattr(ss_publish, "Publisher", FakePublisher)
    assert ra.send_alert("<b>Codex 1.0.0</b>") is True
    assert made[0]["telegram_channel_id"] == "-100123"
    assert made[0]["telegram_token"] == "tok"
    assert "slack_token" not in made[0]


def test_baseline_records_and_sends_nothing(world):
    baseline(world)
    saved = state(world)
    assert saved["openai/codex"]["github"] == "rust-v0.47.0"
    assert saved["openai/codex"]["npm"] == "0.47.0"
    assert "off/repo" not in saved
    assert not any("off/repo" in u for u in world.urls)


def test_new_github_release_alerts_once(world):
    baseline(world)
    world.gh["cline/cline"] = [rel("v3.1.0"), rel("v3.0.0")]
    assert world.run() == "ok sent=1 baselined=0"
    assert len(world.sent) == 1 and world.sent[0].startswith("<b>Cline 3.1.0</b>")
    world.run()
    assert len(world.sent) == 1


def test_npm_and_github_same_version_alert_once_codex_rust_tag(world):
    baseline(world)
    world.gh["openai/codex"] = [rel("rust-v0.48.0"), rel("rust-v0.47.0")]
    world.npm["@openai/codex"] = "0.48.0"
    world.run()
    assert len(world.sent) == 1
    assert world.sent[0].startswith("<b>Codex 0.48.0</b>")
    assert "rust-v" not in world.sent[0].split("\n")[0]
    saved = state(world)["openai/codex"]
    assert saved["github"] == "rust-v0.48.0" and saved["npm"] == "0.48.0"


def test_npm_later_catches_up_without_second_alert(world):
    baseline(world)
    world.gh["openai/codex"] = [rel("rust-v0.48.0")]
    world.run()
    world.npm["@openai/codex"] = "0.48.0"
    world.run()
    assert len(world.sent) == 1
    assert state(world)["openai/codex"]["npm"] == "0.48.0"


def test_npm_only_release_alerts_with_npm_link(world):
    baseline(world)
    world.npm["openclaw"] = "2026.7.10"
    world.run()
    assert len(world.sent) == 1
    assert "npmjs.com/package/openclaw/v/2026.7.10" in world.sent[0]


def test_prerelease_skipped_unless_target_allows(world):
    baseline(world)
    world.gh["openai/codex"] = [rel("rust-v0.48.0-alpha.1", prerelease=True), rel("rust-v0.47.0")]
    world.gh["openclaw/openclaw"] = [rel("v2026.8.2-beta.1", prerelease=True), rel("v2026.8.1")]
    world.run()
    assert len(world.sent) == 1
    assert world.sent[0].startswith("<b>OpenClaw 2026.8.2-beta.1</b> <i>(pre-release)</i>")
    assert "ghcr.io/openclaw/openclaw:2026.8.2-beta.1" in world.sent[0]


def test_inputs_and_unlisted_prefix_tags_skipped(world):
    baseline(world)
    world.gh["cline/cline"] = [rel("inputs-v3.2.0"), rel("nightly-3.2.0"), rel("v3.0.0")]
    world.gh["openai/codex"] = [rel("cli-0.49.0"), rel("rust-v0.47.0")]
    world.run()
    assert world.sent == []


def test_failed_send_does_not_record_and_retries(world):
    baseline(world)
    world.gh["cline/cline"] = [rel("v3.1.0")]
    world.send_ok = False
    assert world.run().startswith("send_failed")
    assert state(world)["cline/cline"]["github"] == "v3.0.0"
    world.send_ok = True
    world.run()
    assert len(world.sent) == 2
    assert state(world)["cline/cline"]["github"] == "v3.1.0"


def test_cap_limits_sends_and_rest_wait(world):
    baseline(world)
    world.gh["openai/codex"] = [rel("rust-v0.48.0")]
    world.gh["openclaw/openclaw"] = [rel("v2026.8.2")]
    world.gh["cline/cline"] = [rel("v3.1.0")]
    assert world.run(max_alerts=2).startswith("capped")
    assert len(world.sent) == 2
    assert state(world)["cline/cline"]["github"] == "v3.0.0"
    world.run(max_alerts=2)
    assert len(world.sent) == 3


def test_fetch_failure_does_not_baseline_that_source(world):
    world.gh = {}
    world.npm = {}
    world.run()
    assert not (world.memory / ra.STATE_NAME).exists()
    world.gh["cline/cline"] = [rel("v3.0.0")]
    world.run()
    assert world.sent == []
    assert state(world)["cline/cline"]["github"] == "v3.0.0"


def test_exception_never_raises(world, monkeypatch):
    monkeypatch.setattr(ra, "github_releases", lambda *a: 1 / 0)
    assert world.run() == "error"


def test_hermes_calendar_tag_and_semver_npm_dedupe(world):
    targets = {"NousResearch/hermes-agent": {"name": "Hermes Agent", "alert": True, "npm": "hermes-agent",
                                             "tag_prefixes": ["v"], "tag_pattern": PATTERN}}
    world.gh = {"NousResearch/hermes-agent": [rel("v2026.8.20", name="Hermes Agent v0.20.5 (v2026.8.20)")]}
    world.npm = {"hermes-agent": "0.20.5"}
    ra.run_release_alerts(memory=world.memory, targets=targets)
    world.gh["NousResearch/hermes-agent"] = [rel("v2026.8.27", name="Hermes Agent v0.20.6 (v2026.8.27)")]
    world.npm["hermes-agent"] = "0.20.6"
    ra.run_release_alerts(memory=world.memory, targets=targets)
    assert len(world.sent) == 1
    assert world.sent[0].startswith("<b>Hermes Agent 0.20.6</b>")


def test_message_format_and_length():
    body = "\n".join([
        "## What's Changed",
        "**Highlights**",
        "* feat(tui): Add `/fork` <command> & [docs](https://x.y/z) by @dev in https://github.com/o/r/pull/1",
        "- fix: Crash on **empty** config ([#12](https://github.com/o/r/pull/12))",
        "```", "- not a note", "```",
        "- Third item.",
        "- Fourth item.",
        "- Fifth item.",
        "**Full Changelog**: https://github.com/o/r/compare/a...b",
    ])
    msg = ra.build_message("Codex", "0.48.0", body=body, url="https://github.com/openai/codex/releases/tag/rust-v0.48.0",
                           published="2026-10-15T14:03:00Z")
    lines = msg.split("\n")
    assert lines[0] == "<b>Codex 0.48.0</b>"
    bullets = [line for line in lines if line.startswith("• ")]
    assert bullets == [
        "• Add /fork &lt;command&gt; &amp; docs",
        "• Crash on empty config",
        "• Third item.",
        "• Fourth item.",
    ]
    assert "Oct 15, 2026" in msg
    assert '🔗 <a href="https://github.com/openai/codex/releases/tag/rust-v0.48.0">Release notes</a>' in msg
    assert "Full Changelog" not in msg and "not a note" not in msg

    huge = ra.build_message("X", "1.0.0", body="\n".join("- " + "word " * 300 for _ in range(10)), url="https://e.x")
    assert len(huge) <= 4096
    assert huge.count("• ") == 4
    assert all(len(line) <= 210 for line in huge.split("\n"))


def test_shipped_targets_include_core_and_forwarded():
    from release_forwarding import load_targets

    table = ra.alert_targets(load_targets())
    for repo in ("openclaw/openclaw", "NousResearch/hermes-agent", "openai/codex", "anthropics/claude-code"):
        assert repo in table and table[repo]["npm"]
    assert table["openai/codex"]["tag_prefixes"] == ["rust-v", "v"]
    assert table["openclaw/openclaw"]["docker"] == "ghcr.io/openclaw/openclaw:{version}"
    assert "cline/cline" in table


def test_collect_runs_alerts_without_paging(monkeypatch):
    from types import SimpleNamespace

    import release_forwarding
    import scheduler

    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(release_forwarding, "forward_release_events", lambda: "disabled")
    calls = []
    monkeypatch.setattr(ra, "run_release_alerts", lambda: calls.append(1) or (1 / 0))
    dms = []
    monkeypatch.setattr(scheduler, "_send_admin_dm", lambda *a, **k: dms.append(a))
    scheduler.collect()
    assert calls == [1] and dms == []
