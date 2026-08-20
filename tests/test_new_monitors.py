import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load("claw_registry_monitor", "claw-registry-monitor.py")
pw = _load("claw_pagewatch_monitor", "claw-pagewatch-monitor.py")
bsky = _load("claw_bsky_monitor", "claw-bsky-monitor.py")
rss = _load("claw_rss_monitor", "claw-rss-monitor.py")
hn = _load("claw_hn_monitor", "claw-hn-monitor.py")


def test_registry_diff_baseline_is_silent():
    assert reg.diff_new_keys([], ["a", "b"]) == []
    assert reg.diff_new_keys(["a"], ["a", "b", "c"]) == ["b", "c"]


def test_hf_trending_filter_requires_code_agent_and_recency():
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    fresh = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=90)).isoformat()
    models = [
        {"id": "org/Kimi-K2.7-Code", "tags": [], "createdAt": fresh},
        {"id": "org/great-image-model", "tags": ["diffusion"], "createdAt": fresh},
        {"id": "org/old-coder", "tags": ["code"], "createdAt": old},
        {"id": "org/seen-coder", "tags": ["code"], "createdAt": fresh},
    ]
    picks = reg.hf_trending_picks(models, known_ids={"org/seen-coder"}, now=now)
    assert [m["id"] for m in picks] == ["org/Kimi-K2.7-Code"]


def test_pagewatch_sitemap_helpers():
    xml = """<urlset>
      <loc>https://www.anthropic.com/news/zoom-partnership-and-investment</loc>
      <loc>https://www.anthropic.com/engineering/advanced-tool-use</loc>
      <loc>https://www.anthropic.com/careers</loc>
    </urlset>"""
    slugs = pw.sitemap_slugs(xml, ("https://www.anthropic.com/news/", "https://www.anthropic.com/engineering/"))
    assert len(slugs) == 2
    assert pw.slug_title("https://www.anthropic.com/news/zoom-partnership-and-investment") == "Zoom partnership and investment"
    assert pw.lane_for_slug(slugs[0]) == "read"  # engineering sorts first
    assert pw.lane_for_slug(slugs[1]) == "ship"


def test_bsky_engagement_gate_and_url():
    assert bsky.passes_engagement({"likeCount": 25, "repostCount": 0})
    assert bsky.passes_engagement({"likeCount": 0, "repostCount": 12})
    assert not bsky.passes_engagement({"likeCount": 5, "repostCount": 2})
    post = {"author": {"handle": "dev.bsky.social"}, "uri": "at://did:plc:x/app.bsky.feed.post/3kabc"}
    assert bsky.post_web_url(post) == "https://bsky.app/profile/dev.bsky.social/post/3kabc"


def test_bsky_queries_cover_2026_harness_names():
    blob = " ".join(bsky.QUERIES).lower()
    for phrase in ("cursor", "devin desktop", "antigravity", "agent client protocol"):
        assert phrase in blob, f"Bluesky queries missing {phrase!r}"


def test_hn_queries_cover_2026_harness_names():
    blob = " ".join(q["query"] for q in hn.HN_QUERIES).lower()
    for phrase in ("antigravity", "devin desktop", "agent client protocol"):
        assert phrase in blob, f"HN queries missing {phrase!r}"


def test_rss_relevance_bypasses_coding_agent_changelogs():
    # Changelog titles are often feature names with no ecosystem keywords
    # ("Origin Code Hosting"). Without a bypass they never reach classify_rss.
    entry = {"title": "Origin Code Hosting", "summary": ""}
    assert rss.is_relevant(entry, "Cursor Changelog", tags=["coding-agent", "official"])
    assert rss.is_relevant(entry, "Amp News", tags=["coding-agent", "official"])
    # Generic GitHub changelog stays keyword-gated.
    assert not rss.is_relevant(entry, "GitHub Changelog", tags=["developer-tools", "official"])


def test_acp_release_feed_is_wired_and_bypasses_keyword_gate():
    names = {f["name"]: f for f in rss.RSS_FEEDS}
    assert "Agent Client Protocol Releases" in names
    assert names["Agent Client Protocol Releases"]["url"].endswith(
        "agent-client-protocol/releases.atom"
    )
    # Version-only titles must still enter the backlog (invariant 2).
    assert rss.is_relevant({"title": "Schema v1.20.0", "summary": ""}, "Agent Client Protocol Releases")


def test_antigravity_html_watch_is_wired():
    keys = {w["key"] for w in pw.HTML_WATCHES}
    assert "antigravity-changelog" in keys
    watch = next(w for w in pw.HTML_WATCHES if w["key"] == "antigravity-changelog")
    assert watch["fingerprint"] == "headings"
    assert watch["html"] == "https://antigravity.google/changelog"


def test_html_heading_fingerprint_ignores_bundle_hash(tmp_path, monkeypatch):
    # Full-page hash would fire on every Astro redeploy. Hash headings only.
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw-state.json")
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [{
        "key": "ag", "label": "Google Antigravity",
        "html": "https://antigravity.google/changelog",
        "page": "https://antigravity.google/changelog",
        "heading": r"<h3[^>]*>([^<]+)</h3>",
        "fingerprint": "headings",
        "lane": "ship",
    }])
    pages = [
        '<script src="/_astro/a.js"></script><h3>Alpha</h3>',
        '<script src="/_astro/b.js"></script><h3>Alpha</h3>',
        '<script src="/_astro/c.js"></script><h3>Beta</h3>',
    ]
    it = iter(pages)
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: next(it))
    assert pw.check_pages(verbose=False) == []          # baseline
    assert pw.check_pages(verbose=False) == []          # bundle hash changed, headings did not
    items = pw.check_pages(verbose=False)
    assert len(items) == 1
    assert "Beta" in items[0]["title"]
    assert items[0]["url"].startswith("https://antigravity.google/changelog#updated-")


def _found(extra):
    base = {"found_at": datetime.now(timezone.utc).isoformat()}
    base.update(extra)
    return base


def test_classify_registry_routes_to_ship():
    c = ct.classify_registry(_found({
        "id": "OpenRouter:moonshotai/kimi-k2.7-code", "registry": "OpenRouter",
        "title": "Kimi K2.7 Code now live on OpenRouter",
        "url": "https://openrouter.ai/moonshotai/kimi-k2.7-code", "summary": "x"}))
    assert c and c["primaryCategory"] == "ship" and c["sourceName"] == "OpenRouter"


def test_classify_pagewatch_lanes():
    ship = ct.classify_pagewatch(_found({
        "id": "pagewatch:anthropic:x", "watch": "Anthropic", "lane": "ship",
        "title": "Anthropic: Something new", "url": "https://www.anthropic.com/news/x", "summary": "s"}))
    read = ct.classify_pagewatch(_found({
        "id": "pagewatch:anthropic:y", "watch": "Anthropic", "lane": "read",
        "title": "Anthropic: Engineering deep dive", "url": "https://www.anthropic.com/engineering/y", "summary": "s"}))
    assert ship["primaryCategory"] == "ship" and read["primaryCategory"] == "read"
    assert ship["score"] > read["score"]


def test_classify_bsky_routes_to_community():
    c = ct.classify_bsky(_found({
        "id": "at://x", "handle": "dev.bsky.social", "likes": 40, "reposts": 5,
        "title": "@dev: Claude Code v2.1.176 ships hooks v2", "url": "https://bsky.app/profile/dev/post/1"}))
    assert c and c["primaryCategory"] == "community"
    assert c["score"] >= 25  # clears the community lane's first-window bar


def test_pagewatch_md_url_unique_per_change(tmp_path, monkeypatch):
    # Regression: bare page URL meant only the FIRST changelog change ever
    # published (postedUrls is URL-keyed). Two consecutive changes must differ.
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "pw-state.json")
    monkeypatch.setattr(pw, "MD_WATCHES", [{
        "key": "fake", "label": "Fake", "md": "https://x/y.md",
        "page": "https://x/y", "heading": r"^##\s+(.+)$", "lane": "ship",
    }])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [])
    contents = iter(["## v1\nbody", "## v2\nbody2", "## v3\nbody3"])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=30: next(contents))
    assert pw.check_pages(verbose=False) == []          # baseline, silent
    items2 = pw.check_pages(verbose=False)
    items3 = pw.check_pages(verbose=False)
    assert len(items2) == 1 and len(items3) == 1
    assert items2[0]["url"] != items3[0]["url"]
    assert items2[0]["url"].startswith("https://x/y#updated-")


def test_registry_litellm_batch_url_carries_date(monkeypatch):
    def fake_fetch(url, headers=None, timeout=30):
        if "api.github.com" in url:
            return {"sha": "abc"}
        return {"model-a": {}, "model-b": {}}
    monkeypatch.setattr(reg, "_fetch_json", fake_fetch)
    state = {"litellmKeys": ["model-a"], "litellmSha": "old"}
    items = reg.check_litellm(state, "2026-06-13T00:00:00+00:00", False)
    assert len(items) == 1
    assert "#new-2026-06-13" in items[0]["url"]
