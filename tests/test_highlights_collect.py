"""ops/highlights collector: inputs, pagination, dedupe, CLI and writer. No network."""
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from ops.highlights import collect as hc
from ops.highlights import inputs as hi
from ops.highlights import writer as hw

FIX = Path(__file__).parent / "fixtures" / "highlights"
REG = "https://ai-wire.example.invalid"
REG_ENV = {"AI_WIRE_BASE_URL": REG}


class RecordingFetch:
    def __init__(self, inner=None, fail=()):
        self.inner = inner or hc.fixture_fetcher(str(FIX))
        self.fail = fail
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if any(f in url for f in self.fail):
            raise ConnectionError(f"boom {url}")
        return self.inner(url)


def ctx_for(day, fetch=None, **kw):
    return hi.Context(window=hc.day_window(day, "America/Los_Angeles"),
                      fetch=fetch or RecordingFetch(), env=kw.pop("env", REG_ENV), **kw)


# --- window -----------------------------------------------------------------

def test_day_window_is_local_midnight_to_midnight_in_utc():
    w = hc.day_window("2026-10-14", "America/Los_Angeles")
    assert w.start == datetime(2026, 10, 14, 7, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
    # DST ends 2026-11-01: that local day is 25 hours long.
    w = hc.day_window("2026-11-01", "America/Los_Angeles")
    assert (w.end - w.start).total_seconds() == 25 * 3600
    assert w.contains(w.start) and not w.contains(w.end)


# --- telegram ---------------------------------------------------------------

def test_parse_tg_page_extracts_text_links_and_time():
    msgs = hi.parse_tg_page((FIX / "tg_clawbytes.html").read_text())
    assert [m["id"] for m in msgs] == list(range(1016, 1024))
    m = next(m for m in msgs if m["id"] == 1022)
    assert m["post"] == "clawbytes/1022"
    assert m["posted_at"] == datetime(2026, 10, 15, 0, 8, 29, tzinfo=timezone.utc)
    assert m["text"].startswith("🚢 Ship — 4 items")
    assert "<" not in m["text"] and "onclick" not in m["text"]
    assert m["links"][0] == "https://platform.claude.com/docs/en/managed-agents/workflow-runs"
    assert all(l.startswith("http") for l in m["links"])


def test_parse_tg_page_ignores_reply_quote_and_tme_links():
    html = """<div class="tgme_widget_message js-widget_message" data-post="chan/7">
      <a class="tgme_widget_message_reply" href="https://t.me/chan/6">
        <div class="tgme_widget_message_text js-message_reply_text">quoted <a href="https://quoted.example">q</a></div></a>
      <div class="tgme_widget_message_text js-message_text">Hello<br/>world
        <a href="https://example.com/a">a</a> <a href="https://t.me/other">t</a> <a href="?q=%23tag">#tag</a></div>
      <a class="tgme_widget_message_date" href="https://t.me/chan/7"><time datetime="2026-10-14T12:00:00+00:00">12:00</time></a>
    </div>"""
    (m,) = hi.parse_tg_page(html)
    assert m["text"].startswith("Hello\nworld")
    assert "quoted" not in m["text"]
    assert m["links"] == ["https://example.com/a"]


def test_telegram_stops_when_first_page_reaches_past_the_day():
    fetch = RecordingFetch()
    run = hi.collect_tg_channel(ctx_for("2026-10-14", fetch), "clawbytes", "clawbytes", "post")
    assert fetch.urls == ["https://t.me/s/clawbytes"]
    assert [i["post_url"] for i in run.items] == [f"https://t.me/clawbytes/{n}" for n in range(1019, 1024)]
    assert {i["kind"] for i in run.items} == {"ship", "read", "community"}
    assert run.error is None


def test_telegram_paginates_with_before_until_older_than_day():
    fetch = RecordingFetch()
    run = hi.collect_tg_channel(ctx_for("2026-10-13", fetch), "clawbytes", "clawbytes", "post")
    assert fetch.urls == ["https://t.me/s/clawbytes", "https://t.me/s/clawbytes?before=1016"]
    ids = [int(i["post_url"].rsplit("/", 1)[1]) for i in run.items]
    assert any(i < 1016 for i in ids) and any(i >= 1016 for i in ids)
    w = hc.day_window("2026-10-13", "America/Los_Angeles")
    assert all(w.contains(hi.parse_dt(i["posted_at"])) for i in run.items)


def test_telegram_page_cap_and_empty_page_stop():
    fetch = RecordingFetch()
    hi.collect_tg_channel(ctx_for("2026-10-13", fetch, max_pages=1), "clawbytes", "clawbytes", "post")
    assert len(fetch.urls) == 1
    # Two days back needs page 3, which has no fixture -> empty page ends the walk.
    fetch = RecordingFetch()
    hi.collect_tg_channel(ctx_for("2026-10-11", fetch, max_pages=5), "clawbytes", "clawbytes", "post")
    assert fetch.urls[-1] == "https://t.me/s/clawbytes?before=1008"
    assert len(fetch.urls) == 3


def test_telegram_later_page_failure_keeps_earlier_items():
    fetch = RecordingFetch(fail=("before=",))
    run = hi.collect_tg_channel(ctx_for("2026-10-13", fetch), "clawbytes", "clawbytes", "post")
    assert run.items and run.error.startswith("partial: ConnectionError")


# --- registry ---------------------------------------------------------------

def test_registry_feed_parse_and_repo_routing():
    entries = hi.parse_registry_feed((FIX / "feed.xml").read_text())
    assert {e["kind"] for e in entries} == {"repo", "news", "tool_release", "model"}
    e = entries[0]
    assert e["source_url"] == "https://github.com/ZZzyy714227/Labsus"
    assert e["link"].startswith(f"{REG}/items/")
    assert "https://" not in e["summary"]

    (run,) = hi.RegistryInput().run(ctx_for("2026-10-15"))
    repo = [i for i in run.items if i["kind"] == "repo"]
    assert repo and all(i["source"] == "interesting-repos" for i in repo)
    assert all(i["source"] == "registry" for i in run.items if i["kind"] != "repo")
    assert all(i["channel"] == "ai-wire" for i in run.items)
    assert repo[0]["links"][0].startswith("https://github.com/")


# --- dedupe / collect -------------------------------------------------------

def test_dedupe_keeps_first_and_normalizes_tracking():
    a = hi.make_item("clawbytes", "t.me/clawbytes", "https://t.me/clawbytes/1", "2026-10-14T10:00:00+00:00",
                     "x", ["https://Example.com/post/?utm_source=tg"], "ship")
    b = hi.make_item("registry", "ai-wire", f"{REG}/items/1", "2026-10-14T11:00:00+00:00",
                     "y", ["https://example.com/post", f"{REG}/items/1"], "news")
    c = hi.make_item("registry", "ai-wire", f"{REG}/items/2", "2026-10-14T11:00:00+00:00",
                     "z", ["https://example.com/post?id=2"], "news")
    kept, dropped = hc.dedupe([a, b, c])
    assert kept == [a, c] and dropped == 1


def test_collect_reports_sources_and_isolates_failures():
    fetch = RecordingFetch(fail=("feed.xml",))
    out = hc.collect(ctx_for("2026-10-14", fetch))
    s = out["sources"]
    assert s["registry"] == {"ok": False, "count": 0, "error": f"ConnectionError: boom {REG}/feed.xml"}
    assert s["telegram:clawbytes"]["ok"] and s["telegram:clawbytes"]["count"] == 5
    assert s["gmail"]["ok"] is None and "GMAIL_USER" in s["gmail"]["skipped"]
    assert s["x"]["skipped"] == "X_BEARER_TOKEN not set"
    for it in out["items"]:
        assert tuple(it) == hi.ITEM_KEYS
    assert [i["posted_at"] for i in out["items"]] == sorted(i["posted_at"] for i in out["items"])


def test_registry_skipped_without_base_url():
    fetch = RecordingFetch()
    out = hc.collect(ctx_for("2026-10-14", fetch, env={}), ["registry"])
    assert out["sources"]["registry"] == {"ok": None, "count": 0, "error": None,
                                          "skipped": "AI_WIRE_BASE_URL not set (or pass --feed-url)"}
    assert fetch.urls == []


def test_registry_feed_url_from_env_or_override():
    assert hi.registry_feed_url(ctx_for("2026-10-14", env={"AI_WIRE_BASE_URL": REG + "/"})) == f"{REG}/feed.xml"
    ctx = ctx_for("2026-10-14", env={}, registry_url="https://other.example.invalid/rss")
    assert hi.registry_feed_url(ctx) == "https://other.example.invalid/rss"
    assert hi.registry_feed_url(ctx_for("2026-10-14", env={})) == ""


def test_unknown_input_is_reported():
    out = hc.collect(ctx_for("2026-10-14"), ["telegram", "nope"])
    assert out["sources"]["nope"]["error"] == "unknown input"


# --- credentialed and jsonfile inputs ---------------------------------------

def test_x_input_with_mocked_api():
    calls = []

    def get_json(url, params, headers):
        calls.append((url, params, headers))
        return {"data": [{"id": "99", "author_id": "1", "created_at": "2026-10-14T18:00:00.000Z",
                          "text": "New thing https://t.co/x",
                          "entities": {"urls": [{"expanded_url": "https://example.com/new"},
                                                {"expanded_url": "https://x.com/a/status/1/photo/1"}]}}],
                "includes": {"users": [{"id": "1", "username": "cursor_ai"}]}}

    ctx = ctx_for("2026-10-14", env={"X_BEARER_TOKEN": "tok", "X_HIGHLIGHTS_USERS": "@cursor_ai, btibor91"})
    assert hi.XInput().enabled(ctx) == (True, None)
    (run,) = hi.XInput(get_json=get_json).run(ctx)
    assert calls[0][1]["query"] == "from:cursor_ai OR from:btibor91 -is:retweet"
    assert calls[0][2]["Authorization"] == "Bearer tok"
    (it,) = run.items
    assert it["post_url"] == "https://x.com/cursor_ai/status/99"
    assert it["links"] == ["https://example.com/new"] and it["source"] == "x"


def test_x_input_error_does_not_leak_token():
    def get_json(url, params, headers):
        raise RuntimeError(headers["Authorization"])

    (run,) = hi.XInput(get_json=get_json).run(ctx_for("2026-10-14", env={"X_BEARER_TOKEN": "sekrit"}))
    assert run.error == "RuntimeError" and "sekrit" not in run.error


def test_gmail_input_with_fake_imap(monkeypatch):
    msg = EmailMessage()
    msg["Subject"] = "Weekly agents letter"
    msg["From"] = "News <news@example.com>"
    msg["Date"] = "Wed, 14 Oct 2026 15:00:00 +0000"
    msg["Message-ID"] = "<abc@example.com>"
    msg.set_content("Read this: https://example.com/story and https://example.com/two")
    old = EmailMessage()
    old["Date"] = "Mon, 12 Oct 2026 15:00:00 +0000"
    old.set_content("old https://example.com/old")

    class FakeImap:
        def login(self, u, p): self.creds = (u, p)
        def select(self, box, readonly): assert readonly
        def search(self, charset, kind, q):
            self.q = q
            return "OK", [b"1 2"]
        def fetch(self, mid, what):
            return "OK", [(b"x", (msg if mid == b"1" else old).as_bytes())]
        def logout(self): pass

    fake = FakeImap()
    monkeypatch.setattr(hi.GmailInput, "imap_factory", staticmethod(lambda: fake))
    ctx = ctx_for("2026-10-14", env={"GMAIL_USER": "u", "GMAIL_APP_PASSWORD": "p", "GMAIL_QUERY": "label:ai"})
    (run,) = hi.GmailInput().run(ctx)
    assert run.error is None and "label:ai after:2026/10/13 before:2026/10/16" in fake.q
    (it,) = run.items
    assert it["post_url"] == "https://example.com/story" and it["kind"] == "newsletter"
    assert it["text"].startswith("Weekly agents letter")


def test_jsonfile_input_merges_and_filters(tmp_path):
    f = tmp_path / "x-export.json"
    f.write_text(json.dumps({"items": [
        {"post_url": "https://x.com/a/status/1", "posted_at": "2026-10-14T20:00:00Z", "text": "hi"},
        {"links": ["https://example.com/z"], "source": "gmail", "kind": "newsletter"},
        {"post_url": "https://x.com/a/status/2", "posted_at": "2026-10-01T20:00:00Z"},
        {"text": "no url"},
    ]}))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    (ok, broken) = hi.JsonFileInput().run(ctx_for("2026-10-14", extra_inputs=[str(f), str(bad)]))
    assert [i["source"] for i in ok.items] == ["x-export", "gmail"]
    assert ok.items[1]["post_url"] == "https://example.com/z"
    assert broken.name == "jsonfile:bad.json" and broken.error.startswith("JSONDecodeError")


# --- writer -----------------------------------------------------------------

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_writer_posts_and_logs_usage_without_key():
    sent, logs = {}, []

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return FakeResp(200, {"model": "claude-sonnet-4-5-20250929", "stop_reason": "end_turn",
                              "content": [{"type": "text", "text": "# Hook\n- **A**: b. https://a"}],
                              "usage": {"input_tokens": 1000, "output_tokens": 200}})

    collected = hc.collect(ctx_for("2026-10-14"), ["telegram"])
    out = hw.write_reel(collected, api_key="sk-test-123", model="claude-sonnet-4-5", post=post, log=logs.append)
    assert out.startswith("# Hook")
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "sk-test-123"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["model"] == "claude-sonnet-4-5"
    assert "https://t.me/clawbytes/1022" in sent["body"]["messages"][0]["content"]
    (line,) = logs
    assert "input_tokens=1000 output_tokens=200 est_cost_usd=0.0060" in line
    assert "sk-test" not in line


def test_writer_errors_and_cost_table():
    with pytest.raises(RuntimeError, match="HTTP 401"):
        hw.call_claude("p", model="m", api_key="k", post=lambda *a, **k: FakeResp(401, {"error": "bad"}), log=lambda m: None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        hw.write_reel({"items": []}, api_key="")
    assert hw.estimate_cost("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert hw.estimate_cost("claude-sonnet-5", 0, 1_000_000) == 10.0
    assert hw.estimate_cost("mystery", 1, 1) is None


def test_build_prompt_truncates_item_text():
    long = {"date": "d", "items": [hi.make_item("s", "c", "https://p", None, "w " * 2000, ["https://l"], "k")]}
    p = hw.build_prompt(long)
    assert "…" in p and len(p) < 1200


# --- CLI --------------------------------------------------------------------

def test_cli_dry_run_never_calls_claude(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    monkeypatch.setenv("AI_WIRE_BASE_URL", REG)

    def post(*a, **k):
        raise AssertionError("Claude called in dry run")

    out = tmp_path / "day.json"
    rc = hc.main(["--date", "2026-10-14", "--fixtures", str(FIX), "--out", str(out),
                  "--dry-run", "--write-reel"], post=post)
    assert rc == 0
    data = json.loads(out.read_text())
    assert set(data) >= {"date", "generated_at", "sources", "items"}
    assert data["date"] == "2026-10-14" and len(data["items"]) == 19
    err = capsys.readouterr().err
    assert "dry run, Claude not called" in err and "--- user ---" in err
    assert "sk-should-not" not in err


def test_cli_write_reel_to_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("HIGHLIGHTS_MODEL", "claude-haiku-4-5")
    monkeypatch.delenv("AI_WIRE_BASE_URL", raising=False)
    seen = {}

    def post(url, headers, json, timeout):
        seen["model"] = json["model"]
        return FakeResp(200, {"content": [{"type": "text", "text": "# Reel"}],
                              "usage": {"input_tokens": 10, "output_tokens": 5}})

    reel = tmp_path / "reel.md"
    rc = hc.main(["--date", "2026-10-14", "--fixtures", str(FIX), "--inputs", "registry",
                  "--feed-url", f"{REG}/feed.xml", "--write-reel", "--reel-out", str(reel)], post=post)
    assert rc == 0 and reel.read_text() == "# Reel\n" and seen["model"] == "claude-haiku-4-5"
    out = capsys.readouterr()
    assert json.loads(out.out)["sources"]["registry"]["count"] == 8
    assert "est_cost_usd=" in out.err


def test_cli_registry_skipped_without_base_url(capsys, monkeypatch):
    monkeypatch.delenv("AI_WIRE_BASE_URL", raising=False)
    rc = hc.main(["--date", "2026-10-14", "--fixtures", str(FIX), "--inputs", "telegram,registry"])
    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out)["sources"]["registry"]["ok"] is None
    assert "registry: 0 items (skipped: AI_WIRE_BASE_URL not set (or pass --feed-url))" in out.err


def test_cli_exit_code_when_everything_fails(capsys, monkeypatch):
    monkeypatch.setenv("AI_WIRE_BASE_URL", REG)
    def fetch(url):
        raise ConnectionError("down")

    assert hc.main(["--date", "2026-10-14", "--inputs", "telegram,registry"], fetch=fetch) == 1
    assert hc.main(["--date", "not-a-date"]) == 2
