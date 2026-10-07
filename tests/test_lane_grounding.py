import clawbytes_threads as ct


def _patch_fetchers(monkeypatch):
    monkeypatch.setattr(ct, "fetch_release_body", lambda url: "feat: real notes")
    monkeypatch.setattr(ct, "fetch_article_snippet", lambda url: "abstract text")


def test_release_urls_ground_with_notes_in_any_lane(monkeypatch):
    _patch_fetchers(monkeypatch)
    item = {"url": "https://github.com/o/r/releases/tag/v1.0"}
    for lane in ("ship", "watch", "read", "community"):
        assert ct.grounding_for_item(lane, item) == "RELEASE NOTES: feat: real notes"


def test_articles_ground_with_snippet_in_every_lane(monkeypatch):
    _patch_fetchers(monkeypatch)
    item = {"url": "https://huggingface.co/papers/2506.12345"}
    for lane in ("watch", "read", "community"):
        assert ct.grounding_for_item(lane, item) == "ARTICLE SNIPPET: abstract text"


def test_reddit_and_empty_urls_are_not_fetched(monkeypatch):
    def boom(url):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(ct, "fetch_release_body", boom)
    monkeypatch.setattr(ct, "fetch_article_snippet", boom)
    assert ct.grounding_for_item("community", {"url": "https://www.reddit.com/r/x/comments/1/"}) == ""
    assert ct.grounding_for_item("read", {"url": ""}) == ""


def test_changelog_grounding_stays_inside_the_named_section(monkeypatch):
    """#968 restated the Sonnet 4.5 deprecation inside the October 1 item.

    The deprecation is the previous heading. Grounding for the October 1
    title must not include it.
    """
    page = (
        "### October 5, 2026\n\n"
        "thinking flag\n\n"
        "### October 1, 2026\n\n"
        "line field on GET /v1/models\n\n"
        "### September 30, 2026\n\n"
        "Claude Sonnet 4.5 is deprecated\n"
    )
    monkeypatch.setattr(ct, "fetch_changelog_markdown", lambda url: page)
    item = {
        "url": "https://docs.claude.com/en/release-notes/overview#updated-9e5058a7",
        "title": "Claude platform release notes — October 1, 2026",
    }
    notes = ct.grounding_for_item("ship", item)
    assert "line field" in notes
    assert "deprecated" not in notes
    assert "October 5" not in notes


def test_empty_fetch_results_yield_no_grounding_label(monkeypatch):
    monkeypatch.setattr(ct, "fetch_release_body", lambda url: "")
    monkeypatch.setattr(ct, "fetch_article_snippet", lambda url: "")
    assert ct.grounding_for_item("ship", {"url": "https://github.com/o/r/releases/tag/v1"}) == ""
    assert ct.grounding_for_item("read", {"url": "https://example.com/post"}) == ""
