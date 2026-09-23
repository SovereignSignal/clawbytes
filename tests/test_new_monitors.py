import importlib.util
import json
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
    for phrase in ("cursor", "devin desktop", "antigravity", "agent client protocol",
                   "kiro", "kilo code", "kimi code", "grok build", "mistral vibe",
                   "oh-my-pi", "oh my pi", "herdr"):
        assert phrase in blob, f"Bluesky queries missing {phrase!r}"


def test_hn_queries_cover_2026_harness_names():
    blob = " ".join(q["query"] for q in hn.HN_QUERIES).lower()
    for phrase in ("antigravity", "devin desktop", "agent client protocol",
                   "kiro", "kilo code", "kimi code", "grok build", "pi coding agent",
                   "oh-my-pi", "herdr"):
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


def test_kiro_html_watch_is_wired():
    keys = {w["key"] for w in pw.HTML_WATCHES}
    assert "kiro-changelog" in keys
    watch = next(w for w in pw.HTML_WATCHES if w["key"] == "kiro-changelog")
    assert watch["fingerprint"] == "headings"
    assert watch["html"] == "https://kiro.dev/changelog"
    assert "h2" in watch["heading"]


def test_2026_harness_release_feeds_are_wired():
    names = {f["name"]: f for f in rss.RSS_FEEDS}
    expected = {
        "Pi Coding Agent Releases": "earendil-works/pi/releases.atom",
        "Oh My Pi Releases": "can1357/oh-my-pi/releases.atom",
        "fx Coding Agent Releases": "vercel-labs/fx/releases.atom",
        "Herdr Releases": "herdrdev/herdr/releases.atom",
        "Kilo Code Releases": "Kilo-Org/kilocode/releases.atom",
        "Kimi Code Releases": "MoonshotAI/kimi-code/releases.atom",
        "Mistral Vibe Releases": "mistralai/mistral-vibe/releases.atom",
        "Open Interpreter Releases": "openinterpreter/openinterpreter/releases.atom",
        "Deep Agents Releases": "langchain-ai/deepagents/releases.atom",
        "Codewhale Releases": "Hmbown/CodeWhale/releases.atom",
        "MiMo Code Releases": "XiaomiMiMo/MiMo-Code/releases.atom",
        "AGNO-AGI Releases": "agno-agi/agno/releases.atom",
        "Tau Coding Agent Releases": "huggingface/tau/releases.atom",
    }
    for name, suffix in expected.items():
        assert name in names, f"{name} missing from RSS_FEEDS"
        assert names[name]["url"].endswith(suffix)
        assert "releases" in names[name]["tags"]
        # Version-only titles must still enter the backlog (invariant 2).
        assert rss.is_relevant({"title": "v1.0.0", "summary": ""}, name)

    # Canonical repos after 2026 moves (old URLs still 301, but don't leave them).
    assert names["opencode Releases"]["url"].endswith("anomalyco/opencode/releases.atom")
    assert names["Goose Releases"]["url"].endswith("aaif-goose/goose/releases.atom")


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


def test_openrouter_drops_non_coding_ids(monkeypatch):
    payload = {"data": [
        {"id": "openai/gpt-4o", "name": "GPT-4o", "description": "general chat"},
        {"id": "moonshotai/kimi-k2.7-code", "name": "Kimi K2.7 Code", "description": "coding"},
        {"id": "acme/http-client", "name": "HTTP Client", "description": "client sdk"},
        {"id": "acme/new-encoder", "name": "Encoder", "description": "encoder weights"},
    ]}
    monkeypatch.setattr(reg, "_fetch_json", lambda url, headers=None, timeout=30: payload)
    baseline = reg.check_openrouter({}, "2026-09-23T00:00:00+00:00", False)
    assert baseline == []
    state = {"openrouterIds": ["openai/gpt-4o-old"]}
    items = reg.check_openrouter(state, "2026-09-23T01:00:00+00:00", False)
    urls = [item["url"] for item in items]
    assert any("kimi-k2.7-code" in url for url in urls)
    assert all("gpt-4o" not in url for url in urls)
    assert all("http-client" not in url and "encoder" not in url for url in urls)
    assert "openai/gpt-4o" in state["openrouterIds"]
    assert "acme/http-client" in state["openrouterIds"]


def test_hf_trending_does_not_match_encoder_or_client():
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    fresh = (now - timedelta(days=2)).isoformat()
    models = [
        {"id": "org/new-encoder", "tags": ["encoder"], "createdAt": fresh},
        {"id": "org/http-client", "tags": ["client"], "createdAt": fresh},
        {"id": "org/Kimi-K2.7-Code", "tags": [], "createdAt": fresh},
    ]
    picks = reg.hf_trending_picks(models, known_ids=set(), now=now)
    assert [m["id"] for m in picks] == ["org/Kimi-K2.7-Code"]


def test_registry_litellm_same_day_batches_differ(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(url, headers=None, timeout=30):
        calls["n"] += 1
        if "api.github.com" in url:
            return {"sha": f"sha-{calls['n']}"}
        if calls["n"] < 4:
            return {"model-a": {}, "model-b": {}}
        return {"model-a": {}, "model-b": {}, "model-c": {}}

    monkeypatch.setattr(reg, "_fetch_json", fake_fetch)
    state = {"litellmKeys": ["model-a"], "litellmSha": "old"}
    first = reg.check_litellm(state, "2026-09-23T01:00:00+00:00", False)
    second = reg.check_litellm(state, "2026-09-23T02:00:00+00:00", False)
    assert len(first) == 1 and len(second) == 1
    assert first[0]["url"] != second[0]["url"]
    assert "#new-2026-09-23-" in first[0]["url"]


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


def test_bsky_does_not_remember_posts_under_the_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(bsky, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(bsky, "STATE_FILE", tmp_path / "claw-bsky-state.json")
    monkeypatch.setattr(bsky, "QUERIES", ['"claude code"'])
    uri = "at://did:plc:x/app.bsky.feed.post/cold1"
    cold = {
        "uri": uri, "likeCount": 1, "repostCount": 0,
        "author": {"handle": "dev.bsky.social"},
        "record": {"text": "claude code hooks"},
    }
    hot = dict(cold, likeCount=30)
    calls = {"n": 0}

    def search(_query):
        calls["n"] += 1
        return [cold if calls["n"] == 1 else hot]

    monkeypatch.setattr(bsky, "search_posts", search)
    assert bsky.check_bsky(verbose=False) == []
    state = json.loads((tmp_path / "claw-bsky-state.json").read_text())
    assert uri not in state["seenUris"]
    items = bsky.check_bsky(verbose=False)
    assert len(items) == 1
    assert items[0]["id"] == uri


def test_sitemap_cap_leaves_overflow_for_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "claw-pagewatch-state.json")
    monkeypatch.setattr(pw, "MD_WATCHES", [])
    monkeypatch.setattr(pw, "HTML_WATCHES", [])
    monkeypatch.setattr(pw, "SITEMAP_WATCHES", [{
        "key": "anthropic",
        "label": "Anthropic",
        "url": "https://example.com/sitemap.xml",
        "prefixes": ("https://www.anthropic.com/news/",),
    }])
    base = [f"https://www.anthropic.com/news/old-{i}" for i in range(2)]
    burst = base + [f"https://www.anthropic.com/news/new-{i}" for i in range(7)]
    pages = iter([base, burst, burst])
    monkeypatch.setattr(pw, "fetch_text", lambda url, timeout=20: "<xml/>")
    monkeypatch.setattr(pw, "sitemap_slugs", lambda xml, prefixes: next(pages))
    assert pw.check_pages(verbose=False) == []
    first = pw.check_pages(verbose=False)
    second = pw.check_pages(verbose=False)
    assert len(first) == pw.MAX_SITEMAP_ITEMS_PER_RUN
    assert len(second) == 2
    assert {item["url"] for item in first + second} == set(burst) - set(base)


def test_rss_new_feed_baselines_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(rss, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(rss, "STATE_FILE", tmp_path / "claw-rss-state.json")
    monkeypatch.setattr(rss, "RSS_FEEDS", [{
        "name": "Brand New Releases",
        "url": "https://example.com/releases.atom",
        "tags": ["releases"],
    }])
    first_xml = """<rss><channel><item><title>v1.0.0</title><link>https://ex/1</link><guid>id-1</guid></item></channel></rss>"""
    second_xml = """<rss><channel>
      <item><title>v2.0.0</title><link>https://ex/2</link><guid>id-2</guid></item>
      <item><title>v1.0.0</title><link>https://ex/1</link><guid>id-1</guid></item>
    </channel></rss>"""
    bodies = iter([first_xml, second_xml])
    monkeypatch.setattr(rss, "fetch_feed", lambda url, timeout=15: next(bodies))
    assert rss.check_feeds(verbose=False)[0] == []
    state = json.loads((tmp_path / "claw-rss-state.json").read_text())
    assert "id-1" in state["lastSeenByFeed"]["Brand New Releases"]
    assert not (tmp_path / "claw-rss-state.json.tmp").exists()
    second, _status = rss.check_feeds(verbose=False)
    assert [item["title"] for item in second] == ["v2.0.0"]


def test_arxiv_relevance_requires_harness_compound():
    assert rss.is_relevant({"title": "Tool-use agents with longer context", "summary": ""}, "ArXiv cs.AI")
    assert not rss.is_relevant({"title": "Early agent world models", "summary": ""}, "ArXiv cs.CL")
    assert not rss.is_relevant({"title": "A survey of diffusion models", "summary": ""}, "ArXiv cs.AI")


def test_windsurf_blog_removed_and_marketing_blogs_not_ship_allowlisted():
    assert all(feed["name"] != "Windsurf Blog" for feed in rss.RSS_FEEDS)
    for name in (
        "windsurf blog", "warp blog", "replit blog", "augment code blog",
        "jetbrains ai blog", "jetbrains junie blog", "zed blog",
    ):
        assert name not in ct.CHANGELOG_SHIP_FEED_NAMES
    for name in ("cursor changelog", "github copilot changelog", "amp news"):
        assert name in ct.CHANGELOG_SHIP_FEED_NAMES
