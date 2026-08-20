# Coverage and quality expansion — 2026-08-20

Research note, not an implementation PR. Goal: rank the next moves that
actually change what @clawbytes posts, instead of adding more feeds that
the classifier will misroute or the Ship bar will ignore.

**Date:** 2026-08-20
**Scope:** clawbytes live path (`scripts/*` monitors → `classify_*` →
lanes → curator → Telegram/Slack).
**Inputs:** `EDITORIAL_SCOPE.md`, `SOURCES.md` (incl. the 2026-06-12
candidate log), classifiers in `clawbytes_threads.py`, live feed probes
on 2026-08-20, the June widening plan, and the current 2026 harness
landscape (Devin Desktop rebrand, Google Antigravity, ACP).

Durable copy in Notion (child of the ClawBytes hub):
https://app.notion.com/p/3c2000c0d590812c8545d810a57a705f

## Verdict

**Quality before coverage.** The June widening already put the important
closed-source harnesses on the wire. The channel is leaking them in
classification, scoring, and Watch — not because the monitors never
fetched them.

Three facts drive the ranking:

1. `classify_rss` only special-cases feed names containing `"releases"`
   or `"release notes"`. Vendor **changelogs/blogs** fall through to
   `READ_TERMS` and land in **Read**, or get dropped. Cursor Changelog,
   GitHub Copilot Changelog, Amp News, Windsurf/Warp/Replit/Augment/
   JetBrains blogs are in this bucket. Cursor's own feed is posting
   operator-facing items *today* (`Cloud Agents and Cursor Harness
   Improvements`, 2026-08-19) — into Read, not Ship.
2. `REPO_PRIORITY` still doesn't include Amp, Factory, Warp, Windsurf/
   Devin Desktop, Replit, Augment, JetBrains, Copilot. Unknown-repo
   GitHub releases start at base 50; Ship's first window requires
   `min_top_score` 58. Fresh age bonus can sneak them in; a day-old
   patch cannot. Afternoon Ship (`min_top_score` 85) is effectively
   OpenClaw / Hermes only.
3. Watch lost its dedicated source on 2026-06-24 (status feeds +
   `claw-security-monitor.py`). Security now depends on substring
   `SECURITY_TERMS` in RSS/Reddit/HN titles. The previous GHSA path
   was broken (wrong REST fields + Brave short-circuit); a correct
   GitHub-advisory pass is still the right Watch rebuild.

Coverage still has real holes (Antigravity, ACP, Devin Desktop rename,
stale Windsurf blog, Terminal-Bench). They are the second wave, not
the first.

---

## What is already working (do not redo)

- Nine monitor classes on a 30-min collect; silent baselining; URL-keyed
  dedup; curator fail-open to the deterministic post.
- GitHub `releases.atom` for the open-source harness/SDK set (Claude
  Code, Codex, Aider, Cline, Roo, Goose, OpenHands, Crush, Qwen Code,
  Smolagents, OpenCode, Gemini CLI, MCP SDKs, E2B, …).
- Cursor / Copilot / Devin / Factory / Amp feeds exist and fetch 200.
- Leaderboards (SWE-bench, Aider, LiveBench) and registries (OpenRouter,
  LiteLLM, HF trending) are sha-gated and emit on real movement.
- Pagewatch covers Claude platform notes, Devin CLI, xAI `.md`, Anthropic
  + DeepSeek sitemaps.
- Two quality flags already merged, **off by default** (`#12`):
  `CLAWBYTES_NORMALIZE_SCORES`, `CLAWBYTES_RELEASE_DIFF`.
- Weekly GitHub-topic + awesome-list discovery still runs. Brave-based
  RSS auto-discovery is gone (correct; Brave deprecated).

---

## Quality leaks (highest ROI)

### Q1. Changelog feeds misrouted to Read — **do first**

`classify_rss` Ship branches key on `"releases"` / `"release notes"` in
the **feed name**. Round-2 vendor feeds were named "Cursor Changelog",
"Amp News", "Windsurf Blog", etc.

Live probe 2026-08-20:

| Feed | HTTP | Latest item | Classifier today |
|---|---|---|---|
| Cursor Changelog | 200 | 2026-08-19 Cloud Agents and Cursor Harness Improvements | Read (`"cursor"` ∈ `READ_TERMS`) |
| Amp News | 200 | 2026-08-19 MCP in Orbs | Read (`"amp news"`) |
| GitHub Copilot Changelog | 200 | (live) | Read (`"copilot"`) |
| Devin Release Notes | 200 | 2026-08-19 | Ship (name contains "release notes") |
| Factory Release Notes | 200 | (live) | Ship |
| Windsurf Blog | 200 | **2026-05-12** (stale) | Read if keyword-hit |

Fix: treat feed names containing `changelog`, ` news` (anchored), or
`blog` **and** a `coding-agent`/`official` tag as Ship, with a vendor
priority — or rename those feeds to include `"release notes"` (cheaper,
but tags are the real signal). Add tests modeled on
`test_aider_release_routes_to_ship` for Cursor Changelog / Amp News.

Invariant: do **not** let `"blog"` alone Ship the LangChain/Mistral/
DeepMind research blogs. Gate on tags or an explicit allow-list of
feed names.

### Q2. `REPO_PRIORITY` / display names lag round-2 vendors

Add keys (order-sensitive; more-specific before `"claude"` / `"gemini"`):

- `"claude code"` already covered via `"claude"` — leave ordering.
- `"devin desktop"`, `"devin"` (Devin before generic words).
- `"antigravity"`
- `"amp news"` / a dedicated `"amp"` is a **substring trap** (`example`,
  `sample`) — keep the compound.
- `"factory"`, `"warp"`, `"windsurf"`, `"replit"`, `"augment code"`,
  `"jetbrains"`, `"junie"`, `"copilot"`, `"agent client protocol"` /
  `"acp "` (trailing space or compound; bare `"acp"` is rare but
  prefer `"agent client protocol"`).

Score targets: Cursor/Copilot/Devin/Antigravity should clear Ship
window 1 (58) on a fresh changelog without relying on age bonus.
Do **not** bump them to 85+; afternoon Ship should stay Claws-weighted.

### Q3. Reddit `READ_REDDIT_TERMS` contradicts editorial scope

`READ_REDDIT_TERMS` includes `"how to"`, `"tutorial"`, `"guide"`.
`EDITORIAL_SCOPE.md` explicitly excludes tutorials and explainers.
Those posts should stay Community (or drop), never Read.

### Q4. Watch has no dedicated source

Rebuild a **GitHub-advisory-only** monitor (no Brave):

- Poll GHSA GraphQL or `github.com/advisories?query=…` REST with the
  fields the old monitor got wrong (use `gh api graphql` locally to
  pin the schema before coding).
- Scope to tracked ecosystems: `langchain`, `langgraph`, `mcp`,
  `claude-code`, `openai/codex`, `cursor`, `huggingface`, `vllm`,
  plus GHSA packages matching `REPO_PRIORITY` names.
- Baseline silently; unique URL per advisory ID (invariant 4).
- Cap: 2 Watch items/day from this source so a CVE dump can't fill
  the lane. Papers stay in Read.

OSV (`api.osv.dev`) is a fine complement for PyPI/npm of the same
packages; not a firehose.

### Q5. Enable the two existing A/B flags

Both are tested and off. Recommend turning on in Railway after one
dry `preview` cycle:

- `CLAWBYTES_NORMALIZE_SCORES=1` — Community/Read stop being HN-point
  monopolies (PR #12 live A/B: HN+HF+Bluesky vs four HN threads).
- `CLAWBYTES_RELEASE_DIFF=1` — thin GitHub notes (Goose-class
  release-bots) get compare-endpoint subjects. Wants `GITHUB_TOKEN`
  (already set).

No code change. Ops change. Revert by unsetting.

### Q6. Curator cannot fetch on the live backend

`docs/curator-prompt.md` tells the curator to WebSearch/WebFetch when
blurbs are generic. The Ollama/OpenAI-compatible path
(`_curate_via_openai`) has **no tools** and appends "curate strictly
from the bundle." Quality then lives or dies on `grounding_for_item`
(GitHub release bodies, changelog `.md`, article snippets; Reddit
skipped on purpose).

Levers, in order:

1. Ship Q1 so changelog items enter Ship *with* `_looks_like_changelog`
   grounding (already implemented).
2. Turn on `CLAWBYTES_RELEASE_DIFF` (Q5).
3. If blurbs stay generic: either give the curator a fetch tool, or
   pre-fetch more aggressively into `fetched` so the tool-less backend
   has the source text. Do not ask gemma to invent operator deltas.

### Q7. Yield feedback loop is dark

`clawbytes_threads.py audit` and `send-clawbytes-audit` exist.
`scheduler.py` does **not** run them (routine Slack/DMs were
deliberately removed). `SOURCES.md` still mentions a Monday 15:45
audit DM — that line is stale.

Do **not** resurrect a scheduled DM. Do write a weekly
`memory/claw-source-yield.json` from `audit --json` (no notify) so
the next expansion round has per-source would_add / rejected counts
instead of guessing. On-demand `audit` remains the human path.

### Q8. Discovery queries are pre-harness

`scripts/claw-source-discovery.py` `SUBREDDIT_QUERIES` still includes
`"machine learning news"`. Known-sub set still lists homelab /
singularity / MachineLearning. HN topic extractor still boosts
`"diffusion"`. Align with `EDITORIAL_SCOPE` or discovery will re-widen
into generic ML.

---

## Coverage holes (second wave)

### C1. Windsurf → Devin Desktop (June 2, 2026)

- `windsurf.com/feed.xml` last item **2026-05-12**. Dead as a news
  source; keep only until confirmed 404, then drop.
- `docs.devin.ai/release-notes/overview/rss.xml` **is** the live
  Cognition changelog (items through 2026-08-19). Already in
  `RSS_FEEDS`. Make sure Q1+Q2 score it as Ship with a Devin
  priority, and add `"devin desktop"` to `READ_TERMS` /
  `RELEVANCE_KEYWORDS`.
- r/windsurf stays useful during the rename; add a search on
  r/DevinAI or similar only if the sub is operator-grade (2026-06-12
  passed r/Devin as consumer/showcase — re-check, don't auto-add).
- Pagewatch: docs.devin.ai already has the CLI `.md` watch; consider
  a Devin Desktop docs `.md` if Mintlify exposes one.

### C2. Google Antigravity — **not covered, should be**

Standalone Google coding agent (IDE + CLI + SDK). Changelog
`https://antigravity.google/changelog` is a gzip HTML SPA, **no RSS**
(blog `/blog/rss.xml` 404). Needs a pagewatch class: either a
Mintlify `.md` sibling (try `…/changelog.md` before building an HTML
diff) or a heading-hash watch like the existing Claude notes.

Add `"antigravity"` to `READ_TERMS` / `RELEVANCE_KEYWORDS` /
`REPO_PRIORITY` the same day so HN/Reddit mentions don't die at the
keyword gate. Baseline silently.

### C3. Agent Client Protocol (ACP)

MCP is covered (servers + py/ts SDKs). ACP is the 2026
editor↔agent counterpart (Zed, JetBrains, GitHub, Devin Desktop,
Gemini CLI). Machine source verified:

`https://github.com/agentclientprotocol/agent-client-protocol/releases.atom` → 200

Noisy (crate + schema + alphas). Filter: drop `alpha`/`rc` (already
in `classify_rss`); optionally keep only `Schema v1.*` stable, not
every Rust crate bump. Add `"agent client protocol"` (not bare
`"acp"`) to vocab. Editorial-scope one-liner next to MCP.

### C4. Terminal-Bench 2.1 — revisit the June pass

June 12 passed Terminal-Bench (private Supabase RPC). As of 2026-08
the board is public at tbench.ai and submissions live in
`harbor-framework/terminal-bench-2-1`. This is now the CLI-agent
board operators actually cite (next to SWE-bench). Fit the existing
leaderboard monitor: sha-gate a GitHub-backed file, emit on top-3
move, unique URL per sha. Probe the repo for a stable JSON/CSV
before writing a parser; if it's still PR-only rows, sha-gate the
submissions directory listing instead.

### C5. Scope-listed operator tooling with no monitor

`EDITORIAL_SCOPE.md` names these; none have a first-party feed in
`RSS_FEEDS`:

| Class | Candidates (validate URL before add) | Lane |
|---|---|---|
| Evals | Inspect AI, Arize Phoenix, LangSmith | Ship on release, Read on method posts |
| Observability | Langfuse, Helicone | Ship |
| Sandboxes | Modal, Daytona (E2B already covered) | Ship |
| Vector DBs | Qdrant, Chroma, LanceDB — **only** if the item is agent-memory/tooling, not generic DB | Ship/Read, tight keywords |
| Browser | Playwright releases are noisy; `browser-use` already covered | leave |

Add **one release.atom per class**, not the whole category. Keyword
gate the blogs; release feeds bypass the gate (invariant 2) so name
them `… Releases`.

### C6. Security / supply chain (pairs with Q4)

After GHSA works: npm/PyPI for `mcp`, `claude-code`, `@anthropic-ai/*`
is still YAGNI if GitHub atom + GHSA cover the same publishes. MCP
**registry firehose** stays passed; a **weekly** "N new servers, notable
ones: …" aggregate would be a Community/Ship experiment, not a 30-min
item stream.

### C7. Vocab + Bluesky/HN query drift

Bluesky phrases are still `"claude code"`, `"codex cli"`, `"openclaw"`,
`"mcp server"`, `"agent harness"`. Missing: `"cursor"`, `"devin
desktop"`, `"antigravity"`, `"agent client protocol"`. HN is closer
(has cursor/windsurf/copilot) but not Antigravity/ACP/Factory/Amp.

Substring traps still apply: never add bare `"amp"`, `"opus"`,
`"droid"`, `"augment"`.

### C8. ArXiv cs.AI / cs.CL

These are firehoses. HF Daily Papers already keyword-scores the
research lane. If Read is noisy, drop ArXiv (or raise `is_relevant`
to require agent/harness terms even on those feeds — they are **not**
named "releases", so the bypass does not apply). Evidence should come
from Q7 yield JSON before deletion.

---

## Still pass / do not do

Unchanged from 2026-06-12 unless noted:

- X/Twitter, Discord — no viable unauth API.
- Provider status feeds — retired, do not resurrect.
- HF Spaces, LMArena, GAIA, BigCodeBench, llm-stats, METR DVC.
- MCP registry as a 30-min firehose.
- r/OpenAI, r/Bard, r/vibecoding, r/replit, r/Devin, r/AgentsOfAI
  (re-check r/Devin only after the Desktop rebrand has an operator sub).
- Notion as a production source (`docs/state-architecture.md`).
- People-tracker (deleted with Brave). The EDITORIAL_SCOPE "people"
  bullet is currently unbacked; do not rebuild until there is a
  first-party RSS/Atom for a named operator, added one-by-one.
- Postgres state migration — reliability, not coverage/quality of
  the channel. Separate track.
- Scheduled ops DMs when healthy.

Borderline, still waiting on yield evidence: r/LLMDevs, AICodeKing
YouTube, IndyDevDan YouTube, lobste.rs `/t/ai`.

SWE-bench Pro / Windsurf-changelog RSC extractor: Devin release-notes
RSS now covers Cognition; Antigravity is the remaining RSC/pagewatch
need (C2). SWE-bench Pro still deferred until a GitHub-backed file
exists.

---

## Recommended sequence

Four small PRs. Stop after 1–2 if the channel already feels fuller;
do not land C5's eval/observability set until Q7 has a week of yield.

### PR A — Routing and vocabulary (code, tests)

The actual coverage expansion for sources we already fetch.

- [ ] Changelog/news/blog + `coding-agent` tag → Ship (Q1), with tests
      for Cursor Changelog and Amp News.
- [ ] `REPO_PRIORITY` + `display_repo_name` for Devin, Amp (compound),
      Factory, Copilot, Antigravity, ACP (Q2). Respect dict-order
      invariant.
- [ ] Drop tutorial terms from `READ_REDDIT_TERMS` (Q3).
- [ ] `READ_TERMS` / `RELEVANCE_KEYWORDS` / Bluesky+HN queries:
      `"devin desktop"`, `"antigravity"`, `"agent client protocol"`
      (C1, C2, C3, C7).
- [ ] `EDITORIAL_SCOPE.md`: Windsurf → Devin Desktop; add Antigravity
      and ACP next to MCP. Curator picks this up with no other change.
- [ ] `SOURCES.md`: mark Windsurf blog stale; log this research round.

### PR B — Ops flags + yield snapshot (config + tiny code)

- [ ] Enable `CLAWBYTES_NORMALIZE_SCORES=1` and
      `CLAWBYTES_RELEASE_DIFF=1` on Railway after a local
      `preview --category ship` / `community` A/B (Q5).
- [ ] Weekly job: write `audit --json` summary to
      `memory/claw-source-yield.json`. No DM, no Slack (Q7).
- [ ] Tighten `claw-source-discovery.py` queries (Q8).

### PR C — New sources that have a machine URL today

- [ ] ACP `releases.atom` with alpha-filter (C3). Validate it does
      not dump 3 crate bumps/week into Ship; if it does, keep only
      `Schema v1.*` titles or raise it to Read.
- [ ] Antigravity pagewatch (C2). Prefer `.md`; else heading hash.
      Silent baseline.
- [ ] Terminal-Bench 2.1 leaderboard if a sha-gateable file exists
      (C4). Else note "still passed — no file" in `SOURCES.md`.

### PR D — Watch rebuild

- [ ] GHSA-only monitor, scoped packages, unique advisory URLs,
      daily cap (Q4). TDD first. No Brave.

---

## How to verify any of the above

```bash
export CLAWBYTES_MEMORY_DIR=/tmp/cb-dev
venv/bin/python -m pytest tests/ content-engine/tests/ -q
venv/bin/python -m py_compile clawbytes_threads.py scripts/*.py
venv/bin/python clawbytes_threads.py collect --run-monitors --summary
venv/bin/python clawbytes_threads.py audit | head -80
venv/bin/python clawbytes_threads.py preview --category ship
venv/bin/python clawbytes_threads.py preview --category watch
```

A Cursor Changelog or Amp News item in `preview --category ship`
(not read) is the Q1 acceptance test. A GHSA for a tracked package
in Watch is the Q4 test. Antigravity's first collect after PR C must
emit **zero** items (baseline).
