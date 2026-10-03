# Release Events v1

A tiny cross-service contract for immediate release notifications.

## Event

```json
{
  "schema": "release-event/v1",
  "id": "software:github:openclaw/openclaw:v2026.10.1",
  "kind": "software",
  "name": "OpenClaw",
  "version": "2026.10.1",
  "source": "clawbytes",
  "source_type": "github_release",
  "url": "https://github.com/openclaw/openclaw/releases/tag/v2026.10.1",
  "published_at": "2026-10-03T18:00:00Z",
  "summary": "optional grounded release notes",
  "metadata": {"repo": "openclaw/openclaw"}
}
```

Required: `schema`, `id`, `kind`, `name`, `version`, `source`, `url`.
`kind` is `software` or `model`.

The event id is the global dedupe key. Producers must make it stable for the
same release across retries. Consumers must treat duplicate ids as idempotent.

## Transport

Producers POST JSON to `RELEASE_EVENTS_URL` with
`Authorization: Bearer $RELEASE_EVENTS_TOKEN`. Failure to forward must never
block the producer's normal publishing path.
