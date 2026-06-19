# ClawBytes

Signal aggregator for the AI coding-harness ecosystem. Collects from RSS/Atom feeds, vendor changelogs, provider status pages, Reddit, Hacker News, HuggingFace papers, security advisories, benchmark leaderboards, model registries, feedless vendor pages, and Bluesky — classifies every item into four editorial lanes, and publishes staggered lane bundles to the [@clawbytes](https://t.me/clawbytes) Telegram channel and a mirrored Slack channel.

Editorial scope lives in [`EDITORIAL_SCOPE.md`](EDITORIAL_SCOPE.md); the full source inventory and the candidate decision log live in [`SOURCES.md`](SOURCES.md).

## Lanes

| Lane | Emoji | Carries |
|------|-------|---------|
| **Ship** | ⚙️ | New releases, changelog moves, model listings, capability shifts |
| **Watch** | 🚨 | Security advisories, provider incidents, breakage, risk |
| **Read** | 📚 | Substantive analysis, papers, deep dives worth the click |
| **Community** | 💬 | What operators are actually discussing (Reddit, HN, Bluesky) |

Each lane has its own TTL, scoring thresholds, and staggered posting windows — see `CATEGORY_META` in `clawbytes_threads.py` for the live config. Items are deterministically ranked and deduped (by source key, backlog id, and posted URL); only the top of a lane that clears its quality bar in its window gets published.

## Architecture

```
source monitors (scripts/)  →  shared backlog JSON  →  classifiers  →  lanes  →  Telegram + Slack
   write per-source state         memory/*.json        clawbytes_threads.py      (mirror)
```

Monitors only **fetch and remember** (each owns a state file under the memory dir). All **editorial judgment** lives in one place — the `classify_*` functions in `clawbytes_threads.py`. Widening intake and tuning routing are therefore independent changes.

**Two-tier enrichment.** Per-item summaries are written by a cheap high-volume model (`CLAWBYTES_LLM_*`). When `CLAWBYTES_USE_CURATOR=1`, each lane bundle also gets a stronger per-publish **curator** pass (`scripts/curator.py`) that drops weak items, rewrites blurbs, and adds an editorial take — runnable on an Ollama-cloud model (`CLAWBYTES_CURATOR_*`) or the Claude CLI. The curator fires ~4×/day (once per lane window), so a strong model there is cheap. Any curator failure or whole-lane decline falls back to the deterministic post — the channel never goes quieter than the un-curated path.

Deployed on Railway as a single always-on container running `scripts/scheduler.py` (APScheduler), against a persistent volume mounted at `CLAWBYTES_MEMORY_DIR`. There is **no database** — state is JSON on the volume. (Earlier docs referenced PostgreSQL/per-service crons; neither is used.)

## Source classes

The authoritative, current list with file references is in [`SOURCES.md`](SOURCES.md). In brief: RSS/Atom feeds (vendor blogs, changelogs, GitHub `releases.atom`, research, ArXiv), provider **status** feeds, Reddit, Hacker News, HuggingFace Daily Papers, GitHub security advisories, weekly **discovery** (GitHub topics, awesome-list diffs, Brave), benchmark **leaderboards** (SWE-bench, Aider, LiveBench — sha-gated, emit on top-3 movement only), model **registries** (OpenRouter, LiteLLM pricing, HF trending), **feedless pages** (Mintlify `.md` hashes + sitemap slug diffs for Anthropic/Claude/Devin CLI/xAI/DeepSeek), and **Bluesky** phrase search.

## Scheduler jobs

`scripts/scheduler.py` runs everything in one process (UTC):

| Job | Schedule | What |
|-----|----------|------|
| `collect` | every :00/:30 | run all monitors → refresh backlog |
| `autopublish` | hourly :05 | publish any lane that is ready in its window (gated by `CLAWBYTES_PUBLISH`) |
| `health_check` | hourly :20 | **alert-only** — DMs the admin only if collect stalled or the channel went silent |
| `discover` | Mon 14:10 | weekly source discovery |

There are **no routine status reports**. Ops DMs are exception-only: the admin hears from the bot only when something breaks. See "Reporting model" below.

## Reporting model

- **@clawbytes (Telegram) + Slack channel** — audience surfaces. Content lane posts only. Every Telegram channel post mirrors to Slack (`mirror_to_slack` in `clawbytes_threads.py`).
- **Admin Telegram DM** (`CLAWBYTES_ADMIN_CHAT_ID`) — operations only, and only on breakage. Every ops DM is prefixed with an unmistakable `🔧 OPS REPORT` banner. Routine previews/audits are available **on demand** (`preview`, `audit`) but are never pushed.

## Environment variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Audience channel id | ✅ |
| `CLAWBYTES_MEMORY_DIR` | Path to the state volume | ✅ (on Railway) |
| `CLAWBYTES_PUBLISH` | `1`/`true` to actually post; otherwise dry | publish gate |
| `CLAWBYTES_ADMIN_CHAT_ID` | Admin user chat id for ops alerts | for alerts |
| `CLAWBYTES_SLACK_CHANNEL_ID` + `SLACK_BOT_TOKEN` | Slack mirror target (audience copy of channel posts) | for mirror |
| `CLAWBYTES_OPS_SLACK_CHANNEL_ID` + `SLACK_BOT_TOKEN` | Slack ops fallback for admin alerts when the Telegram DM can't be delivered (e.g. a Telegram outage) | for alert resilience |
| `CLAWBYTES_LLM_URL` / `CLAWBYTES_LLM_API_KEY` / `CLAWBYTES_LLM_MODEL` | Per-item enrichment LLM (OpenAI-compatible) | for enriched summaries |
| `OPENAI_API_KEY` | Fallback for `CLAWBYTES_LLM_API_KEY` if that's unset | optional |
| `CLAWBYTES_USE_CURATOR` | `1` to run the per-lane curator editorial pass in autopublish | optional |
| `CLAWBYTES_CURATOR_URL` / `CLAWBYTES_CURATOR_MODEL` / `CLAWBYTES_CURATOR_API_KEY` | Curator backend (OpenAI-compatible; key falls back to `CLAWBYTES_LLM_API_KEY`). If unset, the curator uses the Claude CLI. | optional |
| `CLAWBYTES_CURATOR_LANES` | Comma-separated lanes to curate (default all four) | optional |
| `GITHUB_TOKEN` | Lifts GitHub API rate limits (release-note grounding, leaderboard/registry sha checks, discovery) | recommended |
| `BRAVE_API_KEY` | Discovery search | optional |

## Local development

```bash
git clone https://github.com/SovereignSignal/clawbytes.git
cd clawbytes
python3 -m venv venv && venv/bin/pip install -r requirements.txt

# Always run against a throwaway state dir locally — never the repo's memory/.
export CLAWBYTES_MEMORY_DIR=/tmp/cb-dev

python3 clawbytes_threads.py collect --run-monitors --summary   # refresh backlog
python3 clawbytes_threads.py preview --category ship            # render a lane (no send)
python3 clawbytes_threads.py audit                              # source → classifier → score → lane
python3 -m pytest tests/ content-engine/tests/ -q               # test suite
```

Publishing requires `--send` and a live token; `preview` never posts.

## Repo map

**Root:** `clawbytes_threads.py` (the live collector/classifier/publisher), `clawbytes_daily.py` + `claw-digest-generator.py` (legacy single-shot digest), `EDITORIAL_SCOPE.md`, `SOURCES.md`.

**`scripts/` — scheduler + monitors:** `scheduler.py`; the monitors `run_monitors()` invokes directly — `claw-rss-monitor.py`, `claw-reddit-monitor.py`, `claw-hn-monitor.py`, `claw-security-monitor.py`, `claw-moltbook-monitor.py`, `claw-leaderboard-monitor.py`, `claw-registry-monitor.py`, `claw-pagewatch-monitor.py`, `claw-bsky-monitor.py`, and `claw-ecosystem-monitor.sh` (releases + discovery); `claw-hf-papers.py` (HF Daily Papers — run *inside* `claw-ecosystem-monitor.sh`, not standalone); `claw-source-discovery.py` (weekly discovery). (Notion/Proton/people-tracker scripts are legacy VM-era and not wired into the scheduler.)

**`content-engine/` — Slack reporting helpers** (`send-clawbytes-report`, `send-clawbytes-audit`); pure stdlib, used on demand.

## License

MIT
