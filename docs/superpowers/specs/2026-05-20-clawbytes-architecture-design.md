# ClawBytes Operational Architecture — Design Spec

**Date:** 2026-05-20
**Status:** Design approved in brainstorming session (Sov); spec doc pending review
**Supersedes:** Implicit "producer-VM sync" operational model
**Next step:** Implementation plan via writing-plans skill (after spec review)

**Revisions:**
- v1 (initial): used Anthropic API directly
- v2 (current): switched to Claude Code headless mode to route through Sov's existing subscription quota; auth via mounted Railway secret. See §3.5.

---

## 1. Context

ClawBytes is the public signal-surface of the Claws / OpenClaw ecosystem — a curated Telegram channel (`@clawbytes`, channel ID `-100REDACTED`) that posts four staggered "lanes" per day:

- **Ship** — what released that changes operator behavior
- **Watch** — what to worry about (security, supply-chain, breakage)
- **Read** — what to think about (longer-form pieces)
- **Community** — what builders are actually debating

Items cluster into persistent topical threads rather than dumping flat. The voice is opinionated and editorial — a reader should feel that someone with judgment read everything for them.

### Why this redesign exists

Prior operational model: the ClawBack producer VM ran monitors, made editorial decisions, and pushed code changes directly to GitHub. This produced four production regressions in a single push on 2026-05-20 (broken cron command, broken import path, parse-time SyntaxError, removed env-var fallback). Root cause: no review gate between producer-side changes and production deploy. Channel went dark for ~3 hours before discovery.

Additionally, investigation revealed the Railway cron architecture has never functioned: `[[cron]]` blocks at the top level of `railway.toml` are not a recognized schema and are silently ignored. The four lane-publish crons and the collect cron have not been firing on Railway — the entire thread architecture (`clawbytes_threads.py`) has not been operational. What has been running: a one-shot `clawbytes_daily.py --send` at each container start, which posts a flat combined digest and then exits.

### Desired outcomes (the design must serve these)

**For the reader:**
- A curated tape of what matters in the Claws / agent ecosystem — replaces checking 20 sources
- The 4 lanes are an editorial spine, not categories
- Threading turns related items into developing stories, not floods of near-duplicates
- Voice has taste — opinionated takes, not aggregator copy

**For Sov:**
- ClawBytes is part of the Claws product story, not a side project
- Must run without Sov in the loop day-to-day; he can ignore it for a month and find it healthy on return
- Signals surfaced feed the personal vault as provenance
- Editorial standards reflect Sov even when Sov isn't curating

**Anti-outcomes the design must prevent:**
- **Silence** (today's failure mode) — cron-driven publishing with monitored heartbeat
- **Noise** — curator drops low-signal items per scope constitution
- **Drift** — scope constitution gates every source addition and every published item
- **Staleness** — supervisor actively grows and prunes the source list autonomously

---

## 2. Architecture overview

ClawBytes becomes a **seven-service Railway project**. All services build from the same Dockerfile and same repo; each is distinguished by `startCommand` + `cronSchedule`.

```
claw-bytes (Railway project, environment: production)
│
├── Postgres                     (existing, unchanged — growth metrics)
│
├── collect                      cron: */30 * * * *
│     `python clawbytes_threads.py collect --run-monitors`
│     Refreshes source state files; populates backlog.
│
├── publish-ship                 cron: 0 16 * * *    (9am PT)
├── publish-watch                cron: 0 19 * * *    (12pm PT)
├── publish-read                 cron: 0 22 * * *    (3pm PT)
├── publish-comm                 cron: 0  2 * * *    (7pm PT)
│     `python clawbytes_threads.py publish --category <lane> --send --use-curator`
│     Each: build deterministic bundle → ask curator → post to Telegram.
│     Falls back to deterministic bundle if curator unavailable.
│
└── supervisor                   cron: 0 14 * * *    (7am PT)
      `python scripts/supervisor.py`
      Runs Claude in audit mode: code health, cron health, source growth,
      regression detection. Auto-commits source/config changes; opens
      PRs/issues for code matters. Posts a daily all-clear to Sov.
```

### Three logical layers

```
┌──────────────────────────────────────────────────────────┐
│  PUBLISH LAYER  (4× daily, deterministic + curator)       │
│  Posts what's in the backlog. Curator can drop/rewrite,   │
│  cannot add items.                                        │
└──────────────────────────────────────────────────────────┘
                          ▲ reads backlog + thread state
┌──────────────────────────────────────────────────────────┐
│  COLLECT LAYER  (every 30 min, deterministic)             │
│  Runs current source list, populates backlog.             │
└──────────────────────────────────────────────────────────┘
                          ▲ reads source registry
┌──────────────────────────────────────────────────────────┐
│  SOURCING LAYER  (daily, Claude-driven, autonomous)       │
│  Grows/prunes source list. Auto-commits to repo.          │
│  Where ClawBytes evolves without Sov in the loop.         │
└──────────────────────────────────────────────────────────┘
```

The bottom two layers are existing code (`scripts/claw-*-monitor.py`, `clawbytes_threads.py`) being wired to actually fire. The top layer adds the autonomous growth that prevents staleness.

### Two systems, one repo

- **Deterministic core** — existing `clawbytes_threads.py` (publish + collect) and existing `scripts/claw-*-monitor.py` (sources). Behavior unchanged in this design; we wire it to fire correctly.
- **Claude layer** — two new Python entry points:
  - `scripts/curator.py` — called inline per publish, returns approved bundle JSON within a 60s budget
  - `scripts/supervisor.py` — called daily, audits and grows the system

Both invoke **Claude Code in headless mode** (`claude -p` with `--output-format json`) via `subprocess.run`, not the Anthropic API directly. This routes all Claude usage through Sov's existing Claude Code subscription quota rather than incurring separate API billing. See §3.5 for the invocation pattern and §10 for the credentials-mount mechanism.

A shared `scripts/claude_common.py` module handles the subprocess wrapping, scope-constitution loading, prompt assembly, and JSON output parsing.

---

## 3. The Curator

### Purpose

Apply editorial judgment to each lane bundle before it posts. The bundle the deterministic system would have posted is the baseline; the curator's job is to make it *better* (drop noise, sharpen blurbs) within a tight latency budget — or get out of the way.

### Powers

| Mutation | Allowed | Notes |
|---|---|---|
| Drop an item from the bundle | ✅ | Main signal-quality lever |
| Reorder items within the lane | ✅ | Re-rank lead vs supporting |
| Rewrite a per-item blurb | ✅ | Voice consistency, drop hype, fix factual claims |
| Rewrite the "Take" / lead-signal line | ✅ | The editorial spine of each post |
| Skip the entire publish | ✅ (loud) | Logged prominently; supervisor flags if happens >1×/week |
| Add a new item not in the bundle | ❌ | Curator works with what publisher provides; new additions are sourcing-layer territory |
| Change lane / channel target | ❌ | Lane decision is structural; if curator thinks an item is wrong-lane, it drops it and writes the reason to `discovered_references.json` for supervisor |
| Send to Telegram directly | ❌ | Curator returns JSON; publisher does the send |
| Touch any state file or repo content | ❌ | Pure function: bundle JSON in, bundle JSON out |

### Inputs (what the curator sees)

- **Candidate bundle** (items + blurbs + lead-signal line) — JSON on stdin
- **Last 14 days of posted items for this lane** — JSON, for anti-repeat
- **Sources registry** — so it can check whether an item's source is trusted vs. probationary
- **Scope constitution** (`EDITORIAL_SCOPE.md`) — prompt-included; the anchor against drift
- **Curator system prompt** (`docs/curator-prompt.md`) — voice and editorial standards

### Outputs (the contract)

Curator returns to stdout: a single JSON object with the same shape as the input bundle, plus an `_curator` metadata block:

```json
{
  "lane": "ship",
  "lead_signal": "openclaw 2026.5.19 shipped...",
  "take": "Release-driven day. Shipments matter more than discourse.",
  "items": [ {...}, {...}, {...} ],
  "_curator": {
    "approved": true,
    "dropped_item_ids": ["item_abc123"],
    "drop_reasons": {"item_abc123": "empty advisory title"},
    "rewrote_blurbs": ["item_def456"],
    "discovered_references": [
      {"source_type": "blog", "url": "https://example.com/feed", "rationale": "cited 3× by tracked sources this week"}
    ],
    "anchor_check": "in-scope",
    "model": "claude-sonnet-4-6",  // whatever Claude Code reported back; CLAUDE_MODEL env var passes --model
    "tokens_used": {"input": 8472, "output": 1183},  // from Claude Code --output-format json
    "duration_ms": 24831
  }
}
```

If `_curator.approved` is `false`, publisher skips the publish and logs the rationale.

The publisher is also responsible for **persisting** the curator's `_curator.discovered_references` array: each entry gets appended to `memory/discovered_references.json` (a flat append-only list) so the supervisor can drain it on its next run (§5.2 vector #3).

### Fallback (Claude Code failure, auth expiration, timeout)

If the `claude -p` subprocess exits non-zero, times out at 60s, returns malformed JSON, or signals auth failure (e.g., 401-equivalent from Claude Code), **publisher posts the original deterministic bundle as-is**. The fallback event is logged to `memory/degraded_publishes.json` with a `reason` field (`timeout` / `non_zero_exit` / `bad_json` / `auth_expired` / `quota_exhausted`).

This is a fail-open design. Channel reliability outranks editorial purity.

Supervisor reports if fallbacks happen >1×/24h. The `auth_expired` reason is **critical** and triggers an immediate Telegram DM to Sov — only Sov can re-auth Claude Code locally and refresh the mounted credentials (see §3.5 and §7).

### 3.5 Claude Code invocation pattern

Curator and supervisor both use this pattern. Implementation lives in `scripts/claude_common.py`.

```python
# scripts/claude_common.py (sketch)
import subprocess, json, os

def call_claude(prompt: str, *, allowed_tools: list[str] = None, timeout: int = 60) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if allowed_tools is not None:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise ClaudeCodeError(f"exit {result.returncode}: {result.stderr[:500]}")
    return json.loads(result.stdout)
```

**Why headless Claude Code instead of the Anthropic API:**

- **Uses Sov's existing subscription quota** — no separate API billing
- **Tool use comes free** — supervisor can read files, run shell commands, grep code without writing tool-handling glue
- **System prompt and context management are handled by Claude Code** — `claude_common.py` stays small

**Authentication:**

- Claude Code reads credentials from `~/.claude/.credentials.json` (OAuth access token + refresh token)
- Sov authenticates once locally (`claude login`), then exports the file contents into a Railway secret variable `CLAUDE_CREDENTIALS`
- Container entrypoint writes `$CLAUDE_CREDENTIALS` to `~/.claude/.credentials.json` before invoking any cron command
- Access tokens refresh automatically using the refresh token; the refresh token has a months-long TTL
- When the refresh token eventually expires, the container's `claude -p` calls start failing with auth errors → supervisor detects this and DMs Sov → Sov re-auths locally and updates the Railway secret

**Quota considerations:**

- Curator: 4 publish cycles × ~10K input tokens + ~1K output tokens = ~44K tokens/day
- Supervisor: ~1 call/day × ~50K tokens (heavier — reads more code/state) = ~50K tokens/day
- Total: ~95K tokens/day = ~3M tokens/month
- This sits comfortably within the Max plan's quota under typical usage but should be monitored. If quota tightens, the curator drops to a smaller model first; if still tight, deterministic-only mode is the natural backstop.

**Anthropic ToS note:** Using a personal subscription to run a background service in a container is in a gray area of Anthropic's terms — they're written assuming interactive personal use. If this becomes a problem, the migration path is to switch `claude_common.py` to use the Anthropic API directly (drop-in change to `call_claude()`), accepting per-token billing.

---

## 4. The Supervisor

### Purpose

The autonomous custodian. Grows the source list, catches code/cron health issues, opens PRs for fixes, posts a daily all-clear. The role the ClawBack producer used to fill — but as a gated, reviewable process rather than direct master pushes.

### Run schedule

Daily at 14:00 UTC (7am PT), 2 hours before the first publish lane (Ship at 16:00). This ordering matters: supervisor's source-list changes apply before the day's publishes consume them.

### Responsibilities

#### 4.1 Autonomous content sourcing

Detail in §5. Summary: discovers candidate sources, gates them against scope, runs probationary tracking, prunes dead sources, auto-commits all changes.

#### 4.2 Code health audit

1. **Compile check**: `python3 -m py_compile` over every `.py` file in the repo. Catches the class of regression we hit on 2026-05-20.
2. **Smoke check**: `python <entry> --help` on every Python entry point with argparse. Catches broken flags.
3. **Static checks** (cheap): `python -m pyflakes` on top-level orchestrators if available; not required.

#### 4.3 Cron health audit

Query Railway GraphQL API for each cron service's executions over the last 7 days. Flag:
- Any service that hasn't run on schedule (missed ≥1 expected fire)
- Any service with non-zero exit on its most recent run
- Any service with abnormal runtime (>3σ above 30-day mean)

#### 4.4 State-file sanity check

For each monitor's state JSON:
- File exists, parses as JSON
- `foundItems` / `alerts` / etc. non-suspiciously-empty (≥1 item in last 14 days unless source is genuinely quiet)
- `lastCheck` not stale beyond expected cadence

#### 4.4b Claude Code auth health

Supervisor runs a tiny `claude -p "ok"` smoke test at the start of every cycle. If it fails with an auth-related error, supervisor:
1. Logs the failure to `supervisor-log/`
2. Sends a **critical Telegram DM** to Sov: "Claude Code auth expired in container — re-auth locally and update `CLAUDE_CREDENTIALS` Railway secret"
3. Continues the run with Claude-dependent steps skipped (compile-check and cron health audit still run; sourcing growth is paused for the day)

Until Sov rotates the credentials, the publish lanes will fall back to deterministic mode (channel stays alive, just no curation).

#### 4.5 Regression diagnosis & remediation

When supervisor finds a code-side problem:

| Confidence | Action |
|---|---|
| **Confident fix** (mechanical, pattern-matches known failure, fix locally compiles) | Open PR with diff + compile-pass evidence + rationale. Sov reviews & merges. |
| **Diagnosed but uncertain** (knows what's broken, fix is judgment-laden) | Open issue with diagnosis + suggested approach + code links. No diff. Sov decides. |
| **Confused** (something's wrong, root cause unclear) | Open issue with full evidence: symptom, what was checked, what couldn't be determined. Sov investigates. |

Supervisor must never push code directly to master — all code changes flow through PRs.

#### 4.6 Daily summary commit

Supervisor appends a daily summary to `supervisor-log/YYYY-MM-DD.md` in the repo:
- Actions taken (sources added/removed/graduated, PRs opened, issues opened)
- Health audit results (counts, anomalies, missed crons)
- Curator fallback events (count, dates)
- Growth metrics (sources tracked, items per lane, approve/drop ratios)

Git history becomes the audit trail.

#### 4.7 Notification dispatch

See §7.

### Inputs

- Read access to all repo files, all state files, Postgres growth metrics
- Read access to Railway GraphQL API (cron health audit) — via `RAILWAY_TOKEN` secret
- Read/write access to GitHub API (open PRs/issues, commit to repo, query activity) — via `GITHUB_TOKEN` with `repo` scope
- Claude Code installed in the container with valid mounted credentials (see §3.5) — the supervisor inherits the same auth setup as the curator
- Scope constitution + supervisor system prompt (`docs/supervisor-prompt.md`)

### Outputs

- Git commits to source-list files (auto-applied)
- Git commits to `supervisor-log/`
- GitHub PRs (code changes)
- GitHub issues (diagnoses without fixes)
- Telegram DMs to Sov (daily all-clear + critical alerts)
- Postgres growth metrics row

### Forbidden actions

- Push to master directly (only via PR)
- Edit Python code outside of opening a PR
- Touch curator/supervisor own source code (avoid recursion footguns)
- Disable other supervisor responsibilities (e.g., can't decide to skip compile-check)
- Send to the public @clawbytes channel (DMs to Sov only)

---

## 5. Autonomous content sourcing & the scope constitution

The supervisor's growth engine. Runs every supervisor cycle (daily).

### 5.1 The scope constitution

A repo-tracked Markdown file: `EDITORIAL_SCOPE.md` at repo root. Defines what counts as ClawBytes-relevant content. Included verbatim as a system-prompt section in every curator and supervisor Anthropic call.

Strawman content:

```markdown
# ClawBytes Editorial Scope

## In scope
- Claws / OpenClaw and its derivatives (Hermes, PicoClaw, IronClaw, Moltis, etc.)
- AI agent runtimes, frameworks, SDKs (Claude Agent SDK, MCP, LangChain/Graph, etc.)
- LLM releases that materially change agent capabilities
- Security, safety, supply-chain concerns for any of the above
- Tools agent builders rely on (vector DBs, eval frameworks, observability, browser
  automation, code-execution sandboxes)
- People building or shaping the above

## Out of scope
- Generic AI/ML news (image/video gen, etc.) unless notable for agents
- Application-layer chatbot products
- AI ethics meta-discourse not tied to specific agent capability
- VC/stock news not tied to specific Claws-ecosystem moves
- Tutorials/explainers (vs. announcements/analyses)

## Tone & take
- Opinionated, declarative. "X happened. Here's why it matters."
- No hype amplification. If a release is incremental, say so.
- Operator-centric — what changes for someone building/running agents.
- Brevity is a feature.
```

Sov edits this when shifting editorial direction. Both curator and supervisor immediately respect the new boundaries.

### 5.2 Source discovery vectors

The supervisor runs all of the following on each cycle:

1. **Reference extraction** — read last 14 days of items + curator decisions. Pull every external reference (repo URL, blog domain, person name, subreddit, framework name) that isn't in the current source registry. Each becomes a candidate.
2. **Active discovery** — invoke existing `scripts/claw-discover.py` (Brave Search + GitHub trending) and `scripts/claw-source-discovery.py` (RSS/subreddit/HN auto-discovery).
3. **Curator-flagged references** — drain `memory/discovered_references.json` (written by curator when it sees something interesting).
4. **Notion editorial signals** — invoke `scripts/claw_notion_signals.py` to pick up things Sov has been writing about in the vault.

### 5.3 Anchor gate (scope check)

For each candidate, supervisor asks Claude: *"Does this fit the editorial scope below? Answer with anchor_pass: true/false and a one-line rationale."* The full scope constitution is included.

Candidates that fail the gate are dropped immediately. Sov never sees them. (If a class of legitimate signals keeps failing, the scope constitution is what needs editing — not the gate.)

### 5.4 Probationary tracking

Candidates that pass the gate enter a `memory/candidate_sources.json` registry — tracked but not yet posted-from. A candidate is monitored for 14 days. If, during that window, it produces N≥3 items that the curator approves into actual posted bundles, it **graduates** to the first-class `memory/claw-ecosystem-sources.json`.

If a candidate produces zero approved items in 14 days, it's dropped from the candidate registry (silently — no commit churn).

### 5.5 Pruning

For each first-class source:
- If it returns HTTP 404 / 500 / connection error for 3+ consecutive cycles, **remove immediately** (dead source).
- If it produces zero items in 60 days, **remove with rationale** ("source has gone quiet").
- If curator drops >80% of its items over 30 days, **flag for review** (open issue, don't auto-remove — might be a quality-bar dispute).

### 5.6 Source-list commit policy

All source-list changes — graduations, removals, weight adjustments — auto-commit to the repo:

- **One commit per supervisor run**, batching all source changes
- Commit message: structured rationale (which sources, which reason, supporting evidence)
- Author: `Supervisor <supervisor@clawbytes>` (distinct identity)
- Reversible: Sov can `git revert` any supervisor commit if a decision was wrong

Limit: no more than **5 source additions and 5 removals per run**. Excess candidates queue in `memory/candidate_sources.json` for the next supervisor cycle (FIFO). If the queue grows beyond 30 unprocessed candidates, supervisor opens an issue ("ingest backlog growing — consider raising the per-run cap or tightening the scope gate").

### 5.7 Growth metrics

Each supervisor cycle writes a row to a Postgres `growth_metrics` table:

```sql
CREATE TABLE growth_metrics (
  run_at TIMESTAMPTZ PRIMARY KEY,
  sources_tracked INT,
  candidates_in_probation INT,
  sources_added_this_run INT,
  sources_removed_this_run INT,
  items_collected_24h INT,
  items_posted_24h INT,
  curator_approve_rate NUMERIC(4,3),
  fallback_publishes_24h INT,
  scope_violations_24h INT
);
```

Plots over time become the dashboard for "is the system staying healthy."

---

## 6. Day-in-the-life walkthrough

A concrete Wednesday, end to end.

```
14:00 UTC  supervisor cron fires
  ├─ compile-check all .py files                  ✅ pass
  ├─ argparse smoke-test each entry               ✅ pass
  ├─ cron health audit (last 7 days)              ⚠️ publish-watch missed Mon (logs: feed timeout)
  ├─ state-file sanity check                      ✅ all populated, fresh
  ├─ reference extraction from last 14d           → 12 new candidates
  ├─ scope-gate candidates                        → 4 pass anchor, 8 dropped
  ├─ claw-discover.py + source-discovery.py       → 3 more candidates
  ├─ probationary tracking eval                   → 2 from 2 wks ago graduate
  ├─ pruning                                      → 1 dead RSS feed auto-removed
  ├─ auto-commit: claw-ecosystem-sources.json (2 graduated, 1 removed)
  ├─ issue #47 opened: "publish-watch missed Mon — RSS source X timed out"
  ├─ daily summary commit: supervisor-log/2026-05-21.md
  ├─ growth_metrics row inserted
  └─ Telegram DM to Sov: "✅ Wed supervisor: 2 sources graduated, 1 removed, 1 issue"

14:30, 15:00, 15:30, 16:00, ...  collect cron fires every 30 min
  Each: refresh source state JSONs, append fresh items to backlog, exit 0

16:00 UTC  publish-ship cron fires
  ├─ clawbytes_threads.py builds Ship bundle (4 items, ranked)
  ├─ shells out to curator.py with bundle as JSON on stdin
  │     curator (Anthropic, 30s budget):
  │       reads bundle + last 14d Ship posts + scope constitution
  │       decision: keep 3, drop 1 (empty advisory title), rewrite 1 blurb
  │       return JSON
  ├─ publisher receives approved bundle within 28s   ✅
  ├─ posts to @clawbytes Telegram channel             ✅
  ├─ writes posted_items to thread-state.json
  └─ exit 0

  (Alternate: Anthropic API outage)
  ├─ curator subprocess times out at 60s
  ├─ publisher falls back to deterministic bundle (unchanged)
  ├─ posts deterministic version                      ✅ channel stays alive
  └─ logs fallback to memory/degraded_publishes.json

19:00, 22:00, 02:00  publish-watch / publish-read / publish-comm
  Same shape, lane-specific.
```

---

## 7. Failure handling & notifications

### 7.1 Failure modes & catchers

| Failure | Caught by | Response |
|---|---|---|
| Source monitor crash (one feed) | Existing "Monitor returned non-zero" log → supervisor's cron audit | PR or issue opened with diagnosis |
| Collect cron doesn't fire | Supervisor's cron health audit (next day) | Critical DM if collect skipped >2h |
| Curator returns malformed JSON | Publisher's JSON parse → fallback to deterministic | Logged as degraded publish; supervisor opens issue if pattern |
| Claude Code subprocess timeout / crash | Curator subprocess error → fallback to deterministic | Channel stays alive; supervisor DMs Sov if happens 2+ times in 24h |
| Claude Code auth token expired | `claude -p` returns auth error → fallback to deterministic + supervisor 4.4b smoke test catches it | **Critical DM to Sov** — only Sov can rotate the `CLAUDE_CREDENTIALS` Railway secret |
| Subscription quota exhausted | `claude -p` returns quota-related error → fallback to deterministic | Supervisor DMs Sov; deterministic publishing continues |
| Telegram returns 401 (token revoked / bot removed) | Publisher's send fails | **Critical DM to Sov** — channel is silent |
| Postgres down | Falls through to JSON-file state (already primary) | Growth metrics gap, no functional impact on publishing |
| Supervisor itself crashes | No daily DM arrives | **Absence-of-heartbeat IS the alert** — Sov notices when the daily all-clear stops |
| Producer-style regression (bad commit) | Supervisor's compile-check next run | Supervisor's own PRs are compile-checked before opening. Catches anything else pushed raw. |
| Curator dropping >50% of items in a lane | Supervisor's curator-decision audit | Issue opened: scope constitution may need editing, or source quality has degraded |
| Source-list explosion (Claude adding too aggressively) | Per-run cap: 5 additions, 5 removals | Larger swings require issue Sov approves |

### 7.2 Notification channels

| Channel | When | Why |
|---|---|---|
| **GitHub** (PRs + issues + watched-commits) | All code-side findings, all source-list auto-commits | Native to the action |
| **Telegram DM to Sov** | Critical only: channel silent >24h, Telegram 401, supervisor crash (heartbeat absence), curator >50% drop rate in a lane | Things needing *today* attention |
| **Daily supervisor summary DM** | Every supervisor run | The all-clear. Establishes that Sov can ignore the system safely. Silence = good is reversed into noise = good. |

### 7.3 Sov's ongoing operational load

| Cadence | Effort | What |
|---|---|---|
| Daily | ~30 sec | Glance at supervisor all-clear DM |
| Weekly | ~5 min | Triage supervisor's PRs and issues |
| Monthly | ~15 min | Review scope drift, growth metrics, audit recent auto-commits |
| Quarterly / ad-hoc | Variable | Edit scope constitution or curator/supervisor system prompts when direction shifts |

---

## 8. Test plan

### Unit-testable components

- **Curator**: feed a known bundle JSON, mocked Anthropic client, assert returned JSON has expected mutations. Test scope-gate logic in isolation.
- **Supervisor**: each responsibility (compile-check, cron audit, source pruning, etc.) is a separate function with mockable inputs.
- **Source registry mutations**: pure functions that take registry + proposed change → new registry. Easy property-testing.

### Integration tests

- **End-to-end publish with curator**: `clawbytes_threads.py publish --category ship --preview --use-curator` runs full pipeline but posts to a test Telegram chat instead of `-100REDACTED`. Run before any production deploy.
- **Supervisor dry-run mode**: `python scripts/supervisor.py --dry-run` emits all proposed actions to stdout without committing/PR-ing or DM-ing. Run nightly during initial rollout to verify behavior.
- **Migration confidence test**: dry-run supervisor against current state, manually inspect actions. Ship when actions look sane.

### Production monitoring (the heartbeat)

The daily supervisor DM IS the production-monitoring signal. If a DM doesn't arrive, something failed silently. No separate monitoring service required.

---

## 9. What this design removes

| Removed | Why |
|---|---|
| `clawbytes_daily.py` | Superseded by the per-lane thread system. Currently crash-loops on Railway as the startCommand. |
| `Dockerfile` `CMD ["python", "clawbytes_daily.py", "--send"]` | Becomes irrelevant — cron services define their own startCommand. Default CMD becomes a no-op (`tail -f /dev/null` or similar) for the rare case the image runs without a startCommand. |
| `railway.toml` `[deploy] startCommand` | Each cron service has its own startCommand. The repo-root `railway.toml` becomes build-config only. |
| `railway.toml` top-level `[[cron]]` blocks | Not a recognized Railway schema — silently ignored. Replaced by per-service `cronSchedule` configured at the Railway service level. |
| Root duplicate `claw-ecosystem-monitor.sh` | Already removed in commit `11ee56b`. Documented here for completeness. |
| `psycopg2-binary` from `requirements.txt` (if unused after growth-metrics impl) | Re-evaluate post-migration. If growth metrics use Postgres, this stays; if file-only, remove. |
| `feedparser` from `requirements.txt` | Unused — RSS monitor parses with `xml.etree`. Remove. |

---

## 10. Migration plan (sketch — full plan via writing-plans)

**Phase 0 — preconditions**

- Telegram bot token verified and active (done 2026-05-20)
- Four regression fixes deployed (done 2026-05-20: commits `b644996`, `11ee56b`, `ace2f60`, `51acb4f`)

**Phase 1 — scaffolding (no behavior change yet)**

- Write `EDITORIAL_SCOPE.md`, `docs/curator-prompt.md`, `docs/supervisor-prompt.md`
- Write `scripts/claude_common.py` (subprocess wrapper around `claude -p --output-format json`)
- Write `scripts/curator.py` skeleton with `--dry-run` flag (returns bundle unchanged, logs would-be decisions)
- Write `scripts/supervisor.py` skeleton with `--dry-run` flag
- Update `Dockerfile`: add Node + `npm install -g @anthropic-ai/claude-code`
- Update entrypoint script: write `$CLAUDE_CREDENTIALS` env var into `~/.claude/.credentials.json` before exec'ing the cron command
- Sov authenticates Claude Code locally (`claude login`), copies `~/.claude/.credentials.json` contents into a new Railway secret `CLAUDE_CREDENTIALS`
- Optional: set `CLAUDE_MODEL` Railway variable (defaults to whatever Claude Code uses if unset)

**Phase 2 — Railway cron services**

- Create 6 new Railway services in `claw-bytes` project (collect, publish-ship, publish-watch, publish-read, publish-comm, supervisor), all pointing at the same repo
- Configure each service's `startCommand` and `cronSchedule` via dashboard or per-service `railway.json`
- Set `--use-curator` flag off initially (publish runs purely deterministic)
- Retire `clawbytes_daily.py` CMD on existing `clawbytes` service; convert to a no-op startCommand or delete the service

**Phase 3 — deterministic verification**

- Watch one full day of cron firings: collect every 30 min, 4 publish lanes at their schedules
- Verify: state files refresh, backlog populates, lane posts appear in @clawbytes, no errors in any service's logs
- Supervisor service runs in `--dry-run` mode initially — review what it *would* have done daily for ~3 days

**Phase 4 — supervisor live**

- Remove supervisor `--dry-run` flag; let it auto-commit source changes and open PRs
- Verify daily DM arrives
- Monitor growth_metrics for 1 week to confirm sane behavior

**Phase 5 — curator live**

- Turn on `--use-curator` flag for one lane (recommend Read first — lowest stakes if a curator decision is weird)
- Verify curator decisions look reasonable for 3-5 cycles
- Roll out to remaining lanes one at a time

**Phase 6 — cleanup**

- Remove dead `clawbytes_daily.py` from repo
- Remove `[[cron]]` blocks from `railway.toml`
- Remove unused `feedparser`, `psycopg2-binary` from requirements (if applicable)
- Document the new ops model in `README.md`

---

## 11. Open questions / future work

- **Anthropic ToS gray area**: using a personal Claude subscription to run a background container service is not what the subscription is designed for. If Anthropic flags this, the migration path is to swap `scripts/claude_common.py` to call the Anthropic API directly (one-file change), accepting per-token billing. Worth re-reading Anthropic's ToS before going live and possibly contacting them if in doubt.
- **Credentials rotation cadence**: the OAuth refresh token in Claude Code has an undocumented but months-long TTL. First rotation will be a learning event — supervisor's auth-health check (§4.4b) is the early-warning system.
- **Curator prompt versioning**: when Sov edits the curator system prompt mid-week, do we tag posts with the prompt version that produced them? (Probably yes, in `_curator.prompt_version`.)
- **Multi-channel publishing**: the email digest path (`claws_digest.py`) is currently parallel and undocumented. Does it use the same curator/supervisor pipeline? (Out of scope for this design; addressable in a follow-up.)
- **Engagement signals**: if Telegram exposes view counts / forwards, could feed back into source-quality scoring. Future enhancement.
- **Cost ceiling enforcement**: if Anthropic API daily cost exceeds `MAX_COST_PER_DAY`, supervisor should disable curator (fallback to deterministic) and DM Sov. Worth implementing in Phase 5.
- **Recursive editing protection**: supervisor must not edit its own source code or curator source via PR (footgun). Enforce with a path-blocklist in the supervisor's PR-opening helper.

---

*Design captured during brainstorming session 2026-05-20 between Sov and Claude. Next step: implementation plan via writing-plans skill.*
