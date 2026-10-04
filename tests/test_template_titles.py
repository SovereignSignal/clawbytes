"""Deterministic template titles.

Machine ids are not headlines. Changelog titles keep words that sit inside
links and inline formatting, including a product name used mid-sentence.
"""

import clawbytes_threads as ct


def _render(monkeypatch, item, category="ship"):
    monkeypatch.setattr(ct, "LLM_API_KEY", "")
    monkeypatch.setattr(ct, "bundle_for_category", lambda *a, **k: [item])
    return ct.format_category_bundle(category, use_llm=False)


def test_changelog_title_keeps_product_words_inside_the_sentence():
    title = "Dynamic workflows in Copilot CLI and the Copilot app"
    assert ct.normalize_release_title("copilot", title) == title
    broken = "Copilot Dynamic workflows in  CLI and the  app"
    assert ct.normalize_release_title("copilot", title) != broken


def test_changelog_title_keeps_words_inside_links_and_inline_markup():
    linked = (
        "Dynamic workflows in [Copilot CLI](https://docs.github.com/copilot/cli) "
        "and the **Copilot app**"
    )
    html = (
        'Dynamic workflows in <a href="https://docs.github.com/copilot/cli">Copilot CLI</a> '
        "and the <em>Copilot app</em>"
    )
    expected = "Dynamic workflows in Copilot CLI and the Copilot app"
    assert ct.normalize_release_title("copilot", linked) == expected
    assert ct.normalize_release_title("copilot", html) == expected
    assert "in  CLI" not in ct.normalize_release_title("copilot", linked)


def test_release_version_titles_still_normalize():
    assert ct.normalize_release_title("openclaw", "v1.2.3") == "OpenClaw 1.2.3"
    assert ct.normalize_release_title("copilot", "Copilot v1.2.3") == "Copilot 1.2.3"


def test_template_replaces_machine_id_with_product_and_version(monkeypatch):
    item = {
        "primaryCategory": "ship",
        "title": "release-publish/004549970362-1790888386",
        "url": "https://github.com/openclaw/openclaw/releases/tag/v2026.5.19",
        "summary": "New release",
        "sourceType": "rss",
        "sourceName": "OpenClaw Releases",
    }
    rendered = _render(monkeypatch, item)
    assert "release-publish/004549970362-1790888386" not in rendered
    assert "OpenClaw 2026.5.19" in rendered


def test_template_skips_machine_id_when_no_version(monkeypatch):
    item = {
        "primaryCategory": "ship",
        "title": "release-publish/004549970362-1790888386",
        "url": "https://example.com/releases/not-a-version",
        "summary": "New release",
        "sourceType": "rss",
        "sourceName": "OpenClaw Releases",
    }
    rendered = _render(monkeypatch, item)
    assert "release-publish/" not in rendered
    assert "Nothing new" in rendered


def test_template_renders_changelog_sentence_intact(monkeypatch):
    item = {
        "primaryCategory": "ship",
        "title": "Dynamic workflows in Copilot CLI and the Copilot app",
        "url": "https://github.blog/changelog/dynamic-workflows",
        "summary": "GitHub Copilot changelog update",
        "sourceType": "rss",
        "sourceName": "GitHub Copilot Changelog",
    }
    rendered = _render(monkeypatch, item)
    assert "Dynamic workflows in Copilot CLI and the Copilot app" in rendered
    assert "in  CLI" not in rendered
