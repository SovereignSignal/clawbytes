"""Algolia query encoding, and fetch logs for the new watchlist sources.

#37 built the HN URL by interpolating the params object. urllib then rejects
the request: 'URL can't contain control characters'. These tests lock the
encoded query for a search and for the front-page pass.
"""
import importlib.util
import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import clawbytes_threads as ct
import source_health as sh

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hn = _load("claw_hn_fetch", "claw-hn-monitor.py")
rss = _load("claw_rss_fetch_log", "claw-rss-monitor.py")
pw = _load("claw_pagewatch_fetch_log", "claw-pagewatch-monitor.py")


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _selector(url):
    parts = urlsplit(url)
    selector = parts.path
    if parts.query:
        selector = f"{selector}?{parts.query}"
    return selector


def _assert_encoded(url, expected):
    """The request URL is a valid encoded query, for a dict or a list of pairs."""
    selector = _selector(url)
    HTTPConnection._validate_path(None, selector)
    assert " " not in urlsplit(url).query
    assert "{" not in url and "}" not in url
    assert urlsplit(url).query == urlencode(expected)
    assert urlsplit(url).query == urlencode(list(expected.items()))
    parsed = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert parsed == {key: [str(value)] for key, value in expected.items()}


def test_fetch_hn_url_is_encoded_for_search_and_front_page(monkeypatch):
    seen = []

    def _open(req, timeout=None):
        seen.append(req.full_url)
        HTTPConnection._validate_path(None, req.selector)
        return _Body(b'{"hits":[]}')

    monkeypatch.setattr(hn, "urlopen", _open)
    assert hn.fetch_hn("claude code", tags="story") == {"hits": []}
    assert hn.fetch_hn("", tags="front_page", hits_per_page=50, days=None) == {"hits": []}
    assert len(seen) == 2

    search = parse_qs(urlsplit(seen[0]).query, keep_blank_values=True)
    created = search["numericFilters"][0]
    assert created.startswith("created_at_i>")
    _assert_encoded(seen[0], {
        "query": "claude code",
        "tags": "story",
        "page": 0,
        "hitsPerPage": 20,
        "numericFilters": created,
    })
    _assert_encoded(seen[1], {
        "query": "",
        "tags": "front_page",
        "page": 0,
        "hitsPerPage": 50,
    })
    pairs = [("query", "a b"), ("tags", "front_page"), ("hitsPerPage", 50)]
    assert hn._encode_query(pairs) == urlencode(pairs)
    assert hn._encode_query(dict(pairs)) == urlencode(dict(pairs))
    assert hn._encode_query(pairs) == hn._encode_query(dict(pairs))


def test_fetch_hn_mocked_response_returns_stories(monkeypatch):
    payload = {
        "hits": [{
            "objectID": "418",
            "title": "Claude Code tips",
            "points": 42,
            "url": "https://example.com/cc",
        }]
    }

    def _open(req, timeout=None):
        HTTPConnection._validate_path(None, req.selector)
        return _Body(json.dumps(payload).encode())

    monkeypatch.setattr(hn, "urlopen", _open)
    result = hn.fetch_hn("claude code")
    assert result["hits"][0]["objectID"] == "418"
    assert result["hits"][0]["title"] == "Claude Code tips"
    assert result["hits"][0]["points"] == 42


def _rss_xml(items):
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid></item>"
        for title, link in items
    )
    return f"<rss><channel>{body}</channel></rss>"


def test_watchlist_sources_log_fetched_count_or_baseline(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rss, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(rss, "STATE_FILE", tmp_path / "rss.json")
    monkeypatch.setattr(rss, "RSS_FEEDS", [
        {
            "name": "DeepSeek Harness Releases",
            "url": "https://example.com/deepseek-harness.atom",
            "tags": ["releases"],
        },
        {
            "name": "claude.dev Blog",
            "url": "https://claude.dev/rss.xml",
            "tags": ["official"],
        },
        {
            "name": "Other Releases",
            "url": "https://example.com/other.atom",
            "tags": ["releases"],
        },
    ])
    bodies = {
        "https://example.com/deepseek-harness.atom": _rss_xml([
            ("v0.2.0", "https://example.com/dsh/1"),
        ]),
        "https://claude.dev/rss.xml": _rss_xml([
            ("Opus notes", "https://claude.dev/blog/one"),
            ("Sonnet notes", "https://claude.dev/blog/two"),
        ]),
        "https://example.com/other.atom": _rss_xml([
            ("v1", "https://example.com/other/1"),
        ]),
    }
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: bodies.get(url))

    assert rss.check_feeds(verbose=False)[0] == []
    assert capsys.readouterr().out.splitlines() == [
        "INFO deepseek-harness: baseline written",
        "INFO claude.dev: baseline written",
    ]

    items, _status = rss.check_feeds(verbose=False)
    assert items == []
    assert capsys.readouterr().out.splitlines() == [
        "INFO deepseek-harness: 1 items",
        "INFO claude.dev: 2 items",
    ]

    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: None)
    assert rss.check_feeds(verbose=False)[0] == []
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw.json")
    monkeypatch.setattr(pw, "FAILURES", [])
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [
        {
            "key": "anthropic",
            "label": "Anthropic",
            "url": "https://www.anthropic.com/sitemap.xml",
            "prefixes": ("https://www.anthropic.com/news/",),
        },
        {
            "key": "anthropic-research",
            "label": "Anthropic research",
            "url": "https://www.anthropic.com/sitemap.xml",
            "prefixes": ("https://www.anthropic.com/research/",),
        },
    ])
    sitemap = (
        "<urlset>"
        "<loc>https://www.anthropic.com/research/one</loc>"
        "<loc>https://www.anthropic.com/research/two</loc>"
        "<loc>https://www.anthropic.com/news/skip</loc>"
        "</urlset>"
    )
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: sitemap)
    assert pw.check_pages(verbose=False) == []
    assert capsys.readouterr().out.splitlines() == [
        "INFO anthropic-research: baseline written",
    ]

    assert pw.check_pages(verbose=False) == []
    assert capsys.readouterr().out.splitlines() == [
        "INFO anthropic-research: 2 items",
    ]

    def _boom(url, timeout=30):
        raise OSError("sitemap down")

    monkeypatch.setattr(pw, "fetch_text", _boom)
    assert pw.check_pages(verbose=False) == []
    assert capsys.readouterr().out == ""


def test_run_monitors_reprints_watchlist_fetch_logs(monkeypatch, capsys):
    scripts = {
        "claw-rss-monitor.py": (
            "INFO deepseek-harness: baseline written\n"
            "INFO claude.dev: 4 items\n"
            "Found 4 new relevant items\n"
        ),
        "claw-pagewatch-monitor.py": (
            "INFO anthropic-research: 500 items\n"
            "Pagewatch: 0 new item(s)\n"
        ),
        "claw-hn-monitor.py": "Found 1 new HN items\n",
        "claw-moltbook-monitor.py": "Found 0 new relevant items\n",
        "claw-leaderboard-monitor.py": "Leaderboards: 0 new movement item(s)\n",
        "claw-registry-monitor.py": "Registries: 0 new item(s)\n",
        "claw-bsky-monitor.py": "Bluesky: 0 new item(s)\n",
        "claw-advisory-monitor.py": "Advisories: 0 new item(s)\n",
        "claw-ecosystem-monitor.sh": "   Found 0 new release(s)\n   Found 0 new story/stories\n",
    }

    class _P:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def _fake_run(cmd, **kwargs):
        assert kwargs.get("capture_output") is True
        return _P(scripts[Path(cmd[1]).name])

    monkeypatch.setattr(ct.subprocess, "run", _fake_run)
    ct.run_monitors()
    out = capsys.readouterr().out
    assert "INFO deepseek-harness: baseline written" in out.splitlines()
    assert "INFO claude.dev: 4 items" in out.splitlines()
    assert "INFO anthropic-research: 500 items" in out.splitlines()
    assert "source_health source=rss status=ok items=4 error=-" in out
    assert "source_health source=pagewatch status=empty items=0 error=-" in out
    status, items, error = sh.outcome_from_process(
        returncode=0,
        stdout=scripts["claw-pagewatch-monitor.py"],
        stderr="",
    )
    assert (status, items, error) == ("empty", 0, "-")
