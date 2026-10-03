# Release Events v1

A tiny cross-service contract for immediate release notifications.

ClawBytes is one producer. Forwarding ships **inert**. It does nothing until
`RELEASE_EVENTS_URL`, `RELEASE_EVENTS_TOKEN`, and
`CLAWBYTES_RELEASE_FORWARDING=1` are all set. Unsetting the flag stops it on
the next collect with no deploy.

Do not enable the flag until the Release Bot receiver is merged, deployed,
and verified. The first enabled collect only writes a watermark.

## Event

```json
{
  "schema": "release-event/v1",
  "id": "software:github:qwibitai/nanoclaw:v1.2.3",
  "kind": "software",
  "name": "Nanoclaw",
  "version": "1.2.3",
  "source": "clawbytes",
  "source_type": "github_release",
  "url": "https://github.com/qwibitai/nanoclaw/releases/tag/v1.2.3",
  "published_at": "2026-10-03T18:00:00Z",
  "summary": "optional grounded release notes",
  "metadata": {"repo": "qwibitai/nanoclaw", "tag": "v1.2.3", "prerelease": false, "draft": false}
}
```

Required: `schema`, `id`, `kind`, `name`, `version`, `source`, `url`.
`kind` is `software` or `model`. ClawBytes emits `software` only.

`id` is `software:github:<owner>/<repo>:<tag>`, lowercased. It is stable
across retries. `name` is the display name from `release_targets.json`, or
the `owner/repo` string when a row has no display name. It is never the
GitHub release title. `version` is the tag with a leading `v` or `rust-v`
removed (`v1.2.0-rc.1` → `1.2.0-rc.1`, `rust-v0.161.0` → `0.161.0`).

The event id is the producer observation id. Consumers treat duplicate ids
as idempotent.

## Transport

Producers POST JSON to `RELEASE_EVENTS_URL` with
`Authorization: Bearer $RELEASE_EVENTS_TOKEN`. Failure to forward must never
block the producer's normal publishing path.

`release_events.send_release_event` returns a queue status:

| Receiver result | Status | Outbox |
| --- | --- | --- |
| 202, or any other 2xx | `delivered` | done |
| 200 whose body status is `duplicate`, `owned_by_poller`, or `already_seen` (or `"duplicate": true`) | `duplicate` | done |
| 400, other 4xx except 401/408/429 | `rejected` | kept, not retried |
| 401 | `config_error` | stays `retryable`; the rest of that pass is not attempted |
| 408, 429, 5xx, timeout, connection error | `retryable` | attempt count and backoff |

A missing URL or token returns `skipped` and does not open a socket.
`emit_release_event` stays a boolean: true for `delivered` or `duplicate`.

## Gating

All three are required. Any one missing → outcome `disabled`, no HTTP, no
outbox write, no watermark write.

* `RELEASE_EVENTS_URL`
* `RELEASE_EVENTS_TOKEN`
* `CLAWBYTES_RELEASE_FORWARDING` set to `1`, `true`, `yes`, or `on`

The scheduler calls the forwarder in-process after `collect` exits 0. It
does not use the job runner that pages `alert:collect`. A forward exception
or a retryable outcome is logged as `forward_release_events: <outcome>`.
After 48 consecutive failure collects (about a day at the 30-minute cadence)
the scheduler sends one private ops note labeled `forward_release_events`,
then at most once a day while the streak holds. That note is not
`alert:collect`. `disabled`, `baseline`, `stale`, and `ok` are not failures.

The CLI `python3 scripts/forward-release-events.py` inserts the repo root on
`sys.path` and always exits 0. A disabled run prints
`forward_release_events: disabled`.

## Cutover watermark

On the first enabled run with no `release-forwarding-watermark.json`, the
forwarder writes `{ "watermark": "<now UTC>" }` and forwards nothing. The
file lives under `CLAWBYTES_MEMORY_DIR`.

Later runs forward a release only when its `published` time is strictly after
that watermark. The watermark is not moved again. A corrupt watermark file
is left in place and the run returns `error` (no silent reset, no replay).

## Fresh batch only

Input is `claw-ecosystem-new-items.json`. Only a batch whose `timestamp` is
within 45 minutes (and not more than 10 minutes in the future) is ingested.
An older file is a stale batch: nothing in it is queued. Due outbox rows are
still attempted. `baseline: true` rows are ignored. Drafts are always
rejected.

The ecosystem monitor still records `lastSeenReleases` itself. This path
does not write that map. The monitor's release object now also carries
GitHub `prerelease` and `draft`.

## Queue

`release-events-outbox.json` on the same memory directory:

* new rows start as `queued`
* `delivered` and `duplicate` are done
* `retryable` keeps `attempts` and `next_attempt_at` (30s, doubling, cap 6h)
* `rejected` (HTTP 400 or a structurally invalid event) is kept and not retried

Writes use a unique temp file in that directory, fsync, then replace.
Queued rows are written before the POST. Collect has already stored
`lastSeenReleases`, so a crash during the request leaves the row on disk
for the next pass. The file is capped at 200 rows; the oldest terminal
rows are dropped first.
A corrupt outbox is not overwritten. At most 20 due rows are attempted per
collect, so a down receiver cannot stall the scheduler for the whole queue.
One collect attempts each due row once.

## Target policy

`release_targets.json` is the reviewed map, keyed by `owner/repo`:

* `name` — display name used in the event
* `prereleases` — when false, a prerelease tag or GitHub `prerelease: true` is dropped
* `tag_pattern` — regex the tag must match
* `forward` — when false, the row is documentation only
* `legacy_owned` — always dropped, even if `forward` is flipped by mistake

Repos that are not listed are not forwarded.

Prerelease detection looks at the **tag only**, with token boundaries:

```text
(?i)(?:^|[-_.+])(preview|nightly|snapshot|canary|alpha|beta|rc|dev|pre)(?:[-_.+]?\d+)?(?:$|[-_.+])
```

Plus a non-product prefix: `^(?:inputs|nightly|snapshot|canary|preview)[-_]`.
Titles such as "Source tracking", "Architecture overhaul", and "March release"
do not reject a stable tag. `v1.2.0-rc.1` and `v2.0.0-beta` are prereleases
unless that target sets `prereleases: true`. `nightly-*` and `inputs-*` never
qualify (they are not product versions). Drafts never qualify.

OpenClaw is `prereleases: true` so the row matches the legacy poller. It is
still `forward: false` and in the code-level legacy set, so ClawBytes does
not send it.

### Legacy-owned during migration

These four stay with the legacy poller until Sov approves the identity
proposal. Both the JSON (`forward: false`, `legacy_owned: true`) and
`LEGACY_OWNED` in `scripts/release_forwarding.py` refuse them:

| Repo | Display name |
| --- | --- |
| `openclaw/openclaw` | OpenClaw |
| `NousResearch/hermes-agent` | Hermes Agent |
| `openai/codex` | Codex |
| `anthropics/claude-code` | Claude Code |

Other rows with `forward: true` are the coding-agent and claw-family GitHub
release repos ClawBytes already watches. SDK and framework atoms that are
not in the file stay unforwarded. Adding a target is a review of this file,
not an automatic discovery result.

## Rollout

1. Receiver merged, deployed, and verified first.
2. This change deploys with `CLAWBYTES_RELEASE_FORWARDING` unset. Logs show
   `forward_release_events: disabled`. Collect behavior is unchanged.
3. With approval, set the flag (and the URL and token). The first run writes
   the watermark and forwards nothing.
4. A live send only goes to a verified destination, with explicit approval.

## Rollback

Unset `CLAWBYTES_RELEASE_FORWARDING`. Forwarding stops on the next collect.
The outbox and watermark files are additive and can stay. Editorial state,
including `lastSeenReleases`, is untouched. Reverting the change is the
other rollback.
