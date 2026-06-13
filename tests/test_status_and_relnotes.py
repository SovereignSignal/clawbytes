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
