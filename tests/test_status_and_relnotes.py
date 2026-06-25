from datetime import datetime, timezone

import clawbytes_threads as ct


def _item(feed, title):
    return {
        "feed": feed,
        "title": title,
        "link": "https://example.com/x",
        "published": datetime.now(timezone.utc).isoformat(),
    }


# ── provider STATUS incidents are dropped entirely (2026-06-24) ──────────────
#    "Anthropic: Elevated errors on Opus 4.8 Fast" is operational weather, not
#    editorial signal: it has no EDITORIAL_SCOPE mandate, ships a generic blurb,
#    and — even after the #10 dedup and #11 per-vendor/day cap — still reads as
#    the same alert repeating (distinct daily incidents each take the one slot).
#    The status branch of classify_rss now returns None.

def test_provider_status_incident_is_dropped():
    assert ct.classify_rss(_item("Anthropic Status", "Elevated errors across models")) is None


def test_openai_status_incident_is_dropped():
    assert ct.classify_rss(_item("OpenAI Status", "Elevated error rates for GPT 5.5 in Codex")) is None


def test_github_status_incident_is_dropped():
    assert ct.classify_rss(_item("GitHub Status", "Degraded performance for Copilot")) is None


def test_status_feed_item_with_read_keyword_still_dropped():
    # Load-bearing: a status title like "...Opus 4.8..." contains the READ_TERM
    # "opus 4". Dropping the branch must NOT let such an item fall through into
    # the Read lane — the status branch returns None *before* READ_TERMS is
    # consulted, so the whole status feed is suppressed.
    assert ct.classify_rss(_item("Anthropic Status", "Elevated errors on Opus 4.8 Fast")) is None


# ── release-notes / releases routing is unaffected ───────────────────────────

def test_release_notes_feed_routes_to_ship_with_vendor_prefix():
    c = ct.classify_rss(_item("Devin Release Notes", "June 10, 2026"))
    assert c is not None
    assert c["primaryCategory"] == "ship"
    assert c["title"] == "Devin: June 10, 2026"


def test_sdk_release_repo_priorities():
    assert ct.repo_name_from_feed("anthropic-sdk-python Releases") == "anthropic-sdk"
    assert ct.repo_name_from_feed("Microsoft Agent Framework Releases") == "agent framework"


def test_security_paper_routes_to_read_not_watch():
    # Papers belong in Read; Watch is for actionable breakage/risk only.
    item = {
        "title": "Prompt injection and jailbreak benchmark for LLM agents",
        "url": "https://huggingface.co/papers/2606.0001",
        "found_at": datetime.now(timezone.utc).isoformat(),
        "upvotes": 30, "score": 5,
    }
    c = ct.classify_hf_paper(item)
    assert c is not None
    assert "watch" not in c["categories"]
    assert c["primaryCategory"] == "read"


# ── the security/CVE monitor is retired entirely (2026-06-24) ────────────────
#    claw-security-monitor.py (a Brave-search + GitHub-advisory monitor) was
#    unwired and deleted: BRAVE_API_KEY was unset in prod so the monitor's early
#    `return [], "no_api_key"` killed both halves, and the GitHub path read the
#    wrong REST fields (id/url vs ghsa_id/html_url) so it emitted nothing anyway.
#    The "security" candidate kind is no longer classified, the lane is gone from
#    collect_candidates, and classify_security no longer exists.

def test_security_kind_is_no_longer_classified():
    item = {
        "url": "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz",
        "title": "RCE in agent-sdk",
        "advisory_id": "GHSA-xxxx-yyyy-zzzz",
        "found_at": datetime.now(timezone.utc).isoformat(),
    }
    assert ct.classify_source_candidate("security", item) is None


def test_collect_candidates_has_no_security_lane():
    assert "security" not in ct.collect_candidates()


def test_classify_security_function_removed():
    assert not hasattr(ct, "classify_security")


def test_queue_drops_lingering_status_local_backlog_items():
    # Retiring the status classifier stops NEW provider-status items, but ones
    # already in the backlog (pre-deploy residue) must not keep being served
    # for up to 48h until their TTL lapses. queue_for_category drops any leftover
    # https://status.local/ item so the cutover is immediate. Non-status watch
    # items are unaffected.
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    backlog_before = ct.load_json(ct.BACKLOG_FILE, {"items": []})
    state_before = ct.load_json(ct.THREAD_STATE_FILE, {})
    try:
        ct.save_json(ct.BACKLOG_FILE, {"items": [
            {"id": "s1", "url": "https://status.local/anthropic/elevated-errors",
             "title": "Anthropic: Elevated errors on Opus 4.8 Fast",
             "categories": ["watch"], "primaryCategory": "watch",
             "status": "queued", "score": 60.0, "expiresAt": future,
             "sourceType": "rss", "sourceName": "Anthropic Status"},
            {"id": "ok1", "url": "https://github.com/x/y/releases/tag/v1.2.3",
             "title": "y v1.2.3", "categories": ["watch"],
             "primaryCategory": "watch", "status": "queued", "score": 50.0,
             "expiresAt": future, "sourceType": "rss", "sourceName": "y"},
        ]})
        ct.save_json(ct.THREAD_STATE_FILE, {})
        urls = [i["url"] for i in ct.queue_for_category("watch")]
        assert not any(u.startswith("https://status.local/") for u in urls)
        assert "https://github.com/x/y/releases/tag/v1.2.3" in urls
    finally:
        ct.save_json(ct.BACKLOG_FILE, backlog_before)
        ct.save_json(ct.THREAD_STATE_FILE, state_before)
