"""Pagewatch fingerprints the newest changelog entry, not the whole page.

A new top heading still emits a unique ``#updated-`` URL. Edits below that
entry, or a re-fetch of the same version, do not. Legacy state files (no
``topEntries`` key) still load and do not repost the heading they already
recorded.
"""
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "claw_pagewatch_top_entry", SCRIPTS / "claw-pagewatch-monitor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load()


def _bind(monkeypatch, tmp_path, md=None, html=None):
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "claw-pagewatch-state.json")
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    if html is not None:
        monkeypatch.setattr(pw, "MD_WATCHES", [])
        monkeypatch.setattr(pw, "HTML_WATCHES", [html])
    else:
        monkeypatch.setattr(pw, "HTML_WATCHES", [])
        monkeypatch.setattr(pw, "MD_WATCHES", [md])


def _feed(pages):
    it = iter(pages)

    def fetch(url, timeout=30):
        return next(it)

    return fetch


KIRO = {
    "key": "kiro-changelog",
    "label": "Kiro",
    "html": "https://kiro.dev/changelog",
    "page": "https://kiro.dev/changelog",
    "heading": r"<h2[^>]*>(.*?)</h2>",
    "fingerprint": "headings",
    "lane": "ship",
}

CLAUDE = {
    "key": "claude-release-notes",
    "label": "Claude platform release notes",
    "md": "https://docs.claude.com/en/release-notes/overview.md",
    "page": "https://docs.claude.com/en/release-notes/overview",
    "heading": r"^###\s+(.+)$",
    "lane": "ship",
}


def _kiro_html(top, top_body, older="1.1.69", older_body="previous notes", bundle="a"):
    return (
        f'<html><script src="/_astro/{bundle}.js"></script>'
        f"<h2>{top}</h2><p>{top_body}</p>"
        f"<h2>{older}</h2><p>{older_body}</p></html>"
    )


def test_new_release_on_the_same_page_posts_and_churn_does_not(tmp_path, monkeypatch):
    _bind(monkeypatch, tmp_path, html=KIRO)
    pages = [
        _kiro_html("1.1.70", "Ship notes", bundle="a"),
        # Same top entry. Bundle hash, older section, and a typo in 1.1.70 change.
        _kiro_html("1.1.70", "Ship notes (typo fix)", older_body="rewritten history", bundle="b"),
        _kiro_html("1.1.71", "Newer release", older="1.1.70", older_body="Ship notes", bundle="c"),
    ]
    monkeypatch.setattr(pw, "fetch_text", _feed(pages))

    assert pw.check_pages(verbose=False) == []  # baseline, silent
    state = json.loads(pw.STATE_FILE.read_text())
    frozen = state["hashes"]["kiro-changelog"]

    assert pw.check_pages(verbose=False) == []  # same 1.1.70, other page changes
    state = json.loads(pw.STATE_FILE.read_text())
    assert state["hashes"]["kiro-changelog"] == frozen
    assert state["topEntries"]["kiro-changelog"] == "1.1.70"

    items = pw.check_pages(verbose=False)
    assert len(items) == 1
    assert "1.1.71" in items[0]["title"]
    assert items[0]["url"].startswith("https://kiro.dev/changelog#updated-")
    assert not items[0]["url"].endswith(frozen[:8])
    state = json.loads(pw.STATE_FILE.read_text())
    assert state["topEntries"]["kiro-changelog"] == "1.1.71"
    assert state["headings"]["kiro-changelog"] == "1.1.71"


def test_markdown_new_top_entry_posts_lower_edit_does_not(tmp_path, monkeypatch):
    _bind(monkeypatch, tmp_path, md=CLAUDE)
    pages = [
        "### September 1, 2026\nFirst cut\n\n### August 1, 2026\nOlder\n",
        "### September 1, 2026\nFirst cut\n\n### August 1, 2026\nOlder, edited\n",
        "### September 28, 2026\nNew release\n\n### September 1, 2026\nFirst cut\n",
    ]
    monkeypatch.setattr(pw, "fetch_text", _feed(pages))

    assert pw.check_pages(verbose=False) == []
    assert pw.check_pages(verbose=False) == []
    items = pw.check_pages(verbose=False)
    assert len(items) == 1
    assert "September 28, 2026" in items[0]["title"]
    assert items[0]["url"].startswith("https://docs.claude.com/en/release-notes/overview#updated-")


def test_legacy_state_loads_and_same_heading_does_not_repost(tmp_path, monkeypatch):
    _bind(monkeypatch, tmp_path, html=KIRO)
    legacy = {
        "hashes": {"kiro-changelog": "a" * 64},
        "headings": {"kiro-changelog": "1.1.70"},
        "slugs": {"anthropic": ["https://www.anthropic.com/news/old"]},
        "lastCheck": "2026-09-01T00:00:00+00:00",
        "foundItems": [{"id": "pagewatch:kiro-changelog:old", "title": "Kiro — 1.1.70"}],
    }
    pw.STATE_FILE.write_text(json.dumps(legacy))
    monkeypatch.setattr(pw, "fetch_text", _feed([
        _kiro_html("1.1.70", "Ship notes", older_body="unrelated edit", bundle="zzz"),
        _kiro_html("1.1.71", "Newer release", older="1.1.70", older_body="Ship notes"),
    ]))

    assert pw.check_pages(verbose=False) == []
    state = json.loads(pw.STATE_FILE.read_text())
    # Old keys survive. topEntries is additive. The legacy hash is kept so the
    # same 1.1.70 does not mint a new fragment.
    assert state["hashes"]["kiro-changelog"] == "a" * 64
    assert state["headings"]["kiro-changelog"] == "1.1.70"
    assert state["slugs"] == legacy["slugs"]
    assert state["foundItems"] == legacy["foundItems"]
    assert state["topEntries"]["kiro-changelog"] == "1.1.70"
    assert "topEntries" not in legacy  # we did not rewrite the in-memory original

    items = pw.check_pages(verbose=False)
    assert len(items) == 1
    assert "1.1.71" in items[0]["title"]
    assert items[0]["url"].startswith("https://kiro.dev/changelog#updated-")
    assert not items[0]["url"].endswith("a" * 8)


def test_missing_heading_falls_back_to_whole_page(tmp_path, monkeypatch):
    watch = {
        "key": "plain",
        "label": "Plain",
        "md": "https://example.com/notes.md",
        "page": "https://example.com/notes",
        "heading": r"^##\s+(.+)$",
        "lane": "ship",
    }
    _bind(monkeypatch, tmp_path, md=watch)
    monkeypatch.setattr(pw, "fetch_text", _feed([
        "no heading, version one",
        "no heading, version two",
    ]))
    assert pw.check_pages(verbose=False) == []
    items = pw.check_pages(verbose=False)
    assert len(items) == 1
    assert items[0]["url"].startswith("https://example.com/notes#updated-")
    state = json.loads(pw.STATE_FILE.read_text())
    assert "topEntries" not in state or "plain" not in state.get("topEntries", {})


def test_heading_keeps_words_inside_links_and_inline_markup():
    html = (
        '<h2>Dynamic workflows in <a href="https://docs.github.com/copilot/cli">Copilot CLI</a> '
        "and the <em>Copilot app</em></h2>"
    )
    expected = "Dynamic workflows in Copilot CLI and the Copilot app"
    assert pw.first_heading(html, r"<h2[^>]*>(.*?)</h2>") == expected
    markdown = (
        "### Dynamic workflows in [Copilot CLI](https://docs.github.com/copilot/cli) "
        "and the **Copilot app**"
    )
    assert pw.first_heading(markdown, r"^###\s+(.+)$") == expected
    antigravity = next(w for w in pw.HTML_WATCHES if w["key"] == "antigravity-changelog")
    nested = '<h3>Ship <a href="https://example.com/hooks">hooks</a> in the <code>CLI</code></h3>'
    assert pw.first_heading(nested, antigravity["heading"]) == "Ship hooks in the CLI"
    assert "in  CLI" not in expected
