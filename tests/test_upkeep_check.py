"""Tests for ops/upkeep/upkeep_check.py. Offline: fixtures + fakes only."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.upkeep import upkeep_check as uc

FIX = Path(__file__).resolve().parent / "fixtures" / "upkeep"
CONFIG = FIX / "config.json"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SECRET_TOKEN = "123456:SECRET-telegram-token-value"
SECRET_RAILWAY = "railway-secret-token-value"
SECRET_CHAT = "987654321"
AI_WIRE_BASE = "https://ai-wire.example.invalid"
RAILWAY_IDS = {"project_id": "proj-example", "service_id": "svc-example", "environment_id": "env-example"}
# Env the fixture runs need: Railway ids and the registry base URL come from env, never config.
FIX_ENV = {"UPKEEP_RAILWAY_PROJECTS": json.dumps({"demo": RAILWAY_IDS}), "AI_WIRE_BASE_URL": AI_WIRE_BASE}


def _levels(findings):
    return [(f.check, f.level, f.attention) for f in findings]


def _attention(findings):
    return [f for f in findings if f.attention]


class HealthyFetcher(uc.FixtureFetcher):
    """Fixture data with every problem fixed, for the exit-0 path."""

    def __init__(self, railway=True):
        super().__init__(FIX)
        self._railway = railway

    @property
    def railway_available(self):
        return self._railway

    def github_issues(self, repo):
        return [i for i in super().github_issues(repo) if i["number"] == 11]

    def github_pulls(self, repo):
        return [p for p in super().github_pulls(repo) if p["number"] == 13]

    def github_file(self, repo, path):
        return "requests==2.32.5\nfeedparser>=6.0.10\n" if path == "requirements.txt" else None

    def railway_deployments(self, *ids):
        return {"data": {"deployments": {"edges": [{"node": {
            "id": "dep-1", "status": "SUCCESS", "createdAt": "2026-10-14T08:00:00Z"}}]}}}

    def telegram_preview(self, channel):
        return '<a class="tgme_widget_message_date"><time datetime="2026-10-15T11:00:00+00:00"></time></a>'


def _run(argv, fetcher=None, sender=None, env=None):
    out, err = io.StringIO(), io.StringIO()
    sent = []

    def fake_sender(text, env_):
        sent.append(text)
        return True, "alert sent to admin chat (CLAWBYTES_ADMIN_CHAT_ID)"

    code = uc.main(["--config", str(CONFIG), *argv], fetcher=fetcher,
                   sender=sender or fake_sender, now=NOW, env=FIX_ENV if env is None else env,
                   stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue(), sent


# --------------------------------------------------------------------------
# 1. GitHub


def test_github_flags_bug_issue_and_stale_pr_only():
    fx = uc.FixtureFetcher(FIX)
    findings = uc.check_github(fx.github_issues("Example/demo"), fx.github_pulls("Example/demo"),
                               NOW, stale_days=7, bug_labels=["bug"])
    assert findings[0].message == "2 open issue(s) (1 bug), 2 open PR(s)"
    flagged = [f.message for f in _attention(findings)]
    assert flagged == ["bug issue #12: Crash when feed is empty",
                       "PR #10 open 44 days (> 7): Bump deps"]


def test_github_quiet_repo_is_ok():
    findings = uc.check_github([], [], NOW)
    assert _levels(findings) == [("github", "ok", False)]


def test_github_stale_threshold_is_configurable():
    pulls = [{"number": 1, "title": "x", "created_at": "2026-10-10T12:00:00Z"}]  # 5 days
    assert not _attention(uc.check_github([], pulls, NOW, stale_days=7))
    assert _attention(uc.check_github([], pulls, NOW, stale_days=3))


# --------------------------------------------------------------------------
# 2. Dependencies


def test_parse_requirements_skips_options_urls_and_markers():
    text = (FIX / "github/Example__demo/contents/requirements.txt").read_text()
    deps = uc.parse_requirements_txt(text)
    assert [(d.name, d.specs) for d in deps] == [
        ("requests", [("==", "1.2.0")]),
        ("feedparser", [(">=", "6.0.10")]),
        ("apscheduler", [(">=", "3.10"), ("<", "4")]),
        ("psycopg2-binary", [("==", "2.9.13")]),
        ("httpx", []),
    ]
    assert deps[0].pinned == "1.2.0" and deps[1].pinned is None


def test_parse_pyproject_dependencies():
    deps = uc.parse_pyproject((FIX / "github/Example__demo/contents/pyproject.toml").read_text())
    assert [(d.name, d.specs) for d in deps] == [("httpx", [(">=", "0.27")]), ("requests", [(">=", "2.0")])]


def test_dependency_drift_major_flags_minor_informs():
    deps = uc.parse_requirements_txt(
        "requests==1.2.0\nfeedparser==6.0.10\npsycopg2-binary==2.9.13\n"
        "apscheduler>=3.10,<4\nhttpx>=0.27\n")
    latest = {"requests": "2.32.5", "feedparser": "6.0.12", "psycopg2-binary": "2.9.13",
              "apscheduler": "4.0.0", "httpx": "0.27"}
    findings = uc.check_dependencies(deps, latest)
    assert findings[0].message == "5 dep(s), 5 checked against PyPI, 3 with a newer release"
    assert [f.message for f in _attention(findings)] == ["major drift: requests==1.2.0 (latest 2.32.5)"]
    info = [f.message for f in findings if f.level == "info"]
    assert info == ["newer available: feedparser==6.0.10 (latest 6.0.12); "
                    "apscheduler>=3.10,<4 (latest 4.0.0, above cap)"]


def test_dependency_pypi_error_is_check_error_and_respects_flag():
    deps = uc.parse_requirements_txt("requests==2.0\n")
    flagged = uc.check_dependencies(deps, {"requests": ConnectionError("boom")}, flag_errors=True)
    assert flagged[1].level == "warning" and flagged[1].attention
    assert "check error" in flagged[1].message
    quiet = uc.check_dependencies(deps, {"requests": ConnectionError("boom")}, flag_errors=False)
    assert not _attention(quiet)


@pytest.mark.parametrize("a,b,expected", [
    ("2.10.0", "2.9.9", 1), ("1.0", "1.0.0", 0), ("1.0rc1", "1.0", -1), ("3.11.3", "4", -1),
])
def test_compare_versions(a, b, expected):
    assert uc.compare_versions(a, b) == expected


def test_compare_versions_without_packaging(monkeypatch):
    monkeypatch.setattr(uc, "Version", None)
    assert uc.compare_versions("2.10.0", "2.9.9") == 1
    assert uc.compare_versions("1.0", "1.0") == 0
    assert uc.major_of("10.2.3") == 10


# --------------------------------------------------------------------------
# 3. Railway


@pytest.mark.parametrize("status,attention", [
    ("SUCCESS", False), ("SLEEPING", False), ("BUILDING", False), ("DEPLOYING", False),
    ("FAILED", True), ("CRASHED", True), ("REMOVED", True),
])
def test_railway_status(status, attention):
    gql = {"data": {"deployments": {"edges": [{"node": {
        "id": "d", "status": status, "createdAt": "2026-10-15T10:00:00Z"}}]}}}
    (f,) = uc.check_railway(gql, NOW)
    assert f.attention is attention
    assert f.message == f"latest deployment {status}, created 2.0h ago"


def test_railway_no_deployments_and_api_errors():
    assert uc.check_railway({"data": {"deployments": {"edges": []}}}, NOW)[0].attention
    with pytest.raises(RuntimeError, match="Not Authorized"):
        uc.check_railway({"errors": [{"message": "Not Authorized"}]}, NOW)


def test_railway_logs_latest_timestamp():
    gql = json.loads((FIX / "railway/logs_dep-1.json").read_text())
    (ok,) = uc.check_railway_logs(gql, NOW, 26)
    assert not ok.attention and ok.message == "last log line 1.9h ago (limit 26h)"
    (stale,) = uc.check_railway_logs(gql, NOW, 1)
    assert stale.attention
    (empty,) = uc.check_railway_logs({"data": {"deploymentLogs": []}}, NOW, 26)
    assert empty.attention


# --------------------------------------------------------------------------
# 4. Silent-run detection


def test_telegram_latest_post_and_silence():
    html = (FIX / "telegram/demochan.html").read_text()
    assert uc.parse_telegram_latest(html) == datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)
    (stale,) = uc.check_telegram(html, NOW, 26, "demochan")
    assert stale.attention and stale.message == "last post in @demochan 2.2d ago (limit 26h)"
    (fresh,) = uc.check_telegram(html, NOW, 72, "demochan")
    assert not fresh.attention


def test_telegram_no_posts_needs_attention():
    (f,) = uc.check_telegram("<html>no messages</html>", NOW, 26, "demochan")
    assert f.attention and "no posts found" in f.message


def test_ai_wire_feed_global_freshness():
    xml = (FIX / "ai_wire/feed.xml").read_text()
    latest, count = uc.parse_feed_latest(xml)
    assert count == 2 and latest == datetime(2026, 10, 15, 10, 0, 6, tzinfo=timezone.utc)
    assert not uc.check_ai_wire_feed(xml, NOW, 26)[0].attention
    assert uc.check_ai_wire_feed(xml, NOW, 1)[0].attention


# --------------------------------------------------------------------------
# runner, exit codes, alerting


def test_fixture_run_reports_attention_and_exits_1():
    code, out, err, sent = _run(["--fixtures", str(FIX)])
    assert code == 1 and err == ""
    assert "Overall: ATTENTION (5 items)" in out
    assert "[demo] ATTENTION" in out
    assert "major drift: requests==1.2.0 (latest 2.32.5)" in out
    assert "latest deployment CRASHED" in out
    assert "OK        railway-logs: last log line 1.9h ago" in out
    assert "OK        ai-wire:" in out
    assert "alert that WOULD be sent" in out and uc.OPS_BANNER.strip() in out
    assert sent == []  # --fixtures never sends


def test_healthy_run_exits_0_and_sends_nothing():
    code, out, _, sent = _run([], fetcher=HealthyFetcher())
    assert code == 0, out
    assert "Overall: OK" in out and "ATTENTION" not in out
    assert sent == []


def test_attention_sends_alert_when_not_dry_run():
    code, out, _, sent = _run([], fetcher=uc.FixtureFetcher(FIX))
    assert code == 1
    assert len(sent) == 1 and sent[0].startswith(uc.OPS_BANNER)
    assert "alert sent to admin chat" in out


def test_dry_run_never_sends():
    code, out, _, sent = _run(["--dry-run"], fetcher=uc.FixtureFetcher(FIX))
    assert code == 1 and sent == []
    assert "dry run: alert not sent" in out and "--- alert that WOULD be sent" in out


def test_no_alert_never_sends():
    code, out, _, sent = _run(["--no-alert"], fetcher=uc.FixtureFetcher(FIX))
    assert code == 1 and sent == []
    assert "alerting disabled (--no-alert)" in out


def test_dry_run_does_not_touch_real_sender(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **k: calls.append(a))
    code, _, _, _ = _run(["--dry-run"], fetcher=uc.FixtureFetcher(FIX), sender=uc.send_telegram_alert,
                         env={**FIX_ENV, "TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "CLAWBYTES_ADMIN_CHAT_ID": SECRET_CHAT})
    assert code == 1 and calls == []


def test_missing_railway_token_skips_without_flagging():
    code, out, _, _ = _run(["--dry-run"], fetcher=HealthyFetcher(railway=False))
    assert code == 0
    assert "SKIPPED   railway: skipped (no RAILWAY_TOKEN)" in out
    assert "SKIPPED   railway-logs: skipped (no RAILWAY_TOKEN)" in out


@pytest.mark.parametrize("value", [None, "", "{not json", "[1, 2]", json.dumps({"other-bot": RAILWAY_IDS}),
                                   json.dumps({"demo": {"project_id": "proj-example"}})])
def test_missing_or_invalid_railway_projects_skips_without_flagging(value):
    env = {"AI_WIRE_BASE_URL": AI_WIRE_BASE}
    if value is not None:
        env["UPKEEP_RAILWAY_PROJECTS"] = value
    code, out, _, _ = _run(["--dry-run"], fetcher=HealthyFetcher(), env=env)
    assert code == 0, out
    assert "SKIPPED   railway: skipped (UPKEEP_RAILWAY_PROJECTS not set for demo)" in out
    assert "SKIPPED   railway-logs: skipped (UPKEEP_RAILWAY_PROJECTS not set for demo)" in out


def test_railway_projects_from_env_parses_ids_per_bot():
    env = {"UPKEEP_RAILWAY_PROJECTS": json.dumps({
        "demo": {**RAILWAY_IDS, "extra": "ignored"},
        "broken": {"project_id": "proj-x", "service_id": "", "environment_id": "env-x"},
        "wrong-type": "proj-y"})}
    assert uc.railway_projects_from_env(env) == {"demo": RAILWAY_IDS}
    assert uc.railway_projects_from_env({}) == {}


def test_railway_ids_from_env_reach_the_fetcher():
    seen = []

    class Recording(HealthyFetcher):
        def railway_deployments(self, *ids):
            seen.append(ids)
            return super().railway_deployments(*ids)

    code, out, _, _ = _run(["--dry-run"], fetcher=Recording())
    assert code == 0, out
    assert seen == [("proj-example", "svc-example", "env-example")]


def test_ai_wire_skipped_without_base_url():
    env = {"UPKEEP_RAILWAY_PROJECTS": FIX_ENV["UPKEEP_RAILWAY_PROJECTS"]}
    code, out, _, _ = _run(["--dry-run"], fetcher=HealthyFetcher(), env=env)
    assert code == 0, out
    assert "SKIPPED   ai-wire: skipped (AI_WIRE_BASE_URL not set and no --feed-url)" in out


@pytest.mark.parametrize("argv,env,expected", [
    ([], {"AI_WIRE_BASE_URL": AI_WIRE_BASE + "/"}, AI_WIRE_BASE + "/feed.xml"),
    (["--feed-url", "https://other.example.invalid/rss"], {"AI_WIRE_BASE_URL": AI_WIRE_BASE},
     "https://other.example.invalid/rss"),
    (["--feed-url", "https://other.example.invalid/rss"], {}, "https://other.example.invalid/rss"),
])
def test_ai_wire_feed_url_from_env_or_flag(argv, env, expected):
    seen = []

    class Recording(HealthyFetcher):
        def ai_wire_feed(self, url):
            seen.append(url)
            return super().ai_wire_feed(url)

    code, out, _, _ = _run(["--dry-run", *argv], fetcher=Recording(), env=env)
    assert code == 0, out
    assert seen == [expected] and "OK        ai-wire:" in out


def test_infra_identifiers_redacted_from_output():
    class Leaky(HealthyFetcher):
        def railway_deployments(self, *ids):
            raise RuntimeError(f"no service {ids[1]} in {ids[0]}")

        def ai_wire_feed(self, url):
            raise ConnectionError(f"cannot reach {url}")

    code, out, _, _ = _run(["--dry-run"], fetcher=Leaky())
    assert code == 1
    for value in (*RAILWAY_IDS.values(), AI_WIRE_BASE):
        assert value not in out
    assert "<railway service_id>" in out and "<AI_WIRE_BASE_URL>/feed.xml" in out


def test_live_fetcher_railway_availability_follows_token():
    assert not uc.LiveFetcher(env={}).railway_available
    assert uc.LiveFetcher(env={"RAILWAY_TOKEN": "x"}).railway_available


def test_check_error_flag_follows_config(tmp_path):
    class Broken(HealthyFetcher):
        def telegram_preview(self, channel):
            raise ConnectionError("network down")

    code, out, _, _ = _run(["--dry-run"], fetcher=Broken())
    assert code == 1 and "check error: ConnectionError: network down" in out

    cfg = json.loads(CONFIG.read_text())
    cfg["flag_on_check_error"] = False
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    out_io = io.StringIO()
    code = uc.main(["--config", str(path), "--dry-run"], fetcher=Broken(), now=NOW, env=FIX_ENV,
                   stdout=out_io, stderr=io.StringIO())
    assert code == 0 and "WARNING   telegram: check error" in out_io.getvalue()


def test_json_output():
    code, out, _, _ = _run(["--json", "--fixtures", str(FIX)])
    data = json.loads(out)
    assert code == 1 and data["attention"] is True
    assert data["alert"]["status"] == "fixtures: alert not sent"
    checks = {(f["check"], f["level"]) for f in data["bots"][0]["findings"]}
    assert ("railway", "attention") in checks and ("telegram", "attention") in checks
    assert data["global"][0]["check"] == "ai-wire"


def test_internal_error_exits_2(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    err = io.StringIO()
    code = uc.main(["--config", str(bad), "--dry-run"], fetcher=HealthyFetcher(), now=NOW, env={},
                   stdout=io.StringIO(), stderr=err)
    assert code == 2 and "internal error" in err.getvalue()


def test_secrets_never_printed():
    env = {**FIX_ENV, "TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "RAILWAY_TOKEN": SECRET_RAILWAY,
           "CLAWBYTES_ADMIN_CHAT_ID": SECRET_CHAT}

    class Leaky(uc.FixtureFetcher):
        def telegram_preview(self, channel):
            raise RuntimeError(f"failed https://api.example/bot{SECRET_TOKEN}?chat={SECRET_CHAT}")

        def railway_deployments(self, *ids):
            raise RuntimeError(f"auth {SECRET_RAILWAY} rejected")

    for argv in (["--dry-run"], ["--json", "--dry-run"], []):
        code, out, err, _ = _run(argv, fetcher=Leaky(FIX), env=env)
        for secret in (SECRET_TOKEN, SECRET_RAILWAY, SECRET_CHAT):
            assert secret not in out and secret not in err
    assert "<TELEGRAM_BOT_TOKEN>" in out


def test_send_alert_missing_config_skips_and_names_no_values():
    ok, status = uc.send_telegram_alert("x", {"TELEGRAM_BOT_TOKEN": SECRET_TOKEN})
    assert not ok and SECRET_TOKEN not in status
    assert status == "alert skipped (no CLAWBYTES_ADMIN_CHAT_ID/MODELBYTES_ADMIN_CHAT_ID)"


def test_send_alert_uses_modelbytes_chat_as_fallback(monkeypatch):
    seen = {}

    class Resp:
        ok, status_code = True, 200

        def json(self):
            return {"ok": True}

    def fake_post(url, data=None, timeout=None):
        seen.update(url=url, data=data)
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    ok, status = uc.send_telegram_alert(
        "hello", {"TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "MODELBYTES_ADMIN_CHAT_ID": SECRET_CHAT})
    assert ok and status == "alert sent to admin chat (MODELBYTES_ADMIN_CHAT_ID)"
    assert seen["data"]["chat_id"] == SECRET_CHAT and seen["url"].endswith("/sendMessage")
    assert SECRET_TOKEN not in status and SECRET_CHAT not in status


def test_send_alert_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError(f"https://api.telegram.org/bot{SECRET_TOKEN}/sendMessage unreachable")

    monkeypatch.setattr("requests.post", boom)
    ok, status = uc.send_telegram_alert(
        "x", {"TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "CLAWBYTES_ADMIN_CHAT_ID": SECRET_CHAT})
    assert not ok and status == "alert failed (ConnectionError)"


def test_shipped_config_lists_three_bots_without_infra_ids():
    cfg = json.loads(uc.DEFAULT_CONFIG.read_text())
    names = [b["name"] for b in cfg["bots"]]
    assert names == ["clawbytes", "modelbytes", "interesting-repos"]
    assert "feed_url" not in cfg["ai_wire"]
    for bot in cfg["bots"]:
        assert "railway" not in bot
        assert bot["telegram_channel"] and bot["repo"].startswith("SovereignSignal/")
