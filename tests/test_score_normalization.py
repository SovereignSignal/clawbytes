"""Tests for opt-in within-source score normalization (CLAWBYTES_NORMALIZE_SCORES).

The goal: a top-of-its-source item should rank alongside another source's
top item even when their raw score scales differ by an order of magnitude,
instead of losing to a mediocre high-point-count item.
"""
import clawbytes_threads as ct


def test_percentile_ranks_basic_spread():
    assert ct._percentile_ranks([10, 20, 30]) == [0.0, 0.5, 1.0]


def test_percentile_ranks_edges():
    assert ct._percentile_ranks([]) == []
    assert ct._percentile_ranks([42]) == [1.0]  # lone item = top of its source


def test_percentile_ranks_ties_share_mean_position():
    # two-way tie at the bottom -> both get mean of positions 0 and 1 = 0.5/2
    ranks = ct._percentile_ranks([5, 5, 9])
    assert ranks[0] == ranks[1]
    assert ranks[2] == 1.0
    assert 0 < ranks[0] < 1.0


def test_apply_normalized_scores_groups_by_source_type():
    items = [
        {"sourceType": "hackernews", "score": 600},
        {"sourceType": "hackernews", "score": 200},
        {"sourceType": "rss", "score": 110},
    ]
    ct.apply_normalized_scores(items)
    assert items[0]["normScore"] == 1.0   # top HN
    assert items[1]["normScore"] == 0.0   # bottom HN
    assert items[2]["normScore"] == 1.0   # lone rss item = top of its source


def _queue_backlog(monkeypatch, items):
    backlog = {"items": items}
    monkeypatch.setattr(ct, "ensure_files", lambda: None)

    def fake_load(path, default):
        return backlog if path == ct.BACKLOG_FILE else {}

    monkeypatch.setattr(ct, "load_json", fake_load)


def test_queue_ordering_flips_with_normalization(monkeypatch):
    items = [
        {"status": "queued", "categories": ["read"], "url": "hn-a", "sourceType": "hackernews", "score": 600, "title": "A"},
        {"status": "queued", "categories": ["read"], "url": "hn-b", "sourceType": "hackernews", "score": 200, "title": "B"},
        {"status": "queued", "categories": ["read"], "url": "rss-a", "sourceType": "rss", "score": 110, "title": "C"},
        {"status": "queued", "categories": ["read"], "url": "rss-b", "sourceType": "rss", "score": 90, "title": "D"},
    ]
    _queue_backlog(monkeypatch, items)

    monkeypatch.delenv("CLAWBYTES_NORMALIZE_SCORES", raising=False)
    raw_order = [i["url"] for i in ct.queue_for_category("read")]
    assert raw_order == ["hn-a", "hn-b", "rss-a", "rss-b"]  # pure raw score desc

    monkeypatch.setenv("CLAWBYTES_NORMALIZE_SCORES", "1")
    norm_order = [i["url"] for i in ct.queue_for_category("read")]
    # top-of-source rss item now beats the mediocre HN item it used to lose to
    assert norm_order == ["hn-a", "rss-a", "hn-b", "rss-b"]


def test_normalization_leaves_raw_score_untouched(monkeypatch):
    items = [
        {"status": "queued", "categories": ["ship"], "url": "x", "sourceType": "rss", "score": 110, "title": "X"},
        {"status": "queued", "categories": ["ship"], "url": "y", "sourceType": "rss", "score": 90, "title": "Y"},
    ]
    _queue_backlog(monkeypatch, items)
    monkeypatch.setenv("CLAWBYTES_NORMALIZE_SCORES", "1")
    out = ct.queue_for_category("ship")
    assert {i["url"]: i["score"] for i in out} == {"x": 110, "y": 90}
