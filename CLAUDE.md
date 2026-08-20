# ClawBytes — agent working notes

ClawBytes is a signal aggregator for the AI coding-harness ecosystem. It collects from ~9 monitor classes, classifies every item into four editorial lanes (Ship / Watch / Read / Community), and publishes staggered lane bundles to the @clawbytes Telegram channel + a mirrored Slack channel. Read `README.md` for the public overview, `EDITORIAL_SCOPE.md` for what's in/out of scope, and `SOURCES.md` for the live source inventory + candidate decision log.

## Architecture in one breath

`scripts/*` monitors **fetch and remember** (each owns a `memory/<name>-state.json`) → `clawbytes_threads.py` **classifies and publishes** (the `classify_*` functions + the lane machinery) → Telegram, mirrored to Slack. On Railway it all runs in one always-on container via `scripts/scheduler.py` (APScheduler) against a persistent volume. **No database** — state is JSON on the volume at `CLAWBYTES_MEMORY_DIR`.

The split is the point: adding a source and tuning how items are routed are independent changes. A new monitor writes a state file and gets a `collect_candidates()` entry + a `classify_*` branch; routing vocabulary lives in `READ_TERMS`/`SECURITY_TERMS`/`REPO_PRIORITY` and the classifiers.

## Hard invariants — break these and the channel breaks quietly

1. **`repo_name_from_feed()` matches `REPO_PRIORITY` keys as substrings in dict-insertion order.** More-specific keys must precede more-general ones (`"claude agent sdk"` and `"claude code action"` before `"claude"`). Adding a substring-overlapping key in the wrong position silently mis-prioritizes releases.
2. **Release / release-notes / coding-agent changelog feeds bypass the keyword relevance gate** (`is_relevant()` in `claw-rss-monitor.py`): their titles are versions, dates, or feature names with no keywords. The gate keys on `"releases"`, `"release notes"` in the feed name, plus coding-agent-tagged changelog/news feeds (Cursor Changelog, Amp News, Copilot Changelog). The generic GitHub Changelog stays keyword-gated. (Provider **status** feeds were retired 2026-06-24: `classify_rss` drops the status branch and the feeds were removed from `claw-rss-monitor.py`.)
3. **Substring keyword traps.** `READ_TERMS`/`RELEVANCE_KEYWORDS` are substring-matched. Never add bare tokens that live inside common words (`"opus"`⊂corpus, `"droid"`⊂android, `"augment"`⊂augmentation, `"amp"`⊂example). Use anchored compounds (`"opus 4"`, `"augment code"`, `"amp news"`).
4. **Per-item-unique URLs for diff-style sources** (leaderboards, registries, pagewatch). The publish dedup keys on URL via `postedUrls`; a bare page URL means the first movement posts and every future one is swallowed. Encode the change (date/leader/sha) into the id and url.
5. **New monitors must baseline silently.** First sighting records state and emits nothing; only post-baseline diffs become items. Otherwise a new source dumps its entire backlog as "news" on first run.

## Reporting model — do not regress this

- **@clawbytes (Telegram) + Slack channel = audience.** Content lane posts only. Every Telegram channel post auto-mirrors to Slack (`mirror_to_slack`, best-effort — Slack failure never blocks Telegram).
- **Admin Telegram DM (`CLAWBYTES_ADMIN_CHAT_ID`) = ops, exception-only.** The bot DMs the admin ONLY when something breaks (job nonzero exit, or hourly `health_check` finds collect stalled >2h / channel silent >30h). **Silence means healthy.** Never wire a scheduled DM that fires when things are fine — that was explicitly built and removed. Previews/audits are on-demand only. Every ops DM carries the `🔧 OPS REPORT` banner. If the Telegram DM can't be delivered (e.g. a Telegram outage), the alert falls through to a separate Slack ops channel (`CLAWBYTES_OPS_SLACK_CHANNEL_ID`, same `SLACK_BOT_TOKEN`) — distinct from the audience-mirror `CLAWBYTES_SLACK_CHANNEL_ID` — so a Telegram outage can still page the operator. Both paths are isolated; alert delivery never raises.

## Working on this repo

- **The repo is PUBLIC** (`github.com/SovereignSignal/clawbytes`, default branch `main`). Anything committed is immediately visible. No secrets, channel IDs, personal emails, or internal infra (IPs/hostnames) in code, docs, or commit messages — all of that was scrubbed and git history was rewritten on 2026-06-12. Secrets come from env vars only; `.gitignore` blocks `CREDS.md` and `.env.*`.
- **Local testing: always `export CLAWBYTES_MEMORY_DIR=/tmp/cb-dev`** before running anything. The module resolves `MEMORY`, `ALLOWED_SUBREDDITS`, and dynamic feeds at import time; without the override you read/write the repo's tracked `memory/`. Tests do this in `tests/conftest.py`.
- **TDD.** Every classifier branch and monitor has tests under `tests/` (46 currently). Run `python3 -m pytest tests/ content-engine/tests/ -q`. When adding a feed, verify the URL fetches valid XML/JSON before committing — several "covered" feeds had silently rotted (LangChain post-Webflow, a DeepMind feed that served corporate PR).
- **Verify, don't assume.** `python3 clawbytes_threads.py audit` shows where every raw item dies (rejected/skipped/would_add + reason). `preview --category <lane>` renders a lane with live enrichment and never posts. Use `railway run` to render with production tokens/grounding without publishing.
- **Commit identity:** SovereignSignal. End commits with the `Co-Authored-By: Claude` trailer.

## Deploy & ops

Railway project `9b6a552c-…`, service `78e92a76-…` (`clawbytes`), env `d82d0b1e-…`, branch `main`. Push to `main` → auto-deploy. Operate via the **railway-ops** skill (the MCP tools are often Unauthorized; the skill's CLI+token path works). Reach the container, deploy status, and logs through that skill. `CLAWBYTES_PUBLISH=1` is the live-posting gate; `GITHUB_TOKEN` (set) lifts API limits for release-note grounding, leaderboard/registry sha checks, and discovery.

Scheduler jobs (UTC): `collect` every :00/:30, `autopublish` hourly :05, `health_check` hourly :20 (alert-only), `discover` Mon 14:10.

## Watch out for

- The **ClawBack producer** historically pushed blanket "sync" commits that shipped regressions; the VM is now paused, but if such commits ever appear, treat with skepticism and `py_compile` the tree. See `[[clawbytes-producer-sync-hazard]]` in private memory.
- `clawbytes_daily.py` / `claw-digest-generator.py` are **legacy** single-shot digest code, not the live path. The Notion/Proton/people-tracker scripts are VM-era and not wired into the scheduler. The live system is `clawbytes_threads.py` + `scripts/scheduler.py` + the monitors in `run_monitors()`.
- Earlier docs/memory mention PostgreSQL, per-service `[[cron]]` blocks, a daily Slack report, and a tenspire LLM URL — **all superseded.** Trust this file and the running scheduler.

## Enrichment & curation

Two tiers. Per-item lane summaries come from a cheap high-volume model via `CLAWBYTES_LLM_*` (currently gemma on Ollama). When `CLAWBYTES_USE_CURATOR=1`, `autopublish` also runs `scripts/curator.py` per lane — a stronger editorial pass that drops weak items, rewrites blurbs, and adds a take. The curator has two backends: an OpenAI-compatible one (`CLAWBYTES_CURATOR_URL/MODEL/API_KEY`, currently deepseek-v4-pro on Ollama) and the Claude CLI (used only if the OpenAI vars are unset, needs `CLAUDE_CREDENTIALS`). Safety rule baked into `_publish_lane`: **any curator failure, fallback, or whole-lane decline drops to the deterministic post** — breadth/reliability over editorial purity; the curator can drop individual items but cannot silence a lane. Reasoning curator models need a generous `CLAWBYTES_CURATOR_MAX_TOKENS` (default 8000) since hidden CoT eats the completion budget.
