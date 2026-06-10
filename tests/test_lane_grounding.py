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


def test_empty_fetch_results_yield_no_grounding_label(monkeypatch):
    monkeypatch.setattr(ct, "fetch_release_body", lambda url: "")
    monkeypatch.setattr(ct, "fetch_article_snippet", lambda url: "")
    assert ct.grounding_for_item("ship", {"url": "https://github.com/o/r/releases/tag/v1"}) == ""
    assert ct.grounding_for_item("read", {"url": "https://example.com/post"}) == ""
