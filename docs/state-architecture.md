# ClawBytes State Architecture

Date: 2026-05-21

## Decision

Notion is not part of the production ClawBytes architecture. The Notion sync/signal scripts can remain as legacy local utilities for now, but the collector, publisher, curator, supervisor, and content engine should not depend on Notion data.

For VM cutover, keep the existing JSON state under `CLAWBYTES_MEMORY_DIR` because it is simple, inspectable, and already wired through the runtime.

For the next durable state layer, prefer Postgres over Notion or ad hoc files.

## Why Postgres

Postgres gives us:

- Durable source item history across restarts and deploys.
- Queryable publish history and dedupe state.
- Source health and monitor-run history for supervisor decisions.
- A clean path for dashboards and growth metrics.
- A future bridge for multiple processes or services without sharing mutable JSON files.

SQLite would be a reasonable single-VM stepping stone, but ClawBytes already has a likely Postgres path through `DATABASE_URL`, and the data model is relational enough to justify going straight there once the VM cutover is stable.

## Migration Shape

Phase 0: VM cutover

- Keep JSON state as the source of truth.
- Use `/var/lib/clawbytes/memory` for every collect, publish, supervisor, and content-engine job.
- Keep backups of the memory directory.

Phase 1: dual-write

- Add a small storage module with JSON and Postgres backends behind the same operations.
- Continue writing JSON.
- Also write monitor runs, source items, backlog items, publish events, and curator events to Postgres when `DATABASE_URL` is set.
- Supervisor reports whether Postgres writes are healthy, but publishing does not depend on Postgres yet.

Phase 2: read-through

- Read publish history, dedupe state, and source health from Postgres.
- Keep JSON exports for inspection and rollback.
- Add a migration command that imports existing JSON memory into Postgres.

Phase 3: Postgres primary

- Postgres becomes the source of truth.
- JSON files become generated snapshots.
- Remove any remaining production dependency on legacy local state files.

## Proposed Tables

```sql
CREATE TABLE monitor_runs (
  id BIGSERIAL PRIMARY KEY,
  monitor_name TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  items_found INT NOT NULL DEFAULT 0,
  error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE source_items (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_name TEXT,
  source_id TEXT,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  published_at TIMESTAMPTZ,
  discovered_at TIMESTAMPTZ NOT NULL,
  first_seen_run_id BIGINT REFERENCES monitor_runs(id)
);

CREATE TABLE backlog_items (
  id TEXT PRIMARY KEY,
  source_item_id TEXT REFERENCES source_items(id),
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  primary_category TEXT NOT NULL,
  categories TEXT[] NOT NULL DEFAULT '{}',
  score NUMERIC,
  status TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE publish_events (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL,
  sent_at TIMESTAMPTZ NOT NULL,
  telegram_channel_id TEXT,
  message_count INT NOT NULL DEFAULT 1,
  item_ids TEXT[] NOT NULL DEFAULT '{}',
  curator_used BOOLEAN NOT NULL DEFAULT false,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE source_candidates (
  id BIGSERIAL PRIMARY KEY,
  source_type TEXT NOT NULL,
  url TEXT NOT NULL,
  status TEXT NOT NULL,
  discovered_via TEXT,
  discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  graduation_score NUMERIC,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

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

## Guardrails

- Publishing must still work if Postgres is temporarily unavailable until Phase 3.
- No secrets should be logged or stored in state tables.
- Raw payloads should be capped or summarized before storage if they become large.
- The schema should be migrated through explicit migration files, not implicit table creation inside the publisher.
- Backups should include both Postgres dumps and JSON snapshots during the transition.
