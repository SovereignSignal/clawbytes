# ClawBytes source inventory

What we watch, where each list lives in code, and the running log of candidate
sources we've evaluated. When proposing a source, check this file first.

The definitive lists live in code (file references below) — this file inventories
the *classes* and tracks *decisions*, so it stays true even as entries shift.

## Live source classes

| Class | What | Defined in | Cadence |
|---|---|---|---|
| RSS/Atom feeds | ~54 feeds: vendor blogs, changelogs (Cursor, GitHub/Copilot, Zed), GitHub `releases.atom` for harnesses/SDKs/frameworks, research blogs, ArXiv cs.AI/cs.CL | `scripts/claw-rss-monitor.py` (`RSS_FEEDS`) | 30 min |
| GitHub releases (API) | Curated + auto-discovered repos, merged via `claw-ecosystem-sources.json` | `scripts/claw-ecosystem-monitor.sh` | 30 min |
| HF Daily Papers | huggingface.co/papers via `api/daily_papers`, keyword-scored into lanes | `scripts/claw-hf-papers.py` | 30 min |
| Reddit | r/openclaw, r/ClaudeAI, r/ClaudeCode, r/cursor, r/ChatGPTCoding, r/AI_Agents, r/mcp + targeted searches in r/LocalLLaMA, r/selfhosted | `scripts/claw-reddit-monitor.py` (`SUBREDDITS`) | 30 min |
| Hacker News | Algolia queries (harness/agent/MCP terms), 14-day window | `scripts/claw-hn-monitor.py`, `claw-ecosystem-monitor.sh` | 30 min |
| Moltbook | Community posts, HTML scrape | `scripts/claw-moltbook-monitor.py` | 30 min |
| Discovery | GitHub topic/keyword search, awesome lists (awesome-ai-agents, awesome-agents, awesome-mcp-servers, awesome-claude-code, awesome-code-ai) | `claw-ecosystem-monitor.sh --mode discover`, `claw-source-discovery.py` | weekly (Mon 14:10 UTC) |
| Leaderboards | SWE-bench (Verified, bash-only), Aider polyglot, LiveBench — emits only on top-3 movement, sha-gated fetches | `scripts/claw-leaderboard-monitor.py` (`BOARDS`) | 30 min |
| Registries | OpenRouter model list (id diff = minutes-level new-model detection), LiteLLM pricing registry (sha-gated key diff), HF trending (weekly, coding/agent filter) | `scripts/claw-registry-monitor.py` | 30 min |
| Feedless pages | Mintlify `.md` hash watches (Claude platform release notes, Devin CLI, xAI) + sitemap slug diffs (Anthropic news/engineering, DeepSeek news) | `scripts/claw-pagewatch-monitor.py` | 30 min |
| Bluesky | Phrase search ("claude code", "codex cli", "openclaw", "mcp server", "agent harness"), engagement-gated | `scripts/claw-bsky-monitor.py` | 30 min |

Discovered repos/feeds/subreddits land in `claw-ecosystem-sources.json` /
`clawbytes-dynamic-feeds.json` on the volume and are merged automatically —
no code change needed for a discovery to start being watched.

## Candidate log

Format: date · source · verdict (covered / added / passed) · why.

- 2026-06-10 · huggingface.co/papers · **covered** — already ingested via
  `api/daily_papers` every 30 min; several of today's channel items came from it.
- 2026-06-10 · HF trending models · **passed for now** — would catch open-weight
  drops early but overlaps HN/RSS; revisit if model news arrives late.
- 2026-06-10 · HF Spaces · **passed for now** — high noise ratio.
- 2026-06-10 · Windsurf changelog · **passed for now** — no RSS feed; needs a
  page-watcher; revisit if Windsurf news keeps missing.
- 2026-06-10 · swebench.com · **added** — leaderboard monitor watches Verified
  and bash-only boards via the site repo's `data/leaderboards.json`; posts to
  Ship when the top 3 moves.
- 2026-06-10 · aider.chat/docs/leaderboards · **added** — same monitor, via
  `polyglot_leaderboard.yml` in the Aider repo.

### 2026-06-12 round 2 (six-hunter verified sweep)

Fixes from the feed-health audit: LangChain feed URL corrected
(post-Webflow), "Google DeepMind Blog" repointed from Google's corporate
Keyword feed to deepmind.google, AutoGen replaced by microsoft/agent-framework
(successor repo), Lilian Weng dropped (407d stale), The AI Edge dropped
(drifted to general-AI roundups).

**Added** — Devin + Factory release-notes RSS; Amp news; Windsurf, Warp,
Replit, Augment, JetBrains AI + Junie, Sourcegraph, Mistral blogs; lobste.rs
`/t/ai` (the `ml` tag is OCaml — never add it); IndyDevDan YouTube RSS; core
SDK release feeds (anthropic-sdk py+ts, openai-python, python-genai); six
provider status feeds; r/codex, r/Anthropic, r/GithubCopilot, r/windsurf;
LiveBench; OpenRouter models API; LiteLLM pricing registry; HF trending
(verdict flipped from 2026-06-10 pass — with a coding/agent + 30-day filter
it catches model drops like Kimi-K2.7-Code within a day); Claude platform
release notes (.md hash); Anthropic sitemap (news/engineering); Devin CLI
changelog (.md); xAI release notes (.md); DeepSeek news sitemap; Bluesky
search (api.bsky.app, engagement-gated).

**Passed** — Terminal-Bench (data behind private Supabase RPC; revisit if
they publish); LMArena (no machine source since the HF space went stale);
SWE-bench Pro / MCPMark / Windsurf changelog (data embedded in page JS —
needs an RSC-extractor class; deferred); OSWorld (xlsx-only, needs openpyxl);
GAIA (near-saturated benchmark); BigCodeBench + llm-stats (dead repos); METR
(DVC-gated, reports covered via RSS); MCP registry firehose (100+ version
bumps per 3 days — revisit as weekly aggregate); npm /latest polls (covered
via GitHub atom equivalents); litellm release RSS (several/day; pricing file
watched instead); GCP incidents JSON (deferred; Vertex/Gemini filter sketched);
Railway status (our infra, not channel content); Qwen blog (moved to a
feedless SPA; partial coverage via qwen-code releases + HF trending);
r/OpenAI, r/Bard, r/GoogleGemini, r/vibecoding, r/replit, r/Devin,
r/aipromptprogramming, r/AgentsOfAI (consumer/showcase/dead); dev.to/t/ai
(content farm); Theo t3.gg (stale feed), Ray Fernando (off-scope).

**Borderline, revisit on audit evidence** — r/LLMDevs (~25% in-scope),
AICodeKing YouTube (fast but clickbait/daily).

### 2026-08-20 coverage/quality research

Full ranking and PR sequence:
`docs/superpowers/plans/2026-08-20-coverage-and-quality.md`.

Headline: the June widening already fetched the important closed-source
harnesses; `classify_rss` only Ships feed names containing `"releases"`
or `"release notes"`, so Cursor Changelog / Amp News / Copilot Changelog
(all HTTP 200, Cursor posting as of 2026-08-19) land in **Read**. Quality
work (routing, `REPO_PRIORITY`, tutorial-term contradiction, GHSA Watch
rebuild, enable `#12` flags) before adding more feeds.

**Added (proposed, not yet in code)** — Agent Client Protocol
`releases.atom` (HTTP 200; filter alphas / crate-churn); Google
Antigravity changelog (SPA, no RSS — pagewatch); Terminal-Bench 2.1
revisit (public board + `harbor-framework/terminal-bench-2-1`).

**Covered, but misrouted or stale** — Cursor Changelog, Amp News, Copilot
Changelog, Warp/Replit/Augment/JetBrains blogs (misrouted to Read);
Devin Release Notes RSS is the live Cognition changelog (through
2026-08-19); `windsurf.com/feed.xml` last item 2026-05-12 (stale after
the 2026-06-02 Devin Desktop rebrand).

**Passed** — Antigravity `/blog/rss.xml` (404); Devin blog `/blog/rss.xml`
(429 on this probe — don't add; release-notes RSS is enough);
MCP registry firehose, X/Discord, provider status, HF Spaces — unchanged.
r/Devin still passed pending an operator-grade sub after the rebrand.

**Retired line-item** — scheduled Monday 15:45 ingestion-audit DM. The
`audit` CLI and `send-clawbytes-audit` still exist for on-demand use;
`scheduler.py` does not run them (ops DMs stay exception-only).

**Retired** — 2026-06-24 · the six provider **status feeds** (Anthropic,
OpenAI, Cursor, GitHub, HF, OpenRouter) and the **security-advisory monitor**
(`claw-security-monitor.py`; GitHub advisories + Brave CVE search). Provider
status incidents were operational weather, not editorial signal — even capped
at 1/vendor/day they read as the same alert repeating, and they carried no
`EDITORIAL_SCOPE.md` mandate. The security monitor emitted nothing in
production (its Brave key was unset, which short-circuited both halves before
the GitHub pass, and that pass read the wrong REST fields anyway) and Brave is
being deprecated. Security-relevant items still reach Watch/Read via the
`SECURITY_TERMS` keyword routing on the RSS/Reddit/HN feeds.

## How to propose a source

Drop a URL on Sov's channel of choice. Whoever evaluates it:
1. Check this file and the code lists — is it already covered (directly, or via
   an upstream like an awesome list or HN)?
2. If new: validate the feed/API, add to the right monitor list, note it here as
   **added**.
3. If skipped: note it here as **passed** with the reason, so it isn't
   re-evaluated from scratch next time.

The weekly ingestion-audit DM (Mondays 15:45 UTC) reports per-source yield —
the evidence for pruning sources that never produce.
