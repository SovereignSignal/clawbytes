"""ops/digest/draft.py: window selection, grouping, ranking, phrase ban,
HTML escaping, and the dry-run / mocked-Claude pipelines. No network."""
import datetime as dt
import json
from pathlib import Path

import pytest
import requests

from ops.digest import draft as d

FIXTURE = Path(__file__).parent / "fixtures" / "ai-wire-feed-sample.xml"
UTC = dt.timezone.utc
REG = "https://ai-wire.example.invalid"


def _items():
    return d.parse_feed(FIXTURE.read_bytes())


def _item(title="T", kind="news", summary="S.", url="https://example.com/a",
          when=dt.datetime(2026, 10, 14, 12, tzinfo=UTC)):
    return d.Item(title=title, kind=kind, summary=summary, url=url,
                  link=f"{REG}/items/x", published=when)


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call attempted")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(d.requests, "get", boom)
    monkeypatch.setattr(d.requests, "post", boom)


# ---------------------------------------------------------------- parsing

def test_parse_feed_splits_summary_and_primary_url():
    items = _items()
    assert len(items) == 33
    qwen = next(i for i in items if i.title == "Qwen Code 0.25.2")
    assert qwen.kind == "tool_release"
    assert qwen.url == "https://github.com/QwenLM/qwen-code/releases/tag/v0.25.2"
    assert qwen.link.startswith(f"{REG}/items/")
    assert "https://" not in qwen.summary
    assert qwen.published.tzinfo is not None


def test_parse_description_falls_back_when_no_url():
    assert d.parse_description("Just a summary.") == ("Just a summary.", "")
    assert d.parse_description("Two lines\nof text.\n\nhttps://x.dev/p") == (
        "Two lines of text.", "https://x.dev/p")


def test_item_without_primary_url_links_site_page():
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>No source</title><link>{REG}/items/1</link>
<pubDate>Wed, 14 Oct 2026 10:00:00 GMT</pubDate><category>news</category>
<description>Summary only.</description></item></channel></rss>"""
    (it,) = d.parse_feed(feed)
    assert it.url == f"{REG}/items/1"


def test_item_without_any_link_falls_back_to_site_url():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bare</title><pubDate>Wed, 14 Oct 2026 10:00:00 GMT</pubDate>
<description>Summary only.</description></item></channel></rss>"""
    assert d.parse_feed(feed, REG)[0].url == REG
    assert d.parse_feed(feed)[0].url == ""


def test_feed_home_reads_channel_link():
    assert d.feed_home(FIXTURE.read_bytes()) == REG
    assert d.feed_home("<rss><channel><link>ftp://x</link></channel></rss>") == ""


# ---------------------------------------------------------------- window

@pytest.mark.parametrize("issue,days", [
    (dt.date(2026, 10, 20), 4),  # Tuesday covers Fri-Mon
    (dt.date(2026, 10, 16), 3),  # Friday covers Tue-Thu
    (dt.date(2026, 10, 15), 4),  # any other day
])
def test_default_days(issue, days):
    assert d.default_days(issue) == days


def test_tuesday_window_is_friday_through_monday():
    start, end = d.issue_window(dt.date(2026, 10, 20))
    assert start == dt.datetime(2026, 10, 16, tzinfo=UTC)
    assert end == dt.datetime(2026, 10, 20, tzinfo=UTC)


def test_friday_window_keeps_tue_to_thu_and_excludes_issue_day():
    start, end = d.issue_window(dt.date(2026, 10, 16))
    picked = d.in_window(_items(), start, end)
    assert len(picked) == 33  # fixture spans Tue 13 .. Thu 15
    late = _item(when=dt.datetime(2026, 10, 16, 0, 0, tzinfo=UTC))
    assert d.in_window([late], start, end) == []


def test_since_and_days_overrides():
    items = _items()
    start, end = d.issue_window(dt.date(2026, 10, 15), since=dt.date(2026, 10, 14))
    assert {i.published.date() for i in d.in_window(items, start, end)} == {dt.date(2026, 10, 14)}
    start, end = d.issue_window(dt.date(2026, 10, 16), days=1)
    assert {i.published.date() for i in d.in_window(items, start, end)} == {dt.date(2026, 10, 15)}
    with pytest.raises(ValueError):
        d.issue_window(dt.date(2026, 10, 15), since=dt.date(2026, 10, 15))


# ---------------------------------------------------------------- grouping

def test_section_grouping_by_category():
    assert d.section_for(_item(kind="tool_release", title="Foo 1.2")) == "agents"
    assert d.section_for(_item(kind="model", title="Bar 7B")) == "models"
    assert d.section_for(_item(kind="repo", title="baz")) == "repos"
    assert d.section_for(_item(kind="news", title="Copilot adds agent mode")) == "agents"
    assert d.section_for(_item(kind="paper", title="Benchmarking agentic coding")) == "agents"
    assert d.section_for(_item(kind="news", title="Lab raises money", summary="Funding.")) == "also"
    assert d.section_for(_item(kind="paper", title="Protein folding", summary="Biology.")) == "also"
    assert d.section_for(_item(kind="status", title="Outage")) == "also"


def test_grouping_on_fixture():
    groups = d.group_items(_items())
    assert all(i.kind == "model" for i in groups["models"])
    assert all(i.kind == "repo" for i in groups["repos"])
    assert any(i.kind == "news" for i in groups["agents"])
    assert [i.title for i in groups["also"]] == [
        "OpenAI engineer arrested in bugging of executive conference room"]


def test_caps_and_source_diversity():
    sel = d.select(_items(), d.DEFAULT_CAPS)
    assert len(sel["agents"]) == 6 and len(sel["models"]) == 5 and len(sel["repos"]) == 6
    # three Hermes Agent releases in the window, but diversity keeps one
    hermes = [i for i in sel["agents"] if i.title.startswith("Hermes Agent")]
    assert len(hermes) == 1 and hermes[0].title == "Hermes Agent 0.22.2"
    assert len({i.source for i in sel["agents"]}) == 6
    # ids are unique across sections
    ids = [i.id for v in sel.values() for i in v]
    assert len(ids) == len(set(ids))


def test_rank_fills_with_repeats_only_after_unique_sources():
    t = lambda h: dt.datetime(2026, 10, 14, h, tzinfo=UTC)
    a1 = _item("a1", url="https://github.com/o/a/releases/1", when=t(10))
    a2 = _item("a2", url="https://github.com/o/a/releases/2", when=t(9))
    b = _item("b", url="https://github.com/o/b/releases/1", when=t(1))
    assert [i.title for i in d.rank([a1, a2, b], 2)] == ["a1", "b"]
    assert [i.title for i in d.rank([a1, a2, b], 3)] == ["a1", "a2", "b"]


# ---------------------------------------------------------------- phrase ban

@pytest.mark.parametrize("text", [
    "Where AI is heading", "where ai is heading next", "Here is WHERE  AI IS heading.",
])
def test_strip_banned_phrase(text):
    out = d.strip_banned(text)
    assert not d.contains_banned(out)


def test_strip_banned_keeps_rest_of_sentence():
    assert d.strip_banned("Agents grew up (Where AI is heading).") == "Agents grew up."
    assert d.strip_banned("Clean text.") == "Clean text."


def test_prompt_forbids_phrase_and_demands_grounding():
    p = d.SYSTEM_PROMPT
    assert 'Never use the phrase "Where AI is heading"' in p
    assert "Only use facts from the provided items" in p
    assert "Link every item" in p
    assert "No hype" in p


def test_sanitize_removes_phrase_from_feed_titles_in_template():
    it = _item(title="Where AI is heading: a survey", kind="news", summary="Funding.")
    it.id = "i1"
    sel = {"agents": [], "models": [], "repos": [], "also": [it]}
    start, end = d.issue_window(dt.date(2026, 10, 15))
    draft = d.sanitize(d.template_draft(sel, dt.date(2026, 10, 15), start, end), dt.date(2026, 10, 15))
    md = d.render_markdown(draft, dt.date(2026, 10, 15), start, end)
    assert not d.contains_banned(md)
    assert "a survey" in md


# ---------------------------------------------------------------- HTML

def test_html_escapes_text_and_rejects_unsafe_urls():
    evil = _item(title='<script>alert("x")</script> & co', summary="<b>bold</b>",
                 url="javascript:alert(1)")
    evil.link = "javascript:alert(2)"
    draft = d.Draft(title="A <title> & more", opener="Open <img src=x onerror=1>",
                    lookahead="Watch \"this\" & that",
                    sections={"agents": [d.Entry(evil, evil.title, evil.summary)],
                              "models": [], "repos": []})
    start, end = d.issue_window(dt.date(2026, 10, 16))
    page = d.render_html(draft, dt.date(2026, 10, 16), start, end, REG, f"{REG}/feed.xml")
    assert "<script" not in page.lower()
    assert "javascript:" not in page
    assert "<img" not in page and "<b>" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in page
    assert page.count(f'href="{REG}"') == 2  # unsafe item link replaced, plus the call to action
    assert f'href="{REG}/feed.xml"' in page
    for label in ("Agents &amp; tools", "Models", "Repos", "Look ahead"):
        assert label in page
    assert "#1B2A41" in page and "#C8102E" in page
    md = d.render_markdown(draft, dt.date(2026, 10, 16), start, end, REG)
    assert "javascript:" not in md
    assert f"[Read more →]({REG})" in md
    assert "Nothing new in this window." in md


def test_render_without_site_url_drops_unsafe_link_and_call_to_action():
    evil = _item(url="javascript:alert(1)")
    evil.link = "javascript:alert(2)"
    draft = d.Draft(title="T", opener="O.", lookahead="L.",
                    sections={"agents": [d.Entry(evil, "H", "B.")], "models": [], "repos": []})
    start, end = d.issue_window(dt.date(2026, 10, 16))
    page = d.render_html(draft, dt.date(2026, 10, 16), start, end)
    assert "javascript:" not in page and "Read more" not in page
    assert "Follow the live feed" not in page and 'href=""' not in page
    md = d.render_markdown(draft, dt.date(2026, 10, 16), start, end)
    assert "Read more" not in md and "javascript:" not in md


# ---------------------------------------------------------------- pipelines

def test_dry_run_full_pipeline_without_network(tmp_path, capsys, no_network, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AI_WIRE_BASE_URL", raising=False)
    md_out, html_out = tmp_path / "i.md", tmp_path / "i.html"
    rc = d.main(["--dry-run", "--feed-file", str(FIXTURE), "--date", "2026-10-16",
                 "--md-out", str(md_out), "--html-out", str(html_out)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "===== SYSTEM PROMPT =====" in out and '"source_url"' in out
    md = md_out.read_text()
    assert md.startswith("# ClawBytes: ")
    for h in ("## Agents & tools", "## Models", "## Repos", "## Also worth a look", "**Look ahead:**"):
        assert h in md
    # every item carries a link line with its primary source URL
    assert md.count("[Read more →](") == 6 + 5 + 6 + 1
    assert "[Read more →](https://github.com/QwenLM/qwen-code/releases/tag/v0.25.2)" in md
    page = html_out.read_text()
    assert page.startswith("<!DOCTYPE html>") and "<script" not in page.lower()
    assert page.count("Read more &rarr;") == 18
    # no AI_WIRE_BASE_URL: the call to action links the feed's own homepage, no RSS line
    assert f'href="{REG}" style' in page and "subscribe via" not in page


def test_missing_base_url_without_feed_file_exits_2(capsys, no_network, monkeypatch):
    monkeypatch.delenv("AI_WIRE_BASE_URL", raising=False)
    assert d.main(["--dry-run", "--date", "2026-10-16"]) == 2
    assert "AI_WIRE_BASE_URL is not set" in capsys.readouterr().err


def test_feed_url_comes_from_base_url_or_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fetched = []

    class Resp:
        content = FIXTURE.read_bytes()

        def raise_for_status(self):
            pass

    def get(url, **k):
        fetched.append(url)
        return Resp()

    monkeypatch.setenv("AI_WIRE_BASE_URL", REG + "/")
    html_out = tmp_path / "i.html"
    assert d.main(["--dry-run", "--date", "2026-10-16", "--md-out", str(tmp_path / "i.md"),
                   "--html-out", str(html_out)], get=get) == 0
    assert fetched == [f"{REG}/feed.xml"]
    assert f'href="{REG}/feed.xml"' in html_out.read_text()

    monkeypatch.delenv("AI_WIRE_BASE_URL")
    assert d.main(["--dry-run", "--date", "2026-10-16", "--feed-url", "https://other.example.invalid/rss",
                   "--md-out", str(tmp_path / "j.md")], get=get) == 0
    assert fetched[-1] == "https://other.example.invalid/rss"


def _model_json(sel, phrase=False, drop_section=None):
    obj = {"title": "Agents get steadier", "opener": "Tools hardened this week.",
           "lookahead": "Watch the betas.", "sections": {}}
    if phrase:
        obj["opener"] = "This is Where AI is heading."
    for key, items in sel.items():
        if items and key != drop_section:
            obj["sections"][key] = [{"id": i.id, "headline": f"H {i.title}", "body": "B."}
                                    for i in items]
    return json.dumps(obj)


class FakeResp:
    def __init__(self, text, status=200):
        self.status_code = status
        self._body = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 1000, "output_tokens": 200}}

    def json(self):
        return self._body


def _run_live(monkeypatch, tmp_path, responses):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return FakeResp(responses[len(calls) - 1])

    def get(*a, **k):
        raise AssertionError("feed must come from the fixture")

    md_out = tmp_path / "i.md"
    rc = d.main(["--feed-file", str(FIXTURE), "--date", "2026-10-16", "--model", "claude-sonnet-5-5",
                 "--md-out", str(md_out), "--html-out", str(tmp_path / "i.html")],
                get=get, post=post)
    return rc, calls, md_out


def _selection():
    start, end = d.issue_window(dt.date(2026, 10, 16))
    return d.select(d.in_window(_items(), start, end), d.DEFAULT_CAPS)


def test_live_retries_once_on_banned_phrase(monkeypatch, tmp_path, capsys):
    sel = _selection()
    rc, calls, md_out = _run_live(monkeypatch, tmp_path,
                                  [_model_json(sel, phrase=True), _model_json(sel)])
    assert rc == 0 and len(calls) == 2
    c = calls[0]
    assert c["url"] == "https://api.anthropic.com/v1/messages"
    assert c["headers"]["x-api-key"] == "sk-test-secret"
    assert c["headers"]["anthropic-version"] == "2023-06-01"
    assert c["json"]["model"] == "claude-sonnet-5-5"
    assert "banned phrase" in calls[1]["json"]["messages"][-1]["content"]
    md = md_out.read_text()
    assert not d.contains_banned(md) and "Tools hardened this week." in md
    err = capsys.readouterr().err
    assert "input_tokens=1000" in err and "est_cost=$" in err
    assert "sk-test-secret" not in err


def test_live_strips_phrase_if_retry_still_uses_it(monkeypatch, tmp_path):
    sel = _selection()
    rc, calls, md_out = _run_live(monkeypatch, tmp_path,
                                  [_model_json(sel, phrase=True), _model_json(sel, phrase=True)])
    assert rc == 0 and len(calls) == 2
    assert not d.contains_banned(md_out.read_text())
    assert not d.contains_banned((tmp_path / "i.html").read_text())


def test_live_fails_when_section_missing_after_retry(monkeypatch, tmp_path, capsys):
    sel = _selection()
    bad = _model_json(sel, drop_section="models")
    rc, calls, md_out = _run_live(monkeypatch, tmp_path, [bad, bad])
    assert rc == 2 and len(calls) == 2 and not md_out.exists()
    assert 'missing section "models"' in capsys.readouterr().err


def test_live_recovers_when_retry_fixes_section(monkeypatch, tmp_path):
    sel = _selection()
    rc, calls, md_out = _run_live(monkeypatch, tmp_path,
                                  [_model_json(sel, drop_section="repos"), _model_json(sel)])
    assert rc == 0 and len(calls) == 2
    md = md_out.read_text()
    assert "## Repos" in md and "H SymX" in md


def test_cost_estimate():
    assert d.estimate_cost("claude-opus-4-1", {"input_tokens": 1_000_000,
                                               "output_tokens": 1_000_000}) == pytest.approx(90.0)
    assert d.estimate_cost("claude-opus-5-5", {"input_tokens": 1_000_000}) == pytest.approx(4.0)
    assert d.estimate_cost("mystery-model", {"input_tokens": 5}) is None
