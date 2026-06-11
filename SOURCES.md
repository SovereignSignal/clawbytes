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
| Security advisories | GitHub advisories for ~28 watched repos + CVE search | `scripts/claw-security-monitor.py` | 30 min |
| Moltbook | Community posts, HTML scrape | `scripts/claw-moltbook-monitor.py` | 30 min |
| Discovery | GitHub topic/keyword search, awesome lists (awesome-ai-agents, awesome-agents, awesome-mcp-servers, awesome-claude-code, awesome-code-ai), Brave search | `claw-ecosystem-monitor.sh --mode discover`, `claw-source-discovery.py` | weekly (Mon 14:10 UTC) |
| Leaderboards | SWE-bench (Verified, bash-only) and Aider polyglot — emits only on top-3 movement, sha-gated fetches | `scripts/claw-leaderboard-monitor.py` (`BOARDS`) | 30 min |

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
