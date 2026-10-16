# Bot upkeep check

`upkeep_check.py` is a deterministic, read-only daily check over the three
SovereignSignal bots listed in `config.json` (clawbytes, modelbytes,
interesting-repos). It does not import or touch any bot code.

```
python -m ops.upkeep.upkeep_check [--dry-run] [--config PATH] [--json] [--no-alert] [--fixtures DIR] [--feed-url URL]
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Run every read-only check, print the report **and the alert text that would be sent**, send nothing. |
| `--no-alert` | Never send the admin DM. |
| `--json` | Emit JSON (findings, overall `attention`, and alert status) instead of text. |
| `--config PATH` | Use another config (default `ops/upkeep/config.json`). |
| `--fixtures DIR` | Offline run over recorded responses (see `tests/fixtures/upkeep/` for the layout). Never sends. |
| `--feed-url URL` | AI Wire registry RSS to check instead of `$AI_WIRE_BASE_URL/feed.xml`. |

Exit codes: **0** nothing needs attention, **1** something does, **2** internal error.

## Checks (per bot)

1. **GitHub** — open issues and PRs via the REST API. Issues labelled `bug`
   (`bug_labels`) and PRs open longer than `stale_pr_days` (default 7) need
   attention; everything else is counted.
2. **Dependency drift** — reads each file in `requirements_files`
   (`requirements.txt`, `pyproject.toml` `[project].dependencies`) through the
   GitHub contents API and compares against PyPI's latest release. A pinned
   (`==`) dep one or more major versions behind needs attention; newer
   releases for `>=` ranges and minor drift on pins are info only.
3. **Railway** — latest deployment for the bot's project/service/environment
   (from `UPKEEP_RAILWAY_PROJECTS`) via the public GraphQL API. `FAILED`, `CRASHED`, or a `REMOVED` latest
   deployment (no successor) needs attention.
4. **Silent run** — the public Telegram preview `https://t.me/s/<channel>`; the
   newest post older than `max_silence_hours` needs attention. Optionally the
   newest Railway deployment log line (`check_railway_logs`).

Global: the AI Wire registry RSS (`$AI_WIRE_BASE_URL/feed.xml`, or `--feed-url`)
must have an item newer than `ai_wire.max_silence_hours`. The feed does not say which bot sent an item,
so this is only a "registry received anything" check.

A missing token makes that check report `skipped (no RAILWAY_TOKEN)` and it
never flags. Likewise, if `UPKEEP_RAILWAY_PROJECTS` is unset, is not valid JSON,
or has no complete entry for a bot, that bot's Railway checks report
`skipped (UPKEEP_RAILWAY_PROJECTS not set for <bot>)`, and without
`AI_WIRE_BASE_URL` (or `--feed-url`) the registry check reports
`skipped (AI_WIRE_BASE_URL not set and no --feed-url)`. Neither flags. A network or API failure becomes a `check error` warning, which
needs attention unless `flag_on_check_error` is `false`.

## Configuration

`config.json` holds only non-sensitive settings (repos, channels, thresholds).
Tokens and infra identifiers (Railway ids, the registry host) come from env
vars and are never committed. Secret values, Railway ids and the registry base
URL are redacted from all output as a backstop.

| Variable | Required? | Where to set | Purpose |
| --- | --- | --- | --- |
| `UPKEEP_RAILWAY_PROJECTS` | Optional; Railway checks skip without it | GitHub Actions repo secret; local shell env | JSON object keyed by bot name: `{"clawbytes": {"project_id": "...", "service_id": "...", "environment_id": "..."}, "modelbytes": {...}, "interesting-repos": {...}}` |
| `RAILWAY_TOKEN` | Optional; Railway checks skip without it | GitHub Actions repo secret; local shell env | Railway GraphQL (`Authorization: Bearer`, account or team token) |
| `AI_WIRE_BASE_URL` | Optional; registry check skips without it (or pass `--feed-url`) | GitHub Actions repo variable; local shell env | AI Wire registry base URL, no trailing path; the feed is `<base>/feed.xml` |
| `GITHUB_TOKEN` | Optional | GitHub Actions repo secret `UPKEEP_GITHUB_TOKEN` (falls back to the workflow token); local shell env | GitHub API rate limits / cross-repo or private reads |
| `TELEGRAM_BOT_TOKEN` | For alerting | GitHub Actions repo secret; local shell env | Sending the alert |
| `CLAWBYTES_ADMIN_CHAT_ID` | For alerting | GitHub Actions repo secret; local shell env | Admin chat for the alert (`MODELBYTES_ADMIN_CHAT_ID` is accepted as a fallback) |

`--fixtures` runs read the same env vars, so set `UPKEEP_RAILWAY_PROJECTS` (any
placeholder ids, e.g. `{"demo": {"project_id": "proj-example", "service_id":
"svc-example", "environment_id": "env-example"}}`) and `AI_WIRE_BASE_URL` to
exercise the Railway and registry fixtures.

## Alerting

Exception-only, same as the bots' own ops path: when something needs attention
(and not `--dry-run`, `--no-alert`, or `--fixtures`), the full report is sent as
a Telegram `sendMessage` DM to the admin chat, with the same `🔧 OPS REPORT`
banner `scripts/scheduler.py` uses. Nothing is sent when everything is OK.
Delivery failure is reported in the output and never raises; the exit code is
still 1.

## Schedule

`.github/workflows/upkeep.yml` runs daily at 15:17 UTC and on manual dispatch.
It runs `tests/test_upkeep_check.py`, then the check. Exit 1 leaves the
workflow green with a warning annotation and the report in the job summary;
exit 2 fails the job.

## Tests

```
python -m pytest -q tests/test_upkeep_check.py
```

Offline only: fixtures live under `tests/fixtures/upkeep/`.
