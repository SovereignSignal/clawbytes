# ClawBytes → Railway migration design (clawbytes-only)

Date: 2026-06-08
Status: approved (design), pending implementation

## Goal

Move the **ClawBytes ecosystem monitor** (the collect + Telegram-lane publishing
pipeline) off the DDA VM (`claw-content-engine`, 10.40.0.156) onto Railway, for
simpler iteration (git push = deploy) and to drop the VPN/manual-deploy friction.

## Scope

In scope: clawbytes only — `clawbytes_threads.py collect` + `autopublish`, its
monitors, and its `memory/` JSON state. Out of scope (separate session): modelbytes
and the content-engine weekly Slack brief. They stay on the VM for now.

## Why this shape

ClawBytes was previously on Railway and was moved to the VM because independent
cron containers do not share local JSON state, and the whole pipeline is built on a
shared `memory/` dir (backlog, monitor caches, publish/dedup state). The migration
therefore keeps the JSON-state model and solves state-sharing with a single
always-on container + a persistent volume, rather than reintroducing multi-container
cron.

## Architecture

- **One always-on Railway service** (`clawbytes`, project `9b6a552c…`, service
  `78e92a76…`, env `d82d0b1e…`) deployed from `SovereignSignal/clawbytes`, branch
  **`master`** (production HEAD, not the sanitized `main`).
- **One Railway volume** mounted at `/data`. Set `CLAWBYTES_MEMORY_DIR=/data/memory`
  so every scheduled job shares one state dir — the VM's `/var/lib/clawbytes/memory`
  equivalent. (On the VM today this resolves to `<repo>/memory`; on Railway it is the
  volume.)
- **In-process scheduler** `scripts/scheduler.py` (APScheduler) becomes the
  container start command. It replaces systemd timers; `max_instances=1` per job is
  the in-process equivalent of the VM's `flock` overlap guard. Missed runs while the
  container restarts are tolerated (TTL/`--if-ready` semantics already handle gaps;
  `CLAWBYTES_AUTO_REFRESH_BEFORE_PUBLISH` backstops stale collection).
- **Deterministic launch** — no `--use-curator`, so no `CLAUDE_CREDENTIALS` day one.

The existing `Dockerfile` (ENTRYPOINT `entrypoint.sh`, default CMD `tail -f
/dev/null`) is reused; the Railway custom start command overrides CMD to run the
scheduler. `entrypoint.sh`'s credential/identity shims stay (harmless when unset).

## Scheduler jobs (mirroring what the VM actually runs)

| Job | Schedule (prod parity) | Command |
|---|---|---|
| collect | every 30 min (`*:0/30`) | `python3 clawbytes_threads.py collect --run-monitors` |
| autopublish | hourly at minute 05 | `python3 clawbytes_threads.py autopublish --send` |
| daily Slack report | **paused at launch** | `send-clawbytes-report.sh` (revisit; needs `SLACK_WEBHOOK_URL`) |

`autopublish` internally decides which lanes are due by their posting windows, so we
do not replicate four separate lane timers (matches the live VM, not the README).

## Repo changes

1. `scripts/scheduler.py` — APScheduler `BlockingScheduler`; jobs shell out to the
   existing CLI via `subprocess`, inheriting env; logs each run; `coalesce=True`,
   `max_instances=1`, `misfire_grace_time` generous. A `DRY_RUN`/`CLAWBYTES_PUBLISH`
   env gate lets the first deploy run `autopublish` without `--send`.
2. `requirements.txt` — add `apscheduler`.
3. No change to `clawbytes_threads.py` logic (it already honors `CLAWBYTES_MEMORY_DIR`).

## Railway configuration (dashboard / CLI)

- Deploy branch: `master`.
- Volume: create, mount path `/data`.
- Variables: `CLAWBYTES_MEMORY_DIR=/data/memory`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHANNEL_ID`. (Plus `CLAWBYTES_PUBLISH=0` for the dry soak, flipped to `1`
  at go-live.)
- Custom start command: `python3 scripts/scheduler.py`.

## State continuity (prevents duplicate / missed posts)

The backlog holds each item's posted-lane dedup state. Before go-live, **seed the
volume** with the VM's current `/opt/claw/clawbytes/memory/` so Railway starts from
the real deduplicated backlog. A fresh empty backlog would re-post recent items to
the live `@clawbytes` channel.

Seeding options: a one-off `railway run`/shell copy into `/data/memory`, or a temp
`git`/`tar` transfer of the VM snapshot. The publisher and channel id are unchanged,
so once seeded, posted items remain marked posted.

## Cutover sequence (both hosts publish to the same channel — never run both live)

1. Land repo changes on `master`; point the Railway service at `master`; create the
   volume; set vars with `CLAWBYTES_PUBLISH=0`; set the start command. Deploy.
2. Verify the dry service boots, mounts `/data`, and `collect` writes
   `/data/memory/clawbytes-backlog.json`. Confirm `autopublish` runs but does **not**
   send.
3. Seed the volume with the VM's `memory/` snapshot.
4. **Pause the VM** clawbytes timers (spin-down below).
5. Set `CLAWBYTES_PUBLISH=1`, redeploy; confirm one real lane post lands from Railway
   and the channel shows no duplicates.

## DDA spin-down (clawbytes only — leave modelbytes & content-engine running)

```bash
sudo systemctl disable --now \
  clawbytes-collect.timer \
  clawbytes-autopublish.timer \
  claw-clawbytes-report.timer
```

Leave untouched: `claw-modelbytes-report.*`, `claw-content-weekly.*`,
`claw-content-slack.*`, and all modelbytes units. Keep `/opt/claw/clawbytes` +
`memory/` in place as a rollback net; remove only after Railway is stable for
several cycles.

## Rollback

If Railway misbehaves before the VM checkout is deleted: set `CLAWBYTES_PUBLISH=0`
on Railway (stops posting) and `systemctl enable --now` the three VM timers. The VM
backlog is untouched, so it resumes where it left off.

## Seams left for the other session

- `content-engine` (on the VM) reads the clawbytes backlog. After cutover the VM's
  `/opt/claw/clawbytes/memory` stops updating, so the weekly Slack brief's clawbytes
  sections go stale until content-engine is moved/repointed. Known, accepted interim.
- modelbytes is untouched here.

## Out of scope / deferred

- Curator (LLM lane enrichment) — enable post-launch once runtime + state proven.
- Postgres state store — only if clawbytes later needs multiple writers / scale.
- Moving the daily Slack report and content-engine — separate session.
