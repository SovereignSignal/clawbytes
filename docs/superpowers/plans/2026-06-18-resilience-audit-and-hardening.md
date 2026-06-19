# Resilience Audit & Hardening Plan

> **For agentic workers:** implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax. Run the test suite after each task; it must stay
> green. Commit after each task with the SovereignSignal identity and the
> `Co-Authored-By: Claude` trailer.

**Date:** 2026-06-18
**Scope:** clawbytes (primary) + modelbytes (reference / cross-cutting phases).
**Origin:** independent cross-repo audit of `clawbytes` vs `modelbytes`, two
sibling "fetch → classify → enrich → publish to Telegram + Slack mirror"
services that duplicate most of their reliability surface by hand.

## Goal

Close the maturity gap between clawbytes and modelbytes without regressing
either, then consolidate the duplicated hardening into a single shared core so
the two systems stop drifting apart. The product of this plan is a clawbytes
publish path that survives the same transients modelbytes already survives, a
tested failure surface, and (pending owner decision) one place that owns
Telegram/Slack/ops-routing for both.

## Architecture (as-is)

Both services share one shape: source monitors → classifier → optional LLM
enrichment → post to a public Telegram channel, mirrored to Slack, with a
deterministic fallback that must stay live when the smart path fails.

- **modelbytes** (`monitor.py`, ~2700 lines): single daily cron, Postgres state,
  rich ops layer (`publish_runs` audit table, `send_ops_alert` with Telegram-
  then-Slack routing, `ping_heartbeat`, `fallback_streak`, content gates with
  ERROR/WARNING tiers, `_truncate_for_telegram`). The hardened reference.
- **clawbytes** (`clawbytes_threads.py` ~2475 lines + `scripts/scheduler.py`
  APScheduler): continuous lanes, JSON state on a volume, no content gate,
  raise-on-failure `send_telegram`, Telegram-only ops alerts. Structurally a
  sibling, materially less hardened.

## Key invariants (do not violate)

1. **The channel never goes darker than the deterministic path.** Any
   enrichment/curator failure must fall through to a deterministic post, never
   silence a lane. (Existing clawbytes invariant; this plan extends it to
   send-failure too.)
2. **Idempotency holds.** A send that succeeds must be recorded as posted
   before the next cycle, and a send that fails must NOT be recorded as posted.
3. **No secrets in this public repo.** No channel IDs, tokens, hostnames, or
   Railway IDs in code, docs, or commit messages. Use placeholders only.
4. **Local testing always sets `CLAWBYTES_MEMORY_DIR=/tmp/cb-dev`** before
   importing the module (it resolves paths at import time).
5. **TDD.** Write/extend the failing test first. `python3 -m pytest tests/
   content-engine/tests/ -q` must stay green.
6. **modelbytes is the reference.** Where the two disagree on a hardened
   behavior, modelbytes wins and clawbytes converges to it (not the reverse).

## Decision point (owner, before Phase 3)

How to share the duplicated hardening (Telegram send+truncate, Slack mrkdwn
mirror, retry/backoff, redact-secrets, ops-alert routing). Options:

- **A — pip package from git** (`ss-publish`, new SovereignSignal repo;
  `pip install git+…` in both `requirements.txt`). Clean, versioned via tags,
  zero build complexity on Railway. **Recommended.** Cost: a third repo + a
  light release habit (tag bumps).
- **B — vendored copy + sync script.** `ss_publish/` copied into each repo,
  a script keeps them in sync. Zero build/deploy change. Cost: drift risk, the
  thing we're trying to fix.
- **C — git submodule.** Both repos submodule `ss-publish`. Cost: Railway
  builds need `--recurse-submodules`; submodules are a known friction source.
- **D — no shared code; shared spec only.** A hardening SPEC both implement
  against independently. Cheapest now, reintroduces manual porting forever.

Phases 1–2 do not depend on this decision and proceed immediately. Phase 3
blocks on it.

---

## Phase 1 — clawbytes publish-path hardening (P0, no decision needed)

The weakest link. Today a transient Telegram 5xx or an oversize bundle raises
out of `send_telegram`, aborts `_publish_lane`, and skips every later lane in
the hourly `autopublish` pass.

### Task 1.1 — Harden `send_telegram` to bool + truncate + retry ✅
**Files:** `clawbytes_threads.py` (`send_telegram`, ~L1976)

- [x] Return `bool` (True on HTTP success with `ok:true`, False otherwise)
      instead of raising. Callers move to branch-on-false.
- [x] Truncate to Telegram's 4096-char limit at the last newline before the
      limit, with a `…[truncated]` marker (port modelbytes'
      `_truncate_for_telegram`).
- [x] Retry transient 429/5xx with backoff honoring `Retry-After`, max 3
      attempts. Keep `urllib` (monitors are stdlib-only; the publisher already
      uses urllib).
- [x] Slack mirror still best-effort and never blocks (unchanged contract),
      but only mirror on a successful Telegram send.

### Task 1.2 — Make every send call site fail-soft ✅
**Files:** `clawbytes_threads.py` (`_publish_lane` ~L2057, `send_telegram_message_list` ~L2307, the manual `publish` CLI paths in `main`)

- [x] In `_publish_lane`: on `send_telegram` False, do NOT `mark_posted`, log
      to stderr, return `(False, count)`. The lane retries next cycle (it was
      not marked posted). Curator-approved branch and deterministic branch
      both handled.
- [x] Wrap each send in try/except so an unexpected exception degrades to
      "lane not posted this cycle" instead of aborting the whole `autopublish`
      loop. (Wrap lives in `autopublish` around the `_publish_lane` call.)
- [x] `send_telegram_message_list` returns the count actually sent; a mid-list
      failure does not raise out of the caller. Manual `publish` CLI sites now
      check the return and exit 1 on failure without marking posted.

### Task 1.3 — Lightweight pre-send content gate ✅
**Files:** `clawbytes_threads.py` (new `validate_lane_for_publish`, called from `_publish_lane` before send)

Port the ERROR-only subset of modelbytes' gate (the channel-harm bar; format
drift stays a non-blocking warning at most):
- [x] Reject empty body.
- [x] Reject unbalanced `<b>/<i>/<a>/<code>` tags (would 400 on Telegram).
- [~] Reject body still over the 4096 limit after truncation (shouldn't happen;
      defense in depth). — **DROPPED**: `send_telegram` truncates before the
      gate ever sees the body, so a post-truncation overflow is impossible by
      construction. Added defense-in-depth here would be dead code; the existing
      `test_send_truncates_oversize_before_posting` pins the truncation.
- [x] On reject: log to stderr, do not send, do not mark posted. Fall through
      is not possible here (no deterministic alternative to a deterministic
      bundle), so the lane just retries next cycle. Ops-DM deferred to Phase 2
      (needs the Slack-fallback alert router first).

### Task 1.4 — Fix the stale `_publish_lane` docstring ✅
**Files:** `clawbytes_threads.py` (~L2057)

- [x] Docstring claimed "a curator that explicitly declines skips the lane this
      cycle (no send, no quota spent)." Code (and `CLAUDE.md`) actually falls
      through to the deterministic bundle. Rewrote the docstring to match the
      code and added the send-fail-doesn't-mark-posted contract. One-line
      semantics fix; load-bearing for the next reader.

### Task 1.5 — Tests for the publish failure surface ✅
**Files:** `tests/test_publish_hardening.py` (new — 20 tests)

This is the biggest delta vs modelbytes and the quietest failure surface.
- [x] `send_telegram` returns False (not raises) on HTTP 500 and on `ok:false`.
- [x] `send_telegram` truncates an oversize message and succeeds.
- [x] `send_telegram` retries on 429 then succeeds; respects `Retry-After`.
- [x] `_publish_lane` does NOT mark posted when send fails; does mark posted
      when send succeeds.
- [x] `_publish_lane` curator-failure falls through to deterministic post
      (regression test for the existing invariant).
- [x] `validate_lane_for_publish` rejects empty / unbalanced-tags.
- [x] `autopublish` continues to later lanes after one lane's send fails AND
      after one lane crashes.

**Phase 1 result:** 79 passed (59 existing + 20 new), 0 regressions.

---

## Phase 2 — clawbytes ops/alerting parity (P0/P1, no decision needed)

### Task 2.1 — Slack fallback for admin ops DMs ✅
**Files:** `scripts/scheduler.py` (`_send_admin_dm`, new `_send_admin_slack`)

- [x] After the Telegram DM attempt, if it failed AND `CLAWBYTES_OPS_SLACK_*`
      env vars are set, post to a Slack ops channel in an isolated try-block.
      A Telegram outage must still be able to page the operator. Mirror
      modelbytes' two-path isolation. New env var `CLAWBYTES_OPS_SLACK_CHANNEL_ID`
      documented in README + CLAUDE.md (distinct from the audience-mirror
      `CLAWBYTES_SLACK_CHANNEL_ID`).

### Task 2.2 — Isolate monitor execution in `run_monitors` ✅
**Files:** `clawbytes_threads.py` (`run_monitors` ~L978)

- [x] Per-monitor `try/except` (catch `subprocess.TimeoutExpired` and
      `Exception`) so one bad/timeout source cannot starve the rest of the
      batch. Log the failure; continue.
- [x] Drop `shell=True`; use `cwd=WORKSPACE` + arg list (matches
      `scheduler.py`'s already-correct pattern). Removed the now-unused
      `shlex` import.

### Task 2.3 — (optional) concurrent monitor execution ⏸ DEFERRED
**Files:** `clawbytes_threads.py` (`run_monitors`)

- [~] Run the ~10 independent monitor subprocesses in a `ThreadPoolExecutor`
      instead of sequentially. Cuts collect wall-time roughly N×. **Deferred**:
      collect runs every 30 min and the sequential batch comfortably fits well
      inside that window today; the isolation fix (2.2) was the load-bearing
      change. Revisit if monitor count grows or wall-time becomes a problem.

**Phase 2 result:** 90 passed (79 from Phase 1 + 11 new: 6 scheduler
ops-routing + 5 run_monitors isolation), 0 regressions.

---

## Phase 3 — shared publish core (BLOCKS on the decision above)

### Task 3.1 — Stand up the shared core per chosen option
- [ ] Owner picks A/B/C/D.
- [ ] If A: create `SovereignSignal/ss-publish` repo; extract from modelbytes
      (the hardened reference): `telegram_post`, `truncate_for_telegram`,
      `mirror_to_slack`, `html_to_mrkdwn`, `retry_delay`, `redact_secrets`,
      `send_ops_alert` (Telegram-then-Slack). Ship with its own tests.
- [ ] Tag v0.1.0.

### Task 3.2 — Adopt in modelbytes
- [ ] Replace the in-tree copies with imports from the shared core. Keep
      behavior identical (golden tests on the live `pending/*.txt` corpus must
      still pass). This is a refactor, not a behavior change.

### Task 3.3 — Adopt in clawbytes
- [ ] Replace clawbytes' `send_telegram` / `mirror_to_slack` / scheduler
      `_send_admin_dm` with the shared core. clawbytes inherits Phase 1 + 2
      hardening for free.

---

## Phase 4 — cross-cutting resilience (both)

### Task 4.1 — Send-retry on modelbytes too
**Files:** `modelbytes/monitor.py` (`send_telegram_post` ~L2135)

- [ ] modelbytes retries source fetches but not the Telegram POST. Add the
      same 429/5xx + `Retry-After` retry to the send. (If Phase 3 landed, this
      comes for free from the shared core.)

### Task 4.2 — Cap modelbytes' grace window by job budget
**Files:** `modelbytes/monitor.py` (`_wait_for_pending` ~L2323)

- [ ] `_wait_for_pending` can poll up to 600s synchronously. If the Railway job
      timeout is tight, the run can be killed mid-wait with no `posted`/`blocked`
      row recorded — indistinguishable from "cron never fired." Cap the window
      by a fraction of remaining job budget (env-configurable), or hand the
      wait to a separate poller.

### Task 4.3 — clawbytes Postgres migration (per `docs/state-architecture.md`)
- [ ] Lands the durable publish ledger modelbytes already has. Out of scope for
      the immediate hardening; tracked here so it inherits the shared-core ops
      story when it lands.

---

## Phase 5 — code health (opportunity-based)

### Task 5.1 — Archive clawbytes legacy scripts
**Files:** `clawbytes_daily.py`, `claw-digest-generator.py`, the Notion/Proton/
people-tracker scripts under `scripts/`

- [ ] Move to `legacy/` (or archive out of the tree). They're a trap for the
      next agent that opens the repo and picks the wrong entrypoint. Update
      CLAUDE.md + README repo-map.

### Task 5.2 — (defer) monolith split
- [ ] Both cores are ~2500+ lines. Not urgent. Next major change is the trigger.
      Sketch: sources / classify / gates / publish / state, main as orchestrator.

---

## Sequencing

1. **Phase 1** (clawbytes publish hardening) — start now, no decision needed.
2. **Phase 2** (clawbytes ops parity) — start now, no decision needed.
3. **Owner decision** on Phase 3 sharing model.
4. **Phase 3** (shared core) then **Phase 4.1/4.2** (modelbytes send-retry +
   grace cap) — 4.1 may come free from 3.
5. **Phase 5** as opportunity allows.
