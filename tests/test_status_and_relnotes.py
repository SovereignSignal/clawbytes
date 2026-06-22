from datetime import datetime, timezone

import clawbytes_threads as ct


def _item(feed, title):
    return {
        "feed": feed,
        "title": title,
        "link": "https://example.com/x",
        "published": datetime.now(timezone.utc).isoformat(),
    }


def test_provider_incident_routes_to_watch():
    c = ct.classify_rss(_item("Anthropic Status", "Elevated errors across models"))
    assert c is not None
    assert c["primaryCategory"] == "watch"
    assert c["title"] == "Anthropic: Elevated errors across models"


def test_github_status_filtered_to_agent_relevant():
    assert ct.classify_rss(_item("GitHub Status", "Incident with Webhooks")) is None
    c = ct.classify_rss(_item("GitHub Status", "Degraded performance for Copilot"))
    assert c is not None and c["primaryCategory"] == "watch"


def test_openai_status_skips_consumer_incidents():
    assert ct.classify_rss(_item("OpenAI Status", "Disruption for Free and Go users")) is None
    c = ct.classify_rss(_item("OpenAI Status", "Elevated error rates for GPT 5.5 in Codex"))
    assert c is not None and c["primaryCategory"] == "watch"


def test_release_notes_feed_routes_to_ship_with_vendor_prefix():
    c = ct.classify_rss(_item("Devin Release Notes", "June 10, 2026"))
    assert c is not None
    assert c["primaryCategory"] == "ship"
    assert c["title"] == "Devin: June 10, 2026"


def test_sdk_release_repo_priorities():
    assert ct.repo_name_from_feed("anthropic-sdk-python Releases") == "anthropic-sdk"
    assert ct.repo_name_from_feed("Microsoft Agent Framework Releases") == "agent framework"


def test_security_paper_routes_to_read_not_watch():
    # Papers belong in Read; Watch is for actionable incidents/advisories only.
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


# ── incident-dedup (2026-06-22: same Anthropic Opus incident reposted for ──
#    each state update — investigating → identified → resolved — because   ──
#    each emitted a new feed GUID/link, hashed to a new backlog_id)        ──

_STATUS_STATES = ["Investigating", "Identified", "Monitoring", "Resolved", "Update"]


def _incident_item(title, link, guid):
    return {
        "feed": "Anthropic Status",
        "title": title,
        "link": link,
        "id": guid,  # the feed's GUID — changes per state update in the wild
        "published": datetime.now(timezone.utc).isoformat(),
    }


def test_status_incident_state_variants_share_one_canonical_url():
    # The same incident across state updates has different GUIDs/links in the
    # raw feed. They must normalize to ONE canonical url so queue_for_category's
    # postedUrls filter collapses them — otherwise each state update re-posts
    # the incident (the 2026-06-22 repeat). The url is the dedup key; the
    # backlog_id hashes url+title so it may differ, but that doesn't matter —
    # the queue filters on url before id is considered.
    variants = [
        _incident_item("Elevated errors for Claude Opus 4.8",
                       "https://status.anthropic.com/incidents/abc-1", "guid-abc-1"),
        _incident_item("Resolved: Elevated errors for Claude Opus 4.8",
                       "https://status.anthropic.com/incidents/abc-2", "guid-abc-2"),
        _incident_item("Update: Elevated errors for Claude Opus 4.8",
                       "https://status.anthropic.com/incidents/abc-3", "guid-abc-3"),
    ]
    urls = {ct.classify_rss(v)["url"] for v in variants}
    assert len(urls) == 1, f"state variants must share one canonical url; got {urls}"
    # And once one variant is in postedUrls, the others must be filtered out
    # by queue_for_category (the actual repeat-prevention contract).
    canonical = next(iter(urls))
    assert canonical.startswith("https://status.local/anthropic/")


def test_status_incident_canonical_url_is_stable_across_state_prefixes():
    # Every recognized state prefix must normalize away.
    base = "Elevated errors for Claude Opus 4.8"
    urls = set()
    for state in _STATUS_STATES:
        it = _incident_item(f"{state}: {base}",
                            f"https://status.anthropic.com/incidents/x-{state}",
                            f"guid-{state}")
        urls.add(ct.classify_rss(it)["url"])
    urls.add(ct.classify_rss(_incident_item(base, "https://status.anthropic.com/incidents/x", "g"))["url"])
    assert len(urls) == 1


def test_status_incident_distinct_incidents_stay_distinct():
    # The collapse must be incident-specific, not vendor-wide.
    a = ct.classify_rss(_incident_item("Elevated errors for Claude Opus 4.8",
                                       "https://status.anthropic.com/incidents/a", "ga"))
    b = ct.classify_rss(_incident_item("Degraded API latency",
                                       "https://status.anthropic.com/incidents/b", "gb"))
    assert a["url"] != b["url"]


def test_status_canonical_url_carries_vendor_to_avoid_cross_vendor_clash():
    # Two vendors could have an identically-titled incident; the canonical url
    # must include the vendor so they don't collide.
    a = ct.classify_rss(_incident_item("API degradation",
                                       "https://status.anthropic.com/i/1", "g1"))
    a_item = {**_incident_item("API degradation", "https://status.openai.com/i/1", "g2"),
              "feed": "OpenAI Status"}
    b = ct.classify_rss(a_item)
    assert a["url"] != b["url"]
